"""
測試匯入的非同步競態, 選單設定, 重啟與主程式匯入邊界
使用真正的 Qt 選單, 訊號佇列與工作執行緒, 不顯示系統匣或啟動桌布
"""

from pathlib import Path
import os
import sys
from types import SimpleNamespace
import threading
import time
import unittest
from unittest import mock

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon


ROOT = Path(__file__).resolve().parents[1]


# 直接執行測試檔時, 將專案根目錄加入模組搜尋路徑
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from app_loader import QtTestCase, load_app_module
from config import ConfigStore


class PreviewMenu(QMenu):
    """
    使用模擬縮圖工作的 Qt 選單
    """

    def __init__(
        self, title, parent, *, media_dir, thumbnail_dir, loading_image_path,
        preview_size=3, thumbnail_thread=None
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

        self.source = ROOT / "sample.mp4"
        self.destination = ROOT / "imported.mp4"
        config_store = mock.Mock()
        config_store.load.return_value = {}

        self.namespace = {
            "QFileDialog": mock.Mock(),
            "QSystemTrayIcon": mock.Mock(),
            "MediaPreviewMenu": PreviewMenu,
            "ThumbnailThread": mock.Mock(side_effect=lambda *args: mock.Mock()),
            "ICON_PATH": ROOT / "icon" / "bb-video-wallpaper.ico",
            "MEDIA_DIR": ROOT / "test-media",
            "THUMBNAIL_DIR": ROOT / "test-thumbnails",
            "LOADING_IMAGE_PATH": ROOT / "icon" / "loading300.jpg",
            "SINGLE_IMPORT_DIR": ROOT / "video" / "_imports",
            "LANG": {"importing_media": "{percent}%"},
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
        self.namespace["QFileDialog"].getOpenFileName.return_value = (str(self.source), "")
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


    def add_screen(self, screen_id):
        """
        建立桌布替身並發出螢幕新增訊號, 回傳新增的桌布
        """

        screen = mock.Mock()
        wallpaper = self.make_wallpaper(screen_id, screen)
        self.namespace["Wallpaper"].return_value = wallpaper
        self.tray.app.screens.return_value.append(screen)
        self.screen_notifications.screenAdded.emit(screen)
        return wallpaper


    def wait_for_completion(self, worker):
        """
        等待匯入執行緒與完成訊號處理完畢
        """

        self.assertTrue(worker.wait(5000), "匯入工作未結束")
        # 處理佇列中的完成或錯誤訊號
        self.app.processEvents()
        self.assertIsNone(self.tray.import_thread)


    def blocked_import(self, source, import_dir, *, translate, progress, cancelled):
        """
        暫停匯入工作, 由測試控制完成或取消
        """

        self.started.set()

        if not self.release.wait(5):
            raise TimeoutError("測試未釋放匯入工作")

        if cancelled():
            raise self.namespace["MediaImportCancelled"]()

        return self.destination


    def test_exit_delegates_cleanup_and_quits_even_if_shutdown_fails(self) -> None:
        """
        驗證單一桌布清理失敗仍會清理其他桌布並退出, 重複退出不重複清理
        """

        other = self.add_screen("screen-b")
        self.wallpaper.shutdown.side_effect = RuntimeError("shutdown failed")

        self.tray.exit()
        self.tray.app.quit.assert_called_once_with()
        self.tray.exit()
        self.wallpaper.shutdown.assert_called_once_with()
        other.shutdown.assert_called_once_with()
        self.tray.thumbnail_thread.stop.assert_called_once_with()


    def test_auto_pause_failures_restore_checks_and_retry_updates_all_screens(self) -> None:
        """
        三種自動暫停選項在讀取或保存失敗時還原勾選, 重試成功後更新所有螢幕
        """

        other = self.add_screen("screen-b")
        wallpapers = (self.wallpaper, other)
        store = self.namespace["config_store"]
        config = {**self.namespace["CONFIG_DEFAULTS"], "futureOption": 9}
        store.load.side_effect = lambda: config.copy()
        store.save.return_value = True

        for text_key, config_key, attribute in (
            ("auto_pause_fullscreen", "autoPauseIfFullscreen", "auto_pause_if_fullscreen"),
            ("auto_pause_screen_off", "autoPauseIfScreenOff", "auto_pause_if_screen_off"),
            ("auto_pause_battery", "autoPauseIfOnBattery", "auto_pause_if_on_battery"),
        ):
            action = next(
                action for action in self.tray.options_menu.actions()
                if action.text() == self.namespace["tr"](text_key)
            )
            previous = action.isChecked()
            for wallpaper in wallpapers:
                setattr(wallpaper, attribute, previous)

            for failure, patcher in (
                ("read", mock.patch.object(store, "load", side_effect=PermissionError)),
                ("save", mock.patch.object(store, "save", return_value=False)),
            ):
                with self.subTest(option=config_key, failure=failure), patcher:
                    action.trigger()
                    self.assertEqual(action.isChecked(), previous)
                    self.assertEqual([getattr(w, attribute) for w in wallpapers], [previous] * 2)
                    self.assertEqual(config[config_key], previous)

            store.save.reset_mock()
            action.trigger()

            store.save.assert_called_once_with({**config, config_key: not previous})
            self.assertEqual(action.isChecked(), not previous)
            self.assertEqual([getattr(w, attribute) for w in wallpapers], [not previous] * 2)
            config[config_key] = not previous


    def test_preview_size_save_failure_keeps_menus_and_selection(self) -> None:
        """
        保存失敗時還原勾選且不變更各選單, 重試成功後共用新尺寸
        """

        self.add_screen("screen-b")
        menus = (self.tray.media_menu, *self.tray.screen_menus.values())
        store = self.namespace["config_store"]
        store.load.return_value = {"previewSize": 3, "futureOption": 9}
        store.save.return_value = False

        self.tray.preview_size_actions[4].trigger()

        store.save.assert_called_once_with({"previewSize": 4, "futureOption": 9})
        self.assertEqual(self.tray.preview_size, 3)
        self.assertTrue(self.tray.preview_size_actions[3].isChecked())
        self.assertFalse(self.tray.preview_size_actions[4].isChecked())
        for menu in menus:
            menu.set_preview_size.assert_not_called()

        store.save.return_value = True
        self.tray.preview_size_actions[4].trigger()

        self.assertEqual(self.tray.preview_size, 4)
        for menu in menus:
            menu.set_preview_size.assert_called_once_with(4)


    def test_language_restart_ignores_queued_changes_during_shutdown(self) -> None:
        """
        清理期間處理延遲選單訊號時, 不覆寫語言或轉為還原預設
        """

        store = self.namespace["config_store"]
        store.load.return_value = {"language": "zh-TW", "futureOption": 9}
        store.save.return_value = True
        action = QAction(self.menu)
        action.triggered.connect(
            lambda checked=False: self.tray.set_language("ko-KR"),
            Qt.ConnectionType.QueuedConnection
        )
        action.triggered.connect(
            lambda checked=False: self.tray.reset_defaults(),
            Qt.ConnectionType.QueuedConnection
        )
        action.trigger()
        self.wallpaper.shutdown.side_effect = self.app.processEvents

        self.tray.set_language("en-US")

        store.save.assert_called_once_with({"language": "en-US", "futureOption": 9})
        self.assertEqual(self.tray.current_language_code, "en-US")
        self.assertTrue(self.tray.restart_requested)
        self.assertFalse(self.tray.reset_requested)
        self.tray.app.quit.assert_called_once_with()
        self.wallpaper.shutdown.assert_called_once_with()


    def test_reset_request_defers_writes_and_ignores_queued_changes(self) -> None:
        """
        還原要求只先清理, 延遲語言與還原訊號不寫入設定或重複退出
        """

        action = QAction(self.menu)
        action.triggered.connect(
            lambda checked=False: self.tray.set_language("ko-KR"),
            Qt.ConnectionType.QueuedConnection
        )
        action.triggered.connect(
            lambda checked=False: self.tray.reset_defaults(),
            Qt.ConnectionType.QueuedConnection
        )
        action.trigger()
        self.wallpaper.shutdown.side_effect = self.app.processEvents

        self.tray.reset_defaults()

        self.assertTrue(self.tray.restart_requested)
        self.assertTrue(self.tray.reset_requested)
        self.assertEqual(self.tray.current_language_code, "zh-TW")
        self.namespace["config_store"].save.assert_not_called()
        self.namespace["delete_task"].assert_not_called()
        self.tray.app.quit.assert_called_once_with()
        self.wallpaper.shutdown.assert_called_once_with()


    def test_cancel_ignores_late_progress_and_allows_retry_on_main_thread(self) -> None:
        """
        驗證背景進度由主執行緒更新, 取消後忽略延遲進度且可重新匯入
        """

        updated_on = []
        update_progress = self.tray.update_import_progress

        def record_progress(percent):
            """
            記錄進度回呼的執行緒, 再呼叫原本的選單更新方法
            """

            updated_on.append(threading.get_ident())
            update_progress(percent)

        def copy_with_progress(source, import_dir, *, translate, progress, cancelled):
            """
            回報匯入進度後等待測試放行匯入工作
            """

            progress(42)
            return self.blocked_import(source, import_dir, translate=translate, progress=progress, cancelled=cancelled)

        self.tray.update_import_progress = record_progress
        self.namespace["import_media"] = copy_with_progress
        self.tray.add_media()
        worker = self.tray.import_thread
        self.assertTrue(self.started.wait(5))
        self.app.processEvents()
        self.assertEqual(updated_on, [threading.get_ident()])
        self.assertIn("42%", self.tray.add_media_action.text())

        self.tray.cancel_import_action.trigger()
        self.assertTrue(worker.isInterruptionRequested())
        progress_text = self.tray.add_media_action.text()
        self.tray.update_import_progress(80)
        self.assertEqual(self.tray.add_media_action.text(), progress_text)
        self.release.set()
        self.wait_for_completion(worker)
        self.tray.play_from_menu.assert_not_called()
        self.tray.thumbnail_thread.request.assert_not_called()
        self.tray.tray.showMessage.assert_not_called()
        self.namespace["logger"].exception.assert_not_called()

        # 取消後可以開始下一次匯入, 取消按鈕也會重新啟用
        self.namespace["import_media"] = mock.Mock(return_value=self.destination)
        self.tray.add_media()
        self.wait_for_completion(self.tray.import_thread)
        self.tray.play_from_menu.assert_called_once_with(self.destination, "screen-a")


    def test_completed_import_waits_for_reattach_and_plays_only_once(self) -> None:
        """
        驗證匯入結果等待重掛結束, 重複完成訊號也只套用一次
        """

        self.namespace["get_media"].return_value = [self.source]
        self.wallpaper.is_recreating = True
        self.tray.add_media()
        worker = self.tray.import_thread
        self.assertTrue(worker.wait(5000))
        self.app.processEvents()
        self.assertIs(self.tray.import_thread, worker)
        self.tray.play_from_menu.assert_not_called()

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

        self.namespace["get_media"].return_value = [self.source]
        self.wallpaper.is_recreating = True
        self.tray.add_media()
        worker = self.tray.import_thread
        self.assertTrue(worker.wait(5000))
        self.app.processEvents()
        self.assertIs(self.tray.import_thread, worker)

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


    def test_stopping_import_target_also_excludes_followers_but_keeps_other_screens(self) -> None:
        """
        驗證等待重掛時停止來源與跟隨者, 只取消該群組的自動套用且不影響下次匯入
        """

        self.add_screen("screen-b")
        self.add_screen("screen-c")
        self.tray.follow_controller.graph.follow("screen-b", "screen-a")
        self.wallpaper.is_recreating = True
        self.tray.add_media()
        worker = self.tray.import_thread
        self.assertTrue(worker.wait(5000))
        self.app.processEvents()
        self.assertIs(self.tray.import_thread, worker)

        self.tray.stop("screen-a")
        self.wallpaper.is_recreating = False
        self.reattach_notifier.reattach_finished.emit()
        self.app.processEvents()

        self.assertIsNone(self.tray.import_thread)
        self.wallpaper.manual_stop.assert_called_once_with()
        self.tray.play_from_menu.assert_called_once_with(self.destination, "screen-c")

        self.tray.play_from_menu.reset_mock()
        self.tray.add_media()
        self.wait_for_completion(self.tray.import_thread)
        self.assertEqual(self.tray.play_from_menu.call_args_list, [
            mock.call(self.destination, screen_id) for screen_id in ("screen-a", "screen-b", "screen-c")
        ])


    def test_quitting_discards_import_deferred_during_reattach(self) -> None:
        """
        驗證退出時捨棄等待重掛的匯入結果, 不播放或請求縮圖
        """

        self.wallpaper.is_recreating = True
        self.tray.add_media()
        worker = self.tray.import_thread
        self.assertTrue(worker.wait(5000))
        self.app.processEvents()
        self.assertIs(self.tray.import_thread, worker)

        self.tray.is_exiting = True
        self.wallpaper.is_exiting = True
        self.wallpaper.is_recreating = False
        self.reattach_notifier.reattach_finished.emit()
        self.app.processEvents()

        self.assertIsNone(self.tray.import_thread)
        self.tray.play_from_menu.assert_not_called()
        self.tray.thumbnail_thread.request.assert_not_called()
        self.tray.tray.showMessage.assert_not_called()


    def test_quitting_requests_cancellation_before_waiting(self) -> None:
        """
        驗證退出等待匯入前先要求取消, 取消完成後不播放或顯示錯誤
        """

        def wait_for_cancel(source, import_dir, *, translate, progress, cancelled):
            """
            等待匯入取消要求並拋出取消例外, 未及時收到要求則回報逾時
            """

            self.started.set()
            deadline = time.monotonic() + 5

            while not cancelled():
                if time.monotonic() >= deadline:
                    raise TimeoutError("退出時沒有取消匯入")
                self.release.wait(0.01)

            raise self.namespace["MediaImportCancelled"]()

        self.namespace["import_media"] = wait_for_cancel
        self.tray.add_media()
        worker = self.tray.import_thread
        self.assertTrue(self.started.wait(5))

        self.tray.is_exiting = True
        self.tray.wait_for_import()
        self.wait_for_completion(worker)

        self.tray.play_from_menu.assert_not_called()
        self.tray.tray.showMessage.assert_not_called()
        self.namespace["logger"].exception.assert_not_called()


    def test_follow_startup_waits_for_screens_and_checks_offline_followers_on_arrival(self) -> None:
        """
        驗證啟動時保留離線跟隨者, 稍後加入時才還原或清除跟隨
        """

        saved = {"screen-b": "screen-a"}
        for source_present, follower_present, expected in (
            (True, True, saved), (False, True, {}), (True, False, saved), (False, False, saved),
        ):
            with self.subTest(source_present=source_present, follower_present=follower_present):
                config_store = self.namespace["config_store"]
                config_store.load.return_value = {"screenFollows": saved.copy()}
                screen_a, screen_b = mock.Mock(), mock.Mock()
                screens = ([screen_b] if follower_present else []) + ([screen_a] if source_present else [])
                wallpapers = [self.make_wallpaper(key, screen) for key, screen in
                              (("screen-b", screen_b), ("screen-a", screen_a)) if screen in screens]
                application = mock.Mock()
                application.screens.return_value = screens
                factory = self.namespace["Wallpaper"] = mock.Mock(side_effect=wallpapers)
                tray = self.namespace["Tray"](application)
                try:
                    factory.assert_not_called()
                    self.assertTrue(tray.follow_restore_timer.isActive())
                    tray.add_screen(screen_b)
                    factory.assert_not_called()
                    tray.follow_restore_timer.timeout.emit()
                    self.assertEqual(factory.call_count, len(screens))
                    self.assertEqual(tray.follow_controller.graph.sources, expected)
                    if not follower_present:
                        follower = self.make_wallpaper("screen-b", screen_b)
                        factory.side_effect = None
                        factory.return_value = follower
                        screens.append(screen_b)
                        tray.add_screen(screen_b)
                        self.assertEqual(tray.follow_controller.graph.sources, saved if source_present else {})
                        if source_present:
                            follower.apply_playback_state.assert_called_once_with(wallpapers[0].playback_state)
                    if not source_present:
                        self.assertEqual(config_store.save.call_args.args[0]["screenFollows"], {})
                finally:
                    tray.shutdown()
                    tray.menu.deleteLater()


    def test_follow_reconnection_rechecks_source_without_clearing_other_offline_settings(self) -> None:
        """
        驗證依序拔除跟隨者與來源後, 跟隨者接回才清除自身失效設定
        """

        controller = self.tray.follow_controller
        saved = {"screen-b": "screen-a", "offline": "missing"}
        self.namespace["config_store"].load.return_value = {"screenFollows": saved.copy()}
        controller.restore(saved)
        follower = self.add_screen("screen-b")
        follower.apply_playback_state.assert_called_once_with(self.wallpaper.playback_state)
        self.assertEqual(controller.graph.sources, saved)

        for screen in (follower.target_screen, self.screen):
            self.tray.app.screens.return_value.remove(screen)
            self.screen_notifications.screenRemoved.emit(screen)
            self.assertEqual(controller.graph.sources, saved)

        replacement = self.add_screen("screen-b")
        self.assertEqual(controller.graph.sources, {"offline": "missing"})
        self.assertEqual(self.namespace["config_store"].save.call_args.args[0]["screenFollows"], {"offline": "missing"})
        self.add_screen("screen-a")
        self.assertEqual(controller.graph.sources, {"offline": "missing"})
        replacement.apply_playback_state.assert_not_called()


    def test_single_screen_import_does_not_apply_to_screen_added_during_copy(self) -> None:
        """
        驗證單螢幕匯入只套用原目標, 不套用到複製期間新增的螢幕
        """

        self.namespace["get_media"].return_value = [self.source]
        self.namespace["import_media"] = self.blocked_import
        self.tray.add_media()
        worker = self.tray.import_thread
        self.assertTrue(self.started.wait(5))
        self.add_screen("screen-b")
        self.release.set()
        self.wait_for_completion(worker)

        self.tray.play_from_menu.assert_called_once_with(self.destination, "screen-a")


    def test_multiple_screen_import_does_not_autoplay_after_one_screen_is_removed(self) -> None:
        """
        驗證多螢幕匯入期間移除螢幕後仍只更新紀錄, 不改為自動播放
        """

        other = self.add_screen("screen-b")
        self.namespace["get_media"].return_value = [self.source]
        self.namespace["import_media"] = self.blocked_import
        self.tray.add_media()
        worker = self.tray.import_thread
        self.assertTrue(self.started.wait(5))
        self.tray.app.screens.return_value.remove(other.target_screen)
        self.screen_notifications.screenRemoved.emit(other.target_screen)
        self.release.set()
        self.wait_for_completion(worker)

        self.tray.play_from_menu.assert_not_called()
        self.namespace["config_store"].remember_recent.assert_called_once_with(self.destination)


    def test_single_screen_import_does_not_apply_to_replacement_screen(self) -> None:
        """
        驗證原匯入目標移除後不套用到替代螢幕, 仍保存最近紀錄
        """

        self.namespace["get_media"].return_value = [self.source]
        self.namespace["import_media"] = self.blocked_import
        self.tray.add_media()
        worker = self.tray.import_thread
        self.assertTrue(self.started.wait(5))
        self.tray.app.screens.return_value.remove(self.screen)
        self.screen_notifications.screenRemoved.emit(self.screen)
        self.add_screen("screen-b")
        self.release.set()
        self.wait_for_completion(worker)

        self.tray.play_from_menu.assert_not_called()
        self.namespace["config_store"].remember_recent.assert_called_once_with(self.destination)


    def test_removing_screen_waited_on_by_import_finishes_remaining_targets_once(self) -> None:
        """
        驗證移除正在重掛的匯入目標後, 其餘目標只套用一次匯入結果
        """

        self.add_screen("screen-b")
        self.wallpaper.is_recreating = True
        self.tray.add_media()
        worker = self.tray.import_thread
        self.assertTrue(worker.wait(5000))
        self.app.processEvents()
        self.assertIs(self.tray.import_thread, worker)

        self.tray.app.screens.return_value.remove(self.screen)
        self.screen_notifications.screenRemoved.emit(self.screen)
        self.reattach_notifier.reattach_finished.emit()
        self.app.processEvents()

        self.assertIsNone(self.tray.import_thread)
        self.tray.play_from_menu.assert_called_once_with(self.destination, "screen-b")


    def test_old_import_callbacks_cannot_complete_a_new_import(self) -> None:
        """
        驗證舊匯入留下的重掛回呼不會完成或清除新匯入工作
        """

        self.wallpaper.is_recreating = True
        self.tray.add_media()
        old_worker = self.tray.import_thread
        self.assertTrue(old_worker.wait(5000))
        self.app.processEvents()
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


class StartupTests(unittest.TestCase):

    def test_restart_cleans_up_before_launch_and_preserves_startup_mode(self) -> None:
        """
        重啟前完成清理與還原並釋放單例鎖, 保留啟動參數且處理保存與啟動失敗
        """

        for frozen, operation, failure in (
            (False, "exit", None), (False, "restart", None),
            (True, "restart", None), (True, "restart", "launch"),
            (False, "reset", None), (True, "reset", None),
            (False, "reset", "save"),
        ):
            with self.subTest(frozen=frozen, operation=operation, failure=failure):
                restart = operation != "exit"
                reset = operation == "reset"
                module = load_app_module()
                application = mock.Mock()
                application.exec.return_value = 0
                tray = mock.Mock(restart_requested=restart, reset_requested=reset)
                mutex = mock.Mock()
                log = mock.Mock()
                launcher = mock.Mock(side_effect=OSError("無法啟動") if failure == "launch" else None)
                delete_task = mock.Mock()
                store = mock.Mock()
                store.load.return_value = {
                    "fontSize": 11, "language": "ja-JP", "recentVideo": ["old.mp4"],
                    "screenWallpapers": {"screen-a": "old.mp4"},
                    "screenFollows": {"screen-b": "screen-a"}, "futureOption": 9,
                }
                store.save.return_value = True
                if failure == "save":
                    store.save.side_effect = [True, False]
                lifecycle = mock.Mock()
                for name, callback in (
                    ("event_loop", application.exec), ("cleanup", tray.shutdown),
                    ("save_config", store.save), ("delete_task", delete_task),
                    ("close_log", log.shutdown), ("close_mutex", mutex.Close),
                    ("launch", launcher),
                ):
                    lifecycle.attach_mock(callback, name)

                executable = str(ROOT / ("BB Video Wallpaper.exe" if frozen else ".venv/Scripts/python.exe"))
                entry = Path(executable) if frozen else ROOT / "bb-video-wallpaper.py"
                module.__dict__.update({
                    "sys": SimpleNamespace(executable=executable, argv=[str(entry), "--task"], frozen=frozen),
                    "FILE": entry, "ROOT_DIR": ROOT,
                    "APPDATA_DIR": mock.Mock(), "MEDIA_DIR": mock.Mock(),
                    "THUMBNAIL_DIR": mock.Mock(), "cleanup_orphaned_thumbnails": mock.Mock(),
                    "CONFIG_PATH": ROOT / "unused-config.json",
                    "initialize_paths": mock.Mock(), "initialize_logging": mock.Mock(),
                    "initialize_vlc": mock.Mock(return_value=True),
                    "is_already_running": mock.Mock(return_value=(False, mutex)),
                    "ConfigStore": mock.Mock(return_value=store),
                    "check_desktop_ready": mock.Mock(return_value=True),
                    "time": mock.Mock(), "logging": log, "logger": mock.Mock(),
                    "QApplication": mock.Mock(return_value=application), "QFont": mock.Mock(),
                    "Tray": mock.Mock(return_value=tray),
                    "delete_task": delete_task,
                    "subprocess": SimpleNamespace(Popen=launcher, DETACHED_PROCESS=module.subprocess.DETACHED_PROCESS),
                })

                self.assertEqual(module.main(), int(failure is not None))

                expected = [mock.call.save_config(store.load.return_value), mock.call.event_loop()]
                if restart:
                    expected.append(mock.call.cleanup())
                    if reset:
                        expected.append(mock.call.save_config({"language": "ja-JP"}))
                        if failure != "save":
                            expected.append(mock.call.delete_task())
                    if failure != "save":
                        command = [executable] + ([] if frozen else [str(entry)]) + ["--task"]
                        expected.extend([
                            mock.call.close_log(), mock.call.close_mutex(),
                            mock.call.launch(
                                command, cwd=ROOT,
                                env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
                                creationflags=module.subprocess.DETACHED_PROCESS
                            ),
                        ])
                self.assertEqual(lifecycle.mock_calls, expected)
                self.assertEqual(module.logger.exception.call_count, int(failure == "launch"))


    def test_import_does_not_initialize_runtime(self) -> None:
        """
        驗證缺少使用者資料位置與 VLC 時仍可匯入主程式, 且不初始化執行環境
        """

        blocked = mock.Mock(side_effect=AssertionError("匯入時不應初始化執行環境"))
        with (
            mock.patch.dict(os.environ),
            mock.patch.dict(sys.modules, {"vlc": None}),
            mock.patch("pathlib.Path.mkdir", blocked),
            mock.patch("logging.handlers.RotatingFileHandler", blocked),
            mock.patch("ctypes.CDLL", blocked),
            mock.patch("os.add_dll_directory", blocked),
            mock.patch.object(ConfigStore, "load", blocked),
            mock.patch.object(ConfigStore, "load_language", blocked),
            mock.patch.object(ConfigStore, "save", blocked),
            mock.patch("windows_integration.is_already_running", blocked),
            mock.patch("windows_integration.check_desktop_ready", blocked),
            mock.patch("PySide6.QtWidgets.QApplication", blocked),
        ):
            os.environ.pop("LOCALAPPDATA", None)
            original_environment = dict(os.environ)
            load_app_module()

            self.assertEqual(dict(os.environ), original_environment)
            blocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
