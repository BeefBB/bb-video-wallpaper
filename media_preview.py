"""
桌布選單的懸停預覽, 縮圖顯示與視窗定位
媒體目錄, 縮圖目錄與載入圖片由呼叫端提供
"""

from pathlib import Path

from PySide6.QtWidgets import QApplication, QLabel, QMenu, QToolTip, QWidget
from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QPropertyAnimation, QRectF, QSize, Qt, QTimer, Slot
from PySide6.QtGui import QAction, QColor, QCursor, QImage, QPainter, QPixmap, QScreen

from config import get_scaling_mode
from display_painting import draw_cover_image, fit_image_rect
from thumbnails import DEFAULT_RAY_DECAY, ThumbnailThread, get_ray_mode
from windows_integration import screen_pixel_size


class MediaPreviewMenu(QMenu):
    """
    桌布選單的懸停預覽, 在背景驗證並載入對應縮圖
    """

    def __init__(
        self,
        title: str,
        parent: QWidget | None = None,
        *,
        media_dir: Path,
        thumbnail_dir: Path,
        loading_image_path: Path,
        preview_size: int = 3,
        preview_delay_ms: int = 1000,
        preview_reset_delay_ms: int = 2000,
        preview_animation_duration_ms: int = 140,
        thumbnail_thread: ThumbnailThread | None = None
    ) -> None:

        super().__init__(title, parent)
        self.setMouseTracking(True)
        self.target_screen: QScreen | None = None
        self.media_dir = media_dir
        self.display_config: dict = {}

        # 預覽視窗不搶焦點, 不攔截滑鼠事件
        self.preview = QLabel(self, Qt.WindowType.ToolTip)
        self.preview.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.preview.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.preview.setStyleSheet("background: #202020; padding: 4px;")

        self.preview_animation = QPropertyAnimation(self.preview, b"pos", self)
        self.preview_animation.setDuration(preview_animation_duration_ms)
        self.preview_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

        self.loading_image_path = loading_image_path
        self.preview_mode = False
        self.preview_action: QAction | None = None
        self.blurred_thumbnail: QImage | None = None
        self.ray_thumbnail: QImage | None = None

        self.thumbnail_thread: ThumbnailThread = (
            thumbnail_thread if thumbnail_thread is not None
            else ThumbnailThread(media_dir, thumbnail_dir, self)
        )
        self.thumbnail_thread.thumbnail_ready.connect(self.update_preview)
        self.thumbnail_thread.blurred_thumbnail_ready.connect(self.update_blurred_preview)
        self.thumbnail_thread.ray_thumbnail_ready.connect(self.update_ray_preview)

        # 首次懸停達設定時間後顯示預覽
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(preview_delay_ms)
        self.preview_timer.timeout.connect(self.show_preview)

        # 離開所有桌布選項達設定時間後退出預覽模式
        self.reset_timer = QTimer(self)
        self.reset_timer.setSingleShot(True)
        self.reset_timer.setInterval(preview_reset_delay_ms)
        self.reset_timer.timeout.connect(self.reset_preview)

        self.set_preview_size(preview_size)


    def set_preview_size(self, preview_size: int) -> None:
        """
        套用尺寸與對應載入圖片, 清除先前的預覽狀態
        """

        self.reset_preview()
        self.preview_size = preview_size

        if preview_size == 0:
            return

        width = preview_size * 100
        self.loading_pixmap = QPixmap(
            self.loading_image_path.with_name(f"loading{width}.jpg")
        )

        if self.loading_pixmap.isNull():

            self.loading_pixmap = QPixmap(width, max(1, round(width * 9 / 16)))
            self.loading_pixmap.fill(Qt.GlobalColor.darkGray)

        self.loading_pixmap = self.loading_pixmap.scaledToWidth(
            width, Qt.TransformationMode.SmoothTransformation
        )
        self.preview_pixmap = self.loading_pixmap


    def event(self, event) -> bool:

        if event.type() == QEvent.Type.ToolTip:

            action = self.actionAt(event.pos())
            # 空提示會回退到項目文字, 因此直接略過未截斷項目的提示事件
            if action is None or action.data() is None or action.text() == action.toolTip():
                QToolTip.hideText()
                return True

        return super().event(event)


    def mouseMoveEvent(self, event) -> None:

        super().mouseMoveEvent(event)

        if self.preview_size == 0:
            return

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
        self.preview_animation.stop()
        self.preview_action = None
        self.preview.hide()

        if self.preview_mode and not self.reset_timer.isActive():
            self.reset_timer.start()


    def reset_preview(self) -> None:

        self.preview_timer.stop()
        self.reset_timer.stop()
        self.preview_animation.stop()
        self.preview_action = None
        self.blurred_thumbnail = None
        self.ray_thumbnail = None
        self.preview_mode = False
        self.preview.hide()


    def _display_options(self, path: Path, screen: QScreen) -> tuple[str, str | None]:

        media_path = path.relative_to(self.media_dir).as_posix()
        geometry = screen.geometry()
        orientation = "landscape" if geometry.width() >= geometry.height() else "portrait"
        mode = get_scaling_mode(self.display_config, media_path, orientation)
        return mode, self.display_config.get("backgroundColor", {}).get(media_path)


    def show_preview(self) -> None:

        action = self.preview_action
        cursor = QCursor.pos()

        if (
            self.preview_size == 0 or
            not self.isVisible() or
            action is None or
            self.actionAt(self.mapFromGlobal(cursor)) != action
        ):
            return

        # 切換項目時先換成載入圖片, 不保留上一個項目的縮圖
        self.blurred_thumbnail = None
        self.ray_thumbnail = None
        self.preview_pixmap = self.loading_pixmap
        self.display_preview()

        path = Path(action.data())
        screen = self.target_screen or QApplication.screenAt(cursor) or self.screen()
        mode, background = self._display_options(path, screen)
        ray_mode = get_ray_mode(path, background)
        if mode == "fit" and ray_mode is not None:
            self.thumbnail_thread.request(
                path, "thumbnail_ray", preview_size=self.preview_size,
                screen_size=screen_pixel_size(screen).toTuple(), ray_mode=ray_mode, priority=True
            )
        elif mode == "fit" and background == "blur":
            self.thumbnail_thread.request(
                path, "thumbnail_orig", "thumbnail_blur", preview_size=self.preview_size, priority=True
            )
        else:
            self.thumbnail_thread.request(path, "thumbnail_orig", preview_size=self.preview_size, priority=True)


    def _matches_preview(self, media_path: str, preview_size: int) -> bool:

        action = self.preview_action
        return (
            self.preview_mode and action is not None and preview_size == self.preview_size and
            Path(action.data()) == Path(media_path)
        )


    @Slot(str, int, QImage)
    def update_preview(self, media_path: str, preview_size: int, image: QImage) -> None:

        if image.isNull() or not self._matches_preview(media_path, preview_size):
            return

        # 背景只傳遞 QImage, QPixmap 在主執行緒建立
        self.preview_pixmap = QPixmap.fromImage(image)
        self.display_preview()


    @Slot(str, int, QImage)
    def update_blurred_preview(self, media_path: str, preview_size: int, image: QImage) -> None:

        if image.isNull() or not self._matches_preview(media_path, preview_size):
            return

        self.blurred_thumbnail = image
        self.display_preview()


    @Slot(str, int, QSize, str, int, QImage)
    def update_ray_preview(
        self, media_path: str, preview_size: int, screen_size: QSize, ray_mode: str, decay: int, image: QImage
    ) -> None:

        if decay != DEFAULT_RAY_DECAY or image.isNull() or not self._matches_preview(media_path, preview_size):
            return

        path = Path(media_path)
        screen = self.target_screen or QApplication.screenAt(QCursor.pos()) or self.screen()
        mode, background = self._display_options(path, screen)
        if mode == "fit" and get_ray_mode(path, background) == ray_mode and screen_pixel_size(screen) == screen_size:
            self.ray_thumbnail = image
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

        # 比例依目標螢幕, 尺寸與位置限制依選單所在螢幕
        screen = QApplication.screenAt(cursor) or self.screen()
        available = screen.availableGeometry()
        preview_width = self.preview_size * 100

        target_screen = self.target_screen or screen
        size = target_screen.geometry().size().scaled(
            min(preview_width, max(1, available.width() - 8)),
            min(preview_width, max(1, available.height() - 8)),
            Qt.AspectRatioMode.KeepAspectRatio
        )
        size = QSize(max(1, size.width()), max(1, size.height()))

        mode, background = "fill", None

        if self.ray_thumbnail is not None or self.preview_pixmap.cacheKey() != self.loading_pixmap.cacheKey():

            mode, background = self._display_options(Path(action.data()), target_screen)
            if mode == "fit" and background is None:
                average = self.preview_pixmap.toImage().scaled(
                    1, 1, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
                )
                background = average.pixelColor(0, 0).name()

        source = QRectF(self.preview_pixmap.rect())
        bounds = QRectF(0, 0, size.width(), size.height())
        destination = QRectF(bounds)

        if mode == "fill":
            # 直接繪製置中裁切範圍, 避免先放大整張極寬或極高的縮圖
            scale = max(size.width() / source.width(), size.height() / source.height())
            width, height = size.width() / scale, size.height() / scale
            source = QRectF((source.width() - width) / 2, (source.height() - height) / 2, width, height)

        elif mode == "fit":
            destination = fit_image_rect(source.size(), bounds)

        pixmap = QPixmap(size)
        pixmap.fill(QColor("#000000" if background in (None, "blur", "ray1", "ray2") else background))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if mode == "fit" and background in ("ray1", "ray2") and self.ray_thumbnail is not None:
            # 光暈縮圖已合成原圖, 直接繪製整張縮圖
            painter.drawImage(bounds, self.ray_thumbnail, QRectF(self.ray_thumbnail.rect()))
        else:
            if mode == "fit" and background == "blur" and self.blurred_thumbnail is not None:
                draw_cover_image(painter, bounds, self.blurred_thumbnail)
            painter.drawPixmap(destination, self.preview_pixmap, source)
        painter.end()

        self.preview.setPixmap(pixmap)
        self.preview.setFixedSize(size + QSize(8, 8))

        # 優先放在選單左側, 空間不足時改放右側
        menu_rect = self.frameGeometry()
        x = menu_rect.left() - self.preview.width() - 8

        if x < available.left():
            x = menu_rect.right() + 8

        y = self.mapToGlobal(self.actionGeometry(action).topLeft()).y()
        x = max(available.left(), min(x, available.right() - self.preview.width() + 1))
        y = max(available.top(), min(y, available.bottom() - self.preview.height() + 1))

        position = QPoint(x, y)
        appearing = not self.preview.isVisible()

        if appearing:

            self.preview_animation.stop()
            offset = 16 if x < menu_rect.left() else -16
            start_x = max(available.left(), min(x + offset, available.right() - self.preview.width() + 1))
            self.preview.move(start_x, y)

        # 同一位置的縮圖更新不重播動畫, 換項目時從目前位置接續
        if appearing or self.preview_animation.endValue() != position:

            self.preview_animation.stop()
            self.preview_animation.setStartValue(self.preview.pos())
            self.preview_animation.setEndValue(position)
            self.preview_animation.start()

        self.preview.show()
        self.preview_mode = True
