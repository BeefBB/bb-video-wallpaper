"""
測試主程式匯入與啟動的邊界, 使用暫存資料與模擬原生呼叫
"""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from app_loader import load_app_module
from config import ConfigStore


class StartupTests(unittest.TestCase):

    def setUp(self) -> None:

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.localappdata = self.root / "local"
        self.enterContext(mock.patch.dict(os.environ, {"LOCALAPPDATA": str(self.localappdata)}))
        self.module = load_app_module()
        self.enterContext(mock.patch.object(self.module, "logger"))


    def test_import_does_not_initialize_runtime(self):
        """
        驗證缺少使用者資料位置與 VLC 時仍可匯入
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

        self.assertFalse(self.localappdata.exists())


    def prepare_main(self, *, already_running: bool = False, vlc_ready: bool = True) -> mock.Mock:
        """
        以替身提供原生操作及事件迴圈
        """

        application = mock.Mock()
        application.exec.return_value = 0
        for name, replacement in {
            "is_already_running": mock.Mock(return_value=(already_running, mock.sentinel.mutex)),
            "initialize_logging": mock.Mock(),
            "initialize_vlc": mock.Mock(return_value=vlc_ready),
            "show_already_running_toast": mock.Mock(),
            "check_desktop_ready": mock.Mock(return_value=True),
            "run_as_admin": mock.Mock(),
            "QApplication": mock.Mock(return_value=application),
            "QFont": mock.Mock(),
            "Tray": mock.Mock(),
        }.items():
            self.enterContext(mock.patch.object(self.module, name, replacement))
        self.enterContext(mock.patch.object(self.module.time, "sleep"))
        return application


    def test_main_initializes_config_before_tray(self):
        """
        啟動系統匣前已建立可讀取的設定與媒體資料夾
        """

        application = self.prepare_main()
        self.enterContext(mock.patch.object(sys, "argv", [str(ROOT / "bb-video-wallpaper.py")]))

        def create_tray(app: mock.Mock) -> None:
            self.assertIs(app, application)
            self.assertTrue(self.module.CONFIG_PATH.is_file())
            self.assertTrue(self.module.MEDIA_DIR.is_dir())
            self.assertIn("app_name", self.module.LANG)

        self.module.Tray.side_effect = create_tray
        self.module.main()
        self.module.Tray.assert_called_once_with(application)


    def test_duplicate_instance_only_loads_language_and_shows_notification(self):
        """
        重複啟動時顯示通知並提早結束
        """

        self.prepare_main(already_running=True)

        self.assertEqual(self.module.main(), 0)
        self.module.show_already_running_toast.assert_called_once_with()
        self.module.initialize_logging.assert_not_called()
        self.module.initialize_vlc.assert_not_called()
        self.assertFalse(self.localappdata.exists())


    def test_vlc_failure_stops_startup_before_config_initialization_and_qt(self):
        """
        VLC 初始化失敗時中止啟動
        """

        self.prepare_main(vlc_ready=False)

        self.assertEqual(self.module.main(), 1)
        self.module.QApplication.assert_not_called()
        self.module.Tray.assert_not_called()
        self.module.check_desktop_ready.assert_not_called()
        self.assertFalse(self.module.CONFIG_PATH.exists())


if __name__ == "__main__":
    unittest.main()
