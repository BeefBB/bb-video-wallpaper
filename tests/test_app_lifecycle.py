"""
測試播放, 匯入與桌布重掛之間的競態
使用真正的 Qt 訊號, 計時器與工作執行緒, 不啟動桌布或修改使用者設定
"""

import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QMenu, QSystemTrayIcon
from shiboken6 import isValid


ROOT = Path(__file__).resolve().parents[1]
PLAYING_EVENT = object()


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from playback import PlaybackState
from app_loader import QtTestCase, load_app_module
import bb_video_display


class HeadlessWidget(QObject):
    """
    保留 Qt 訊號能力, 以替身處理視窗操作
    """

    def __init__(self):
        """
        初始化 Qt 物件, 將視窗操作替換為可記錄呼叫的替身
        """

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
        """
        初始化 Qt 物件並保存螢幕位置與尺寸
        """

        super().__init__()
        self._geometry = geometry


    def geometry(self) -> QRect:
        """
        回傳目前保存的螢幕位置與尺寸
        """

        return self._geometry


class HeadlessDesktopEvents(QObject):
    """
    以 Qt 訊號模擬工作列重建通知
    """

    taskbar_created = Signal()

    def __init__(self):
        """
        初始化 Qt 通知物件並以替身記錄關閉操作
        """

        super().__init__()
        self.close = mock.Mock()


class DeferredPlayer:
    """
    模擬載入期間暫停尚未生效, Playing 事件稍後才送達的播放器
    """

    def __init__(self):
        """
        初始化停止狀態與播放進度, 建立事件管理及視窗綁定替身
        """

        self.state = "stopped"
        self.position = 0
        self.pause_calls = []
        self.callbacks = {}
        self.last_playing_callback = None

        self.events = mock.Mock(spec=["event_attach", "event_detach"])
        self.events.event_attach.side_effect = self.attach_event
        self.events.event_detach.side_effect = self.callbacks.pop

        self.video_set_key_input = mock.Mock()
        self.video_set_mouse_input = mock.Mock()
        self.set_hwnd = mock.Mock()


    def attach_event(self, event, callback) -> None:

        self.callbacks[event] = callback
        if event is PLAYING_EVENT:
            self.last_playing_callback = callback


    def event_manager(self) -> mock.Mock:
        """
        回傳可註冊與移除播放事件回呼的替身
        """

        return self.events


    def play(self) -> int:
        """
        將暫停狀態恢復播放, 其他未播放狀態則改為載入中
        """

        if self.state == "paused":
            self.state = "playing"

        elif self.state != "playing":
            self.state = "opening"
            if self.position < 0:
                self.position = 0

        return 0


    def set_pause(self, paused: int) -> None:
        """
        記錄暫停要求與執行緒, 僅在已播放或暫停時切換狀態
        """

        self.pause_calls.append((paused, threading.get_ident()))

        if self.state in {"playing", "paused"}:
            self.state = "paused" if paused else "playing"


    def stop(self) -> None:
        """
        將播放器設為停止, 並使播放進度失效
        """

        self.state = "stopped"
        self.position = -1


    def set_media(self, media) -> None:
        """
        模擬更換媒體時將播放進度歸零
        """

        self.position = 0


    def get_time(self) -> int:
        """
        回傳目前保存的播放進度
        """

        return self.position


    def get_state(self) -> str:
        """
        回傳目前的播放器狀態
        """

        return self.state


    def get_length(self) -> int:
        """
        回傳固定為 10000 毫秒的媒體長度
        """

        return 10000


    def is_seekable(self) -> bool:
        """
        只在播放或暫停狀態回報可調整播放進度
        """

        return self.state in {"playing", "paused"}


    def set_time(self, position: int) -> None:
        """
        直接保存指定的播放進度
        """

        self.position = position


    def finish_loading(self) -> None:
        """
        完成尚在載入的播放狀態並觸發 Playing 回呼, 停止狀態不重新播放
        """

        # 停止後仍可能收到先前的通知, 但通知本身不會重新啟動播放器
        if self.state == "opening":
            self.state = "playing"
            if self.position < 0:
                self.position = 0

        callback = self.callbacks.get(PLAYING_EVENT, self.last_playing_callback)
        if callback is not None:
            callback(None)


    def release(self) -> None:
        """
        將播放器標記為已釋放
        """

        self.state = "released"


class ImageDisplay:
    """
    記錄圖片的載入, 顯示與目標視窗, 不建立原生視窗
    """

    def __init__(self):
        """
        初始化空白圖片, 隱藏狀態與目標視窗, 並建立延後刪除替身
        """

        self.current_image: Path | None = None
        self.visible = False
        self.hwnd = 0
        self.deleteLater = mock.Mock()
        self.set_scaling = mock.Mock()
        self.set_background_image = mock.Mock()


    def set_media(self, path: Path) -> None:
        """
        保存指定圖片路徑, 不變更顯示狀態
        """

        self.current_image = path


    def show(self) -> None:
        """
        將已設定的圖片標記為顯示, 缺少圖片時拋出例外
        """

        if self.current_image is None:
            raise RuntimeError("尚未設定圖片")

        self.visible = True


    def hide(self) -> None:
        """
        隱藏圖片並保留已載入的路徑
        """

        self.visible = False


    def set_hwnd(self, hwnd: int) -> None:
        """
        保存目標視窗代碼, 目標改變時先隱藏圖片
        """

        if hwnd != self.hwnd:
            self.visible = False

        self.hwnd = hwnd


    def close(self) -> None:
        """
        隱藏圖片並清除已載入的路徑
        """

        self.visible = False
        self.current_image = None


class VideoDisplay(bb_video_display.Instance):
    """
    保留影片模組的播放與停止流程, 只替換原生視窗及影像軌查詢
    """

    def __init__(self, hardware_decoding: str = "any") -> None:

        super().__init__(hardware_decoding=hardware_decoding)
        self._hwnd = 1
        self._bound_hwnd = 0
        self._layout = mock.Mock()
        self._read_video_size = mock.Mock()
        try:
            self._ensure_player()
        except Exception:
            self.close()
            self.deleteLater()
            raise


    def show(self) -> None:

        if self._hwnd and self._hwnd != self._bound_hwnd:
            assert self._player is not None
            self._player.set_hwnd(self._hwnd)
            self._bound_hwnd = self._hwnd
        self._visible = True


    def hide(self) -> None:

        self._visible = False


    def set_hwnd(self, hwnd: int, *, process_events: bool = False) -> None:

        if hwnd != self._hwnd:
            self._bound_hwnd = 0
        super().set_hwnd(hwnd, process_events=process_events)


class PlaybackStateTests(QtTestCase):
    """
    桌布播放狀態, 重掛與資源釋放
    """

    def setUp(self) -> None:
        """
        建立播放器, 圖片輸出與桌布替身
        """

        super().setUp()

        media_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        for name in ("sample.mp4", "new.mp4"):
            (media_dir / name).touch()

        self.player = DeferredPlayer()
        image_module = mock.Mock()
        image_module.Instance.return_value = mock.Mock(wraps=ImageDisplay())

        instance = mock.Mock()
        instance.media_player_new.return_value = self.player

        vlc = mock.Mock()
        self.vlc = vlc
        vlc.Instance.return_value = instance
        vlc.EventType.MediaPlayerPlaying = PLAYING_EVENT
        vlc.State.Playing = "playing"
        vlc.State.Paused = "paused"
        vlc.State.Stopped = "stopped"
        vlc.State.NothingSpecial = "nothing"
        self.enterContext(mock.patch.dict(sys.modules, {"vlc": vlc}))
        self.enterContext(mock.patch.object(bb_video_display.win32gui, "IsWindow", return_value=True))

        config_store = mock.Mock()
        config_store.load.return_value = {}

        application = mock.Mock()
        application.primaryScreen.return_value = HeadlessScreen(QRect(0, 0, 1920, 1080))

        self.namespace = {
            # Qt 類別與視窗替身
            "QApplication": application,
            "DesktopEventWindow": HeadlessDesktopEvents,
            "bb_image_display": image_module,
            "bb_video_display": mock.Mock(Instance=VideoDisplay),
            "config_store": config_store,
            "get_media": mock.Mock(return_value=[]),
            "MEDIA_DIR": media_dir,
            "SUPPORTED_IMAGE_EXTENSIONS": {".png", ".jpg"},
            "SUPPORTED_VIDEO_EXTENSIONS": {".mp4"},
            "is_on_battery": mock.Mock(return_value=False),
            "is_any_fullscreen": mock.Mock(return_value=False),
            "check_desktop_ready": mock.Mock(return_value=True),
            "get_monitor_id": mock.Mock(return_value="monitor-1"),
            "set_windows_as_wallpaper": mock.Mock(),
            "register_power_notification": mock.Mock(return_value=2**40 + 1),
            "unregister_power_notification": mock.Mock(),
            "get_display_state": mock.Mock(return_value=None),
            "wintypes": mock.Mock(),
            "logger": mock.Mock(),
        }
        # 匯入時替換 QWidget 基底, 保留真正的 Qt 訊號與計時器
        with mock.patch("PySide6.QtWidgets.QWidget", HeadlessWidget):
            module = load_app_module()
        module.__dict__.update(self.namespace)
        self.namespace = module.__dict__
        self.enterContext(mock.patch.object(bb_video_display, "wake_vlc_video_windows"))
        self.enterContext(mock.patch.object(bb_video_display, "logger", self.namespace["logger"]))

        wallpaper_class = self.namespace["Wallpaper"]
        # 保留原始方法供個別案例呼叫
        self.unregister_power_notification = wallpaper_class.unregister_power_notification

        self.enterContext(mock.patch.object(wallpaper_class, "unregister_power_notification"))

        self.wallpaper = wallpaper_class()
        self.addCleanup(self.dispose_wallpaper)
        self.wallpaper.check_auto_pause_timer.stop()
        self.wallpaper.fullscreen_detection_timer.stop()

        tray_class = self.namespace["Tray"]
        # 建立播放命令入口
        self.tray = tray_class.__new__(tray_class)
        self.tray.wallpapers = {self.wallpaper.screen_id: self.wallpaper}
        self.tray.is_exiting = False
        self.tray.waiting_for_screens = False
        self.tray.follow_controller = self.namespace["FollowController"](self.tray.wallpapers, config_store)
        self.tray.follow_sync_timer = mock.Mock()
        self.tray.follow_restore_timer = mock.Mock()
        self.tray.import_thread = None
        self.tray.import_screen_id = self.wallpaper.screen_id
        self.tray.import_applies_to_all = False
        self.tray.import_stopped_screens = set()
        self.tray.media_menu = mock.Mock()
        self.tray.thumbnail_thread = mock.Mock()
        self.tray.screen_menus = {}
        self.tray.app = mock.Mock()
        self.media_path = self.namespace["MEDIA_DIR"] / "sample.mp4"
        self.image_path = self.namespace["MEDIA_DIR"] / "sample.PNG"
        self.connect_follow_events(self.wallpaper)


    def connect_follow_events(self, wallpaper) -> None:
        """
        接上群組自動暫停處理, 並以佇列訊號通知控制器播放或重掛已就緒
        """

        wallpaper.auto_pause_handler = self.tray.follow_controller.check_auto_pause
        for signal in (wallpaper.playback_started, wallpaper.reattach_finished):
            signal.connect(
                lambda s=wallpaper.screen_id, w=wallpaper: self.tray.follow_controller.on_ready(s, w),
                Qt.ConnectionType.QueuedConnection
            )


    def dispose_wallpaper(self) -> None:
        """
        停止重掛排程並釋放桌布與監聽物件
        """

        wallpaper = self.wallpaper

        if not isValid(wallpaper):
            return

        wallpaper.is_exiting = True
        wallpaper.reattach_timer.stop()

        if wallpaper.desktop_events is not None:
            wallpaper.desktop_events.deleteLater()

        wallpaper.instance_video.close()
        wallpaper.deleteLater()


    def start_loading(self) -> None:
        """
        開始載入影片, 保留尚未送達的 Playing 事件
        """

        self.wallpaper.set_play_media(self.media_path)
        self.tray.play()
        self.assertEqual(self.player.state, "opening")


    def test_delayed_blur_respects_current_media_mode_and_saved_choice(self) -> None:
        """
        背景產生期間換片, 切換縮放或選回純色, 已排隊的結果不可覆蓋新選擇
        """

        directory = self.media_path.parent
        store = self.namespace["ConfigStore"](directory / "config.json", directory, ROOT / "lang")
        self.namespace["config_store"] = store
        self.wallpaper.set_play_media(self.media_path)
        screen_id = self.wallpaper.screen_id
        self.tray.set_scaling(screen_id, "fit")
        self.tray.set_background_color(screen_id, "blur")
        self.tray.thumbnail_thread.request_background.assert_called_with(self.media_path)
        self.assertEqual(store.load()["backgroundColor"][self.media_path.name], "blur")

        image = QImage(20, 10, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.blue)

        def deliver() -> None:
            self.tray.apply_blurred_background(str(self.media_path), image)

        QTimer.singleShot(0, deliver)
        self.wallpaper.set_play_media(directory / "new.mp4")
        self.app.processEvents()
        self.assertFalse(self.wallpaper.has_blurred_background)

        self.wallpaper.set_play_media(self.media_path)
        QTimer.singleShot(0, deliver)
        self.tray.set_scaling(screen_id, "fill")
        self.app.processEvents()
        self.assertFalse(self.wallpaper.has_blurred_background)

        self.tray.set_scaling(screen_id, "fit")
        deliver()
        self.assertIs(self.wallpaper.instance_video._background_image, image)
        QTimer.singleShot(0, deliver)
        self.tray.set_background_color(screen_id, "#123456")
        self.app.processEvents()
        self.assertIsNone(self.wallpaper.instance_video._background_image)
        self.assertEqual(self.wallpaper.instance_video._background_color.name(), "#123456")


    def start_playing(self) -> None:
        """
        開始載入影片並送出 Playing 事件, 處理 Qt 佇列至完成播放回呼
        """

        self.start_loading()
        self.player.finish_loading()
        self.app.processEvents()


    def add_following_wallpaper(self):
        """
        登記第二台螢幕的桌布與跟隨事件, 並安排資源清理
        """

        instance = mock.Mock()
        instance.media_player_new.return_value = DeferredPlayer()
        self.vlc.Instance.return_value = instance
        self.namespace["bb_image_display"].Instance.return_value = mock.Mock(wraps=ImageDisplay())
        self.namespace["get_monitor_id"].return_value = "screen-b"
        wallpaper = self.namespace["Wallpaper"](target_screen=HeadlessScreen(QRect(0, 0, 1920, 1080)))
        self.addCleanup(lambda: wallpaper.deleteLater() if isValid(wallpaper) else None)
        self.addCleanup(wallpaper.shutdown)
        wallpaper.check_auto_pause_timer.stop()
        wallpaper.fullscreen_detection_timer.stop()
        self.tray.wallpapers[wallpaper.screen_id] = wallpaper
        self.connect_follow_events(wallpaper)
        return wallpaper


    def test_follow_recovery_preserves_pending_position_then_realigns_followers(self) -> None:
        """
        驗證來源重掛期間保留待恢復進度, 就緒後才重新對齊跟隨者
        """

        self.start_loading()
        self.player.finish_loading()
        follower = self.add_following_wallpaper()
        self.tray.follow_controller.follow(follower.screen_id, self.wallpaper.screen_id)
        follower.instance_video._player.finish_loading()
        self.app.processEvents()
        self.player.position = 7500
        follower.instance_video._player.position = 1000
        self.wallpaper.reattach_to_desktop()
        self.app.processEvents()
        self.assertEqual(self.wallpaper.instance_video._pending_time, 7500)
        self.assertEqual(follower.instance_video._player.position, 1000)

        self.player.finish_loading()
        self.app.processEvents()
        self.app.processEvents()
        self.assertIsNone(self.wallpaper.instance_video._pending_time)
        self.assertEqual(follower.instance_video._player.position, 7500)


    def test_playing_callback_from_worker_pauses_on_qt_thread(self) -> None:
        """
        驗證背景執行緒送出的 Playing 事件由 Qt 主執行緒套用先前的暫停要求
        """

        self.start_loading()
        self.tray.pause()
        self.player.pause_calls.clear()
        self.assertEqual(self.player.state, "opening")
        self.assertEqual(self.wallpaper.playback_state, PlaybackState("pause"))

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


    def test_latest_play_or_stop_wins_over_delayed_playing_event(self) -> None:
        """
        驗證較新的播放或停止命令優先於延遲的 Playing 事件
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


    def test_stop_or_media_selection_cancels_pending_seek_during_load_or_retry(self) -> None:
        """
        驗證載入中或掛載失敗等待重試時, 停止或切換媒體會捨棄舊進度
        """

        cases = (
            (False, "stop"), (False, "new_video"),
            (True, "stop"), (True, "same_video"), (True, "new_video"), (True, "image"),
        )
        for failed, action in cases:
            with self.subTest(failed=failed, action=action):
                if failed:
                    self.start_playing()
                    self.player.position = 4321
                    self.namespace["set_windows_as_wallpaper"].side_effect = [RuntimeError("attachment failed"), None]
                    self.wallpaper.reattach_to_desktop()
                else:
                    self.start_loading()
                    self.wallpaper.instance_video._pending_time = 4321

                if action == "stop":
                    self.tray.stop()
                else:
                    path = {
                        "same_video": self.media_path,
                        "new_video": self.media_path.with_name("new.mp4"),
                        "image": self.image_path,
                    }[action]
                    if failed:
                        self.wallpaper.select_media(path)
                    else:
                        self.wallpaper.set_play_media(path)

                with mock.patch.object(self.player, "set_time") as seek:
                    if failed:
                        self.wallpaper.reattach_timer.start(0)
                        self.app.processEvents()
                    self.player.finish_loading()
                    self.app.processEvents()
                seek.assert_not_called()
                self.assertIsNone(self.wallpaper.instance_video._pending_time)
                self.assertEqual(self.wallpaper.play_state, "stop" if action == "stop" else "play")


    def test_failed_reattach_retries_preserve_video_position(self) -> None:
        """
        驗證掛載失敗後的舊 Playing 通知不清除進度, 重試仍保留手動暫停
        """

        self.start_playing()
        self.tray.pause()
        self.player.position = 4321
        self.namespace["set_windows_as_wallpaper"].side_effect = [
            RuntimeError("attachment failed"), RuntimeError("attachment failed"), None,
        ]

        self.wallpaper.reattach_to_desktop()
        for _ in range(2):
            self.assertEqual(self.player.get_time(), -1)
            # 舊 Playing 通知不能清掉等待重試的進度
            self.player.finish_loading()
            self.app.processEvents()
            self.assertTrue(self.wallpaper.reattach_timer.isActive())
            self.wallpaper.reattach_timer.start(0)
            self.app.processEvents()

        self.player.finish_loading()
        self.app.processEvents()
        self.assertEqual(self.player.position, 4321)
        self.assertEqual(self.wallpaper.play_state, "pause")
        self.assertEqual(self.player.state, "paused")
        self.assertFalse(self.wallpaper.reattach_timer.isActive())
        self.assertEqual(self.wallpaper.reattach_retry_count, 0)


    def test_failed_background_stop_retries_with_saved_position(self) -> None:
        """
        驗證背景停止清除進度後失敗時保留視窗, 重試仍恢復停止前的位置
        """

        self.start_playing()
        self.player.position = 4321
        original_stop = self.player.stop
        failed = False

        def fail_first_stop():
            """
            停止播放器並清除進度, 僅第一次呼叫時額外拋出停止失敗例外
            """

            nonlocal failed
            original_stop()
            if not failed:
                failed = True
                raise RuntimeError("stop failed after clearing position")

        with mock.patch.object(self.player, "stop", side_effect=fail_first_stop):
            self.wallpaper.reattach_to_desktop()
            self.assertEqual(self.player.get_time(), -1)
            self.wallpaper.destroy.assert_not_called()
            self.wallpaper.hide.assert_not_called()
            self.assertFalse(self.wallpaper.is_recreating)
            self.assertEqual(self.wallpaper.reattach_retry_count, 1)
            self.assertTrue(self.wallpaper.reattach_timer.isActive())
            self.wallpaper.reattach_timer.start(0)
            self.app.processEvents()
            self.player.finish_loading()
            self.app.processEvents()

        self.assertEqual(self.player.position, 4321)
        self.assertEqual(self.player.state, "playing")


    def test_shutdown_is_idempotent_and_blocks_late_operations_without_quitting(self) -> None:
        """
        桌面等待期間關閉只釋放一次, 已排入與後續抵達的事件不再操作輸出
        """

        self.start_loading()
        self.tray.pause()
        # 在關閉前排入 Playing 事件, 確認處理佇列時不再操作已釋放播放器
        self.player.finish_loading()
        pause_calls = list(self.player.pause_calls)
        playing_callback = self.player.callbacks[PLAYING_EVENT]
        self.namespace["set_windows_as_wallpaper"].reset_mock()
        self.namespace["check_desktop_ready"].return_value = False
        self.wallpaper.reattach_to_desktop()
        self.assertTrue(self.wallpaper.reattach_timer.isActive())
        listener = self.wallpaper.desktop_events
        listener.taskbar_created.emit()
        instance = self.wallpaper.instance_video._instance
        handle = 2**40 + 1
        self.wallpaper.power_notify = handle
        self.wallpaper.unregister_power_notification.side_effect = lambda: self.unregister_power_notification(self.wallpaper)

        with (
            mock.patch.object(self.player, "stop", wraps=self.player.stop) as stop,
            mock.patch.object(self.player, "release", wraps=self.player.release) as release,
        ):
            operations = mock.Mock()
            operations.attach_mock(stop, "stop")
            operations.attach_mock(release, "release_player")
            operations.attach_mock(instance.release, "release_instance")
            operations.attach_mock(self.wallpaper.destroy, "destroy")

            self.wallpaper.shutdown()
            self.wallpaper.shutdown()
            self.wallpaper.unregister_power_notification()
            # 模擬解除事件註冊後仍抵達的原生回呼
            playing_callback(None)
            self.wallpaper.on_taskbar_created()
            self.wallpaper.reattach_to_desktop()
            self.app.processEvents()

            self.assertEqual(operations.mock_calls, [
                mock.call.stop(),
                mock.call.release_player(),
                mock.call.release_instance(),
                mock.call.destroy(True, True),
            ])

        self.assertEqual(self.player.state, "released")
        self.assertEqual(self.player.callbacks, {})
        self.assertEqual(self.player.pause_calls, pause_calls)
        self.assertFalse(any(timer.isActive() for timer in self.wallpaper.findChildren(QTimer)))
        listener.close.assert_called_once_with()
        self.namespace["set_windows_as_wallpaper"].assert_not_called()
        self.tray.app.quit.assert_not_called()
        self.namespace["unregister_power_notification"].assert_called_once_with(handle)
        self.assertIsNone(self.wallpaper.power_notify)
        self.namespace["logger"].exception.assert_not_called()


    def test_screen_removal_during_reattach_defers_qobject_deletion_until_reattach_returns(self) -> None:
        """
        驗證重掛途中移除螢幕時先清理輸出, 等重掛返回後才刪除 Qt 桌布物件
        """

        self.start_loading()
        self.namespace["set_windows_as_wallpaper"].reset_mock()
        self.tray.rebuild_screen_menus = mock.Mock()
        continue_stop = threading.Event()
        original_stop = self.player.stop
        errors = []

        def stop_waiting_for_removal():
            """
            等待螢幕移除回呼放行後停止播放器, 逾時則回報移除未被處理
            """

            if not continue_stop.wait(2):
                raise RuntimeError("screen removal was not processed")
            original_stop()

        def remove_during_stop():
            """
            放行停止工作並移除螢幕, 確認處理延後刪除事件後桌布仍未過早銷毀
            """

            continue_stop.set()
            self.tray.remove_screen(self.wallpaper.target_screen)
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if not isValid(self.wallpaper):
                errors.append("wallpaper was deleted while reattachment was still using it")

        QTimer.singleShot(0, remove_during_stop)
        with mock.patch.object(self.player, "stop", side_effect=stop_waiting_for_removal) as stop:
            self.wallpaper.reattach_to_desktop()

        self.assertEqual(errors, [])
        self.assertEqual(self.tray.wallpapers, {})
        self.assertEqual(self.player.state, "released")
        stop.assert_called_once_with()
        self.namespace["set_windows_as_wallpaper"].assert_not_called()
        self.wallpaper.create.assert_not_called()
        self.namespace["logger"].exception.assert_not_called()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(self.wallpaper))


class PreviewMenu(QMenu):
    """
    使用模擬縮圖工作的 Qt 選單
    """

    def __init__(
        self, title, parent, *, media_dir, thumbnail_dir, loading_image_path,
        preview_size=3, preview_delay_ms=1000, preview_reset_delay_ms=2000,
        preview_animation_duration_ms=140, thumbnail_thread=None
    ):
        """
        建立 Qt 選單, 保存共用縮圖工作物件並替換預覽重設操作
        """

        super().__init__(title, parent)
        self.thumbnail_thread = thumbnail_thread if thumbnail_thread is not None else mock.Mock()
        self.reset_preview = mock.Mock()
        self.preview_size = preview_size
        self.set_preview_size = mock.Mock(side_effect=lambda size: setattr(self, "preview_size", size))


class ScreenNotifications(QObject):
    """
    以真正 Qt 訊號模擬螢幕拔插
    """

    screenAdded = Signal(object)
    screenRemoved = Signal(object)


class ReattachNotifier(QObject):
    """
    以真正 Qt 訊號通知當次重掛結束
    """

    reattach_finished = Signal()


class ImportMenuTests(QtTestCase):
    """
    系統匣選單與背景匯入流程
    """

    def setUp(self) -> None:
        """
        建立選單, 匯入替身與執行緒同步事件
        """

        super().setUp()

        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = directory / "sample.mp4"
        self.source.write_bytes(b"sample contents")
        self.destination = directory / "imported.mp4"
        config_store = mock.Mock()
        config_store.load.return_value = {"maxBatchImportKiB": 30 * 1024 * 1024}

        self.namespace = {
            "QFileDialog": mock.Mock(),
            "QSystemTrayIcon": mock.Mock(),
            "MediaPreviewMenu": PreviewMenu,
            "ThumbnailThread": mock.Mock(side_effect=lambda *args: mock.Mock()),
            "ICON_PATH": ROOT / "icon" / "bb-video-wallpaper.ico",
            "MEDIA_DIR": directory / "media",
            "THUMBNAIL_DIR": directory / "thumbnails",
            "LOADING_IMAGE_PATH": ROOT / "icon" / "loading300.jpg",
            "SINGLE_IMPORT_DIR": directory / "media" / "_imports",
            "LANG": json.loads((ROOT / "lang" / "zh-TW.json").read_text(encoding="utf-8")),
            "SUPPORTED_MEDIA_EXTENSIONS": {".mp4", ".png"},
            "SUPPORTED_VIDEO_EXTENSIONS": {".mp4"},
            "config_store": config_store,
            "task_exists": lambda: False,
            "FILE": ROOT / "BB Video Wallpaper.exe",
            "create_task": mock.Mock(),
            "delete_task": mock.Mock(),
            "logger": mock.Mock(),
            "import_media": mock.Mock(return_value=self.destination),
            "get_media": mock.Mock(return_value=[]),
        }
        self.namespace["QSystemTrayIcon"].MessageIcon = QSystemTrayIcon.MessageIcon
        self.namespace["QFileDialog"].getOpenFileNames.return_value = ([str(self.source)], "")
        module = load_app_module()
        module.__dict__.update(self.namespace)
        self.namespace = module.__dict__

        self.screen = mock.Mock()
        self.wallpaper = wallpaper = self.make_wallpaper("screen-a", self.screen)
        self.reattach_notifier = ReattachNotifier()
        wallpaper.reattach_finished = self.reattach_notifier.reattach_finished
        self.namespace["Wallpaper"] = mock.Mock(return_value=wallpaper)
        self.screen_notifications = ScreenNotifications()
        application = mock.Mock()
        application.screens.return_value = [self.screen]
        application.screenAdded = self.screen_notifications.screenAdded
        application.screenRemoved = self.screen_notifications.screenRemoved
        self.tray = self.namespace["Tray"](application)
        self.menu = self.tray.tray.setContextMenu.call_args.args[0]
        self.tray.play_from_menu = mock.Mock()
        self.tray.refresh_media_menu = mock.Mock()
        # 匯入工作的開始與放行事件
        self.started = threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.dispose_tray)


    def dispose_tray(self) -> None:
        """
        結束匯入工作並釋放選單
        """

        # 放行仍在等待的匯入工作
        self.release.set()
        worker = self.tray.import_thread

        if worker is not None:
            worker.requestInterruption()
            worker.wait(5000)

        self.app.processEvents()
        self.tray.shutdown()
        self.menu.close()
        self.menu.deleteLater()


    def make_wallpaper(self, screen_id, screen):
        """
        建立指定螢幕的桌布替身, 預設為可播放與同步進度的影片
        """

        wallpaper = mock.Mock()
        wallpaper.screen_id = screen_id
        wallpaper.target_screen = screen
        wallpaper.current_media = self.source
        wallpaper.play_state = "play"
        wallpaper.is_exiting = False
        wallpaper.is_recreating = False
        wallpaper.playback_state = self.namespace["PlaybackState"]("play")
        wallpaper.is_image.return_value = False
        wallpaper.get_auto_pause_reason.return_value = None
        wallpaper.get_playback_time.return_value = 2000
        wallpaper.get_playback_length.return_value = 10000
        wallpaper.seek_time.return_value = True
        return wallpaper


    def wait_for_completion(self, worker):
        """
        等待匯入執行緒與完成訊號處理完畢
        """

        self.assertTrue(worker.wait(5000), "匯入工作未結束")
        # 處理佇列中的完成或錯誤訊號
        self.app.processEvents()
        self.assertIsNone(self.tray.import_thread)


    def blocked_import(self, source, import_dir, *, translate, progress, cancelled, expected_size):
        """
        暫停匯入工作, 由測試控制完成或取消
        """

        self.started.set()

        if not self.release.wait(5):
            raise TimeoutError("測試未釋放匯入工作")

        if cancelled():
            raise self.namespace["MediaImportCancelled"]()

        return self.destination


    def complete_import_during_reattach(self):
        """
        讓匯入工作結束, 確認結果仍在等待桌布重掛
        """

        self.namespace["get_media"].return_value = [self.source]
        self.wallpaper.is_recreating = True
        self.tray.add_media()
        worker = self.tray.import_thread
        self.assertTrue(worker.wait(5000))
        self.app.processEvents()
        self.assertIs(self.tray.import_thread, worker)
        self.tray.play_from_menu.assert_not_called()
        return worker


    def test_completed_import_waits_for_reattach_and_plays_only_once(self) -> None:
        """
        等待中的匯入遇到重複重掛通知也只套用一次
        """

        worker = self.complete_import_during_reattach()

        # 等待中的回呼若又遇上重掛, 繼續等下一次結束訊號
        self.reattach_notifier.reattach_finished.emit()
        self.app.processEvents()
        self.assertIs(self.tray.import_thread, worker)
        self.wallpaper.is_recreating = False
        self.reattach_notifier.reattach_finished.emit()
        self.reattach_notifier.reattach_finished.emit()
        self.app.processEvents()

        self.assertIsNone(self.tray.import_thread)
        self.tray.play_from_menu.assert_called_once_with(self.destination, "screen-a")
        self.tray.refresh_media_menu.assert_called_once_with()


    def test_stop_overrides_queued_import_after_reattach_and_allows_new_import(self) -> None:
        """
        驗證停止優先於已排入的重掛回呼, 保留匯入內容且新匯入仍可自動播放
        """

        self.complete_import_during_reattach()

        self.wallpaper.is_recreating = False
        self.reattach_notifier.reattach_finished.emit()
        self.tray.stop()
        self.app.processEvents()

        self.assertIsNone(self.tray.import_thread)
        self.wallpaper.manual_stop.assert_called_once_with()
        self.tray.play_from_menu.assert_not_called()
        self.tray.thumbnail_thread.request.assert_called_once_with(self.destination, preview_size=self.tray.preview_size)
        self.namespace["config_store"].remember_recent.assert_called_once_with(self.destination)
        self.tray.refresh_media_menu.assert_called_once_with()

        self.tray.add_media()
        self.wait_for_completion(self.tray.import_thread)
        self.tray.play_from_menu.assert_called_once_with(self.destination, "screen-a")


    def test_old_import_callbacks_cannot_complete_a_new_import(self) -> None:
        """
        驗證舊匯入留下的重掛回呼不會完成或清除新匯入工作
        """

        old_worker = self.complete_import_during_reattach()
        self.wallpaper.is_recreating = False
        self.tray.finish_import(old_worker)

        self.namespace["import_media"] = self.blocked_import
        self.tray.add_media()
        new_worker = self.tray.import_thread
        self.assertTrue(self.started.wait(5))
        self.reattach_notifier.reattach_finished.emit()
        self.app.processEvents()

        self.assertIs(self.tray.import_thread, new_worker)
        self.release.set()
        self.wait_for_completion(new_worker)


if __name__ == "__main__":
    unittest.main()
