"""
測試過期縮圖, 共用工作物件與跨執行緒訊號
使用隱藏的 Qt 元件, 不啟動桌布或初始化 VLC
"""

from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, Signal
from PySide6.QtGui import QAction, QColor, QImage
from PySide6.QtWidgets import QApplication


ROOT = Path(__file__).resolve().parents[1]

# 直接執行測試檔時, 將專案根目錄加入模組搜尋路徑
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

import media_preview


class ThumbnailWorker(QObject):
    """
    由測試控制縮圖結果訊號
    """

    thumbnail_ready = Signal(str, QImage)

class MediaPreviewTests(unittest.TestCase):
    """
    縮圖回傳的有效性與 Qt 執行緒邊界
    """

    @classmethod
    def setUpClass(cls) -> None:
        """
        取得或建立 Qt 應用程式, 確認可供預覽選單測試使用
        """

        cls.app = QApplication.instance() or QApplication([])

        if not isinstance(cls.app, QApplication):
            raise unittest.SkipTest("預覽測試需要 QApplication")


    def setUp(self) -> None:
        """
        建立隱藏預覽選單與縮圖訊號替身, 預設第一個媒體項目正在預覽
        """

        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.media_dir = directory / "media"
        self.thumbnail_dir = directory / "thumbnails"
        self.loading_path = directory / "loading.png"
        self.worker = ThumbnailWorker()
        self.enterContext(mock.patch.object(media_preview, "ThumbnailThread", return_value=self.worker))
        self.menu = media_preview.MediaPreviewMenu(
            "Media", media_dir=self.media_dir, thumbnail_dir=self.thumbnail_dir,
            loading_image_path=self.loading_path,
        )
        self.menu.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.menu.preview.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.addCleanup(self.dispose_menu)
        self.first = self.add_media_action("first.mp4")
        self.second = self.add_media_action("second.mp4")
        self.menu.preview_action = self.first
        self.menu.preview_mode = True
        self.menu.preview_pixmap = self.menu.loading_pixmap
        self.display_preview_mock = mock.Mock()
        self.menu.display_preview = self.display_preview_mock


    def dispose_menu(self) -> None:
        """
        停止預覽並釋放選單與縮圖工作物件
        """

        self.menu.reset_preview()
        self.menu.close()
        self.menu.deleteLater()
        self.worker.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


    def add_media_action(self, name):
        """
        建立帶有媒體路徑的選單項目
        """

        action = QAction(name, self.menu)
        action.setData(str(self.media_dir / name))
        self.menu.addAction(action)
        return action


    def test_multiple_menus_share_one_thumbnail_worker_and_receive_its_results(self):
        """
        驗證共用縮圖工作物件的兩個選單都會收到並套用同一筆縮圖結果
        """

        other = media_preview.MediaPreviewMenu(
            "Other screen",
            media_dir=self.media_dir,
            thumbnail_dir=self.thumbnail_dir,
            loading_image_path=self.loading_path,
            thumbnail_thread=self.worker  # type: ignore
        )
        self.addCleanup(other.deleteLater)
        self.assertIs(other.thumbnail_thread, self.menu.thumbnail_thread)

        action = other.addAction("first.mp4")
        action.setData(self.first.data())
        other.preview_action = action
        other.preview_mode = True
        other.display_preview = mock.Mock()
        image = QImage(80, 40, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.red)

        self.worker.thumbnail_ready.emit(self.first.data(), image)

        self.display_preview_mock.assert_called_once_with()
        other.display_preview.assert_called_once_with()
        self.assertEqual(self.menu.preview_pixmap.size(), image.size())
        self.assertEqual(other.preview_pixmap.size(), image.size())


    def thumbnail(self):
        """
        建立背景回傳的測試縮圖
        """

        image = QImage(120, 60, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.blue)
        return image


    def test_stale_null_and_inactive_results_do_not_replace_preview(self):
        """
        過期, 空白與預覽結束後的結果不更新畫面
        """

        self.menu.preview_action = self.second
        loading_key = self.menu.preview_pixmap.cacheKey()

        self.worker.thumbnail_ready.emit(self.first.data(), self.thumbnail())
        self.assertEqual(self.menu.preview_pixmap.cacheKey(), loading_key)

        self.worker.thumbnail_ready.emit(self.second.data(), QImage())
        self.assertEqual(self.menu.preview_pixmap.cacheKey(), loading_key)

        self.menu.reset_preview()
        self.worker.thumbnail_ready.emit(self.second.data(), self.thumbnail())
        self.assertEqual(self.menu.preview_pixmap.cacheKey(), loading_key)
        self.assertTrue(self.menu.preview.isHidden())


    def test_background_result_updates_preview_on_qt_main_thread(self):
        """
        背景縮圖結果由 Qt 主執行緒更新預覽
        """

        displayed_on = []
        display = self.menu.display_preview

        def record_display():
            """
            記錄預覽顯示回呼的執行緒, 再呼叫顯示替身
            """

            displayed_on.append(threading.get_ident())
            display()

        self.menu.display_preview = record_display
        image = self.thumbnail()
        media_path = self.first.data()
        thread = threading.Thread(target=lambda: self.worker.thumbnail_ready.emit(media_path, image))
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        # 尚未處理 Qt 事件佇列
        self.assertEqual(displayed_on, [])

        self.app.processEvents()

        self.assertEqual(displayed_on, [threading.get_ident()])
        self.assertEqual(self.menu.preview_pixmap.toImage().pixelColor(0, 0), QColor("blue"))


if __name__ == "__main__":
    unittest.main()
