"""
使用隱藏視窗驗證 WorkerW 掛載座標, 備援容器與圖片輸出的原生生命週期
只操作測試視窗, 不變更使用者桌面
"""

from pathlib import Path
import tempfile
import sys
import unittest
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QWidget


ROOT = Path(__file__).resolve().parents[1]

# 直接執行測試檔時, 將專案根目錄加入模組搜尋路徑
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from app_loader import QtTestCase
import bb_image_display
import win32con
import win32gui
import windows_integration as windows


class WorkerWNativeTests(QtTestCase):
    """
    使用隱藏父視窗驗證真正的 WorkerW 與桌布掛載
    """

    def test_wallpaper_keeps_screen_geometry_with_offset_parent(self) -> None:
        """
        驗證正負原點與非客戶區偏移下, 掛載及重複掛載保留桌布的位置與尺寸
        """

        for origin in ((-800, -200), (800, 200)):
            for legacy in (False, True):
                with self.subTest(origin=origin, legacy=legacy):
                    parent = win32gui.CreateWindowEx(
                        0, "Static", "", win32con.WS_OVERLAPPEDWINDOW,
                        *origin, 640, 480, 0, 0, windows.win32api.GetModuleHandle(None), None
                    )
                    wallpaper = QWidget()

                    try:
                        icon_layer = win32gui.CreateWindowEx(
                            0, "Static", "", win32con.WS_CHILD,
                            0, 0, 640, 480, parent, 0, windows.win32api.GetModuleHandle(None), None
                        )
                        wallpaper.setWindowFlags(
                            Qt.WindowType.FramelessWindowHint |
                            Qt.WindowType.Tool |
                            Qt.WindowType.WindowDoesNotAcceptFocus
                        )
                        wallpaper.setGeometry(-10000, -10000, 320, 180)
                        hwnd = int(wallpaper.winId())
                        expected = win32gui.GetWindowRect(hwnd)

                        for _ in range(2):
                            found = [parent, icon_layer, parent] if legacy else [0, parent]
                            # 只替換桌面搜尋與訊息, 掛載和座標操作使用真正的 Windows API
                            with (
                                mock.patch.object(win32gui, "FindWindow", return_value=parent),
                                mock.patch.object(win32gui, "FindWindowEx", side_effect=found),
                                mock.patch.object(win32gui, "SendMessageTimeout"),
                            ):
                                windows.set_windows_as_wallpaper(hwnd)

                            self.assertEqual(win32gui.GetAncestor(hwnd, win32con.GA_PARENT), parent)
                            self.assertEqual(win32gui.GetWindowRect(hwnd), expected)
                            wallpaper.show()
                            self.app.processEvents()
                            self.assertEqual(win32gui.GetWindowRect(hwnd), expected)
                            self.assertFalse(win32gui.IsWindowVisible(parent))

                    finally:
                        wallpaper.destroy()
                        wallpaper.deleteLater()
                        if win32gui.IsWindow(parent):
                            win32gui.DestroyWindow(parent)


    def test_created_workerw_is_opaque_and_class_can_be_reused(self) -> None:
        """
        驗證連續建立的 WorkerW 保持隱藏且完全不透明, 並可重用視窗類別
        """

        # 父視窗保持隱藏, 不接觸 Explorer 或使用者桌面
        parent = windows.win32gui.CreateWindowEx(
            0, "Static", "", windows.win32con.WS_OVERLAPPED,
            0, 0, 320, 240, 0, 0, windows.win32api.GetModuleHandle(None), None
        )

        try:

            for _ in range(2):
                hwnd = windows.create_workerw(parent)

                try:
                    self.assertEqual(windows.win32gui.GetAncestor(hwnd, windows.win32con.GA_PARENT), parent)
                    _, alpha, flags = windows.win32gui.GetLayeredWindowAttributes(hwnd)
                    self.assertEqual(alpha, 255)
                    self.assertTrue(flags & windows.win32con.LWA_ALPHA)
                    self.assertFalse(windows.win32gui.IsWindowVisible(hwnd))

                finally:
                    windows.win32gui.DestroyWindow(hwnd)

        finally:
            windows.win32gui.DestroyWindow(parent)


@unittest.skipUnless(sys.platform == "win32", "圖片原生視窗測試需要 Windows")
class ImageDisplayTests(QtTestCase):
    """
    圖片輸出的原生視窗生命週期
    """

    def setUp(self) -> None:
        """
        建立暫存圖片與隱藏的目標視窗
        """

        super().setUp()

        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.image_path = Path(directory) / "測試圖片.png"
        image = QImage(120, 80, QImage.Format.Format_RGB32)
        image.fill(QColor("red"))
        self.assertTrue(image.save(str(self.image_path)))

        self.targets: list[QWidget] = []
        self.target = self.create_target()
        self.display = bb_image_display.Instance()
        self.addCleanup(self.dispose_windows)
        self.display.set_hwnd(int(self.target.winId()))


    def create_target(self) -> "QWidget":
        """
        建立隱藏的圖片輸出目標
        """

        target = QWidget()
        target.resize(240, 135)
        self.targets.append(target)
        return target


    def dispose_windows(self) -> None:
        """
        釋放圖片輸出與測試用目標視窗
        """

        self.display.close()
        self.display.deleteLater()

        for target in self.targets:
            target.close()
            target.deleteLater()



    def test_native_host_lifecycle_preserves_image_and_external_targets(self) -> None:
        """
        驗證圖片原生容器的顯示, 重綁與釋放, 保留圖片與外部目標視窗
        """

        self.display.set_media(self.image_path)
        window = self.display.window
        assert window is not None
        self.assertEqual(window._host_hwnd, 0)
        self.display.show()
        host = window._host_hwnd
        image_key = window.image.cacheKey()
        self.assertEqual(win32gui.GetParent(host), int(self.target.winId()))
        style = win32gui.GetWindowLong(int(window.winId()), win32con.GWL_STYLE)
        self.assertTrue(style & win32con.WS_CHILD)
        self.assertFalse(style & win32con.WS_POPUP)

        # 解除綁定後可更換目標, 保留圖片與外部視窗
        self.display.set_hwnd(0)
        self.assertFalse(win32gui.IsWindow(host))
        self.assertFalse(window.isVisible())
        replacement = self.create_target()
        target_hwnd = int(replacement.winId())
        self.display.set_hwnd(target_hwnd)
        self.assertTrue(window.isVisible())
        self.assertIs(self.display.window, window)
        self.assertEqual(window.image.cacheKey(), image_key)
        self.assertEqual(win32gui.GetParent(window._host_hwnd), target_hwnd)
        self.assertTrue(win32gui.IsWindow(int(self.target.winId())))

        self.display.hide()
        self.display.set_hwnd(0)
        self.display.set_hwnd(target_hwnd)
        self.assertFalse(window.isVisible())
        self.assertEqual(window.image.cacheKey(), image_key)

        self.display.show()
        host = window._host_hwnd
        # 關閉時釋放圖片容器並保留目標供下次使用
        self.display.close()
        self.assertFalse(win32gui.IsWindow(host))
        self.assertTrue(win32gui.IsWindow(target_hwnd))
        self.assertEqual(self.display.get_hwnd(), target_hwnd)
        self.assertIsNone(self.display.window)
        self.assertIsNone(self.display.current_image)
        self.display.set_media(self.image_path)
        window = self.display.window
        assert window is not None
        self.display.set_hwnd(target_hwnd)
        self.assertFalse(window.isVisible())
        self.display.show()
        self.assertEqual(win32gui.GetParent(window._host_hwnd), target_hwnd)


if __name__ == "__main__":
    unittest.main()
