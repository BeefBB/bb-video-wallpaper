"""
桌布選單的懸停預覽, 縮圖顯示與視窗定位
媒體目錄, 縮圖目錄與載入圖片由呼叫端提供
"""

from pathlib import Path

from PySide6.QtWidgets import QApplication, QLabel, QMenu
from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QCursor, QImage, QPixmap

from thumbnails import ThumbnailThread


class MediaPreviewMenu(QMenu):
    """
    桌布選單的懸停預覽, 在背景驗證並載入對應縮圖
    """

    def __init__(
        self,
        title: str,
        parent=None,
        *,
        media_dir: Path,
        thumbnail_dir: Path,
        loading_image_path: Path
    ) -> None:

        super().__init__(title, parent)
        self.setMouseTracking(True)


        # 預覽視窗不搶焦點, 也不攔截滑鼠事件
        self.preview = QLabel(self, Qt.WindowType.ToolTip)
        self.preview.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.preview.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.preview.setStyleSheet("background: #202020; padding: 4px;")


        self.loading_pixmap = QPixmap(loading_image_path)

        if self.loading_pixmap.isNull():

            self.loading_pixmap = QPixmap(300, 169)
            self.loading_pixmap.fill(Qt.GlobalColor.darkGray)

        self.loading_pixmap = self.loading_pixmap.scaledToWidth(
            300, Qt.TransformationMode.SmoothTransformation
        )

        self.preview_pixmap = self.loading_pixmap
        self.preview_mode = False
        self.preview_action = None


        self.thumbnail_thread = ThumbnailThread(media_dir, thumbnail_dir, self)
        self.thumbnail_thread.thumbnail_ready.connect(self.update_preview)


        # 首次懸停 0.5 秒後顯示預覽
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(500)
        self.preview_timer.timeout.connect(self.show_preview)


        # 離開所有桌布選項滿 1 秒後退出預覽模式
        self.reset_timer = QTimer(self)
        self.reset_timer.setSingleShot(True)
        self.reset_timer.setInterval(1000)
        self.reset_timer.timeout.connect(self.reset_preview)


    def mouseMoveEvent(self, event) -> None:

        super().mouseMoveEvent(event)
        action = self.actionAt(event.position().toPoint())

        if (
            action is None or
            not action.isEnabled() or
            action.data() is None
        ):

            self.leave_preview()
            return


        self.reset_timer.stop()

        if action == self.preview_action:
            return

        self.preview_action = action

        if self.preview_mode:
            self.show_preview()

        else:
            self.preview_timer.start()


    def leaveEvent(self, event) -> None:

        self.leave_preview()
        super().leaveEvent(event)


    def hideEvent(self, event) -> None:

        self.reset_preview()
        super().hideEvent(event)


    def leave_preview(self) -> None:

        self.preview_timer.stop()
        self.preview_action = None
        self.preview.hide()

        if self.preview_mode and not self.reset_timer.isActive():
            self.reset_timer.start()


    def reset_preview(self) -> None:

        self.preview_timer.stop()
        self.reset_timer.stop()
        self.preview_action = None
        self.preview_mode = False
        self.preview.hide()


    def show_preview(self) -> None:

        action = self.preview_action
        cursor = QCursor.pos()

        if (
            not self.isVisible() or
            action is None or
            self.actionAt(self.mapFromGlobal(cursor)) != action
        ):
            return

        # 切換項目時先換成載入圖片, 不保留上一個項目的縮圖
        self.preview_pixmap = self.loading_pixmap
        self.display_preview()

        self.thumbnail_thread.request(Path(action.data()))


    @Slot(str, QImage)
    def update_preview(self, media_path: str, image: QImage) -> None:

        action = self.preview_action

        if (
            not self.preview_mode or
            action is None or
            Path(action.data()) != Path(media_path) or
            image.isNull()
        ):
            return

        # 背景只傳遞 QImage, QPixmap 在主執行緒建立
        self.preview_pixmap = QPixmap.fromImage(image)
        self.display_preview()


    def display_preview(self) -> None:

        action = self.preview_action
        cursor = QCursor.pos()

        if (
            not self.isVisible() or
            action is None or
            self.actionAt(self.mapFromGlobal(cursor)) != action or
            self.preview_pixmap.isNull()
        ):
            return


        # 等比例縮小預覽圖片, 避免超出目前螢幕的可用範圍
        screen = QApplication.screenAt(cursor) or self.screen()
        available = screen.availableGeometry()

        pixmap = self.preview_pixmap.scaled(
            min(300, max(1, available.width() - 8)),
            max(1, available.height() - 8),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )
        self.preview.setPixmap(pixmap)
        self.preview.adjustSize()


        # 優先放在選單右側, 空間不足時改放左側
        menu_rect = self.frameGeometry()
        x = menu_rect.right() + 8

        if x + self.preview.width() > available.right() + 1:
            x = menu_rect.left() - self.preview.width() - 8


        y = self.mapToGlobal(self.actionGeometry(action).topLeft()).y()
        x = max(available.left(), min(x, available.right() - self.preview.width() + 1))
        y = max(available.top(), min(y, available.bottom() - self.preview.height() + 1))


        self.preview.move(x, y)
        self.preview.show()
        self.preview_mode = True
