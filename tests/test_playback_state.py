"""
測試圖片與影片切換, 桌布重掛及延遲播放事件之間的狀態
使用真正的 Qt 訊號佇列, 重掛計時器與模擬輸出, 不啟動桌布或修改使用者設定
"""

from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QRect, Qt, QTimer, Signal
from PySide6.QtTest import QSignalSpy, QTest
from shiboken6 import isValid


ROOT = Path(__file__).resolve().parents[1]
PLAYING_EVENT = object()


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from config import ConfigStore
from playback import PlaybackState
from app_loader import load_app_module


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


    def set_geometry(self, geometry: QRect) -> None:
        """
        更新螢幕位置與尺寸, 並發出幾何變更訊號
        """

        self._geometry = geometry
        self.geometryChanged.emit(geometry)


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

        self.events = mock.Mock(spec=["event_attach", "event_detach"])
        self.events.event_attach.side_effect = self.callbacks.__setitem__
        self.events.event_detach.side_effect = self.callbacks.pop

        self.video_set_crop_geometry = mock.Mock()
        self.set_hwnd = mock.Mock()


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

        self.callbacks[PLAYING_EVENT](None)


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


    def set_image(self, path: Path) -> None:
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


class PlaybackStateTests(unittest.TestCase):
    """
    桌布播放狀態, 重掛與資源釋放
    """

    @classmethod
    def setUpClass(cls) -> None:
        """
        取得或建立供播放狀態測試處理訊號與計時器的 Qt 核心應用程式
        """

        cls.app = QCoreApplication.instance() or QCoreApplication([])


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
        vlc.State.Playing = "playing"
        vlc.State.Paused = "paused"

        config_store = mock.Mock()
        config_store.load.return_value = {}

        application = mock.Mock()
        application.primaryScreen.return_value = HeadlessScreen(QRect(0, 0, 1920, 1080))

        self.namespace = {
            # Qt 類別與視窗替身
            "QApplication": application,
            "DesktopEventWindow": HeadlessDesktopEvents,
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
            "logger": mock.Mock(),
        }
        # 匯入時替換 QWidget 基底, 保留真正的 Qt 訊號與計時器
        with mock.patch("PySide6.QtWidgets.QWidget", HeadlessWidget):
            module = load_app_module()
        module.__dict__.update(self.namespace)
        self.namespace = module.__dict__


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
        self.tray.wallpapers = {self.wallpaper.screen_id: self.wallpaper}
        self.tray.is_exiting = False
        self.tray.waiting_for_screens = False
        self.tray.follow_controller = self.namespace["FollowController"](self.tray.wallpapers, config_store)
        self.tray.follow_sync_timer = mock.Mock()
        self.tray.follow_restore_timer = mock.Mock()
        self.tray.import_thread = None
        self.tray.import_screen_id = self.wallpaper.screen_id
        self.tray.import_applies_to_all = False
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
                Qt.ConnectionType.QueuedConnection,
            )


    def add_following_wallpaper(self, screen_id="screen-b"):
        """
        建立並登記獨立桌布, 接上跟隨控制器的事件處理
        """

        wallpaper = self.create_independent_wallpaper(screen_id)
        self.tray.wallpapers[screen_id] = wallpaper
        self.connect_follow_events(wallpaper)
        return wallpaper


    def dispose_wallpaper(self, wallpaper=None) -> None:
        """
        停止重掛排程並釋放桌布與監聽物件
        """

        wallpaper = self.wallpaper if wallpaper is None else wallpaper

        if not isValid(wallpaper):
            return

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


    def start_playing(self) -> None:
        """
        開始載入影片並送出 Playing 事件, 處理 Qt 佇列至完成播放回呼
        """

        self.start_loading()
        self.player.finish_loading()
        self.app.processEvents()


    def create_independent_wallpaper(self, screen_id: str):
        """
        建立指定螢幕識別的獨立播放器與圖片輸出, 停止輪詢計時器並登記清理
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


    def test_follow_join_waits_for_playing_before_seeking_and_preserves_manual_pause(self):
        """
        驗證跟隨者等待 Playing 事件後才對齊來源進度, 並保留手動暫停
        """

        self.start_playing()
        self.player.position = 6500
        self.tray.pause(self.wallpaper.screen_id)
        follower = self.add_following_wallpaper()
        player = follower.instance_vlc_player
        self.assertTrue(self.tray.follow_controller.follow(follower.screen_id, self.wallpaper.screen_id))
        self.assertEqual(follower.play_state, "pause")
        self.assertEqual(player.state, "opening")
        self.assertNotEqual(player.position, 6500)

        player.finish_loading()
        self.app.processEvents()
        self.assertEqual((player.state, player.position), ("paused", 6500))
        self.assertEqual(follower.playback_state, PlaybackState("pause"))


    def test_follow_recovery_preserves_pending_position_then_realigns_followers(self):
        """
        驗證來源重掛期間保留待恢復進度, 就緒後才重新對齊跟隨者
        """

        self.start_loading()
        self.player.finish_loading()
        follower = self.add_following_wallpaper()
        self.tray.follow_controller.follow(follower.screen_id, self.wallpaper.screen_id)
        follower.instance_vlc_player.finish_loading()
        self.app.processEvents()
        self.player.position = 7500
        follower.instance_vlc_player.position = 1000
        self.wallpaper.reattach_to_desktop()
        self.app.processEvents()
        self.assertEqual(self.wallpaper.pending_reattach_time, 7500)
        self.assertEqual(follower.instance_vlc_player.position, 1000)

        self.player.finish_loading()
        self.app.processEvents()
        self.assertIsNone(self.wallpaper.pending_reattach_time)
        self.assertEqual(follower.instance_vlc_player.position, 7500)


    def test_follow_new_local_media_ignores_queued_source_ready_events(self):
        """
        驗證跟隨者改選本機圖片後解除跟隨, 並忽略已排入佇列的來源就緒訊號
        """

        self.start_loading()
        self.player.finish_loading()
        follower = self.add_following_wallpaper()
        self.tray.follow_controller.follow(follower.screen_id, self.wallpaper.screen_id)
        self.player.finish_loading()
        self.tray.play_from_menu(self.image_path, follower.screen_id)
        self.app.processEvents()
        self.assertEqual(self.tray.follow_controller.graph.sources, {})
        self.assertEqual(follower.current_media, self.image_path)
        self.assertTrue(follower.instance_image.visible)


    def test_follow_ignores_vlc_progress_until_ready_and_when_not_seekable(self):
        """
        驗證載入中或等待恢復進度時不提供同步位置, 無法調整進度時拒絕定位
        """

        self.start_loading()
        self.assertIsNone(self.wallpaper.get_playback_time())
        self.assertFalse(self.wallpaper.seek_time(4000))
        self.player.finish_loading()
        self.app.processEvents()
        with mock.patch.object(self.player, "is_seekable", return_value=False):
            self.assertFalse(self.wallpaper.seek_time(4000))
        self.assertTrue(self.wallpaper.seek_time(4000))
        self.wallpaper.pending_reattach_time = 6000
        self.assertIsNone(self.wallpaper.get_playback_time())
        self.assertFalse(self.wallpaper.seek_time(2000))
        self.assertEqual(self.wallpaper.pending_reattach_time, 6000)


    def test_screens_restore_their_own_wallpaper_after_shared_history_is_cleared(self):
        """
        驗證清除共用紀錄後各螢幕仍還原自己的桌布, 底層載入與重掛不改寫設定
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
            tray.wallpapers = {"screen-a": screen_a, "screen-b": screen_b}
            tray.clear_recent_media()
            self.assertEqual(store.load()["recentVideo"], [])
            original_config = store.config_path.read_bytes()
            screen_a.set_play_media(second)
            screen_b.set_play_media(first)
            self.assertEqual(store.config_path.read_bytes(), original_config)
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
        驗證新螢幕沿用舊最近紀錄並保存獨立選擇, 不更新共用歷史
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
        驗證已保存的媒體遺失時暫用可用桌布, 不覆寫原本的螢幕選擇
        """

        store = self.namespace["config_store"]
        store.load.return_value = {"screenWallpapers": {"screen-a": "missing.mp4"}}
        self.namespace["get_media"].return_value = [self.media_path]
        wallpaper = self.create_independent_wallpaper("screen-a")

        self.assertEqual(wallpaper.current_media, self.media_path)
        store.remember_wallpaper.assert_not_called()


    def test_playing_callback_from_worker_pauses_on_qt_thread(self):
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


    def test_latest_play_or_stop_wins_over_delayed_playing_event(self):
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


    def test_queued_playing_event_after_exit_does_not_touch_player(self):
        """
        驗證退出後佇列中的 Playing 事件不再操作已釋放的播放器
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


    def test_video_reattach_stops_old_output_before_destroying_and_rebinding(self):
        """
        驗證重掛先停止舊輸出再銷毀與綁定視窗, 並保留暫停與進度且不重複排程
        """

        self.start_playing()
        self.tray.pause()
        self.player.position = 4321
        self.wallpaper.attach_to_desktop.reset_mock()
        self.wallpaper.attach_to_desktop.side_effect = lambda: self.attach_to_desktop(self.wallpaper)
        original_stop = self.player.stop

        def stop_and_clear_time():
            """
            確認重建時由背景執行緒停止輸出, 並模擬停止後進度失效
            """

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
            self.wallpaper.schedule_reattach(0)
            self.wallpaper.reattach_to_desktop()
            self.app.processEvents()
            self.assertFalse(self.wallpaper.reattach_timer.isActive())
            self.wallpaper.attach_to_desktop.assert_called_once_with()

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
        驗證重掛停止輸出時仍處理 Qt 事件與 VLC 喚醒, 並忽略延遲暫停
        """

        self.start_loading()
        self.tray.pause()
        self.wallpaper.is_recreating = True
        processed = threading.Event()
        woken = threading.Event()
        self.namespace["wake_vlc_video_windows"].side_effect = woken.set

        def stop_waiting_for_ui():
            """
            等待 Qt 事件與 VLC 視窗喚醒, 未及時處理則回報事件迴圈阻塞
            """

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


    def test_reattach_seeks_after_playing_when_initial_seek_is_ignored(self):
        """
        驗證重掛時未被接受的進度設定會在 Playing 事件後補套
        """

        self.start_playing()
        self.tray.pause()
        self.player.position = 4321

        def seek_when_ready(position):
            """
            只在播放器已播放或暫停時接受進度設定
            """

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
        驗證停止或切換媒體會取消待恢復的重掛進度
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


    def test_failed_reattach_retries_preserve_video_position(self):
        """
        驗證一次或兩次掛載失敗後重試仍保留進度與原播放狀態
        """

        for state in ("play", "pause"):
            for failures in (1, 2):
                with self.subTest(state=state, failures=failures):
                    self.start_playing()
                    getattr(self.tray, state)()
                    self.player.position = 4321
                    self.wallpaper.attach_to_desktop.side_effect = [
                        *[RuntimeError("attachment failed") for _ in range(failures)], None,
                    ]

                    self.wallpaper.reattach_to_desktop()
                    for _ in range(failures):
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
                    self.assertEqual(self.wallpaper.play_state, state)
                    self.assertEqual(self.player.state, "paused" if state == "pause" else "playing")
                    self.assertFalse(self.wallpaper.reattach_timer.isActive())
                    self.assertEqual(self.wallpaper.reattach_retry_count, 0)


    def test_failed_reattach_position_is_cancelled_by_stop_or_media_selection(self):
        """
        驗證掛載失敗等待重試時, 停止或重新選擇媒體會捨棄舊進度
        """

        for action in ("stop", "same_video", "new_video", "image"):
            with self.subTest(action=action):
                self.start_playing()
                self.player.position = 4321
                self.wallpaper.attach_to_desktop.side_effect = [RuntimeError("attachment failed"), None]
                self.wallpaper.reattach_to_desktop()

                if action == "stop":
                    self.tray.stop()
                else:
                    path = {
                        "same_video": self.media_path,
                        "new_video": self.media_path.with_name("new.mp4"),
                        "image": self.image_path,
                    }[action]
                    self.wallpaper.select_media(path)

                with mock.patch.object(self.player, "set_time") as seek:
                    self.wallpaper.reattach_timer.start(0)
                    self.app.processEvents()
                    self.player.finish_loading()
                    self.app.processEvents()

                seek.assert_not_called()
                self.assertIsNone(self.wallpaper.pending_reattach_time)
                self.assertEqual(self.wallpaper.play_state, "stop" if action == "stop" else "play")


    def test_failed_background_stop_retries_with_saved_position(self):
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


    def test_reattach_before_delayed_seek_preserves_original_position(self):
        """
        驗證延遲進度尚未補套前再次重掛並失敗, 重試後仍恢復最初位置
        """

        self.start_playing()
        self.player.position = 4321

        def seek_when_ready(position):
            """
            忽略載入期間的進度設定, 只在已播放或暫停時保存位置
            """

            if self.player.state in {"playing", "paused"}:
                self.player.position = position

        with mock.patch.object(self.player, "set_time", side_effect=seek_when_ready):
            self.wallpaper.reattach_to_desktop()
            self.assertEqual(self.player.position, 0)
            self.wallpaper.attach_to_desktop.side_effect = [RuntimeError("attachment failed"), None]
            self.wallpaper.reattach_to_desktop()
            self.wallpaper.reattach_timer.start(0)
            self.app.processEvents()
            self.player.finish_loading()
            self.app.processEvents()

        self.assertEqual(self.player.position, 4321)
        self.assertIsNone(self.wallpaper.pending_reattach_time)


    def test_import_completion_waits_for_reattach_and_cancels_old_position(self):
        """
        驗證背景停止期間完成的匯入延後至重掛結束套用, 重試也不將舊進度套到匯入媒體
        """

        for fail_attach in (False, True):
            for kind in ("video", "same_video", "image"):
                with self.subTest(fail_attach=fail_attach, kind=kind):
                    self.start_playing()
                    self.player.position = 4321
                    destination = {
                        "video": self.media_path.with_name("imported.mp4"),
                        "same_video": self.media_path,
                        "image": self.image_path,
                    }[kind]
                    worker = mock.Mock(destination=destination, cancelled=False)
                    worker.isRunning.return_value = False
                    self.tray.import_thread = worker
                    self.tray.add_media_action = mock.Mock()
                    self.tray.cancel_import_action = mock.Mock()
                    self.tray.media_menu = mock.Mock()
                    self.tray.thumbnail_thread = mock.Mock()
                    self.tray.refresh_media_menu = mock.Mock()
                    self.tray.tray = mock.Mock()
                    self.namespace["tr"] = lambda key: key
                    self.wallpaper.attach_to_desktop.side_effect = (
                        [RuntimeError("attachment failed"), None] if fail_attach else None
                    )
                    callback_finished = threading.Event()
                    errors = []
                    main_thread = threading.get_ident()
                    original_stop = self.player.stop

                    def stop_waiting_for_import():
                        """
                        背景停止前等待匯入回呼完成, 記錄主執行緒過早停止的錯誤
                        """

                        if threading.get_ident() != main_thread:
                            if not callback_finished.wait(2):
                                raise RuntimeError("import completion was not processed")
                        elif not callback_finished.is_set():
                            errors.append("player stopped on main thread while background stop was pending")
                        original_stop()

                    def finish_import_during_stop():
                        """
                        在停止期間完成匯入回呼, 確認結果仍延後套用並回報完成或錯誤
                        """

                        try:
                            self.assertTrue(self.wallpaper.is_recreating)
                            self.tray.finish_import()
                            self.assertIs(self.tray.import_thread, worker)
                            self.assertEqual(self.wallpaper.current_media, self.media_path)
                            worker.deleteLater.assert_not_called()
                        except Exception as error:
                            errors.append(str(error))
                        finally:
                            callback_finished.set()

                    QTimer.singleShot(0, finish_import_during_stop)
                    with mock.patch.object(self.player, "stop", side_effect=stop_waiting_for_import):
                        self.wallpaper.reattach_to_desktop()
                        self.app.processEvents()

                    self.assertEqual(errors, [])
                    self.assertIsNone(self.tray.import_thread)
                    worker.deleteLater.assert_called_once_with()
                    self.assertEqual(self.wallpaper.current_media, destination)
                    self.tray.thumbnail_thread.request.assert_called_once_with(destination)
                    self.tray.refresh_media_menu.assert_called_once_with()
                    if fail_attach:
                        self.wallpaper.reattach_timer.start(0)
                        self.app.processEvents()
                    self.player.finish_loading()
                    self.app.processEvents()
                    self.assertEqual(self.wallpaper.play_state, "play")
                    self.assertIsNone(self.wallpaper.pending_reattach_time)
                    if kind == "image":
                        self.assertTrue(self.image_output.visible)
                    else:
                        self.assertEqual(self.player.position, 0)


    def test_auto_pause_during_loading_applies_and_resumes(self):
        """
        驗證載入期間的螢幕關閉, 電池或全螢幕條件會暫停影片, 條件解除後恢復
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
                self.assertEqual(self.wallpaper.playback_state, PlaybackState(
                    "play", ((self.wallpaper.screen_id, reason),)
                ))
                self.player.finish_loading()
                self.app.processEvents()
                self.assertEqual(self.player.state, "paused")

                self.wallpaper.screen_off = False
                self.namespace["is_on_battery"].return_value = False
                self.namespace["is_any_fullscreen"].return_value = False
                self.wallpaper.check_auto_pause()

                self.assertEqual(self.wallpaper.play_state, "play")
                self.assertEqual(self.player.state, "playing")
                self.assertEqual(self.wallpaper.playback_state, PlaybackState("play"))


    def test_auto_paused_recovery_keeps_play_intent_and_resumes_when_condition_clears(self):
        """
        驗證自動暫停的影片重掛後保留播放意圖與進度, 條件解除才恢復播放
        """

        self.start_playing()
        self.player.position = 4321
        self.wallpaper.screen_off = True
        self.wallpaper.check_auto_pause()
        state = self.wallpaper.playback_state

        self.wallpaper.reattach_to_desktop()
        self.assertEqual(self.wallpaper.playback_state, state)
        self.player.finish_loading()
        self.app.processEvents()
        self.assertEqual((self.player.state, self.player.position), ("paused", 4321))

        self.wallpaper.screen_off = False
        self.wallpaper.check_auto_pause()
        self.assertEqual(self.wallpaper.playback_state, PlaybackState("play"))
        self.assertEqual(self.player.state, "playing")


    def test_paused_video_switches_to_visible_image_and_ignores_old_event(self):
        """
        驗證暫停影片切換成圖片後立即顯示, 舊 Playing 事件不再套用暫停
        """

        self.start_loading()
        self.tray.pause()
        self.player.finish_loading()
        pause_calls = list(self.player.pause_calls)

        self.wallpaper.set_play_media(self.image_path)
        self.app.processEvents()

        self.assertEqual(self.wallpaper.play_state, "play")
        self.assertEqual(self.wallpaper.playback_state, PlaybackState("play"))
        self.assertTrue(self.image_output.visible)
        self.assertEqual(self.player.state, "stopped")
        self.assertEqual(self.player.pause_calls, pause_calls)

        self.player.finish_loading()
        self.app.processEvents()
        self.assertEqual(self.player.pause_calls, pause_calls)


    def test_bad_image_preserves_previous_output_state_and_history(self):
        """
        驗證圖片載入失敗時保留原媒體, 播放與顯示狀態, 不寫入設定
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


    def test_destroy_notification_skips_reattach_while_recreating_or_exiting(self):
        """
        驗證一般銷毀通知會排定重掛, 重建或退出期間則略過
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
        驗證螢幕變更與視窗銷毀通知合併為同一次重掛排程
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
        驗證掛載失敗最多重試兩次, 合併新通知且等待桌面不消耗重試次數
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

                if attempt == 0:
                    # 等待桌面就緒不消耗剩下的掛載重試
                    self.namespace["check_desktop_ready"].return_value = False
                    for _ in range(3):
                        timer.start(0)
                        self.app.processEvents()
                        self.assertTrue(timer.isActive())
                        self.assertEqual(self.wallpaper.reattach_retry_count, 1)
                        self.wallpaper.attach_to_desktop.assert_called_once_with()
                    self.namespace["check_desktop_ready"].return_value = True

        self.assertFalse(timer.isActive())
        self.namespace["logger"].error.assert_called_once()


    def test_taskbar_notification_recovers_without_destroy_event(self):
        """
        驗證工作列重建通知可重設重試次數並排定一次重掛, 不需視窗銷毀事件
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


    def test_exit_cancels_desktop_wait_and_ignores_queued_or_new_reattach_requests(self):
        """
        驗證退出會取消桌面等待並釋放輸出, 忽略佇列中與新收到的重掛要求
        """

        self.wallpaper.attach_to_desktop.reset_mock()
        self.namespace["check_desktop_ready"].return_value = False
        self.wallpaper.reattach_to_desktop()
        self.assertTrue(self.wallpaper.reattach_timer.isActive())
        listener = self.wallpaper.desktop_events
        listener.taskbar_created.emit()

        self.tray.exit()
        self.wallpaper.reattach_to_desktop()
        self.wallpaper.on_screen_changed(mock.sentinel.geometry)
        self.app.processEvents()

        listener.close.assert_called_once_with()
        self.assertIsNone(self.wallpaper.desktop_events)
        self.assertFalse(self.wallpaper.reattach_timer.isActive())
        self.assertEqual(self.player.state, "released")
        self.namespace["check_desktop_ready"].assert_called_once_with()
        self.wallpaper.attach_to_desktop.assert_not_called()
        self.wallpaper.destroy.assert_called_once_with(True, True)
        self.namespace["logger"].exception.assert_not_called()


    def test_unready_desktop_preserves_output_until_ready(self):
        """
        驗證等待桌面就緒時保留既有輸出與重試次數, 就緒後恢復暫停與進度
        """

        self.start_playing()
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


    def test_unregister_clears_handle_even_if_api_fails(self):
        """
        驗證取消電源通知失敗時仍清除保存的 handle 並記錄例外
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
        驗證圖片重掛只解除與重新綁定視窗, 保留顯示狀態且不重讀圖片或操作影片
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
        驗證重複關閉只清理一次並阻止後續操作, 不退出共用 Qt 應用程式
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
            self.wallpaper.manual_play()
            self.wallpaper.manual_pause()
            self.wallpaper.manual_stop()
            self.wallpaper.apply_playback_state(PlaybackState("play"), force=True)
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
        驗證監聽視窗, 停止輸出, 圖片或播放器釋放失敗時仍清理其餘資源
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
        self.namespace["logger"].exception.assert_called()


    def test_shutdown_during_reattach_reuses_stop_and_prevents_rebinding(self):
        """
        驗證重掛期間關閉會共用正在進行的停止工作, 不再重建或綁定視窗
        """

        self.start_loading()
        self.player.position = 4321
        self.wallpaper.attach_to_desktop.reset_mock()
        continue_stop = threading.Event()
        original_stop = self.player.stop

        def stop_waiting_for_shutdown():
            """
            等待關閉回呼放行後停止播放器, 逾時則回報關閉未被處理
            """

            if not continue_stop.wait(2):
                raise RuntimeError("shutdown was not processed")
            original_stop()

        def shutdown_during_stop():
            """
            放行背景停止工作並在重掛途中關閉桌布
            """

            continue_stop.set()
            self.wallpaper.shutdown()

        QTimer.singleShot(0, shutdown_during_stop)

        with mock.patch.object(self.player, "stop", side_effect=stop_waiting_for_shutdown) as stop:
            self.wallpaper.reattach_to_desktop()

        stop.assert_called_once_with()
        self.assertTrue(self.wallpaper.is_exiting)
        self.assertFalse(self.wallpaper.is_recreating)
        self.assertIsNone(self.wallpaper._reattach_position)
        self.assertEqual(self.player.state, "released")
        self.wallpaper.attach_to_desktop.assert_not_called()
        self.wallpaper.create.assert_not_called()
        self.wallpaper.destroy.assert_called_once_with(True, True)
        self.namespace["logger"].exception.assert_not_called()


    def test_screen_removal_during_reattach_defers_qobject_deletion_until_reattach_returns(self):
        """
        驗證重掛途中移除螢幕時先清理輸出, 等重掛返回後才刪除 Qt 桌布物件
        """

        self.start_loading()
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
        self.namespace["logger"].exception.assert_not_called()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(self.wallpaper))


if __name__ == "__main__":
    unittest.main()
