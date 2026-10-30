"""
測試獨立預覽選單的懸停狀態, 縮圖訊號與螢幕邊界
使用隱藏的 Qt 元件與模擬游標, 不啟動桌布或初始化 VLC
"""

from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QPoint, QPointF, QRect, Qt, Signal
from PySide6.QtGui import QAction, QColor, QHideEvent, QImage, QMouseEvent
from PySide6.QtWidgets import QApplication


ROOT = Path(__file__).resolve().parents[1]

# 直接執行測試檔時, 將專案根目錄加入模組搜尋路徑
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

import media_preview


class ThumbnailWorker(QObject):
    """
    由測試控制縮圖請求與結果訊號
    """

    thumbnail_ready = Signal(str, QImage)

    def __init__(self):

        super().__init__()
        self.request = mock.Mock()


class MediaPreviewTests(unittest.TestCase):
    """
    媒體預覽選單的懸停狀態與縮圖顯示
    """

    @classmethod
    def setUpClass(cls) -> None:
        """
        建立 Qt 應用程式
        """

        cls.app = QApplication.instance() or QApplication([])

        if not isinstance(cls.app, QApplication):
            raise unittest.SkipTest("預覽測試需要 QApplication")


    def setUp(self) -> None:
        """
        建立隱藏預覽選單並固定游標與螢幕範圍
        """

        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.media_dir = directory / "media"
        self.thumbnail_dir = directory / "thumbnails"
        self.loading_path = directory / "loading.png"
        image = QImage(40, 20, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.darkGray)
        self.assertTrue(image.save(str(self.loading_path)))

        self.worker = ThumbnailWorker()
        self.worker_class = self.enterContext(mock.patch.object(media_preview, "ThumbnailThread", return_value=self.worker))
        self.menu = media_preview.MediaPreviewMenu(
            "Media",
            media_dir=self.media_dir,
            thumbnail_dir=self.thumbnail_dir,
            loading_image_path=self.loading_path
        )
        self.menu.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.menu.preview.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.addCleanup(self.dispose_menu)

        self.first = self.add_media_action("first.mp4")
        self.second = self.add_media_action("second.mp4")
        self.non_media = self.menu.addAction("Open folder")
        self.disabled = self.add_media_action("missing.mp4")
        self.disabled.setEnabled(False)

        # 固定選單位置, 游標與螢幕範圍
        self.visible = self.enterContext(mock.patch.object(self.menu, "isVisible", return_value=True))
        self.action_at = self.enterContext(mock.patch.object(self.menu, "actionAt", return_value=self.first))
        self.frame_rect = QRect(200, 100, 160, 80)
        self.frame_geometry = self.enterContext(mock.patch.object(self.menu, "frameGeometry", return_value=self.frame_rect))
        self.enterContext(mock.patch.object(self.menu, "actionGeometry", return_value=QRect(0, 20, 160, 24)))
        self.enterContext(mock.patch.object(self.menu, "mapToGlobal", side_effect=lambda point: point + self.frame_rect.topLeft()))
        cursor = self.enterContext(mock.patch.object(media_preview, "QCursor"))
        cursor.pos.return_value = QPoint(220, 120)
        application = self.enterContext(mock.patch.object(media_preview, "QApplication"))
        self.screen = application.screenAt.return_value
        self.screen.availableGeometry.return_value = QRect(0, 0, 1280, 720)


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


    def hover(self, action):
        """
        模擬游標移到指定選單項目
        """

        self.action_at.return_value = action
        event = QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(10, 10),
            QPointF(220, 120),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier
        )
        self.menu.mouseMoveEvent(event)


    def activate_preview(self, action=None):
        """
        模擬懸停並進入預覽模式
        """

        self.hover(action if action is not None else self.first)
        if self.menu.preview_timer.isActive():
            # 直接觸發懸停計時器
            self.menu.preview_timer.stop()
            self.menu.preview_timer.timeout.emit()
        self.assertTrue(self.menu.preview_mode)


    def thumbnail(self, width=120, height=60):
        """
        建立指定尺寸的測試縮圖
        """

        image = QImage(width, height, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.blue)
        return image


    def test_constructor_uses_supplied_paths_without_creating_directories(self):
        """
        建構時使用指定路徑但不建立目錄
        """

        self.worker_class.assert_called_once_with(self.media_dir, self.thumbnail_dir, self.menu)
        self.assertEqual((self.menu.loading_pixmap.width(), self.menu.loading_pixmap.height()), (300, 150))
        self.assertFalse(self.media_dir.exists())
        self.assertFalse(self.thumbnail_dir.exists())


    def test_missing_loading_image_uses_fallback(self):
        """
        載入圖片不存在時使用備援畫面
        """

        menu = media_preview.MediaPreviewMenu(
            "Media",
            media_dir=self.media_dir,
            thumbnail_dir=self.thumbnail_dir,
            loading_image_path=self.loading_path.with_name("missing.png")
        )
        try:
            self.assertFalse(menu.loading_pixmap.isNull())
            self.assertEqual((menu.loading_pixmap.width(), menu.loading_pixmap.height()), (300, 169))
        finally:
            menu.deleteLater()


    def test_first_hover_waits_before_requesting_thumbnail(self):
        """
        首次懸停經過延遲後才請求縮圖
        """

        self.hover(self.first)

        self.assertTrue(self.menu.preview_timer.isActive())
        self.assertEqual(self.menu.preview_timer.interval(), 500)
        self.assertFalse(self.menu.preview_mode)
        self.worker.request.assert_not_called()

        self.menu.preview_timer.stop()
        self.menu.preview_timer.timeout.emit()

        self.assertTrue(self.menu.preview_mode)
        self.worker.request.assert_called_once_with(Path(self.first.data()))
        self.assertEqual(self.menu.preview_pixmap.cacheKey(), self.menu.loading_pixmap.cacheKey())


    def test_switch_in_preview_mode_requests_immediately_and_clears_old_image(self):
        """
        預覽模式切換項目時立即請求並清除舊縮圖
        """

        self.activate_preview()
        self.worker.thumbnail_ready.emit(self.first.data(), self.thumbnail())
        self.assertNotEqual(self.menu.preview_pixmap.cacheKey(), self.menu.loading_pixmap.cacheKey())

        self.hover(self.second)

        self.assertFalse(self.menu.preview_timer.isActive())
        self.assertEqual(self.menu.preview_pixmap.cacheKey(), self.menu.loading_pixmap.cacheKey())
        self.assertEqual(self.worker.request.call_args_list, [
            mock.call(Path(self.first.data())),
            mock.call(Path(self.second.data())),
        ])


    def test_return_before_reset_is_immediate_and_return_after_reset_waits(self):
        """
        重設前返回可立即預覽, 重設後重新等待懸停
        """

        self.activate_preview()
        self.menu.leave_preview()

        self.assertIsNone(self.menu.preview_action)
        self.assertTrue(self.menu.preview.isHidden())
        self.assertTrue(self.menu.reset_timer.isActive())
        self.assertEqual(self.menu.reset_timer.interval(), 1000)

        self.hover(self.second)

        self.assertFalse(self.menu.reset_timer.isActive())
        self.assertFalse(self.menu.preview_timer.isActive())
        self.worker.request.assert_called_with(Path(self.second.data()))

        self.menu.leave_preview()
        self.menu.reset_timer.stop()
        self.menu.reset_timer.timeout.emit()
        self.worker.request.reset_mock()
        self.hover(self.first)

        self.assertFalse(self.menu.preview_mode)
        self.assertTrue(self.menu.preview_timer.isActive())
        self.worker.request.assert_not_called()


    def test_non_media_disabled_and_empty_areas_hide_preview(self):
        """
        停用項目, 非媒體項目與空白區域會隱藏預覽
        """

        for action in (None, self.non_media, self.disabled):
            with self.subTest(action=action):
                self.activate_preview()
                self.hover(action)

                self.assertIsNone(self.menu.preview_action)
                self.assertTrue(self.menu.preview.isHidden())
                self.assertFalse(self.menu.preview_timer.isActive())
                self.assertTrue(self.menu.reset_timer.isActive())


    def test_stale_null_and_inactive_results_do_not_replace_preview(self):
        """
        過期, 空白與預覽結束後的結果不更新畫面
        """

        self.activate_preview(self.second)
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

        self.activate_preview()
        displayed_on = []
        display = self.menu.display_preview

        def record_display():

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


    def test_preview_scales_and_stays_inside_screen_edges(self):
        """
        依螢幕範圍縮放並調整預覽位置
        """

        self.activate_preview()

        for available, frame_rect, dimensions in (
            (QRect(0, 0, 1280, 720), QRect(1150, 650, 160, 80), (1200, 600)),
            (QRect(-1280, -720, 1280, 720), QRect(-100, -100, 160, 80), (300, 4000)),
            (QRect(100, 100, 80, 60), QRect(150, 140, 160, 80), (300, 4000)),
        ):
            with self.subTest(available=available, dimensions=dimensions):
                self.screen.availableGeometry.return_value = available
                self.frame_rect = frame_rect
                self.frame_geometry.return_value = frame_rect
                self.worker.thumbnail_ready.emit(self.first.data(), self.thumbnail(*dimensions))

                self.assertTrue(available.contains(self.menu.preview.geometry()))
                pixmap = self.menu.preview.pixmap()
                self.assertLessEqual(pixmap.width(), min(300, available.width() - 8))
                self.assertLessEqual(pixmap.height(), available.height() - 8)
                self.assertAlmostEqual(pixmap.width(), pixmap.height() * dimensions[0] / dimensions[1], delta=1)


    def test_hidden_menu_or_changed_hover_does_not_request_thumbnail(self):
        """
        選單隱藏或懸停項目改變後不發出舊縮圖請求
        """

        self.menu.preview_action = self.first
        self.visible.return_value = False
        self.menu.show_preview()
        self.worker.request.assert_not_called()

        self.visible.return_value = True
        self.action_at.return_value = self.second
        self.menu.show_preview()
        self.worker.request.assert_not_called()


    def test_hiding_menu_resets_mode_timers_and_preview(self):
        """
        隱藏選單時重設預覽模式並停止計時器
        """

        self.activate_preview()
        self.menu.leave_preview()
        self.assertTrue(self.menu.reset_timer.isActive())

        self.menu.hideEvent(QHideEvent())

        self.assertFalse(self.menu.preview_mode)
        self.assertIsNone(self.menu.preview_action)
        self.assertFalse(self.menu.preview_timer.isActive())
        self.assertFalse(self.menu.reset_timer.isActive())
        self.assertTrue(self.menu.preview.isHidden())


if __name__ == "__main__":
    unittest.main()
