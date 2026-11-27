"""
Qt 圖片顯示模組, 接收主程式提供的 HWND 並共用既有 QApplication

在 Qt 主執行緒先建立 QApplication 與目標視窗

    import bb_image_display

    display = bb_image_display.Instance()
    display.set_hwnd(int(target_window.winId()))
    display.set_scaling("fit", "#202020")
    display.set_media("image.png")
    display.show()

預設為 fill 與黑色背景
"""

from ctypes import wintypes
from pathlib import Path
import weakref

import win32api
import win32con
import win32gui

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QRectF, Qt, Slot
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter, QPaintEvent, QRasterWindow, QWindow
from PySide6.QtWidgets import QApplication

from display_painting import draw_cover_image, fit_image_rect


__all__ = ["Instance"]


class _TargetEvents(QAbstractNativeEventFilter):
    """
    接收目標視窗的尺寸與銷毀事件
    """

    def __init__(self, window: "_ImageWindow") -> None:

        super().__init__()
        self._window = weakref.ref(window)


    def nativeEventFilter(self, event_type, message) -> tuple[bool, int]:

        window = self._window()

        if window is None or event_type != b"windows_generic_MSG":
            return False, 0

        msg = wintypes.MSG.from_address(int(message))

        if not window._target_hwnd or msg.hWnd != window._target_hwnd:
            return False, 0

        if msg.message in (win32con.WM_SIZE, win32con.WM_WINDOWPOSCHANGED):
            window._sync_geometry()

        elif msg.message == win32con.WM_DESTROY:
            window._release_host()

        return False, 0


class _ImageWindow(QRasterWindow):
    """
    繪製圖片與背景
    """

    def __init__(self, image_path: Path, target_hwnd: int) -> None:

        super().__init__()


        self._target_hwnd = target_hwnd
        self._host_hwnd = 0
        self._host_window: QWindow | None = None
        self._target_events: _TargetEvents | None = None
        self.scaling_mode = "fill"
        self.background_color = QColor(Qt.GlobalColor.black)
        self.background_image: QImage | None = None

        reader = QImageReader(str(image_path))
        reader.setAutoTransform(True)
        self.image = reader.read()

        if self.image.isNull():
            raise RuntimeError(f"無法讀取圖片: {reader.errorString()}")

        self.setTitle("Qt image display")

        self.setFlags(
            Qt.WindowType.SubWindow |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowDoesNotAcceptFocus |
            Qt.WindowType.WindowTransparentForInput
        )


    def set_hwnd(self, hwnd: int) -> None:
        """
        設定目標 HWND
        """

        if hwnd == self._target_hwnd:
            self._sync_geometry()
            return

        self._release_host()
        self._target_hwnd = hwnd


    def show(self) -> None:
        """
        顯示圖片視窗, 需要時建立原生容器
        """

        if not self._target_hwnd or not win32gui.IsWindow(self._target_hwnd):
            self._release_host()
            raise RuntimeError("必須先設定有效的目標視窗")

        if self._host_hwnd and not win32gui.IsWindow(self._host_hwnd):
            self._release_host()

        if not self._host_hwnd:
            self._create_host()

        try:

            super().show()

            # 每次顯示後確保是原生子視窗, 避免 Qt 保留 WS_POPUP
            hwnd = int(self.winId())
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE) & 0xFFFFFFFF

            win32gui.SetWindowLong(
                hwnd,
                win32con.GWL_STYLE,
                (style & ~win32con.WS_POPUP) | win32con.WS_CHILD
            )
            win32gui.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                win32con.SWP_FRAMECHANGED |
                win32con.SWP_NOMOVE |
                win32con.SWP_NOSIZE |
                win32con.SWP_NOZORDER |
                win32con.SWP_NOACTIVATE
            )

            win32gui.ShowWindow(self._host_hwnd, win32con.SW_SHOWNOACTIVATE)
            self._sync_geometry()

        except Exception:
            self._release_host()
            raise


    def hide(self) -> None:
        """
        隱藏圖片視窗與原生容器
        """

        if self._host_hwnd and win32gui.IsWindow(self._host_hwnd):
            win32gui.ShowWindow(self._host_hwnd, win32con.SW_HIDE)

        super().hide()


    def close(self) -> bool:
        """
        關閉圖片視窗並釋放原生容器
        """

        try:
            return super().close()

        finally:
            self._release_host()


    def paintEvent(self, event: QPaintEvent) -> None:

        if self.width() <= 0 or self.height() <= 0:
            return

        bounds = QRectF(0, 0, self.width(), self.height())
        painter = QPainter(self)
        painter.fillRect(bounds, self.background_color)
        if self.scaling_mode == "fill":
            draw_cover_image(painter, bounds, self.image)
            return

        source = QRectF(self.image.rect())
        target = QRectF(bounds)
        if self.scaling_mode == "fit":
            if self.background_image is not None:
                draw_cover_image(painter, bounds, self.background_image)
            target = fit_image_rect(source.size(), bounds)

        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(target, self.image, source)


    def _create_host(self) -> None:
        """
        在目標視窗內建立原生容器, 處理父子視窗裁切與合成
        """

        app = QApplication.instance()

        if app is None:
            raise RuntimeError("必須先建立 QApplication")

        if not self._target_hwnd or not win32gui.IsWindow(self._target_hwnd):
            raise RuntimeError("必須先設定有效的目標視窗")

        self.create()
        left, top, right, bottom = win32gui.GetClientRect(self._target_hwnd)

        try:

            self._host_hwnd = win32gui.CreateWindowEx(
                win32con.WS_EX_LAYERED |
                win32con.WS_EX_TRANSPARENT |
                win32con.WS_EX_NOACTIVATE |
                win32con.WS_EX_TOOLWINDOW,
                "Static",
                "",
                win32con.WS_CHILD | win32con.WS_CLIPCHILDREN | win32con.WS_CLIPSIBLINGS,
                0, 0, right - left, bottom - top,
                self._target_hwnd,
                None,
                win32api.GetModuleHandle(None),
                None
            )

            parent = win32gui.GetAncestor(self._host_hwnd, win32con.GA_PARENT)

            if parent != self._target_hwnd:
                raise RuntimeError("圖片容器未掛載到指定視窗")

            # 保持完全不透明, 由 Windows 重導向並合成容器內的繪圖
            win32gui.SetLayeredWindowAttributes(self._host_hwnd, 0, 255, win32con.LWA_ALPHA)

            # 以原生容器作為 Qt 視窗的父視窗
            self._host_window = QWindow.fromWinId(self._host_hwnd)

            if self._host_window is None:
                raise RuntimeError("Qt 無法連接圖片容器")

            self.setParent(self._host_window)
            self.setPosition(0, 0)
            self._sync_geometry()

            self._target_events = _TargetEvents(self)
            app.installNativeEventFilter(self._target_events)

        except Exception:
            self._release_host()
            raise


    def _sync_geometry(self) -> None:
        """
        依目標視窗的客戶區同步原生容器與 Qt 視窗尺寸
        """

        if (
            not self._host_hwnd or not win32gui.IsWindow(self._host_hwnd) or
            not self._target_hwnd or not win32gui.IsWindow(self._target_hwnd)
        ):
            return

        left, top, right, bottom = win32gui.GetClientRect(self._target_hwnd)
        width, height = right - left, bottom - top

        win32gui.SetWindowPos(
            self._host_hwnd, 0, 0, 0, width, height,
            win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
        )

        ratio = self.devicePixelRatio()
        self.resize(round(width / ratio), round(height / ratio))
        self.update()


    def _release_host(self) -> None:
        """
        釋放原生容器
        """

        if self._target_events is not None:

            app = QApplication.instance()

            if app is not None:
                app.removeNativeEventFilter(self._target_events)

            self._target_events = None

        try:

            self.hide()
            self.setParent(None)
            self.destroy()

        finally:

            self._host_window = None

            hwnd = self._host_hwnd
            self._host_hwnd = 0

            if hwnd and win32gui.IsWindow(hwnd):
                win32gui.DestroyWindow(hwnd)


class Instance(QObject):
    """
    在 Qt 主執行緒使用, 將圖片輸出到主程式指定的 HWND
    建立物件時不讀圖或建立視窗, 目標視窗由主程式管理
    """

    def __init__(self) -> None:

        app = QApplication.instance()

        if app is None:
            raise RuntimeError("必須先建立 QApplication")

        super().__init__(app)

        self.window: _ImageWindow | None = None
        self._hwnd = 0
        self._visible = False
        self.current_image: Path | None = None
        self._scaling_mode = "fill"
        self._background_color = QColor(Qt.GlobalColor.black)
        self._background_image: QImage | None = None

        app.aboutToQuit.connect(self.close)


    def set_hwnd(self, hwnd: int) -> None:
        """
        設定目標 HWND, 0 表示解除掛載
        """

        hwnd = int(hwnd)
        if hwnd and not win32gui.IsWindow(hwnd):
            raise ValueError("無效的目標視窗控制代碼")

        self._hwnd = hwnd

        if self.window is not None:
            self.window.set_hwnd(hwnd)
            if hwnd and self._visible and not self.window.isVisible():
                self.window.show()


    def get_hwnd(self) -> int:
        """
        取得目標 HWND
        """

        return self._hwnd


    def set_media(self, path: Path | str) -> None:
        """
        載入圖片, 讀取失敗時保留原本的圖片與顯示狀態
        """

        source = Path(path).resolve()

        if self.window is None:
            self.window = _ImageWindow(source, self._hwnd)
            self.window.background_image = self._background_image

        else:

            reader = QImageReader(str(source))
            reader.setAutoTransform(True)
            image = reader.read()

            if image.isNull():
                raise RuntimeError(f"無法讀取圖片: {reader.errorString()}")

            self.window.image = image

        self.current_image = source
        self.set_scaling(self._scaling_mode, self._background_color)


    def set_scaling(self, mode: str, background_color: QColor | str = "#000000") -> None:
        """
        設定縮放模式(fill/fit/stretch)及不透明背景色
        """

        if mode not in ("fill", "fit", "stretch"):
            raise ValueError("無效的縮放模式")

        color = QColor(background_color)
        if not color.isValid() or color.alpha() != 255:
            raise ValueError("背景色必須是有效的不透明顏色")

        self._scaling_mode = mode
        self._background_color = color

        if self.window is not None:
            self.window.scaling_mode = mode
            self.window.background_color = QColor(color)
            self.window.update()


    def set_background_image(self, image: QImage | None) -> None:

        self._background_image = image
        if self.window is not None:
            self.window.background_image = image
            self.window.update()


    @Slot()
    def show(self) -> None:
        """
        顯示輸出
        """

        if self.window is None:
            raise RuntimeError("尚未設定圖片")

        try:
            if self._hwnd and win32gui.IsWindow(self._hwnd):
                self.window.show()

        except (RuntimeError, win32gui.error):
            # 目標視窗可能在檢查後被銷毀
            if win32gui.IsWindow(self._hwnd):
                raise

        self._visible = True


    @Slot()
    def hide(self) -> None:
        """
        隱藏輸出
        """

        self._visible = False
        if self.window is not None:
            self.window.hide()


    @Slot()
    def close(self) -> None:
        """
        釋放資源, 保留目標 HWND 與顯示選項
        """

        try:
            if self.window is not None:
                self.window.close()

        finally:
            self.window = None
            self.current_image = None
            self._visible = False
