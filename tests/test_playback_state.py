"""
測試圖片與影片切換, 桌布重掛及延遲播放事件之間的狀態
使用真正的 Qt 訊號佇列, 重掛計時器與模擬輸出, 不啟動桌布或修改使用者設定
"""

import ast
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QObject, QRect, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QScreen
from PySide6.QtTest import QSignalSpy, QTest


ROOT = Path(__file__).resolve().parents[1]
PLAYING_EVENT = object()


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from config import ConfigStore


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
    """
    以 Qt 訊號模擬工作列重建通知
    """

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
        self.deleteLater = mock.Mock()


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
    """
    桌布播放狀態, 重掛與資源釋放
    """

    @classmethod
    def setUpClass(cls) -> None:
        """
        建立 Qt 應用程式並載入桌布與系統匣類別
        """

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
        """
        建立播放器, 圖片輸出與桌布替身
        """

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
            # Qt 類別與視窗替身
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
            # 建立真正的計時器, 啟動等待由測試停止
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
            "get_monitor_id": mock.Mock(return_value="monitor-1"),
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
        # 保留原始方法供個別案例呼叫
        self.attach_to_desktop = wallpaper_class.attach_to_desktop
        self.unregister_power_notification = wallpaper_class.unregister_power_notification

        for name in ("attach_to_desktop", "unregister_power_notification"):
            self.enterContext(mock.patch.object(wallpaper_class, name))

        self.wallpaper = wallpaper_class()
        self.addCleanup(self.dispose_wallpaper)
        self.wallpaper.check_auto_pause_timer.stop()
        self.wallpaper.fullscreen_detection_timer.stop()

        tray_class = self.namespace["Tray"]
        # 建立播放命令入口
        self.tray = tray_class.__new__(tray_class)
        self.tray.wallpaper = self.wallpaper
        self.tray.app = mock.Mock()
        self.media_path = self.namespace["MEDIA_DIR"] / "sample.mp4"
        self.image_path = self.namespace["MEDIA_DIR"] / "sample.PNG"


    def dispose_wallpaper(self, wallpaper=None) -> None:
        """
        停止重掛排程並釋放桌布與監聽物件
        """

        wallpaper = self.wallpaper if wallpaper is None else wallpaper
        wallpaper.is_exiting = True
        wallpaper.reattach_timer.stop()

        if wallpaper.desktop_events is not None:
            wallpaper.desktop_events.deleteLater()

        self.player.callbacks.clear()
        wallpaper.deleteLater()
        # 處理 deleteLater 排入的刪除事件
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


    def start_loading(self) -> None:
        """
        開始載入影片, 保留尚未送達的 Playing 事件
        """

        self.wallpaper.set_play_media(self.media_path)
        self.tray.play()
        self.assertEqual(self.player.state, "opening")


    def create_independent_wallpaper(self, screen_id: str):
        """
        建立各自持有播放器與圖片輸出的桌布, 用於驗證跨螢幕保存
        """

        instance = mock.Mock()
        instance.media_player_new.return_value = DeferredPlayer()
        self.namespace["vlc"].Instance.return_value = instance
        self.namespace["bb_image_display"].Instance.return_value = mock.Mock(wraps=ImageDisplay())
        self.namespace["get_monitor_id"].return_value = screen_id
        wallpaper = self.namespace["Wallpaper"](target_screen=HeadlessScreen(QRect(0, 0, 1920, 1080)))
        self.addCleanup(wallpaper.deleteLater)
        self.addCleanup(wallpaper.shutdown)
        wallpaper.check_auto_pause_timer.stop()
        wallpaper.fullscreen_detection_timer.stop()
        return wallpaper


    def test_screens_restore_their_own_wallpaper_after_shared_history_is_cleared(self):
        """
        兩個螢幕交錯選擇與清除共用紀錄後, 重新建立仍恢復各自桌布
        """

        with tempfile.TemporaryDirectory() as directory:
            media_dir = Path(directory) / "media"
            media_dir.mkdir()
            first = media_dir / "a.mp4"
            second = media_dir / "b.png"
            first.touch()
            second.touch()
            store = ConfigStore(Path(directory) / "config.json", media_dir, ROOT / "lang")
            self.namespace["config_store"] = store
            self.namespace["MEDIA_DIR"] = media_dir
            self.namespace["get_media"].return_value = [first, second]

            screen_a = self.create_independent_wallpaper("screen-a")
            screen_a.select_media(first)
            screen_b = self.create_independent_wallpaper("screen-b")
            screen_b.select_media(second)
            config = store.load()
            self.assertEqual(config["screenWallpapers"], {"screen-a": "a.mp4", "screen-b": "b.png"})
            self.assertEqual(config["recentVideo"], ["b.png", "a.mp4"])

            tray = self.namespace["Tray"].__new__(self.namespace["Tray"])
            tray.wallpaper = screen_a
            tray.clear_recent_media()
            self.assertEqual(store.load()["recentVideo"], [])
            original_config = store.config_path.read_bytes()
            screen_a.shutdown()
            screen_b.shutdown()

            restored_b = self.create_independent_wallpaper("screen-b")
            restored_a = self.create_independent_wallpaper("screen-a")
            self.assertEqual(restored_a.current_media, first)
            self.assertEqual(restored_b.current_media, second)
            restored_a.reattach_to_desktop()
            restored_b.reattach_to_desktop()
            self.assertEqual(store.config_path.read_bytes(), original_config)
            self.namespace["logger"].exception.assert_not_called()


    def test_legacy_recent_selection_is_migrated_without_reordering_history(self):
        """
        沒有獨立選擇的螢幕沿用舊最近桌布, 保存時不更新共用紀錄
        """

        store = self.namespace["config_store"]
        store.load.return_value = {
            "recentVideo": ["legacy.mp4", "older.png"],
            "screenWallpapers": {"other-screen": "other.mp4"},
        }

        with mock.patch.object(Path, "is_file", return_value=True):
            wallpaper = self.create_independent_wallpaper("new-screen")

        self.assertEqual(wallpaper.current_media, self.namespace["MEDIA_DIR"] / "legacy.mp4")
        store.remember_wallpaper.assert_called_once_with(
            "new-screen", wallpaper.current_media
        )
        store.remember_recent.assert_not_called()


    def test_missing_saved_media_keeps_selection_while_using_available_fallback(self):
        """
        保存的檔案暫時不存在時使用可用桌布, 不覆蓋原本的螢幕選擇
        """

        store = self.namespace["config_store"]
        store.load.return_value = {"screenWallpapers": {"screen-a": "missing.mp4"}}
        self.namespace["get_media"].return_value = [self.media_path]
        wallpaper = self.create_independent_wallpaper("screen-a")

        self.assertEqual(wallpaper.current_media, self.media_path)
        store.remember_wallpaper.assert_not_called()


    def test_loading_or_reattaching_does_not_save_a_selection_or_change_history(self):
        """
        底層載入與重掛不寫入各螢幕選擇或共用最近紀錄
        """

        store = self.namespace["config_store"]

        for path in (self.media_path, self.image_path):
            with self.subTest(path=path):
                self.wallpaper.set_play_media(path)
                self.wallpaper.reattach_to_desktop()
                store.remember_wallpaper.assert_not_called()
                store.remember_recent.assert_not_called()
                store.save.assert_not_called()


    def test_default_target_stays_on_original_screen_after_primary_changes(self):
        """
        主螢幕改變後仍使用建立時的目標螢幕
        """

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
        """
        依指定螢幕設定位置, 裁切比例與重掛目標
        """

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
        """
        只有目標螢幕的幾何變更才觸發重掛並更新尺寸
        """

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
        """
        找不到螢幕時在建立媒體輸出前失敗
        """

        self.namespace["QApplication"].primaryScreen.return_value = None
        self.namespace["vlc"].Instance.reset_mock()
        self.namespace["bb_image_display"].Instance.reset_mock()

        with self.assertRaisesRegex(RuntimeError, "找不到可用的目標螢幕"):
            self.namespace["Wallpaper"]()

        self.namespace["vlc"].Instance.assert_not_called()
        self.namespace["bb_image_display"].Instance.assert_not_called()


    def test_pause_during_loading_applies_after_playing_event(self):
        """
        載入期間收到暫停, Playing 事件後套用暫停
        """

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
        """
        背景執行緒送出 Playing 事件後由 Qt 主執行緒暫停
        """

        self.start_loading()
        self.tray.pause()
        self.player.pause_calls.clear()

        # 從背景執行緒送出 Playing 事件
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
        """
        延遲 Playing 事件不覆蓋較新的播放或停止命令
        """

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
        """
        退出後忽略已排入佇列的 Playing 事件
        """

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
        """
        螢幕變更後重掛時保留播放狀態與進度
        """

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
        """
        先停止舊影片輸出, 再銷毀與重新綁定視窗
        """

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
        """
        停止影片輸出時持續處理 Qt 事件並忽略延遲暫停
        """

        self.start_loading()
        self.tray.pause()
        self.wallpaper.is_recreating = True
        processed = threading.Event()
        woken = threading.Event()
        self.namespace["wake_vlc_video_windows"].side_effect = woken.set

        def stop_waiting_for_ui():
            # 等待 Qt 事件與 VLC 視窗喚醒
            if not processed.wait(2) or not woken.wait(2):
                raise RuntimeError("Qt event loop blocked")

        QTimer.singleShot(0, processed.set)
        self.player.finish_loading()
        self.player.pause_calls.clear()

        with mock.patch.object(self.player, "stop", side_effect=stop_waiting_for_ui):
            self.wallpaper.stop_video_output()

        self.assertTrue(processed.is_set())
        self.assertEqual(self.player.pause_calls, [])
        self.assertEqual(self.wallpaper.play_state, "pause")
        self.namespace["wake_vlc_video_windows"].assert_called_once_with()
        self.assertFalse(any(timer.isActive() for timer in self.wallpaper.findChildren(QTimer)))


    def test_failed_background_stop_preserves_window_and_schedules_retry(self):
        """
        背景停止失敗時保留視窗並排定重試
        """

        with mock.patch.object(self.player, "stop", side_effect=RuntimeError("stop failed")):
            self.wallpaper.reattach_to_desktop()

        self.wallpaper.destroy.assert_not_called()
        self.wallpaper.hide.assert_not_called()
        self.assertFalse(self.wallpaper.is_recreating)
        self.assertTrue(self.wallpaper.reattach_timer.isActive())
        self.assertEqual(self.wallpaper.reattach_retry_count, 1)
        self.namespace["logger"].exception.assert_called_once()


    def test_reattach_seeks_after_playing_when_initial_seek_is_ignored(self):
        """
        重掛時未能立即恢復進度, 等 Playing 事件後補套
        """

        self.start_loading()
        self.player.finish_loading()
        self.app.processEvents()
        self.tray.pause()
        self.player.position = 4321

        def seek_when_ready(position):
            # 載入完成後才接受進度設定
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
        """
        停止或切換媒體時取消尚未套用的重掛進度
        """

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
        """
        載入期間的自動暫停會生效, 條件解除後恢復播放
        """

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
        """
        自動暫停條件解除後仍保留手動暫停
        """

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
        """
        圖片使用 Qt 顯示, 停止後再播放時重用已載入的圖片
        """

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
        """
        切換圖片與影片時停止並清理前一種輸出
        """

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
        """
        停止狀態切換媒體時不提前顯示輸出
        """

        self.wallpaper.set_play_media(self.image_path)
        self.assertEqual(self.wallpaper.play_state, "stop")
        self.assertFalse(self.image_output.visible)

        self.wallpaper.set_play_media(self.media_path)
        self.assertEqual(self.player.state, "stopped")
        self.assertEqual(self.wallpaper.play_state, "stop")


    def test_paused_video_switches_to_visible_image_and_ignores_old_event(self):
        """
        暫停影片切換成圖片後直接顯示並忽略舊播放事件
        """

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
        """
        圖片不受手動與自動暫停影響
        """

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
        """
        圖片載入失敗時保留既有輸出, 狀態與最近紀錄
        """

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
                    self.namespace["config_store"].remember_wallpaper.reset_mock()
                    self.instance_image.set_image.side_effect = RuntimeError("bad image")

                    with self.assertRaisesRegex(RuntimeError, "bad image"):
                        self.wallpaper.select_media(self.image_path.with_name("bad.png"))

                    self.assertEqual(self.wallpaper.current_media, previous_media)
                    self.assertEqual(self.wallpaper.play_state, command)
                    self.assertEqual(self.player.state, previous_state)
                    self.assertEqual(self.image_output.current_image, previous_image)
                    self.assertEqual(self.image_output.visible, previous_visible)
                    self.namespace["config_store"].save.assert_not_called()
                    self.namespace["config_store"].remember_wallpaper.assert_not_called()


    def test_select_media_saves_this_screen_and_updates_shared_history(self):
        """
        使用者選擇圖片成功後啟動顯示, 保存此螢幕選擇與共用最近紀錄
        """

        self.wallpaper.enable_auto_pause = False
        self.wallpaper.select_media(self.image_path)

        self.assertTrue(self.image_output.visible)
        self.assertTrue(self.wallpaper.enable_auto_pause)
        self.namespace["config_store"].remember_recent.assert_called_once_with(self.image_path)
        self.namespace["config_store"].remember_wallpaper.assert_called_once_with(
            self.wallpaper.screen_id, self.image_path
        )


    def test_attach_binds_both_outputs_to_same_hwnd(self):
        """
        圖片與影片輸出綁定同一個桌布 HWND
        """

        self.attach_to_desktop(self.wallpaper)

        self.namespace["set_windows_as_wallpaper"].assert_called_once_with(1)
        self.player.set_hwnd.assert_called_once_with(1)
        self.instance_image.set_hwnd.assert_called_once_with(1)
        self.namespace["register_power_notification"].assert_called_once_with(1)
        self.assertEqual(self.wallpaper.power_notify, 2**40 + 1)


    def test_display_notification_updates_auto_pause(self):
        """
        螢幕亮滅通知更新自動暫停狀態
        """

        self.start_loading()

        for event_type, display_state in ((b"windows_generic_MSG", 0), ("windows_generic_MSG", 1)):
            with self.subTest(display_state=display_state):
                self.namespace["get_display_state"].return_value = display_state
                result = self.wallpaper.nativeEvent(event_type, 123)

                self.assertEqual(result, (True, 0))
                self.assertEqual(self.wallpaper.screen_off, display_state == 0)
                self.assertEqual(self.wallpaper.play_state, "pause" if display_state == 0 else "play")


    def test_destroy_notification_skips_reattach_while_recreating_or_exiting(self):
        """
        重建或退出期間收到視窗銷毀通知時不再排定重掛
        """

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
        """
        螢幕變更與視窗銷毀通知共用一次重掛排程
        """

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
        # 確認下一個等待區間沒有重複重掛
        QTest.qWait(250)

        self.assertEqual(timeouts.count(), 1)
        self.wallpaper.attach_to_desktop.assert_called_once_with()
        self.wallpaper.destroy.assert_called_once_with(True, True)
        self.assertFalse(timer.isActive())


    def test_failed_reattach_retries_twice_and_merges_new_events(self):
        """
        掛載失敗最多重試兩次, 新通知沿用既有排程
        """

        self.namespace["wintypes"].MSG.from_address.return_value.message = self.namespace["WM_DESTROY"]
        self.wallpaper.attach_to_desktop.reset_mock()
        self.wallpaper.attach_to_desktop.side_effect = RuntimeError("desktop unavailable")
        timer = self.wallpaper.reattach_timer
        self.wallpaper.on_screen_changed(mock.sentinel.geometry)

        # 首次掛載加上兩次重試
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
        """
        只收到工作列重建通知也能恢復桌布掛載
        """

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
        """
        工作列重建後等待新桌面就緒再掛載
        """

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
        """
        退出後不因佇列中的工作列通知重新啟動重掛
        """

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
        """
        通知監聽初始化失敗時桌布仍可使用
        """

        self.namespace["DesktopEventWindow"] = mock.Mock(side_effect=OSError("registration failed"))
        wallpaper = self.namespace["Wallpaper"]()
        self.addCleanup(self.dispose_wallpaper, wallpaper)

        self.assertIsNone(wallpaper.desktop_events)
        self.assertTrue(wallpaper.check_auto_pause_timer.isActive())
        self.namespace["logger"].exception.assert_called_once_with("建立工作列重建通知視窗失敗")


    def test_unready_desktop_preserves_output_until_ready(self):
        """
        等待桌面就緒期間保留既有媒體輸出
        """

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
        """
        等待桌面就緒時保留掛載失敗的重試次數
        """

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
        """
        退出時取消等待桌面就緒
        """

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
        self.wallpaper.destroy.assert_called_once_with(True, True)
        self.namespace["logger"].exception.assert_not_called()


    def test_successful_retry_clears_retry_count(self):
        """
        重試成功後清除重試次數
        """

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
        """
        直接重掛時取消尚未執行的排程
        """

        self.wallpaper.attach_to_desktop.reset_mock()
        self.wallpaper.schedule_reattach(0)
        self.wallpaper.reattach_to_desktop()
        self.app.processEvents()

        self.wallpaper.attach_to_desktop.assert_called_once_with()
        self.assertFalse(self.wallpaper.reattach_timer.isActive())


    def test_exit_cancels_pending_reattach_and_rejects_new_requests(self):
        """
        退出時取消重掛排程並拒絕新的要求
        """

        self.wallpaper.attach_to_desktop.reset_mock()
        self.wallpaper.schedule_reattach(0)
        self.tray.exit()
        self.wallpaper.on_screen_changed(mock.sentinel.geometry)
        self.app.processEvents()

        self.assertFalse(self.wallpaper.reattach_timer.isActive())
        self.wallpaper.attach_to_desktop.assert_not_called()
        self.wallpaper.destroy.assert_called_once_with(True, True)
        self.assertEqual(self.player.state, "released")
        self.namespace["logger"].exception.assert_not_called()


    def test_unregister_clears_handle_even_if_api_fails(self):
        """
        取消電源通知失敗時仍清除通知 handle
        """

        handle = 2**40 + 1
        self.wallpaper.power_notify = handle
        self.namespace["unregister_power_notification"].side_effect = OSError("invalid handle")

        self.unregister_power_notification(self.wallpaper)

        self.namespace["unregister_power_notification"].assert_called_once_with(handle)
        self.assertIsNone(self.wallpaper.power_notify)
        self.namespace["logger"].exception.assert_called_once()


    def test_image_reattach_rebinds_without_reload_or_vlc_seek(self):
        """
        圖片重掛時重新綁定目標, 不重讀圖片或調整影片進度
        """

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


    def test_shutdown_is_idempotent_and_blocks_late_operations_without_quitting(self):
        """
        重複關閉可安全清理並阻止後續操作, 不退出 Qt 應用程式
        """

        self.start_loading()
        self.wallpaper.pending_reattach_time = 4321
        self.wallpaper.attach_to_desktop.reset_mock()
        self.wallpaper.reattach_timer.start(10000)
        self.wallpaper.check_auto_pause_timer.start(10000)
        self.wallpaper.fullscreen_detection_timer.start(10000)
        listener = self.wallpaper.desktop_events
        instance = self.wallpaper.instance_vlc

        with (
            mock.patch.object(self.player, "stop", wraps=self.player.stop) as stop,
            mock.patch.object(self.player, "release", wraps=self.player.release) as release,
        ):
            operations = mock.Mock()
            operations.attach_mock(stop, "stop")
            operations.attach_mock(self.instance_image.close, "close_image")
            operations.attach_mock(release, "release_player")
            operations.attach_mock(instance.release, "release_instance")
            operations.attach_mock(self.wallpaper.destroy, "destroy")

            self.wallpaper.shutdown()
            self.wallpaper.shutdown()
            self.wallpaper.media_play()
            self.wallpaper.media_pause()
            self.wallpaper.media_stop()
            self.wallpaper.set_play_media(self.media_path)
            self.wallpaper.select_media(self.media_path)
            self.attach_to_desktop(self.wallpaper)
            self.wallpaper.enable_fullscreen_detection()
            self.wallpaper.check_auto_pause()
            self.wallpaper.on_taskbar_created()
            self.wallpaper.target_screen.set_geometry(QRect(0, 0, 2560, 1440))
            self.app.processEvents()

            self.assertEqual(operations.mock_calls, [
                mock.call.stop(),
                mock.call.close_image(),
                mock.call.release_player(),
                mock.call.release_instance(),
                mock.call.destroy(True, True),
            ])

        self.assertTrue(self.wallpaper.is_exiting)
        self.assertIsNone(self.wallpaper.current_media)
        self.assertIsNone(self.wallpaper.pending_reattach_time)
        self.assertEqual(self.wallpaper.play_state, "stop")
        self.assertEqual(self.player.state, "released")
        self.assertEqual(self.player.callbacks, {})
        self.assertFalse(self.wallpaper.fullscreen_detection_ready)
        self.assertFalse(any(timer.isActive() for timer in self.wallpaper.findChildren(QTimer)))
        self.assertIsNone(self.wallpaper.desktop_events)
        listener.close.assert_called_once_with()
        self.instance_image.set_hwnd.assert_called_with(0)
        self.instance_image.deleteLater.assert_called_once_with()
        self.wallpaper.unregister_power_notification.assert_called_once_with()
        self.wallpaper.attach_to_desktop.assert_not_called()
        self.namespace["is_any_fullscreen"].assert_not_called()
        self.tray.app.quit.assert_not_called()
        self.namespace["logger"].exception.assert_not_called()


    def test_shutdown_continues_after_listener_stop_image_and_player_release_failures(self):
        """
        個別清理步驟失敗時仍釋放其餘資源
        """

        listener = self.wallpaper.desktop_events
        listener.close.side_effect = RuntimeError("listener close failed")
        self.instance_image.close.side_effect = RuntimeError("image close failed")

        with (
            mock.patch.object(self.player, "stop", side_effect=RuntimeError("stop failed")),
            mock.patch.object(self.player, "release", side_effect=RuntimeError("release failed")),
        ):
            self.wallpaper.shutdown()

        self.wallpaper.unregister_power_notification.assert_called_once_with()
        self.instance_image.set_hwnd.assert_called_with(0)
        self.instance_image.deleteLater.assert_called_once_with()
        self.wallpaper.instance_vlc.release.assert_called_once_with()
        self.wallpaper.hide.assert_called_once_with()
        self.wallpaper.destroy.assert_called_once_with(True, True)
        self.assertIsNone(self.wallpaper.desktop_events)
        self.assertEqual(self.player.callbacks, {})
        self.assertEqual(self.namespace["logger"].exception.call_count, 4)


    def test_shutdown_during_reattach_reuses_stop_and_prevents_rebinding(self):
        """
        重掛途中關閉時共用停止工作並阻止重新綁定
        """

        self.start_loading()
        self.wallpaper.attach_to_desktop.reset_mock()
        continue_stop = threading.Event()
        original_stop = self.player.stop

        def stop_waiting_for_shutdown():

            if not continue_stop.wait(2):
                raise RuntimeError("shutdown was not processed")
            original_stop()

        def shutdown_during_stop():

            continue_stop.set()
            self.wallpaper.shutdown()

        QTimer.singleShot(0, shutdown_during_stop)

        with mock.patch.object(self.player, "stop", side_effect=stop_waiting_for_shutdown) as stop:
            self.wallpaper.reattach_to_desktop()

        stop.assert_called_once_with()
        self.assertTrue(self.wallpaper.is_exiting)
        self.assertFalse(self.wallpaper.is_recreating)
        self.assertEqual(self.player.state, "released")
        self.wallpaper.attach_to_desktop.assert_not_called()
        self.wallpaper.create.assert_not_called()
        self.wallpaper.destroy.assert_called_once_with(True, True)
        self.namespace["logger"].exception.assert_not_called()


    def test_shutdown_does_not_affect_another_wallpaper(self):
        """
        關閉一個桌布時不影響另一個桌布
        """

        other_player = DeferredPlayer()
        other_instance = mock.Mock()
        other_instance.media_player_new.return_value = other_player
        other_image = mock.Mock(wraps=ImageDisplay())
        self.namespace["vlc"].Instance.return_value = other_instance
        self.namespace["bb_image_display"].Instance.return_value = other_image
        other_screen = HeadlessScreen(QRect(1920, 0, 2560, 1440))
        other = self.namespace["Wallpaper"](target_screen=other_screen)
        self.addCleanup(other.deleteLater)
        self.addCleanup(other.shutdown)
        other.set_play_media(self.media_path)
        other.media_play()
        other.media_pause()
        other_image.close.reset_mock()

        self.wallpaper.shutdown()
        other_player.finish_loading()
        self.app.processEvents()
        other_screen.set_geometry(QRect(1920, 0, 1440, 2560))

        self.assertEqual(other_player.state, "paused")
        self.assertFalse(other.is_exiting)
        self.assertTrue(other.check_auto_pause_timer.isActive())
        self.assertTrue(other.reattach_timer.isActive())
        other_instance.release.assert_not_called()
        other_image.close.assert_not_called()
        other.destroy.assert_not_called()
        self.tray.app.quit.assert_not_called()
        self.namespace["logger"].exception.assert_not_called()


    def test_image_exit_releases_image_and_vlc(self):
        """
        播放圖片時退出會釋放圖片與 VLC 資源
        """

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
        """
        啟動時直接顯示初始圖片
        """

        self.namespace["get_media"].return_value = [self.image_path]
        wallpaper = self.namespace["Wallpaper"]()
        self.addCleanup(self.dispose_wallpaper, wallpaper)

        self.assertEqual(wallpaper.current_media, self.image_path)
        self.assertEqual(wallpaper.play_state, "play")
        self.assertTrue(self.image_output.visible)
        self.assertEqual(self.player.state, "stopped")


    def test_bad_startup_image_keeps_app_available(self):
        """
        初始圖片載入失敗時應用程式仍可使用
        """

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
