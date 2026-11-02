"""
驗證匯入主程式不建立使用者資料, 載入 VLC 或操作原生環境
"""

import os
from pathlib import Path
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from app_loader import load_app_module
from config import ConfigStore


class StartupTests(unittest.TestCase):

    def test_import_does_not_initialize_runtime(self):
        """
        驗證缺少使用者資料位置與 VLC 時仍可匯入主程式, 且不初始化執行環境
        """

        blocked = mock.Mock(side_effect=AssertionError("匯入時不應初始化執行環境"))
        with (
            mock.patch.dict(os.environ),
            mock.patch.dict(sys.modules, {"vlc": None}),
            mock.patch("pathlib.Path.mkdir", blocked),
            mock.patch("logging.handlers.RotatingFileHandler", blocked),
            mock.patch("ctypes.CDLL", blocked),
            mock.patch("os.add_dll_directory", blocked),
            mock.patch.object(ConfigStore, "load", blocked),
            mock.patch.object(ConfigStore, "load_language", blocked),
            mock.patch.object(ConfigStore, "initialize_if_missing", blocked),
            mock.patch("windows_integration.is_already_running", blocked),
            mock.patch("windows_integration.check_desktop_ready", blocked),
            mock.patch("PySide6.QtWidgets.QApplication", blocked),
        ):
            os.environ.pop("LOCALAPPDATA", None)
            original_environment = dict(os.environ)
            load_app_module()

            self.assertEqual(dict(os.environ), original_environment)
            blocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
