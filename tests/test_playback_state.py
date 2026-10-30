"""
測試圖片與影片切換, 桌布重掛及延遲播放事件之間的狀態
使用真正的 Qt 訊號佇列, 重掛計時器與模擬輸出, 不啟動桌布或修改使用者設定
"""

import ast
from pathlib import Path
import threading
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QObject, QRect, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QScreen
from PySide6.QtTest import QSignalSpy, QTest


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


class HeadlessScreen(QObject):
    """
    模擬螢幕位置與尺寸, 透過真正的 Qt 訊號通知變更
    """

    geometryChanged = Signal(QRect)

    def __init__(self, geometry: QRect) -> None:

        super().__init__()
        self._geometry = geometry


    def geometry(self) -> QRect:

        return self._geometry


    def set_geometry(self, geometry: QRect) -> None:

        self._geometry = geometry
        self.geometryChanged.emit(geometry)


class HeadlessDesktopEvents(QObject):

    taskbar_created = Signal()

    def __init__(self):

        super().__init__()
        self.close = mock.Mock()


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

        # 僅載入原始類別, 避免模組頂層初始化使用者目錄與 VLC
        source_path = ROOT / "bb-video-wallpaper.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        classes = ast.Module(
            body=[
                node for node in tree.body
                if isinstance(node, ast.ClassDef) and
                node.name in {"VlcStopThread", "Wallpaper", "Tray"}
            ],
            type_ignores=[]
        )
        cls.playback_code = compile(classes, str(source_path), "exec")


    def setUp(self) -> None:

        self.player = DeferredPlayer()
        self.image_output = ImageDisplay()
        self.instance_image = mock.Mock(wraps=self.image_output)
        image_module = mock.Mock()
        image_module.Instance.return_value = self.instance_image

        instance = mock.Mock()
        instance.media_player_new.return_value = self.player

        vlc = mock.Mock()
        vlc.Instance.return_value = instance
        vlc.EventType.MediaPlayerPlaying = PLAYING_EVENT

        config_store = mock.Mock()
        config_store.load.return_value = {}

        application = mock.Mock()
        application.primaryScreen.return_value = HeadlessScreen(QRect(0, 0, 1920, 1080))

        self.namespace = {
            "QWidget": HeadlessWidget,
            "Signal": Signal,
            "Slot": Slot,
            "Qt": Qt,
            "Path": Path,
            "QApplication": application,
            "QScreen": QScreen,
            "QEventLoop": QEventLoop,
            "QThread": QThread,
            "DesktopEventWindow": HeadlessDesktopEvents,
            # 建立真正的計時器, 靜態 singleShot 保留替身以略過啟動等待
            "QTimer": mock.Mock(side_effect=QTimer),
            "vlc": vlc,
            "bb_image_display": image_module,
            "config_store": config_store,
            "get_media": mock.Mock(return_value=[]),
            "MEDIA_DIR": ROOT / "test-media",
            "SUPPORTED_IMAGE_EXTENSIONS": {".png", ".jpg"},
            "SUPPORTED_VIDEO_EXTENSIONS": {".mp4"},
            "is_on_battery": mock.Mock(return_value=False),
            "is_any_fullscreen": mock.Mock(return_value=False),
            "check_desktop_ready": mock.Mock(return_value=True),
            "wake_vlc_video_windows": mock.Mock(),
            "set_windows_as_wallpaper": mock.Mock(),
            "register_power_notification": mock.Mock(return_value=2**40 + 1),
            "unregister_power_notification": mock.Mock(),
            "get_display_state": mock.Mock(return_value=None),
            "wintypes": mock.Mock(),
            "WM_DESTROY": 2,
            "logger": mock.Mock(),
        }
        exec(self.playback_code, self.namespace)


        wallpaper_class = self.namespace["Wallpaper"]
        self.attach_to_desktop = wallpaper_class.attach_to_desktop
        self.unregister_power_notification = wallpaper_class.unregister_power_notification

        for name in ("attach_to_desktop", "unregister_power_notification"):
            self.enterContext(mock.patch.object(wallpaper_class, name))

        self.wallpaper = wallpaper_class()
        self.addCleanup(self.dispose_wallpaper)
        self.wallpaper.check_auto_pause_timer.stop()

        tray_class = self.namespace["Tray"]
        self.tray = tray_class.__new__(tray_class)
        self.tray.wallpaper = self.wallpaper
        self.tray.app = mock.Mock()
        self.media_path = self.namespace["MEDIA_DIR"] / "sample.mp4"
        self.image_path = self.namespace["MEDIA_DIR"] / "sample.PNG"


    def dispose_wallpaper(self, wallpaper=None) -> None:

        wallpaper = self.wallpaper if wallpaper is None else wallpaper
        wallpaper.is_exiting = True
        wallpaper.reattach_timer.stop()

        if wallpaper.desktop_events is not None:
            wallpaper.desktop_events.deleteLater()

        self.player.callbacks.clear()
        wallpaper.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


    def start_loading(self) -> None:

        self.wallpaper.set_play_media(self.media_path)
        self.tray.play()
        self.assertEqual(self.player.state, "opening")


    def test_default_target_stays_on_original_screen_after_primary_changes(self):

        application = self.namespace["QApplication"]
        original_screen = application.primaryScreen.return_value
        self.assertIs(self.wallpaper.target_screen, original_screen)
        self.wallpaper.setGeometry.assert_called_once_with(original_screen.geometry())

        application.primaryScreen.return_value = HeadlessScreen(QRect(1920, 0, 2560, 1440))
        self.wallpaper.set_play_media(self.media_path)
        self.wallpaper.reattach_to_desktop()

        self.wallpaper.setGeometry.assert_called_with(original_screen.geometry())
        self.player.video_set_crop_geometry.assert_called_with("1920:1080")
        application.primaryScreen.assert_called_once_with()
        self.namespace["logger"].exception.assert_not_called()


    def test_explicit_target_controls_geometry_crop_and_reattach(self):

        target_screen = HeadlessScreen(QRect(-1080, -240, 1080, 1920))
        application = self.namespace["QApplication"]
        application.primaryScreen.reset_mock()
        wallpaper = self.namespace["Wallpaper"](target_screen=target_screen)
        self.addCleanup(self.dispose_wallpaper, wallpaper)
        wallpaper.check_auto_pause_timer.stop()

        self.assertIs(wallpaper.target_screen, target_screen)
        wallpaper.setGeometry.assert_called_once_with(target_screen.geometry())
        wallpaper.set_play_media(self.media_path)
        self.player.video_set_crop_geometry.assert_called_once_with("1080:1920")

        application.primaryScreen.return_value = HeadlessScreen(QRect(0, 0, 3840, 2160))
        wallpaper.setGeometry.reset_mock()
        wallpaper.reattach_to_desktop()

        wallpaper.setGeometry.assert_called_once_with(target_screen.geometry())
        self.player.video_set_crop_geometry.assert_called_with("1080:1920")
        application.primaryScreen.assert_not_called()
        self.namespace["logger"].exception.assert_not_called()


    def test_only_target_geometry_changes_reattach_with_updated_size_and_crop(self):

        target_screen = HeadlessScreen(QRect(-1080, 0, 1080, 1920))
        wallpaper = self.namespace["Wallpaper"](target_screen=target_screen)
        self.addCleanup(self.dispose_wallpaper, wallpaper)
        wallpaper.check_auto_pause_timer.stop()
        wallpaper.set_play_media(self.media_path)

        primary_screen = self.namespace["QApplication"].primaryScreen.return_value
        primary_screen.set_geometry(QRect(0, 0, 2560, 1440))
        self.assertFalse(wallpaper.reattach_timer.isActive())
        self.wallpaper.reattach_timer.stop()

        geometry = QRect(-1440, 200, 1440, 2560)
        target_screen.set_geometry(geometry)
        self.assertTrue(wallpaper.reattach_timer.isActive())
        wallpaper.reattach_timer.start(0)
        self.app.processEvents()

        self.assertFalse(wallpaper.reattach_timer.isActive())
        wallpaper.setGeometry.assert_called_with(geometry)
        self.player.video_set_crop_geometry.assert_called_with("1440:2560")
        self.namespace["logger"].exception.assert_not_called()


    def test_no_available_screen_fails_before_creating_outputs(self):

        self.namespace["QApplication"].primaryScreen.return_value = None
        self.namespace["vlc"].Instance.reset_mock()
        self.namespace["bb_image_display"].Instance.reset_mock()

        with self.assertRaisesRegex(RuntimeError, "找不到可用的目標螢幕"):
            self.namespace["Wallpaper"]()

        self.namespace["vlc"].Instance.assert_not_called()
        self.namespace["bb_image_display"].Instance.assert_not_called()


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
                    event_already_queued=event_already_queued
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

                self.wallpaper.on_screen_changed(mock.sentinel.new_geometry)
                timer = self.wallpaper.reattach_timer
                self.assertTrue(timer.isActive())
                self.assertEqual(timer.interval(), 200)
                # 縮短測試等待, 仍透過真正的 Qt timeout 執行重掛
                timer.start(0)
                self.app.processEvents()
                self.assertFalse(timer.isActive())

                self.assertEqual(self.wallpaper.play_state, command)
                self.assertEqual(self.player.position, 4321)
                self.player.finish_loading()
                self.app.processEvents()

                self.assertEqual(self.player.state, expected_state)
                self.assertEqual(self.wallpaper.play_state, command)
                self.assertEqual(self.wallpaper.current_media, self.media_path)
                self.assertFalse(self.wallpaper.is_recreating)
                self.namespace["logger"].exception.assert_not_called()


    def test_video_reattach_stops_old_output_before_destroying_and_rebinding(self):

        self.start_loading()
        self.player.finish_loading()
        self.app.processEvents()
        self.tray.pause()
        self.player.position = 4321
        self.wallpaper.attach_to_desktop.side_effect = lambda: self.attach_to_desktop(self.wallpaper)
        original_stop = self.player.stop

        def stop_and_clear_time():
            if self.wallpaper.is_recreating and not self.wallpaper.destroy.called:
                self.assertNotEqual(threading.get_ident(), main_thread)
            original_stop()
            self.player.position = -1

        main_thread = threading.get_ident()
        with mock.patch.object(self.player, "stop", side_effect=stop_and_clear_time) as stop:
            operations = mock.Mock()
            operations.attach_mock(stop, "stop")
            operations.attach_mock(self.wallpaper.destroy, "destroy")
            operations.attach_mock(self.player.set_hwnd, "set_hwnd")
            self.wallpaper.reattach_to_desktop()

        self.assertEqual(operations.mock_calls[:3], [
            mock.call.stop(),
            mock.call.destroy(True, True),
            mock.call.set_hwnd(1),
        ])
        self.player.finish_loading()
        self.app.processEvents()
        self.assertEqual(self.wallpaper.play_state, "pause")
        self.assertEqual(self.player.state, "paused")
        self.assertEqual(self.player.position, 4321)
        self.namespace["logger"].exception.assert_not_called()


    def test_reattach_stop_keeps_qt_events_running_and_ignores_pending_pause(self):

        self.start_loading()
        self.tray.pause()
        self.wallpaper.is_recreating = True
        processed = threading.Event()
        woken = threading.Event()
        self.namespace["wake_vlc_video_windows"].side_effect = woken.set

        def stop_waiting_for_ui():
            if not processed.wait(2) or not woken.wait(2):
                raise RuntimeError("Qt event loop blocked")

        QTimer.singleShot(0, processed.set)
        self.player.finish_loading()
        self.player.pause_calls.clear()

        with mock.patch.object(self.player, "stop", side_effect=stop_waiting_for_ui):
            self.wallpaper.stop_video_for_reattach()

        self.assertTrue(processed.is_set())
        self.assertEqual(self.player.pause_calls, [])
        self.assertEqual(self.wallpaper.play_state, "pause")
        self.namespace["wake_vlc_video_windows"].assert_called_once_with()
        self.assertFalse(any(timer.isActive() for timer in self.wallpaper.findChildren(QTimer)))


    def test_failed_background_stop_preserves_window_and_schedules_retry(self):

        with mock.patch.object(self.player, "stop", side_effect=RuntimeError("stop failed")):
            self.wallpaper.reattach_to_desktop()

        self.wallpaper.destroy.assert_not_called()
        self.wallpaper.hide.assert_not_called()
        self.assertFalse(self.wallpaper.is_recreating)
        self.assertTrue(self.wallpaper.reattach_timer.isActive())
        self.assertEqual(self.wallpaper.reattach_retry_count, 1)
        self.namespace["logger"].exception.assert_called_once()


    def test_reattach_seeks_after_playing_when_initial_seek_is_ignored(self):

        self.start_loading()
        self.player.finish_loading()
        self.app.processEvents()
        self.tray.pause()
        self.player.position = 4321

        def seek_when_ready(position):
            if self.player.state in {"playing", "paused"}:
                self.player.position = position

        with mock.patch.object(self.player, "set_time", side_effect=seek_when_ready):
            self.wallpaper.reattach_to_desktop()
            self.assertEqual(self.player.position, 0)
            self.assertEqual(self.wallpaper.pending_reattach_time, 4321)
            self.player.finish_loading()
            self.app.processEvents()

        self.assertEqual(self.player.position, 4321)
        self.assertEqual(self.player.state, "paused")
        self.assertIsNone(self.wallpaper.pending_reattach_time)


    def test_new_media_or_stop_cancels_pending_reattach_seek(self):

        for action in ("stop", "new_media"):
            with self.subTest(action=action):
                self.start_loading()
                self.wallpaper.pending_reattach_time = 4321
                if action == "stop":
                    self.tray.stop()
                else:
                    self.wallpaper.set_play_media(self.media_path.with_name("new.mp4"))

                with mock.patch.object(self.player, "set_time") as seek:
                    self.player.finish_loading()
                    self.app.processEvents()

                seek.assert_not_called()
                self.assertIsNone(self.wallpaper.pending_reattach_time)


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
        self.instance_image.set_image.assert_called_once_with(self.image_path)


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
                    self.instance_image.set_image.side_effect = None
                    self.wallpaper.set_play_media(previous_media)
                    self.tray.play()
                    getattr(self.tray, command)()

                    previous_state = self.player.state
                    previous_image = self.image_output.current_image
                    previous_visible = self.image_output.visible
                    self.namespace["config_store"].save.reset_mock()
                    self.instance_image.set_image.side_effect = RuntimeError("bad image")

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
        self.instance_image.set_hwnd.assert_called_once_with(1)
        self.namespace["register_power_notification"].assert_called_once_with(1)
        self.assertEqual(self.wallpaper.power_notify, 2**40 + 1)


    def test_display_notification_updates_auto_pause(self):

        self.start_loading()

        for event_type, display_state in ((b"windows_generic_MSG", 0), ("windows_generic_MSG", 1)):
            with self.subTest(display_state=display_state):
                self.namespace["get_display_state"].return_value = display_state
                result = self.wallpaper.nativeEvent(event_type, 123)

                self.assertEqual(result, (True, 0))
                self.assertEqual(self.wallpaper.screen_off, display_state == 0)
                self.assertEqual(self.wallpaper.play_state, "pause" if display_state == 0 else "play")


    def test_destroy_notification_skips_reattach_while_recreating_or_exiting(self):

        self.namespace["wintypes"].MSG.from_address.return_value.message = self.namespace["WM_DESTROY"]
        timer = self.wallpaper.reattach_timer

        self.assertEqual(self.wallpaper.nativeEvent(b"windows_generic_MSG", 123), (True, 0))
        self.assertTrue(timer.isActive())
        self.assertEqual(timer.interval(), 200)

        timer.stop()
        self.wallpaper.is_recreating = True
        self.assertEqual(self.wallpaper.nativeEvent(b"windows_generic_MSG", 123), (True, 0))
        self.assertFalse(timer.isActive())

        self.wallpaper.is_recreating = False
        self.wallpaper.is_exiting = True
        self.assertEqual(self.wallpaper.nativeEvent(b"windows_generic_MSG", 123), (True, 0))
        self.assertFalse(timer.isActive())


    def test_screen_and_destroy_events_share_one_pending_reattach(self):

        self.namespace["wintypes"].MSG.from_address.return_value.message = self.namespace["WM_DESTROY"]
        self.wallpaper.attach_to_desktop.reset_mock()
        timer = self.wallpaper.reattach_timer
        timeouts = QSignalSpy(timer.timeout)

        self.wallpaper.on_screen_changed(mock.sentinel.first_geometry)
        timer_id = timer.timerId()
        self.wallpaper.nativeEvent(b"windows_generic_MSG", 123)
        self.wallpaper.on_screen_changed(mock.sentinel.last_geometry)

        self.assertEqual(timer.timerId(), timer_id)
        self.assertEqual(timer.interval(), 200)
        self.assertTrue(timeouts.wait(1000))
        QTest.qWait(250)

        self.assertEqual(timeouts.count(), 1)
        self.wallpaper.attach_to_desktop.assert_called_once_with()
        self.wallpaper.destroy.assert_called_once_with(True, True)
        self.assertFalse(timer.isActive())


    def test_failed_reattach_retries_twice_and_merges_new_events(self):

        self.namespace["wintypes"].MSG.from_address.return_value.message = self.namespace["WM_DESTROY"]
        self.wallpaper.attach_to_desktop.reset_mock()
        self.wallpaper.attach_to_desktop.side_effect = RuntimeError("desktop unavailable")
        timer = self.wallpaper.reattach_timer
        self.wallpaper.on_screen_changed(mock.sentinel.geometry)

        for attempt in range(3):
            with self.subTest(attempt=attempt):
                self.assertTrue(timer.isActive())
                self.assertEqual(timer.interval(), 200 if attempt == 0 else 2000)
                timer_id = timer.timerId()
                self.wallpaper.on_screen_changed(mock.sentinel.new_geometry)
                self.wallpaper.nativeEvent(b"windows_generic_MSG", 123)
                self.assertEqual(timer.timerId(), timer_id)

                # 保留正式重試間隔的檢查, 再加速這次 timeout
                timer.start(0)
                self.app.processEvents()

                self.assertEqual(self.wallpaper.attach_to_desktop.call_count, attempt + 1)
                self.assertEqual(self.wallpaper.reattach_retry_count, min(attempt + 1, 2))
                self.assertFalse(self.wallpaper.is_recreating)

        self.assertFalse(timer.isActive())
        self.namespace["logger"].error.assert_called_once_with("重新掛載桌布失敗")


    def test_taskbar_notification_recovers_without_destroy_event(self):

        self.wallpaper.attach_to_desktop.reset_mock()
        self.wallpaper.reattach_retry_count = 2
        self.wallpaper.desktop_events.taskbar_created.emit()
        self.wallpaper.desktop_events.taskbar_created.emit()
        self.app.processEvents()

        timer = self.wallpaper.reattach_timer
        self.assertTrue(timer.isActive())
        self.assertEqual(timer.interval(), 200)
        self.assertEqual(self.wallpaper.reattach_retry_count, 0)
        timer.start(0)
        self.app.processEvents()

        self.wallpaper.attach_to_desktop.assert_called_once_with()
        self.wallpaper.destroy.assert_called_once_with(True, True)
        self.assertFalse(timer.isActive())


    def test_taskbar_notification_waits_for_new_desktop(self):

        self.wallpaper.attach_to_desktop.reset_mock()
        self.namespace["check_desktop_ready"].return_value = False
        self.wallpaper.desktop_events.taskbar_created.emit()
        self.app.processEvents()
        timer = self.wallpaper.reattach_timer
        timer.start(0)
        self.app.processEvents()

        self.wallpaper.attach_to_desktop.assert_not_called()
        self.assertTrue(timer.isActive())
        self.assertEqual(timer.interval(), 2000)
        self.assertEqual(self.wallpaper.reattach_retry_count, 0)

        self.namespace["check_desktop_ready"].return_value = True
        timer.start(0)
        self.app.processEvents()
        self.wallpaper.attach_to_desktop.assert_called_once_with()
        self.assertFalse(timer.isActive())


    def test_queued_taskbar_notification_does_not_restart_after_exit(self):

        self.wallpaper.attach_to_desktop.reset_mock()
        listener = self.wallpaper.desktop_events
        listener.taskbar_created.emit()
        self.tray.exit()
        self.app.processEvents()

        listener.close.assert_called_once_with()
        self.assertIsNone(self.wallpaper.desktop_events)
        self.assertFalse(self.wallpaper.reattach_timer.isActive())
        self.wallpaper.attach_to_desktop.assert_not_called()
        self.namespace["logger"].exception.assert_not_called()


    def test_listener_initialization_failure_keeps_wallpaper_available(self):

        self.namespace["DesktopEventWindow"] = mock.Mock(side_effect=OSError("registration failed"))
        wallpaper = self.namespace["Wallpaper"]()
        self.addCleanup(self.dispose_wallpaper, wallpaper)

        self.assertIsNone(wallpaper.desktop_events)
        self.assertTrue(wallpaper.check_auto_pause_timer.isActive())
        self.namespace["logger"].exception.assert_called_once_with("建立工作列重建通知視窗失敗")


    def test_unready_desktop_preserves_output_until_ready(self):

        self.start_loading()
        self.player.finish_loading()
        self.app.processEvents()
        self.tray.pause()
        self.player.position = 4321
        self.wallpaper.reattach_retry_count = 1
        self.wallpaper.attach_to_desktop.reset_mock()
        self.instance_image.reset_mock()
        self.namespace["check_desktop_ready"].return_value = False
        timer = self.wallpaper.reattach_timer

        for _ in range(3):
            self.wallpaper.schedule_reattach(0)
            timer.start(0)
            self.app.processEvents()

            self.assertTrue(timer.isActive())
            self.assertEqual(timer.interval(), 2000)
            self.assertEqual(self.wallpaper.reattach_retry_count, 1)
            self.assertFalse(self.wallpaper.is_recreating)
            self.wallpaper.destroy.assert_not_called()
            self.wallpaper.create.assert_not_called()
            self.wallpaper.hide.assert_not_called()
            self.wallpaper.attach_to_desktop.assert_not_called()
            self.wallpaper.unregister_power_notification.assert_not_called()
            self.instance_image.set_hwnd.assert_not_called()
            self.assertEqual(self.wallpaper.current_media, self.media_path)
            self.assertEqual(self.wallpaper.play_state, "pause")
            self.assertEqual(self.player.state, "paused")
            self.assertEqual(self.player.position, 4321)

        self.namespace["check_desktop_ready"].return_value = True
        timer.start(0)
        self.app.processEvents()
        self.player.finish_loading()
        self.app.processEvents()

        self.wallpaper.attach_to_desktop.assert_called_once_with()
        self.wallpaper.destroy.assert_called_once_with(True, True)
        self.assertFalse(timer.isActive())
        self.assertEqual(self.wallpaper.reattach_retry_count, 0)
        self.assertEqual(self.player.state, "paused")
        self.assertEqual(self.player.position, 4321)
        self.namespace["logger"].exception.assert_not_called()


    def test_desktop_wait_preserves_remaining_attachment_retries(self):

        self.wallpaper.attach_to_desktop.reset_mock()
        self.wallpaper.attach_to_desktop.side_effect = RuntimeError("attachment failed")
        self.wallpaper.reattach_to_desktop()
        timer = self.wallpaper.reattach_timer
        self.assertEqual(self.wallpaper.reattach_retry_count, 1)

        self.namespace["check_desktop_ready"].return_value = False
        for _ in range(3):
            timer.start(0)
            self.app.processEvents()
            self.assertTrue(timer.isActive())
            self.assertEqual(timer.interval(), 2000)
            self.assertEqual(self.wallpaper.reattach_retry_count, 1)
            self.wallpaper.attach_to_desktop.assert_called_once_with()

        self.namespace["check_desktop_ready"].return_value = True
        timer.start(0)
        self.app.processEvents()
        self.assertEqual(self.wallpaper.attach_to_desktop.call_count, 2)
        self.assertEqual(self.wallpaper.reattach_retry_count, 2)
        self.assertTrue(timer.isActive())

        timer.start(0)
        self.app.processEvents()
        self.assertEqual(self.wallpaper.attach_to_desktop.call_count, 3)
        self.assertFalse(timer.isActive())
        self.namespace["logger"].error.assert_called_once_with("重新掛載桌布失敗")


    def test_exit_cancels_desktop_wait(self):

        self.wallpaper.attach_to_desktop.reset_mock()
        self.namespace["check_desktop_ready"].return_value = False
        self.wallpaper.reattach_to_desktop()
        self.assertTrue(self.wallpaper.reattach_timer.isActive())

        self.tray.exit()
        self.wallpaper.reattach_to_desktop()
        self.app.processEvents()

        self.assertFalse(self.wallpaper.reattach_timer.isActive())
        self.namespace["check_desktop_ready"].assert_called_once_with()
        self.wallpaper.attach_to_desktop.assert_not_called()
        self.wallpaper.destroy.assert_not_called()
        self.namespace["logger"].exception.assert_not_called()


    def test_successful_retry_clears_retry_count(self):

        self.wallpaper.attach_to_desktop.reset_mock()
        self.wallpaper.attach_to_desktop.side_effect = [RuntimeError("desktop unavailable"), None]
        self.wallpaper.reattach_to_desktop()
        timer = self.wallpaper.reattach_timer
        self.assertTrue(timer.isActive())
        self.assertEqual(timer.interval(), 2000)
        self.assertEqual(self.wallpaper.reattach_retry_count, 1)

        timer.start(0)
        self.app.processEvents()

        self.assertEqual(self.wallpaper.attach_to_desktop.call_count, 2)
        self.assertEqual(self.wallpaper.reattach_retry_count, 0)
        self.assertFalse(timer.isActive())


    def test_direct_reattach_cancels_pending_timer(self):

        self.wallpaper.attach_to_desktop.reset_mock()
        self.wallpaper.schedule_reattach(0)
        self.wallpaper.reattach_to_desktop()
        self.app.processEvents()

        self.wallpaper.attach_to_desktop.assert_called_once_with()
        self.assertFalse(self.wallpaper.reattach_timer.isActive())


    def test_exit_cancels_pending_reattach_and_rejects_new_requests(self):

        self.wallpaper.attach_to_desktop.reset_mock()
        self.wallpaper.schedule_reattach(0)
        self.tray.exit()
        self.wallpaper.on_screen_changed(mock.sentinel.geometry)
        self.app.processEvents()

        self.assertFalse(self.wallpaper.reattach_timer.isActive())
        self.wallpaper.attach_to_desktop.assert_not_called()
        self.wallpaper.destroy.assert_not_called()
        self.assertEqual(self.player.state, "released")
        self.namespace["logger"].exception.assert_not_called()


    def test_unregister_clears_handle_even_if_api_fails(self):

        handle = 2**40 + 1
        self.wallpaper.power_notify = handle
        self.namespace["unregister_power_notification"].side_effect = OSError("invalid handle")

        self.unregister_power_notification(self.wallpaper)

        self.namespace["unregister_power_notification"].assert_called_once_with(handle)
        self.assertIsNone(self.wallpaper.power_notify)
        self.namespace["logger"].exception.assert_called_once()


    def test_image_reattach_rebinds_without_reload_or_vlc_seek(self):

        for command in ("play", "stop"):
            with self.subTest(state=command):
                self.wallpaper.set_play_media(self.image_path)
                self.tray.play()
                getattr(self.tray, command)()
                self.instance_image.reset_mock()
                self.namespace["config_store"].save.reset_mock()
                self.wallpaper.winId.return_value = 2
                self.wallpaper.attach_to_desktop.side_effect = (
                    lambda: self.attach_to_desktop(self.wallpaper)
                )

                operations = mock.Mock()
                operations.attach_mock(self.instance_image.set_hwnd, "image_hwnd")
                operations.attach_mock(self.wallpaper.destroy, "destroy")

                with (
                    mock.patch.object(self.player, "get_time", side_effect=AssertionError("image seek")),
                    mock.patch.object(self.player, "set_time", side_effect=AssertionError("image seek")),
                    mock.patch.object(self.player, "stop", side_effect=AssertionError("image stop")),
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
                self.instance_image.set_image.assert_not_called()
                self.namespace["config_store"].save.assert_not_called()
                self.namespace["logger"].exception.assert_not_called()


    def test_image_exit_releases_image_and_vlc(self):

        self.wallpaper.set_play_media(self.image_path)
        self.tray.play()
        self.instance_image.close.reset_mock()
        self.tray.exit()

        self.assertFalse(self.image_output.visible)
        self.assertIsNone(self.image_output.current_image)
        self.instance_image.close.assert_called_once_with()
        self.assertEqual(self.player.state, "released")
        self.tray.app.quit.assert_called_once_with()
        self.namespace["logger"].exception.assert_not_called()


    def test_startup_displays_initial_image(self):

        self.namespace["get_media"].return_value = [self.image_path]
        wallpaper = self.namespace["Wallpaper"]()
        self.addCleanup(self.dispose_wallpaper, wallpaper)

        self.assertEqual(wallpaper.current_media, self.image_path)
        self.assertEqual(wallpaper.play_state, "play")
        self.assertTrue(self.image_output.visible)
        self.assertEqual(self.player.state, "stopped")


    def test_bad_startup_image_keeps_app_available(self):

        self.namespace["get_media"].return_value = [self.image_path]
        self.instance_image.set_image.side_effect = RuntimeError("bad image")
        wallpaper = self.namespace["Wallpaper"]()
        self.addCleanup(self.dispose_wallpaper, wallpaper)

        self.assertIsNone(wallpaper.current_media)
        self.assertEqual(wallpaper.play_state, "stop")
        self.assertFalse(self.image_output.visible)
        self.namespace["config_store"].save.assert_not_called()
        self.namespace["logger"].exception.assert_called_once()
        self.assertEqual(wallpaper.check_auto_pause_timer.interval(), 500)
        self.assertTrue(wallpaper.check_auto_pause_timer.isActive())


if __name__ == "__main__":
    unittest.main()
