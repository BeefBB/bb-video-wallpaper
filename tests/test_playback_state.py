"""
測試影片載入與延遲播放事件之間的暫停狀態
使用真正的 Qt 訊號佇列與模擬播放器, 不啟動桌布或修改使用者設定
"""

import ast
from pathlib import Path
import threading
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, Signal, Slot


ROOT = Path(__file__).resolve().parents[1]
PLAYING_EVENT = object()


class HeadlessWidget(QObject):
    """
    保留 Qt 訊號能力, 以替身處理視窗操作
    """

    def __init__(self):

        super().__init__()


        for name in (
            "setWindowFlags",
            "setAttribute",
            "setGeometry",
            "hide",
            "show",
            "destroy",
            "create",
        ):
            setattr(self, name, mock.Mock())

        self.winId = mock.Mock(return_value=1)


class DeferredPlayer:
    """
    模擬載入期間暫停尚未生效, Playing 事件稍後才送達的播放器
    """

    def __init__(self):

        self.state = "stopped"
        self.position = 0
        self.pause_calls = []
        self.callbacks = {}

        self.events = mock.Mock(spec=["event_attach", "event_detach"])
        self.events.event_attach.side_effect = self.callbacks.__setitem__
        self.events.event_detach.side_effect = self.callbacks.pop

        self.video_set_crop_geometry = mock.Mock()


    def event_manager(self) -> mock.Mock:

        return self.events


    def play(self) -> int:

        if self.state == "paused":
            self.state = "playing"

        elif self.state != "playing":
            self.state = "opening"

        return 0


    def set_pause(self, paused: int) -> None:

        self.pause_calls.append((paused, threading.get_ident()))

        if self.state in {"playing", "paused"}:
            self.state = "paused" if paused else "playing"


    def stop(self) -> None:

        self.state = "stopped"


    def set_media(self, media) -> None:

        self.position = 0


    def get_time(self) -> int:

        return self.position


    def set_time(self, position: int) -> None:

        self.position = position


    def finish_loading(self) -> None:

        # 停止後仍可能收到先前的通知, 但通知本身不會重新啟動播放器
        if self.state == "opening":
            self.state = "playing"

        self.callbacks[PLAYING_EVENT](None)


    def release(self) -> None:

        self.state = "released"


class PlaybackStateTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:

        cls.app = QCoreApplication.instance() or QCoreApplication([])

        # 與匯入測試相同, 僅載入原始類別, 避免模組頂層初始化使用者目錄與 VLC
        source_path = ROOT / "bb-video-wallpaper.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        classes = ast.Module(
            body=[
                node for node in tree.body
                if isinstance(node, ast.ClassDef)
                and node.name in {"Wallpaper", "Tray"}
            ],
            type_ignores=[],
        )
        cls.playback_code = compile(classes, str(source_path), "exec")


    def setUp(self) -> None:

        self.player = DeferredPlayer()
        instance = mock.Mock()
        instance.media_player_new.return_value = self.player

        vlc = mock.Mock()
        vlc.Instance.return_value = instance
        vlc.EventType.MediaPlayerPlaying = PLAYING_EVENT

        config_store = mock.Mock()
        config_store.load.return_value = {}

        application = mock.Mock()
        geometry = application.primaryScreen.return_value.geometry.return_value
        geometry.width.return_value = 1920
        geometry.height.return_value = 1080

        self.namespace = {
            "QWidget": HeadlessWidget,
            "Signal": Signal,
            "Slot": Slot,
            "Qt": Qt,
            "Path": Path,
            "QApplication": application,
            "QTimer": mock.Mock(),
            "vlc": vlc,
            "config_store": config_store,
            "get_media": mock.Mock(return_value=[]),
            "MEDIA_DIR": ROOT / "test-media",
            "SUPPORTED_IMAGE_EXTENSIONS": {".png", ".jpg"},
            "SUPPORTED_VIDEO_EXTENSIONS": {".mp4"},
            "is_on_battery": mock.Mock(return_value=False),
            "is_any_fullscreen": mock.Mock(return_value=False),
            "logger": mock.Mock(),
        }
        exec(self.playback_code, self.namespace)


        wallpaper_class = self.namespace["Wallpaper"]

        for name in ("attach_to_desktop", "unregister_power_notification"):
            self.enterContext(mock.patch.object(wallpaper_class, name))

        self.wallpaper = wallpaper_class()
        self.addCleanup(self.dispose_wallpaper)

        tray_class = self.namespace["Tray"]
        self.tray = tray_class.__new__(tray_class)
        self.tray.wallpaper = self.wallpaper
        self.tray.app = mock.Mock()
        self.media_path = self.namespace["MEDIA_DIR"] / "sample.mp4"


    def dispose_wallpaper(self) -> None:

        self.wallpaper.is_exiting = True
        self.player.callbacks.clear()
        self.wallpaper.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


    def start_loading(self) -> None:

        self.wallpaper.set_play_media(self.media_path)
        self.tray.play()
        self.assertEqual(self.player.state, "opening")


    def test_pause_during_loading_applies_after_playing_event(self):

        self.start_loading()
        self.tray.pause()
        self.assertEqual(self.wallpaper.play_state, "pause")
        self.assertEqual(self.player.state, "opening")

        self.player.finish_loading()
        self.assertEqual(self.player.state, "playing")
        self.app.processEvents()

        self.assertEqual(self.player.state, "paused")
        self.assertEqual(self.wallpaper.play_state, "pause")


    def test_playing_callback_from_worker_pauses_on_qt_thread(self):

        self.start_loading()
        self.tray.pause()
        self.player.pause_calls.clear()

        worker = threading.Thread(target=self.player.finish_loading)
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.player.pause_calls, [])

        self.app.processEvents()

        self.assertEqual(self.player.state, "paused")
        self.assertTrue(self.player.pause_calls)
        self.assertTrue(all(
            thread_id == threading.get_ident()
            for _, thread_id in self.player.pause_calls
        ))


    def test_latest_play_or_stop_wins_over_delayed_playing_event(self):

        cases = (
            ("play", "playing"),
            ("stop", "stopped"),
        )

        for command, expected_state in cases:

            for event_already_queued in (False, True):

                with self.subTest(
                    command=command,
                    event_already_queued=event_already_queued,
                ):
                    self.tray.stop()
                    self.start_loading()
                    self.tray.pause()

                    if event_already_queued:
                        self.player.finish_loading()

                    getattr(self.tray, command)()
                    pause_calls = list(self.player.pause_calls)

                    if not event_already_queued:
                        self.player.finish_loading()

                    self.app.processEvents()

                    self.assertEqual(self.wallpaper.play_state, command)
                    self.assertEqual(self.player.state, expected_state)
                    self.assertEqual(self.player.pause_calls, pause_calls)


    def test_queued_playing_event_after_exit_does_not_touch_player(self):

        self.start_loading()
        self.tray.pause()
        self.player.finish_loading()
        pause_calls = list(self.player.pause_calls)

        self.tray.exit()
        self.app.processEvents()

        self.assertEqual(self.player.state, "released")
        self.assertEqual(self.player.pause_calls, pause_calls)
        self.assertNotIn(PLAYING_EVENT, self.player.callbacks)
        self.namespace["logger"].exception.assert_not_called()


    def test_screen_change_preserves_playback_state_and_position(self):

        cases = (
            ("play", "playing"),
            ("pause", "paused"),
            ("stop", "stopped"),
        )

        for command, expected_state in cases:

            with self.subTest(command=command):
                self.tray.stop()
                self.start_loading()
                self.player.finish_loading()
                self.app.processEvents()
                getattr(self.tray, command)()
                self.player.position = 4321

                single_shot = self.namespace["QTimer"].singleShot
                single_shot.reset_mock()
                self.wallpaper.on_screen_changed(mock.sentinel.new_geometry)
                single_shot.assert_called_once()
                _, callback = single_shot.call_args.args
                callback()

                self.assertEqual(self.wallpaper.play_state, command)
                self.assertEqual(self.player.position, 4321)
                self.player.finish_loading()
                self.app.processEvents()

                self.assertEqual(self.player.state, expected_state)
                self.assertEqual(self.wallpaper.play_state, command)
                self.assertEqual(self.wallpaper.current_media, self.media_path)
                self.assertFalse(self.wallpaper.is_recreating)
                self.namespace["logger"].exception.assert_not_called()


    def test_auto_pause_during_loading_applies_and_resumes(self):

        self.wallpaper.auto_pause_if_on_battery = True
        self.wallpaper.fullscreen_detection_ready = True

        for reason in ("screen_off", "battery", "fullscreen"):

            with self.subTest(reason=reason):
                self.tray.stop()
                self.start_loading()
                self.wallpaper.screen_off = reason == "screen_off"
                self.namespace["is_on_battery"].return_value = reason == "battery"
                self.namespace["is_any_fullscreen"].return_value = reason == "fullscreen"

                self.wallpaper.check_auto_pause()
                self.assertEqual(self.wallpaper.play_state, "pause")
                self.player.finish_loading()
                self.app.processEvents()
                self.assertEqual(self.player.state, "paused")

                self.wallpaper.screen_off = False
                self.namespace["is_on_battery"].return_value = False
                self.namespace["is_any_fullscreen"].return_value = False
                self.wallpaper.check_auto_pause()

                self.assertEqual(self.wallpaper.play_state, "play")
                self.assertEqual(self.player.state, "playing")


    def test_manual_pause_survives_auto_pause_condition_clearing(self):

        self.start_loading()
        self.wallpaper.screen_off = True
        self.wallpaper.check_auto_pause()
        self.tray.pause()
        self.wallpaper.screen_off = False
        self.wallpaper.check_auto_pause()
        self.player.finish_loading()
        self.app.processEvents()

        self.assertEqual(self.wallpaper.play_state, "pause")
        self.assertEqual(self.player.state, "paused")
        self.assertFalse(self.wallpaper.enable_auto_pause)


if __name__ == "__main__":
    unittest.main()
