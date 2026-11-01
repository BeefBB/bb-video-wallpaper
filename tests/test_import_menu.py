"""
測試匯入期間的系統匣選單與取消操作
使用真正的 Qt 選單, 訊號佇列與工作執行緒, 不顯示系統匣或啟動桌布
"""

import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QThread, Qt, Signal
from PySide6.QtGui import QAction, QIcon, QScreen
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon


ROOT = Path(__file__).resolve().parents[1]


# 直接執行測試檔時, 將專案根目錄加入模組搜尋路徑
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from media_import import MediaImportCancelled
from app_loader import load_app_module


class PreviewMenu(QMenu):
    """
    使用模擬縮圖工作的 Qt 選單
    """

    def __init__(self, title, parent, *, media_dir, thumbnail_dir, loading_image_path, thumbnail_thread=None):

        super().__init__(title, parent)
        self.media_dir = media_dir
        self.thumbnail_dir = thumbnail_dir
        self.loading_image_path = loading_image_path
        self.thumbnail_thread = thumbnail_thread if thumbnail_thread is not None else mock.Mock()
        self.reset_preview = mock.Mock()


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


class ImportMenuTests(unittest.TestCase):
    """
    系統匣選單與背景匯入流程
    """

    @classmethod
    def setUpClass(cls) -> None:
        """
        建立 Qt 應用程式並載入匯入執行緒與系統匣類別
        """

        cls.app = QApplication.instance() or QApplication([])

        if not isinstance(cls.app, QApplication):
            raise unittest.SkipTest("選單測試需要 QApplication")


    def setUp(self) -> None:
        """
        建立選單, 匯入替身與執行緒同步事件
        """

        self.language = json.loads((ROOT / "lang" / "zh-TW.json").read_text(encoding="utf-8"))
        self.source = ROOT / "sample.mp4"
        self.destination = ROOT / "imported.mp4"
        config_store = mock.Mock()
        config_store.load.return_value = {}

        self.namespace = {
            "Path": Path,
            "Qt": Qt,
            "QThread": QThread,
            "Signal": Signal,
            "QAction": QAction,
            "QIcon": QIcon,
            "QScreen": QScreen,
            "QMenu": QMenu,
            "QFileDialog": mock.Mock(),
            "QSystemTrayIcon": mock.Mock(),
            "MediaPreviewMenu": PreviewMenu,
            "ThumbnailThread": mock.Mock(side_effect=lambda *args: mock.Mock()),
            "MediaImportCancelled": MediaImportCancelled,
            "ICON_PATH": ROOT / "icon" / "bb-video-wallpaper.ico",
            "MEDIA_DIR": ROOT / "test-media",
            "THUMBNAIL_DIR": ROOT / "test-thumbnails",
            "LOADING_IMAGE_PATH": ROOT / "icon" / "loading.jpg",
            "SINGLE_IMPORT_DIR": ROOT / "video" / "_imports",
            "LANG": self.language,
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
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


    def visible_labels(self):
        """
        取得目前顯示的選單文字與分隔線
        """

        return [
            "-" if action.isSeparator() else action.text()
            for action in self.menu.actions() if action.isVisible()
        ]


    def make_wallpaper(self, screen_id, screen):

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

        screen = mock.Mock()
        wallpaper = self.make_wallpaper(screen_id, screen)
        self.namespace["Wallpaper"].return_value = wallpaper
        self.tray.app.screens.return_value.append(screen)
        self.screen_notifications.screenAdded.emit(screen)
        return wallpaper


    def assert_idle_menu(self):
        """
        確認選單已回到可再次匯入的閒置狀態
        """

        self.assertEqual(self.visible_labels(), [
            "播放", "暫停", "停止", "-", "選擇影片", "新增影片", "開啟影片資料夾",
            "-", "選項", "-", "退出",
        ])
        self.assertTrue(self.tray.add_media_action.isEnabled())
        self.assertFalse(self.tray.cancel_import_action.isVisible())


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


    def test_idle_and_cancelled_file_dialog_keep_original_menu(self):
        """
        閒置與取消選檔時維持原本選單
        """

        self.assert_idle_menu()
        self.namespace["QFileDialog"].getOpenFileName.return_value = ("", "")
        self.tray.add_media()

        self.assert_idle_menu()
        self.assertIsNone(self.tray.import_thread)
        self.namespace["import_media"].assert_not_called()


    def test_exit_delegates_cleanup_and_quits_even_if_shutdown_fails(self):
        """
        退出時清理桌布, 清理失敗仍結束應用程式
        """

        other = self.add_screen("screen-b")
        operations = mock.Mock()
        operations.attach_mock(self.wallpaper.shutdown, "first_shutdown")
        operations.attach_mock(other.shutdown, "second_shutdown")
        operations.attach_mock(self.tray.app.quit, "quit")
        self.wallpaper.shutdown.side_effect = RuntimeError("shutdown failed")

        self.tray.exit()

        self.assertEqual(operations.mock_calls, [
            mock.call.first_shutdown(), mock.call.second_shutdown(), mock.call.quit(),
        ])
        self.tray.exit()
        self.wallpaper.shutdown.assert_called_once_with()
        other.shutdown.assert_called_once_with()


    def test_startup_toggle_passes_entry_point_to_task_creation(self):
        """
        啟用開機啟動時傳入正確的程式路徑
        """

        self.tray.toggle_startup(True)

        self.namespace["create_task"].assert_called_once_with(self.namespace["FILE"])
        self.namespace["delete_task"].assert_not_called()

        self.tray.toggle_startup(False)

        self.namespace["delete_task"].assert_called_once_with()


    def test_preview_menu_receives_paths_and_stops_its_worker_on_quit(self):
        """
        預覽選單使用指定路徑, 退出時停止縮圖工作
        """

        preview = self.tray.media_menu
        self.assertEqual(preview.media_dir, self.namespace["MEDIA_DIR"])
        self.assertEqual(preview.thumbnail_dir, self.namespace["THUMBNAIL_DIR"])
        self.assertEqual(preview.loading_image_path, self.namespace["LOADING_IMAGE_PATH"])
        self.assertIs(preview.thumbnail_thread, self.tray.thumbnail_thread)
        self.namespace["ThumbnailThread"].assert_called_once_with(
            self.namespace["MEDIA_DIR"], self.namespace["THUMBNAIL_DIR"]
        )
        self.tray.app.aboutToQuit.connect.assert_any_call(self.tray.shutdown)
        self.tray.shutdown()
        self.tray.thumbnail_thread.stop.assert_called_once_with()


    def test_comparing_menu_has_one_extra_cancel_row_and_cancel_restores_it(self):
        """
        比對檔案時顯示取消項目, 取消後恢復選單
        """

        self.namespace["import_media"] = self.blocked_import
        self.tray.add_media()
        worker = self.tray.import_thread
        self.assertTrue(self.started.wait(5))

        self.assertEqual(self.visible_labels(), [
            "播放", "暫停", "停止", "-", "選擇影片", "比對中…", "取消匯入",
            "開啟影片資料夾", "-", "選項", "-", "退出",
        ])
        self.assertFalse(self.tray.add_media_action.isEnabled())
        self.tray.cancel_import_action.trigger()
        self.assertTrue(worker.isInterruptionRequested())
        self.assertFalse(self.tray.cancel_import_action.isEnabled())

        self.release.set()
        self.wait_for_completion(worker)

        self.assert_idle_menu()
        self.tray.play_from_menu.assert_not_called()
        self.tray.thumbnail_thread.request.assert_not_called()
        self.tray.tray.showMessage.assert_not_called()
        self.namespace["logger"].exception.assert_not_called()

        # 取消後可以開始下一次匯入, 取消按鈕也會重新啟用
        self.namespace["import_media"] = mock.Mock(return_value=self.destination)
        self.tray.add_media()
        worker = self.tray.import_thread
        self.assertTrue(self.tray.cancel_import_action.isEnabled())
        self.wait_for_completion(worker)
        self.assert_idle_menu()
        self.tray.play_from_menu.assert_called_once_with(self.destination, "screen-a")


    def test_worker_progress_runs_on_main_thread_and_ignores_updates_after_cancel(self):
        """
        在主執行緒更新進度, 取消後忽略後續更新
        """

        updated_on = []
        worker_threads = []
        update_progress = self.tray.update_import_progress

        def record_progress(percent):

            updated_on.append(threading.get_ident())
            update_progress(percent)

        def copy_with_progress(source, import_dir, *, translate, progress, cancelled):

            worker_threads.append(threading.get_ident())
            progress(42)
            return self.blocked_import(source, import_dir, translate=translate, progress=progress, cancelled=cancelled)

        self.tray.update_import_progress = record_progress
        self.namespace["import_media"] = copy_with_progress
        self.tray.add_media()
        worker = self.tray.import_thread
        self.assertTrue(self.started.wait(5))
        self.app.processEvents()

        self.assertEqual(self.tray.add_media_action.text(), "匯入中 42%")
        self.assertTrue(self.tray.cancel_import_action.isVisible())
        self.assertEqual(updated_on, [threading.get_ident()])
        self.assertNotEqual(worker_threads, updated_on)

        self.tray.cancel_import_action.trigger()
        self.tray.update_import_progress(80)
        self.assertEqual(self.tray.add_media_action.text(), "匯入中 42%")
        self.release.set()
        self.wait_for_completion(worker)
        self.assert_idle_menu()


    def test_success_plays_imported_media_and_hides_cancel_action(self):
        """
        媒體庫為空時, 匯入成功後套用桌布並恢復選單
        """

        self.tray.add_media()
        self.wait_for_completion(self.tray.import_thread)

        self.assert_idle_menu()
        self.namespace["import_media"].assert_called_once_with(
            self.source,
            self.namespace["SINGLE_IMPORT_DIR"],
            translate=self.namespace["tr"],
            progress=mock.ANY,
            cancelled=mock.ANY
        )
        self.tray.play_from_menu.assert_called_once_with(self.destination, "screen-a")
        self.tray.thumbnail_thread.request.assert_called_once_with(self.destination)
        self.tray.refresh_media_menu.assert_called_once_with()
        self.tray.tray.showMessage.assert_not_called()


    def test_failure_restores_menu_and_shows_error(self):
        """
        匯入失敗後恢復選單並顯示錯誤
        """

        self.namespace["import_media"].side_effect = OSError("disk full")
        self.tray.add_media()
        self.wait_for_completion(self.tray.import_thread)

        self.assert_idle_menu()
        self.tray.play_from_menu.assert_not_called()
        self.assertIn("disk full", self.tray.tray.showMessage.call_args.args[1])


    def test_completed_import_waits_for_reattach_and_plays_only_once(self):
        """
        匯入完成時若正在重掛, 保留結果並在重掛結束後只套用一次
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
        self.assert_idle_menu()
        self.tray.play_from_menu.assert_called_once_with(self.destination, "screen-a")
        self.tray.refresh_media_menu.assert_called_once_with()


    def test_quitting_discards_import_deferred_during_reattach(self):
        """
        匯入完成後等待重掛期間退出, 不因延後回呼重新播放
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


    def test_single_recent_media_can_be_cleared_without_changing_wallpapers(self):
        """
        只有一筆最近紀錄時仍可從選單清空, 並保留各螢幕桌布
        """

        wallpapers = {"screen-a": "sample.mp4", "screen-b": "other.png"}
        store = self.namespace["config_store"]
        store.load.return_value = {
            "recentVideo": ["sample.mp4"],
            "screenWallpapers": wallpapers.copy(),
        }
        self.namespace["get_media"] = mock.Mock(return_value=[])
        self.tray.media_menu.reset_preview = mock.Mock()
        self.namespace["Tray"].refresh_media_menu(self.tray)

        clear_actions = [
            action for action in self.tray.media_menu.actions()
            if action.text() == self.language["clear_recent_videos"]
        ]
        self.assertEqual(len(clear_actions), 1)
        self.assertTrue(clear_actions[0].isEnabled())
        clear_actions[0].trigger()

        store.save.assert_called_once()
        saved = store.save.call_args.args[0]
        self.assertEqual(saved["recentVideo"], [])
        self.assertEqual(saved["screenWallpapers"], wallpapers)
        self.assertTrue(all(
            action.text() not in {"播放", "暫停", "停止"}
            for action in self.tray.media_menu.actions()
        ))


    def test_bad_wallpaper_shows_localized_error_without_starting_playback(self):
        """
        媒體載入失敗時顯示對應語言的錯誤並停止後續播放
        """

        self.wallpaper.select_media.side_effect = RuntimeError("bad image")
        self.wallpaper.playback_state = self.namespace["PlaybackState"]("pause")

        for language_path in (ROOT / "lang").glob("*.json"):
            with self.subTest(language=language_path.name):
                self.namespace["LANG"] = json.loads(language_path.read_text(encoding="utf-8"))
                self.tray.tray.showMessage.reset_mock()
                self.namespace["Tray"].play_from_menu(self.tray, self.source)

                self.wallpaper.manual_play.assert_not_called()
                self.assertEqual(self.wallpaper.playback_state.requested_state, "pause")
                self.assertEqual(
                    self.tray.tray.showMessage.call_args.args[1],
                    self.namespace["LANG"]["media_load_failed"].format(error="bad image")
                )


    def test_quitting_requests_cancellation_before_waiting(self):
        """
        退出時先取消匯入再等待執行緒結束
        """

        def wait_for_cancel(source, import_dir, *, translate, progress, cancelled):

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


    def test_startup_creates_one_wallpaper_per_screen_and_ignores_duplicate_notifications(self):

        self.namespace["Wallpaper"].assert_called_once_with(target_screen=self.screen)
        self.wallpaper.show.assert_called_once_with()
        other = self.add_screen("screen-b")

        self.assertEqual(self.tray.wallpapers, {"screen-a": self.wallpaper, "screen-b": other})
        self.screen_notifications.screenAdded.emit(other.target_screen)
        self.assertEqual(self.namespace["Wallpaper"].call_count, 2)
        self.assertEqual(self.visible_labels()[:6], [
            "全部播放", "全部暫停", "全部停止", "-", "螢幕 1", "螢幕 2",
        ])
        for menu in self.tray.screen_menus.values():
            self.assertIs(menu.thumbnail_thread, self.tray.thumbnail_thread)
        self.namespace["ThumbnailThread"].assert_called_once_with(
            self.namespace["MEDIA_DIR"], self.namespace["THUMBNAIL_DIR"]
        )


    def test_initial_screen_enumeration_handles_multiple_or_no_screens(self):

        for count in (2, 0):
            with self.subTest(count=count):
                application = mock.Mock()
                screens = [mock.Mock() for _ in range(count)]
                wallpapers = [self.make_wallpaper(f"initial-{i}", screen) for i, screen in enumerate(screens)]
                application.screens.return_value = screens
                factory = self.namespace["Wallpaper"] = mock.Mock(side_effect=wallpapers)
                tray = self.namespace["Tray"](application)

                self.assertEqual(factory.call_args_list, [mock.call(target_screen=screen) for screen in screens])
                self.assertEqual(list(tray.wallpapers.values()), wallpapers)
                self.assertEqual(len(tray.screen_menus), count)
                tray.shutdown()
                tray.menu.close()
                tray.menu.deleteLater()


    def test_screen_removal_restores_single_menu_and_reconnection_creates_new_wallpaper(self):

        other = self.add_screen("screen-b")
        self.tray.app.screens.return_value.remove(other.target_screen)
        self.screen_notifications.screenRemoved.emit(other.target_screen)

        self.assertEqual(self.tray.wallpapers, {"screen-a": self.wallpaper})
        other.shutdown.assert_called_once_with()
        other.deleteLater.assert_called_once_with()
        self.wallpaper.shutdown.assert_not_called()
        self.tray.thumbnail_thread.stop.assert_not_called()
        self.namespace["config_store"].save.assert_not_called()
        self.assert_idle_menu()

        replacement = self.add_screen("screen-b")
        self.assertIs(self.tray.wallpapers["screen-b"], replacement)
        self.assertIsNot(replacement, other)


    def test_last_screen_can_be_removed_and_added_again(self):

        self.tray.app.screens.return_value.clear()
        self.screen_notifications.screenRemoved.emit(self.screen)

        self.assertEqual(self.tray.wallpapers, {})
        self.assertTrue(all(not action.isEnabled() for action, _ in self.tray.playback_actions))
        self.assertNotIn("選擇影片", self.visible_labels())
        self.add_screen("screen-a")
        self.assert_idle_menu()


    def test_screen_creation_failure_keeps_existing_wallpaper_available(self):

        self.namespace["Wallpaper"].side_effect = RuntimeError("no output")
        self.screen_notifications.screenAdded.emit(mock.Mock())

        self.assertEqual(self.tray.wallpapers, {"screen-a": self.wallpaper})
        self.wallpaper.shutdown.assert_not_called()
        self.namespace["logger"].exception.assert_called_once()


    def test_individual_and_global_commands_and_options_target_the_correct_wallpapers(self):

        other = self.add_screen("screen-b")
        self.tray.pause("screen-b")
        other.manual_pause.assert_called_once_with()
        self.wallpaper.manual_pause.assert_not_called()

        self.tray.play()
        self.wallpaper.manual_play.assert_called_once_with()
        other.manual_play.assert_called_once_with()
        self.tray.stop()
        self.wallpaper.manual_stop.assert_called_once_with()
        other.manual_stop.assert_called_once_with()
        self.assertTrue(all(not action.isCheckable() for action, _ in self.tray.playback_actions))

        options = (
            ("auto_pause_fullscreen", "autoPauseIfFullscreen", "auto_pause_if_fullscreen", True),
            ("auto_pause_screen_off", "autoPauseIfScreenOff", "auto_pause_if_screen_off", True),
            ("auto_pause_battery", "autoPauseIfOnBattery", "auto_pause_if_on_battery", False),
        )
        for action, (text_key, config_key, attribute, default) in zip(self.tray.options_menu.actions(), options):
            with self.subTest(option=config_key):
                self.assertEqual(action.text(), self.language[text_key])
                self.assertTrue(action.isCheckable())
                self.assertEqual(action.isChecked(), default)
                action.trigger()
                saved = self.namespace["config_store"].save.call_args.args[0]
                self.assertEqual(saved[config_key], not default)
                self.assertIs(getattr(self.wallpaper, attribute), not default)
                self.assertIs(getattr(other, attribute), not default)

        self.tray.play("removed-screen")
        self.wallpaper.manual_play.assert_called_once_with()


    def test_pause_all_starts_stopped_videos_before_pausing_them(self):

        other = self.add_screen("screen-b")
        other.play_state = "stop"
        operations = mock.Mock()
        operations.attach_mock(other.manual_play, "play")
        operations.attach_mock(other.manual_pause, "pause")

        self.tray.pause()

        self.assertEqual(operations.mock_calls, [mock.call.play(), mock.call.pause()])
        self.wallpaper.manual_play.assert_not_called()
        self.wallpaper.manual_pause.assert_called_once_with()


    def test_pause_all_does_not_start_stopped_images(self):

        other = self.add_screen("screen-b")
        other.current_media = self.source.with_suffix(".png")
        other.play_state = "stop"

        self.tray.pause()

        other.manual_play.assert_not_called()


    def test_screen_menu_selects_its_own_media_and_reflects_its_playback_state(self):

        other = self.add_screen("screen-b")
        other.play_state = "pause"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.mp4"
            path.touch()
            other.current_media = path
            self.namespace["get_media"].return_value = [path]
            self.tray.play_from_menu = self.namespace["Tray"].play_from_menu.__get__(self.tray)
            menu = self.tray.screen_menus["screen-b"]
            menu.aboutToShow.emit()

            actions = menu.actions()
            self.assertEqual([action.text() for action in actions[:3]], ["播放", "暫停", "停止"])
            self.assertTrue(actions[1].isChecked())
            actions[0].trigger()
            other.manual_play.assert_called_once_with()
            self.wallpaper.manual_play.assert_not_called()
            actions[1].trigger()
            other.manual_pause.assert_called_once_with()
            self.wallpaper.manual_pause.assert_not_called()
            actions[2].trigger()
            other.manual_stop.assert_called_once_with()
            self.wallpaper.manual_stop.assert_not_called()
            media_action = next(action for action in actions if action.data() == str(path))
            self.assertTrue(media_action.isChecked())
            media_action.trigger()
            other.select_media.assert_called_once_with(path)
            self.wallpaper.select_media.assert_not_called()


    def test_follow_menu_disables_cycles_and_marks_only_the_selected_source(self):

        second = self.add_screen("screen-b")
        third = self.add_screen("screen-c")
        self.tray.follow_controller.follow("screen-b", "screen-a")
        self.tray.follow_controller.follow("screen-c", "screen-b")
        first_menu = self.tray.screen_menus["screen-a"]
        first_menu.aboutToShow.emit()
        sources = first_menu.actions()[-1].menu().actions()
        self.assertEqual([action.text() for action in sources], ["螢幕 2", "螢幕 3"])
        self.assertTrue(all(not action.isEnabled() for action in sources))

        second_menu = self.tray.screen_menus["screen-b"]
        second_menu.aboutToShow.emit()
        self.assertTrue(second_menu.actions()[-1].isChecked())
        self.assertTrue(all(not action.isChecked() for action in second_menu.actions()[:3]))
        sources = second_menu.actions()[-1].menu().actions()
        self.assertTrue(sources[0].isChecked())
        self.assertTrue(sources[0].isEnabled())
        self.assertFalse(sources[1].isEnabled())
        self.tray.play("screen-b")
        self.assertEqual(self.tray.follow_controller.graph.sources, {"screen-c": "screen-b"})
        second.manual_play.assert_called_once()
        self.wallpaper.manual_play.assert_not_called()


    def test_follow_timer_corrects_progress_every_sixty_seconds_and_stops_on_exit(self):

        follower = self.add_screen("screen-b")
        menu = self.tray.screen_menus["screen-b"]
        menu.aboutToShow.emit()
        menu.actions()[-1].menu().actions()[0].trigger()
        self.assertEqual(self.tray.follow_controller.graph.sources, {"screen-b": "screen-a"})
        self.assertEqual(self.tray.follow_sync_timer.interval(), 60000)
        self.assertTrue(self.tray.follow_sync_timer.isActive())
        self.wallpaper.get_playback_time.return_value = 6500
        self.tray.follow_sync_timer.timeout.emit()
        follower.seek_time.assert_called_once_with(6500)
        self.tray.shutdown()
        self.assertFalse(self.tray.follow_sync_timer.isActive())
        self.assertFalse(self.tray.follow_restore_timer.isActive())


    def test_follow_startup_waits_for_screens_and_removes_missing_sources_after_grace(self):

        for source_present in (True, False):
            with self.subTest(source_present=source_present):
                config_store = self.namespace["config_store"]
                config_store.load.return_value = {"screenFollows": {"screen-b": "screen-a"}}
                screen_a, screen_b = mock.Mock(), mock.Mock()
                screens = [screen_a, screen_b] if source_present else [screen_b]
                wallpapers = [self.make_wallpaper(key, screen) for key, screen in
                              (("screen-a", screen_a), ("screen-b", screen_b)) if screen in screens]
                application = mock.Mock()
                application.screens.return_value = screens
                factory = self.namespace["Wallpaper"] = mock.Mock(side_effect=wallpapers)
                tray = self.namespace["Tray"](application)
                try:
                    factory.assert_not_called()
                    self.assertEqual(tray.follow_restore_timer.interval(), 3000)
                    self.assertTrue(tray.follow_restore_timer.isActive())
                    tray.add_screen(screen_b)
                    factory.assert_not_called()
                    tray.follow_restore_timer.timeout.emit()
                    self.assertEqual(factory.call_count, len(screens))
                    expected = {"screen-b": "screen-a"} if source_present else {}
                    self.assertEqual(tray.follow_controller.graph.sources, expected)
                    if not source_present:
                        self.assertEqual(config_store.save.call_args.args[0]["screenFollows"], {})
                finally:
                    tray.shutdown()
                    tray.menu.deleteLater()


    def test_single_screen_import_into_existing_library_plays_media(self):
        """
        單螢幕在媒體庫已有桌布時仍自動播放匯入結果
        """

        self.namespace["get_media"].return_value = [self.source]
        self.tray.add_media()
        self.wait_for_completion(self.tray.import_thread)

        self.tray.play_from_menu.assert_called_once_with(self.destination, "screen-a")


    def test_multiple_screen_import_into_existing_library_updates_history_without_changing_wallpapers(self):
        """
        多螢幕在媒體庫已有桌布時只更新最近紀錄
        """

        self.add_screen("screen-b")
        self.namespace["get_media"].return_value = [self.source]
        self.tray.add_media()
        self.wait_for_completion(self.tray.import_thread)

        self.tray.play_from_menu.assert_not_called()
        self.namespace["config_store"].remember_recent.assert_called_once_with(self.destination)
        self.tray.thumbnail_thread.request.assert_called_once_with(self.destination)


    def test_single_screen_import_does_not_apply_to_screen_added_during_copy(self):
        """
        匯入期間新增螢幕時, 只套用原本的單螢幕
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


    def test_multiple_screen_import_does_not_autoplay_after_one_screen_is_removed(self):
        """
        多螢幕匯入期間移除螢幕時, 不改為自動播放
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


    def test_single_screen_import_does_not_apply_to_replacement_screen(self):
        """
        原本的匯入目標已拔除時, 不套用到另一台螢幕
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


    def test_first_import_applies_to_all_connected_screens(self):

        self.add_screen("screen-b")
        self.tray.add_media()
        self.wait_for_completion(self.tray.import_thread)

        self.assertEqual(self.tray.play_from_menu.call_args_list, [
            mock.call(self.destination, "screen-a"), mock.call(self.destination, "screen-b"),
        ])


    def test_removing_screen_waited_on_by_import_finishes_remaining_targets_once(self):

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


    def test_old_import_callbacks_cannot_complete_a_new_import(self):

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


if __name__ == "__main__":
    unittest.main()
