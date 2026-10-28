"""
Qt 圖片顯示模組, 接收主程式提供的 HWND 並共用既有 QApplication

在 Qt 主執行緒先建立 QApplication 與目標視窗, 再使用:
    import bb_image_display

    display = bb_image_display.Instance()
    display.set_hwnd(int(target_window.winId()))
    display.set_image("image.png")
    display.show()

主程式負責目標視窗的建立, 顯示, 桌面掛載與 Qt 事件迴圈
模組只管理圖片及其內部子視窗
"""

from ctypes import wintypes
from pathlib import Path
import weakref

import win32api
import win32con
import win32gui

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QRectF, Qt, Slot
from PySide6.QtGui import QImageReader, QPainter, QPaintEvent, QRasterWindow, QWindow
from PySide6.QtWidgets import QApplication


__all__ = ["Instance"]


class _TargetEvents(QAbstractNativeEventFilter):
    """
    接收目標視窗的尺寸與銷毀事件, 不使用輪詢計時器
    """

    def __init__(self, window: "ImageWallpaper") -> None:

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


class ImageWallpaper(QRasterWindow):
    """
    在指定視窗內等比例裁切顯示圖片, 自行管理繪圖所需的內部容器
    """

    def __init__(self, image_path: Path, target_hwnd: int) -> None:

        super().__init__()


        self._target_hwnd = target_hwnd
        self._host_hwnd = 0
        self._host_window: QWindow | None = None
        self._target_events: _TargetEvents | None = None

        reader = QImageReader(str(image_path))
        reader.setAutoTransform(True)
        self.image = reader.read()

        if self.image.isNull():
            raise RuntimeError(f"無法讀取圖片: {reader.errorString()}")


        self.paint_count = 0
        self.setTitle("Qt image display")

        # 以 QWindow 管理外部父視窗, 直接接收繪圖事件
        self.setFlags(
            Qt.WindowType.SubWindow |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowDoesNotAcceptFocus |
            Qt.WindowType.WindowTransparentForInput
        )


    def _create_host(self) -> None:
        """
        在目標視窗內建立圖片容器, 處理父子視窗裁切與合成
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
        依目標視窗的客戶區與縮放比例更新圖片尺寸
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


    def set_hwnd(self, hwnd: int) -> None:
        """
        改用新的目標視窗, 保留圖片與原本的顯示狀態
        """

        if hwnd == self._target_hwnd:
            self._sync_geometry()
            return

        visible = self.isVisible()
        self._release_host()
        self._target_hwnd = hwnd

        if visible and hwnd:
            self.show()


    def show(self) -> None:
        """
        在指定視窗中顯示圖片, 第一次顯示時才建立內部容器
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
        隱藏圖片與內部容器, 保留主程式目標視窗的顯示狀態
        """

        if self._host_hwnd and win32gui.IsWindow(self._host_hwnd):
            win32gui.ShowWindow(self._host_hwnd, win32con.SW_HIDE)

        super().hide()


    def is_visible(self) -> bool:

        return bool(
            self.isVisible() and
            self._host_hwnd and
            win32gui.IsWindowVisible(self._host_hwnd)
        )


    def _release_host(self) -> None:
        """
        釋放內部原生視窗, 保留圖片及主程式提供的視窗
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


    def close(self) -> bool:
        """
        關閉圖片視窗並釋放內部容器, 不關閉目標視窗
        """

        try:
            return super().close()

        finally:
            self._release_host()


    def paintEvent(self, event: QPaintEvent) -> None:

        if self.width() <= 0 or self.height() <= 0:
            return

        self.paint_count += 1


        # 等比例置中裁切填滿, 僅在 Qt 要求重繪時繪製
        scale = max(
            self.width() / self.image.width(),
            self.height() / self.image.height()
        )
        width = self.width() / scale
        height = self.height() / scale

        source = QRectF(
            (self.image.width() - width) / 2,
            (self.image.height() - height) / 2,
            width,
            height
        )


        target = QRectF(0, 0, self.width(), self.height())

        painter = QPainter(self)
        painter.fillRect(target, Qt.GlobalColor.black)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawImage(target, self.image, source)


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


        self.window: ImageWallpaper | None = None
        self._hwnd = 0
        self.current_image: Path | None = None

        app.aboutToQuit.connect(self.close)


    def set_hwnd(self, hwnd: int) -> None:
        """
        設定主程式提供的視窗控制代碼, 0 表示解除目標視窗
        """

        hwnd = int(hwnd)

        if hwnd and not win32gui.IsWindow(hwnd):
            raise ValueError("無效的目標視窗控制代碼")

        self._hwnd = hwnd

        if self.window is not None:
            self.window.set_hwnd(hwnd)


    def get_hwnd(self) -> int:

        return self._hwnd


    def set_image(self, path: Path | str) -> None:
        """
        載入圖片, 讀取失敗時保留原本的圖片與顯示狀態
        """

        source = Path(path).resolve()

        if self.window is None:
            self.window = ImageWallpaper(source, self._hwnd)

        else:

            reader = QImageReader(str(source))
            reader.setAutoTransform(True)
            image = reader.read()

            if image.isNull():
                raise RuntimeError(f"無法讀取圖片: {reader.errorString()}")

            self.window.image = image
            self.window.update()


        self.current_image = source


    @Slot()
    def show(self) -> None:
        """
        在指定視窗顯示已載入的圖片, 由主程式管理目標視窗及桌面掛載
        """

        if self.window is None:
            raise RuntimeError("尚未設定圖片")

        self.window.show()


    @Slot()
    def hide(self) -> None:
        """
        隱藏圖片輸出, 保留已載入的圖片與主程式提供的視窗
        """

        if self.window is not None:
            self.window.hide()


    def is_visible(self) -> bool:

        return bool(
            self.window is not None and
            self.window.is_visible()
        )


    @Slot()
    def close(self) -> None:
        """
        釋放圖片及內部容器, 保留目標 HWND 供重新載入圖片
        """

        try:

            if self.window is not None:
                self.window.close()

        finally:

            self.window = None
            self.current_image = None
