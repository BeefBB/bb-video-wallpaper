"""
測試匯入期間的系統匣選單與取消操作
使用真正的 Qt 選單, 訊號佇列與工作執行緒, 不顯示系統匣或啟動桌布
"""

import ast
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEvent, QThread, Qt, Signal
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon


ROOT = Path(__file__).resolve().parents[1]


# 直接執行測試檔時, 將專案根目錄加入模組搜尋路徑
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from media_import import MediaImportCancelled


class PreviewMenu(QMenu):
    """
    使用模擬縮圖工作的 Qt 選單
    """

    def __init__(self, title, parent, *, media_dir, thumbnail_dir, loading_image_path):

        super().__init__(title, parent)
        self.media_dir = media_dir
        self.thumbnail_dir = thumbnail_dir
        self.loading_image_path = loading_image_path
        self.thumbnail_thread = mock.Mock()


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

        # 避免主程式頂層建立使用者目錄, 日誌與 VLC
        source_path = ROOT / "bb-video-wallpaper.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        nodes = ast.Module(
            body=[
                node for node in tree.body
                if (
                    isinstance(node, ast.ClassDef) and
                    node.name in {"MediaImportThread", "Tray"}
                ) or (
                    isinstance(node, ast.FunctionDef) and node.name == "tr"
                )
            ],
            type_ignores=[]
        )
        cls.menu_code = compile(nodes, str(source_path), "exec")


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
            "QMenu": QMenu,
            "QFileDialog": mock.Mock(),
            "QSystemTrayIcon": mock.Mock(),
            "MediaPreviewMenu": PreviewMenu,
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
        }
        self.namespace["QSystemTrayIcon"].MessageIcon = QSystemTrayIcon.MessageIcon
        self.namespace["QFileDialog"].getOpenFileName.return_value = (str(self.source), "")
        exec(self.menu_code, self.namespace)

        wallpaper = mock.Mock()
        wallpaper.is_exiting = False
        self.tray = self.namespace["Tray"](mock.Mock(), wallpaper)
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

        operations = mock.Mock()
        operations.attach_mock(self.tray.wallpaper.shutdown, "shutdown")
        operations.attach_mock(self.tray.app.quit, "quit")

        for error in (None, RuntimeError("shutdown failed")):
            with self.subTest(error=error):
                operations.reset_mock()
                self.tray.wallpaper.shutdown.side_effect = error
                self.tray.exit()

                self.assertEqual(operations.mock_calls, [mock.call.shutdown(), mock.call.quit()])


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
        self.tray.app.aboutToQuit.connect.assert_any_call(preview.thumbnail_thread.stop)


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
        self.tray.media_menu.thumbnail_thread.request.assert_not_called()
        self.tray.tray.showMessage.assert_not_called()
        self.namespace["logger"].exception.assert_not_called()

        # 取消後可以開始下一次匯入, 取消按鈕也會重新啟用
        self.namespace["import_media"] = mock.Mock(return_value=self.destination)
        self.tray.add_media()
        worker = self.tray.import_thread
        self.assertTrue(self.tray.cancel_import_action.isEnabled())
        self.wait_for_completion(worker)
        self.assert_idle_menu()
        self.tray.play_from_menu.assert_called_once_with(self.destination)


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
        匯入成功後播放媒體並恢復選單
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
        self.tray.play_from_menu.assert_called_once_with(self.destination)
        self.tray.media_menu.thumbnail_thread.request.assert_called_once_with(self.destination)
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


    def test_bad_wallpaper_shows_localized_error_without_starting_playback(self):
        """
        媒體載入失敗時顯示對應語言的錯誤並停止後續播放
        """

        self.tray.wallpaper.set_play_media.side_effect = RuntimeError("bad image")
        self.tray.wallpaper.enable_auto_pause = False

        for language_path in (ROOT / "lang").glob("*.json"):
            with self.subTest(language=language_path.name):
                self.namespace["LANG"] = json.loads(language_path.read_text(encoding="utf-8"))
                self.tray.tray.showMessage.reset_mock()
                self.namespace["Tray"].play_from_menu(self.tray, self.source)

                self.tray.wallpaper.media_play.assert_not_called()
                self.assertFalse(self.tray.wallpaper.enable_auto_pause)
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

        self.tray.wallpaper.is_exiting = True
        self.tray.wait_for_import()
        self.wait_for_completion(worker)

        self.tray.play_from_menu.assert_not_called()
        self.tray.tray.showMessage.assert_not_called()
        self.namespace["logger"].exception.assert_not_called()


if __name__ == "__main__":
    unittest.main()
