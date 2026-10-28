"""
使用隱藏的 Qt 目標視窗驗證圖片輸出, 不掛載桌布或修改使用者設定
"""

from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]

# 直接執行測試檔時, 將專案根目錄加入模組搜尋路徑
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))


if sys.platform == "win32":
    import win32con
    import win32gui

    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QApplication, QWidget

    import bb_image_display


@unittest.skipUnless(sys.platform == "win32", "圖片原生視窗測試需要 Windows")
class ImageDisplayTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:

        app = QApplication.instance() or QApplication([])

        if not isinstance(app, QApplication):
            raise unittest.SkipTest("圖片原生視窗測試需要 QApplication")

        cls.app = app


    def setUp(self) -> None:

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

        target = QWidget()
        target.resize(240, 135)
        self.targets.append(target)
        return target


    def dispose_windows(self) -> None:

        self.display.close()
        self.display.deleteLater()

        for target in self.targets:
            target.close()
            target.deleteLater()

        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


    def test_show_creates_child_and_hide_then_show_reuses_it(self):

        self.display.set_image(self.image_path)
        window = self.display.window
        assert window is not None

        self.assertEqual(window._host_hwnd, 0)
        self.display.show()

        host = window._host_hwnd
        self.assertEqual(win32gui.GetParent(host), int(self.target.winId()))
        style = win32gui.GetWindowLong(int(window.winId()), win32con.GWL_STYLE)
        self.assertTrue(style & win32con.WS_CHILD)
        self.assertFalse(style & win32con.WS_POPUP)

        self.display.hide()
        self.assertFalse(window.isVisible())
        self.assertTrue(win32gui.IsWindow(host))
        self.display.show()
        self.assertTrue(window.isVisible())
        self.assertEqual(window._host_hwnd, host)


    def test_bad_image_preserves_existing_image_window_and_visibility(self):

        self.display.set_image(self.image_path)
        self.display.show()
        window = self.display.window
        assert window is not None

        image_key = window.image.cacheKey()
        host = window._host_hwnd
        invalid_path = self.image_path.with_name("bad.png")
        invalid_path.write_bytes(b"invalid image")

        with self.assertRaises(RuntimeError):
            self.display.set_image(invalid_path)

        self.assertIs(self.display.window, window)
        self.assertEqual(self.display.current_image, self.image_path.resolve())
        self.assertEqual(window.image.cacheKey(), image_key)
        self.assertEqual(window._host_hwnd, host)
        self.assertTrue(window.isVisible())


    def test_detach_and_rebind_preserves_image_and_target_windows(self):

        self.display.set_image(self.image_path)
        self.display.show()
        window = self.display.window
        assert window is not None

        image_key = window.image.cacheKey()
        old_host = window._host_hwnd

        self.display.set_hwnd(0)
        self.assertFalse(win32gui.IsWindow(old_host))
        self.assertFalse(window.isVisible())
        self.assertEqual(window.image.cacheKey(), image_key)

        replacement = self.create_target()
        self.display.set_hwnd(int(replacement.winId()))
        self.display.show()

        self.assertIs(self.display.window, window)
        self.assertEqual(window.image.cacheKey(), image_key)
        self.assertEqual(win32gui.GetParent(window._host_hwnd), int(replacement.winId()))
        self.assertTrue(win32gui.IsWindow(int(self.target.winId())))
        self.assertTrue(window.isVisible())


    def test_target_resize_updates_child_geometry(self):

        self.display.set_image(self.image_path)
        self.display.show()
        window = self.display.window
        assert window is not None

        win32gui.SetWindowPos(
            int(self.target.winId()), 0, 0, 0, 320, 180,
            win32con.SWP_NOMOVE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE,
        )
        self.app.processEvents()

        self.assertEqual(
            win32gui.GetClientRect(window._host_hwnd),
            win32gui.GetClientRect(int(self.target.winId())),
        )


    def test_close_releases_image_host_and_keeps_target_for_next_image(self):

        self.display.set_image(self.image_path)
        self.display.show()
        window = self.display.window
        assert window is not None

        host = window._host_hwnd
        target_hwnd = int(self.target.winId())
        self.display.close()

        self.assertFalse(win32gui.IsWindow(host))
        self.assertTrue(win32gui.IsWindow(target_hwnd))
        self.assertEqual(self.display.get_hwnd(), target_hwnd)
        self.assertIsNone(self.display.window)
        self.assertIsNone(self.display.current_image)

        self.display.set_image(self.image_path)
        self.display.show()
        window = self.display.window
        assert window is not None

        self.assertEqual(win32gui.GetParent(window._host_hwnd), target_hwnd)


if __name__ == "__main__":
    unittest.main()
