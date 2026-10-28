"""
測試圖片與影片切換, 桌布重掛及延遲播放事件之間的狀態
使用真正的 Qt 訊號佇列與模擬輸出, 不啟動桌布或修改使用者設定
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
        self.set_hwnd = mock.Mock()


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


class ImageDisplay:
    """
    記錄圖片的載入, 顯示與目標視窗, 不建立原生視窗
    """

    def __init__(self):

        self.current_image: Path | None = None
        self.visible = False
        self.hwnd = 0


    def set_image(self, path: Path) -> None:

        self.current_image = path


    def show(self) -> None:

        if self.current_image is None:
            raise RuntimeError("尚未設定圖片")

        self.visible = True


    def hide(self) -> None:

        self.visible = False


    def set_hwnd(self, hwnd: int) -> None:

        if hwnd != self.hwnd:
            self.visible = False

        self.hwnd = hwnd


    def close(self) -> None:

        self.visible = False
        self.current_image = None


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
        self.image_output = ImageDisplay()
        self.image_display = mock.Mock(wraps=self.image_output)
        image_module = mock.Mock()
        image_module.Instance.return_value = self.image_display

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
            "bb_image_display": image_module,
            "config_store": config_store,
            "get_media": mock.Mock(return_value=[]),
            "MEDIA_DIR": ROOT / "test-media",
            "SUPPORTED_IMAGE_EXTENSIONS": {".png", ".jpg"},
            "SUPPORTED_VIDEO_EXTENSIONS": {".mp4"},
            "is_on_battery": mock.Mock(return_value=False),
            "is_any_fullscreen": mock.Mock(return_value=False),
            "set_windows_as_wallpaper": mock.Mock(),
            "ctypes": mock.Mock(),
            "GUID_CONSOLE_DISPLAY_STATE": mock.sentinel.power_guid,
            "DEVICE_NOTIFY_WINDOW_HANDLE": 0,
            "logger": mock.Mock(),
        }
        exec(self.playback_code, self.namespace)


        wallpaper_class = self.namespace["Wallpaper"]
        self.attach_to_desktop = wallpaper_class.attach_to_desktop

        for name in ("attach_to_desktop", "unregister_power_notification"):
            self.enterContext(mock.patch.object(wallpaper_class, name))

        self.wallpaper = wallpaper_class()
        self.addCleanup(self.dispose_wallpaper)

        tray_class = self.namespace["Tray"]
        self.tray = tray_class.__new__(tray_class)
        self.tray.wallpaper = self.wallpaper
        self.tray.app = mock.Mock()
        self.media_path = self.namespace["MEDIA_DIR"] / "sample.mp4"
        self.image_path = self.namespace["MEDIA_DIR"] / "sample.PNG"


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


    def test_image_uses_qt_and_stop_then_play_reuses_loaded_image(self):

        self.wallpaper.set_play_media(self.image_path)
        self.tray.play()

        self.assertTrue(self.image_output.visible)
        self.assertEqual(self.wallpaper.play_state, "play")
        self.assertEqual(self.player.state, "stopped")
        self.namespace["vlc"].Instance.return_value.media_new.assert_not_called()
        self.player.video_set_crop_geometry.assert_not_called()

        self.tray.stop()
        self.assertFalse(self.image_output.visible)
        self.assertEqual(self.wallpaper.play_state, "stop")
        self.assertEqual(self.image_output.current_image, self.image_path)

        self.tray.play()
        self.assertTrue(self.image_output.visible)
        self.assertEqual(self.wallpaper.play_state, "play")
        self.image_display.set_image.assert_called_once_with(self.image_path)


    def test_switching_video_image_video_stops_previous_output(self):

        self.start_loading()
        self.player.finish_loading()
        self.app.processEvents()

        self.wallpaper.set_play_media(self.image_path)
        self.assertEqual(self.player.state, "stopped")
        self.assertTrue(self.image_output.visible)
        self.assertEqual(self.wallpaper.current_media, self.image_path)

        self.wallpaper.set_play_media(self.media_path)
        self.assertFalse(self.image_output.visible)
        self.assertIsNone(self.image_output.current_image)
        self.assertEqual(self.player.state, "opening")
        self.assertEqual(self.wallpaper.current_media, self.media_path)


    def test_image_switch_while_stopped_stays_hidden_until_play(self):

        self.wallpaper.set_play_media(self.image_path)
        self.assertEqual(self.wallpaper.play_state, "stop")
        self.assertFalse(self.image_output.visible)

        self.wallpaper.set_play_media(self.media_path)
        self.assertEqual(self.player.state, "stopped")
        self.assertEqual(self.wallpaper.play_state, "stop")


    def test_paused_video_switches_to_visible_image_and_ignores_old_event(self):

        self.start_loading()
        self.tray.pause()
        self.player.finish_loading()
        pause_calls = list(self.player.pause_calls)

        self.wallpaper.set_play_media(self.image_path)
        self.app.processEvents()

        self.assertEqual(self.wallpaper.play_state, "play")
        self.assertTrue(self.image_output.visible)
        self.assertEqual(self.player.state, "stopped")
        self.assertEqual(self.player.pause_calls, pause_calls)

        self.player.finish_loading()
        self.app.processEvents()
        self.assertEqual(self.player.pause_calls, pause_calls)


    def test_images_ignore_manual_and_auto_pause(self):

        self.wallpaper.set_play_media(self.image_path)
        self.tray.play()
        self.wallpaper.media_pause()
        self.wallpaper.screen_off = True
        self.wallpaper.check_auto_pause()

        self.assertTrue(self.image_output.visible)
        self.assertEqual(self.wallpaper.play_state, "play")
        self.assertEqual(self.player.pause_calls, [])
        self.namespace["is_on_battery"].assert_not_called()
        self.namespace["is_any_fullscreen"].assert_not_called()


    def test_bad_image_preserves_previous_output_state_and_history(self):

        for previous_media in (self.media_path, self.image_path):
            for command in ("play", "pause", "stop"):
                if previous_media == self.image_path and command == "pause":
                    continue

                with self.subTest(media=previous_media, state=command):
                    self.image_display.set_image.side_effect = None
                    self.wallpaper.set_play_media(previous_media)
                    self.tray.play()
                    getattr(self.tray, command)()

                    previous_state = self.player.state
                    previous_image = self.image_output.current_image
                    previous_visible = self.image_output.visible
                    self.namespace["config_store"].save.reset_mock()
                    self.image_display.set_image.side_effect = RuntimeError("bad image")

                    with self.assertRaisesRegex(RuntimeError, "bad image"):
                        self.wallpaper.set_play_media(self.image_path.with_name("bad.png"))

                    self.assertEqual(self.wallpaper.current_media, previous_media)
                    self.assertEqual(self.wallpaper.play_state, command)
                    self.assertEqual(self.player.state, previous_state)
                    self.assertEqual(self.image_output.current_image, previous_image)
                    self.assertEqual(self.image_output.visible, previous_visible)
                    self.namespace["config_store"].save.assert_not_called()


    def test_successful_image_is_saved_in_recent_history(self):

        self.namespace["config_store"].load.return_value = {
            "recentVideo": ["sample.mp4", "sample.PNG", "other.png"],
        }
        self.wallpaper.set_play_media(self.image_path)

        saved = self.namespace["config_store"].save.call_args.args[0]
        self.assertEqual(saved["recentVideo"], ["sample.PNG", "sample.mp4", "other.png"])


    def test_attach_binds_both_outputs_to_same_hwnd(self):

        self.attach_to_desktop(self.wallpaper)

        self.namespace["set_windows_as_wallpaper"].assert_called_once_with(1)
        self.player.set_hwnd.assert_called_once_with(1)
        self.image_display.set_hwnd.assert_called_once_with(1)


    def test_image_reattach_rebinds_without_reload_or_vlc_seek(self):

        for command in ("play", "stop"):
            with self.subTest(state=command):
                self.wallpaper.set_play_media(self.image_path)
                self.tray.play()
                getattr(self.tray, command)()
                self.image_display.reset_mock()
                self.namespace["config_store"].save.reset_mock()
                self.wallpaper.winId.return_value = 2
                self.wallpaper.attach_to_desktop.side_effect = (
                    lambda: self.attach_to_desktop(self.wallpaper)
                )

                operations = mock.Mock()
                operations.attach_mock(self.image_display.set_hwnd, "image_hwnd")
                operations.attach_mock(self.wallpaper.destroy, "destroy")

                with (
                    mock.patch.object(self.player, "get_time", side_effect=AssertionError("image seek")),
                    mock.patch.object(self.player, "set_time", side_effect=AssertionError("image seek")),
                ):
                    self.wallpaper.reattach_to_desktop()

                self.assertEqual(operations.mock_calls, [
                    mock.call.image_hwnd(0),
                    mock.call.destroy(True, True),
                    mock.call.image_hwnd(2),
                ])
                self.assertEqual(self.image_output.hwnd, 2)
                self.assertEqual(self.image_output.visible, command == "play")
                self.assertEqual(self.wallpaper.play_state, command)
                self.assertEqual(self.player.state, "stopped")
                self.image_display.set_image.assert_not_called()
                self.namespace["config_store"].save.assert_not_called()
                self.namespace["logger"].exception.assert_not_called()


    def test_image_exit_releases_image_and_vlc(self):

        self.wallpaper.set_play_media(self.image_path)
        self.tray.play()
        self.image_display.close.reset_mock()
        self.tray.exit()

        self.assertFalse(self.image_output.visible)
        self.assertIsNone(self.image_output.current_image)
        self.image_display.close.assert_called_once_with()
        self.assertEqual(self.player.state, "released")
        self.tray.app.quit.assert_called_once_with()
        self.namespace["logger"].exception.assert_not_called()


    def test_startup_displays_initial_image(self):

        self.namespace["get_media"].return_value = [self.image_path]
        wallpaper = self.namespace["Wallpaper"]()
        self.addCleanup(wallpaper.deleteLater)

        self.assertEqual(wallpaper.current_media, self.image_path)
        self.assertEqual(wallpaper.play_state, "play")
        self.assertTrue(self.image_output.visible)
        self.assertEqual(self.player.state, "stopped")


    def test_bad_startup_image_keeps_app_available(self):

        self.namespace["get_media"].return_value = [self.image_path]
        self.image_display.set_image.side_effect = RuntimeError("bad image")
        wallpaper = self.namespace["Wallpaper"]()
        self.addCleanup(wallpaper.deleteLater)

        self.assertIsNone(wallpaper.current_media)
        self.assertEqual(wallpaper.play_state, "stop")
        self.assertFalse(self.image_output.visible)
        self.namespace["config_store"].save.assert_not_called()
        self.namespace["logger"].exception.assert_called_once()
        wallpaper.timer.start.assert_called_with(500)


if __name__ == "__main__":
    unittest.main()
