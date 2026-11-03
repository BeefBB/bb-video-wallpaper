"""
主程式載入與共用 Qt 測試環境
"""

import importlib.util
from pathlib import Path
from types import ModuleType
import unittest


ROOT = Path(__file__).resolve().parents[1]


class QtTestCase(unittest.TestCase):
    """
    共用 GUI 應用程式, 在各案例清理完物件後處理延後刪除
    """

    @classmethod
    def setUpClass(cls) -> None:
        """
        建立可供所有 Qt 測試使用的應用程式, 錯誤類型直接回報失敗
        """

        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])
        if not isinstance(cls.app, QApplication):
            raise RuntimeError("Qt 測試需要 QApplication, 不可先建立 QCoreApplication")


    def setUp(self) -> None:
        """
        在案例資源釋放後統一處理 QObject 的延後刪除
        """

        from PySide6.QtCore import QCoreApplication, QEvent

        # 最先登記, 讓各案例的視窗清理先執行
        self.addCleanup(QCoreApplication.sendPostedEvents, None, QEvent.Type.DeferredDelete)


def load_app_module() -> ModuleType:
    """
    以獨立模組狀態載入主程式, 回傳尚未執行啟動入口的模組
    """

    spec = importlib.util.spec_from_file_location("bb_video_wallpaper", ROOT / "bb-video-wallpaper.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
