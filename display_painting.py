from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter


def draw_cover_image(painter: QPainter, bounds: QRectF, image: QImage) -> None:
    """
    只繪製置中裁切的來源範圍, 避免建立放大後的整張圖片
    """

    if bounds.isEmpty() or image.isNull():
        return

    source = QRectF(image.rect())
    crop = QRectF(source)
    crop.setSize(bounds.size().scaled(source.size(), Qt.AspectRatioMode.KeepAspectRatio))
    crop.moveCenter(source.center())
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.drawImage(bounds, image, crop)
