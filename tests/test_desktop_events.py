"""
用隱藏頂層視窗驗證原生工作列通知, 不重啟 Explorer 或廣播給其他程式
"""

import ast
from ctypes import wintypes
from pathlib import Path
import sys
import unittest

import win32api
import win32con
import win32gui

from PySide6.QtCore import QCoreApplication, QEvent, Qt, Signal
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QWidget


ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from windows_integration import register_taskbar_created_notification


class DesktopEventTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:

        cls.app = QApplication.instance() or QApplication([])

        if not isinstance(cls.app, QApplication):
            raise unittest.SkipTest("桌面通知測試需要 QApplication")

        # 僅載入監聽視窗類別, 避免主程式初始化 VLC 與使用者設定
        source_path = ROOT / "bb-video-wallpaper.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        nodes = ast.Module(
            body=[node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DesktopEventWindow"],
            type_ignores=[],
        )
        namespace = {
            "QWidget": QWidget, "Qt": Qt, "Signal": Signal, "wintypes": wintypes,
            "register_taskbar_created_notification": register_taskbar_created_notification,
        }
        exec(compile(nodes, str(source_path), "exec"), namespace)
        cls.listener_class = namespace["DesktopEventWindow"]


    def setUp(self) -> None:

        self.listener = self.listener_class()
        self.addCleanup(self.dispose_listener)


    def dispose_listener(self) -> None:

        self.listener.close()
        self.listener.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


    def test_hidden_top_level_window_receives_only_taskbar_notification(self):

        hwnd = int(self.listener.winId())
        events = QSignalSpy(self.listener.taskbar_created)
        self.assertFalse(win32gui.IsWindowVisible(hwnd))
        self.assertEqual(win32gui.GetAncestor(hwnd, win32con.GA_ROOT), hwnd)
        self.assertIsNone(self.listener.parentWidget())

        win32gui.SendMessage(hwnd, win32con.WM_NULL, 0, 0)
        self.assertEqual(events.count(), 0)
        win32gui.SendMessage(hwnd, self.listener.taskbar_created_message, 0, 0)
        self.assertEqual(events.count(), 1)
        self.assertFalse(win32gui.IsWindowVisible(hwnd))


    def test_listener_survives_destruction_of_separate_wallpaper_container(self):

        container = win32gui.CreateWindowEx(
            0, "Static", "", win32con.WS_OVERLAPPED,
            0, 0, 320, 240, 0, 0, win32api.GetModuleHandle(None), None,
        )
        target = QWidget()

        try:
            target_hwnd = int(target.winId())
            win32gui.SetParent(target_hwnd, container)
            win32gui.DestroyWindow(container)
            self.assertFalse(win32gui.IsWindow(target_hwnd))

            hwnd = int(self.listener.winId())
            self.assertTrue(win32gui.IsWindow(hwnd))
            events = QSignalSpy(self.listener.taskbar_created)
            win32gui.SendMessage(hwnd, self.listener.taskbar_created_message, 0, 0)
            self.assertEqual(events.count(), 1)

        finally:
            target.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if win32gui.IsWindow(container):
                win32gui.DestroyWindow(container)


if __name__ == "__main__":
    unittest.main()
