"""
測試 Windows 桌面掛載, 電源通知與開機排程的模組邊界
模擬外部 API, 不變更桌面, Mutex 或工作排程
"""

import ctypes
from ctypes import wintypes
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]

# 直接執行測試檔時, 將專案根目錄加入模組搜尋路徑
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

import windows_integration as windows


class WindowsIntegrationTests(unittest.TestCase):

    def test_single_instance_returns_mutex_handle_for_its_lifetime(self):

        with (
            mock.patch.object(windows.win32event, "CreateMutex", return_value=mock.sentinel.mutex),
            mock.patch.object(windows.win32api, "GetLastError") as last_error,
        ):
            for error_code, expected in ((0, False), (windows.winerror.ERROR_ALREADY_EXISTS, True)):
                with self.subTest(error_code=error_code):
                    last_error.return_value = error_code
                    self.assertEqual(windows.is_already_running(), (expected, mock.sentinel.mutex))


    def test_task_creation_quotes_supplied_path_and_keeps_background_settings(self):

        executable = Path("C:/Apps With Spaces/BB Video Wallpaper.exe")

        with mock.patch.object(windows.subprocess, "run") as run:
            windows.create_task(executable)

        self.assertEqual(run.call_count, 2)
        command = run.call_args_list[0].args[0]
        self.assertEqual(command[command.index("/TN") + 1], windows.TASK_NAME)
        self.assertEqual(command[command.index("/TR") + 1], f'"{executable}" --task')

        settings = run.call_args_list[1].args[0][-1]
        self.assertIn(f"Get-ScheduledTask -TaskName '{windows.TASK_NAME}'", settings)
        self.assertIn("DisallowStartIfOnBatteries = $false", settings)
        self.assertIn("StopIfGoingOnBatteries = $false", settings)
        self.assertIn("ExecutionTimeLimit = 'PT0S'", settings)

        for call in run.call_args_list:
            self.assertTrue(call.kwargs["check"])
            self.assertEqual(call.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)


    def test_failed_task_creation_does_not_try_to_update_settings(self):

        error = subprocess.CalledProcessError(1, "schtasks")

        with mock.patch.object(windows.subprocess, "run", side_effect=error) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                windows.create_task(Path("C:/app.exe"))

        run.assert_called_once()


    def test_wallpaper_uses_existing_workerw_on_modern_desktop(self):

        with mock.patch.object(windows, "win32gui") as gui:
            gui.FindWindow.return_value = 10
            gui.FindWindowEx.side_effect = [0, 20]

            windows.set_windows_as_wallpaper(100)

            gui.SetParent.assert_called_once_with(100, 20)
            gui.SetWindowPos.assert_not_called()


    def test_wallpaper_creates_workerw_when_modern_desktop_has_none(self):

        with (
            mock.patch.object(windows, "win32gui") as gui,
            mock.patch.object(windows, "create_workerw", return_value=20) as create,
        ):
            gui.FindWindow.return_value = 10
            gui.FindWindowEx.side_effect = [0, 0]

            windows.set_windows_as_wallpaper(100)

            create.assert_called_once_with(10)
            gui.SetParent.assert_called_once_with(100, 20)


    def test_wallpaper_stays_behind_icons_on_legacy_desktop(self):

        with mock.patch.object(windows, "win32gui") as gui:
            gui.FindWindow.return_value = 10
            gui.FindWindowEx.side_effect = [20, 30, 40]

            windows.set_windows_as_wallpaper(100)

            gui.SetParent.assert_called_once_with(100, 40)
            self.assertEqual(gui.SetWindowPos.call_args.args[:2], (100, 30))


    def test_missing_progman_does_not_change_window_parent(self):

        with mock.patch.object(windows, "win32gui") as gui:
            gui.FindWindow.return_value = 0

            with self.assertRaisesRegex(RuntimeError, "Progman"):
                windows.set_windows_as_wallpaper(100)

            gui.SetParent.assert_not_called()
            gui.SendMessageTimeout.assert_not_called()


    def test_virtual_desktop_manager_is_created_on_demand_and_reused(self):

        with (
            mock.patch.object(windows, "_virtual_desktop_manager", None),
            mock.patch.object(windows, "CreateObject") as create,
        ):
            create.return_value.IsWindowOnCurrentVirtualDesktop.side_effect = [True, False]

            self.assertTrue(windows.is_on_current_desktop(10))
            self.assertFalse(windows.is_on_current_desktop(20))
            create.assert_called_once()


    def test_fullscreen_detection_ignores_windows_from_other_desktops(self):

        with (
            mock.patch.object(windows, "win32gui") as gui,
            mock.patch.object(windows, "win32api") as api,
            mock.patch.object(windows, "is_on_current_desktop") as current_desktop,
            mock.patch.object(windows.ctypes.windll.user32, "GetShellWindow", return_value=10),
        ):
            gui.EnumWindows.side_effect = lambda callback, data: callback(20, data)
            gui.IsWindowVisible.return_value = True
            gui.IsIconic.return_value = False
            gui.GetClassName.return_value = "AppWindow"
            gui.GetWindowText.return_value = "Video editor"
            gui.GetWindowRect.return_value = (0, 0, 1920, 1040)
            api.GetMonitorInfo.return_value = {"Work": (0, 0, 1920, 1040)}

            current_desktop.return_value = False
            self.assertFalse(windows.is_any_fullscreen(100))
            gui.GetWindowRect.assert_not_called()

            current_desktop.return_value = True
            self.assertTrue(windows.is_any_fullscreen(100))


    def test_battery_detection_preserves_unknown_and_failed_status(self):

        with mock.patch.object(windows.ctypes.windll.kernel32, "GetSystemPowerStatus") as get_status:
            for ac_line, succeeded, expected in ((0, 1, True), (1, 1, False), (255, 1, False), (0, 0, False)):
                with self.subTest(ac_line=ac_line, succeeded=succeeded):

                    def query(pointer):

                        status = ctypes.cast(pointer, ctypes.POINTER(windows.SYSTEM_POWER_STATUS)).contents
                        status.ACLineStatus = ac_line
                        return succeeded

                    get_status.side_effect = query
                    self.assertEqual(windows.is_on_battery(), expected)


    def test_display_notification_decodes_off_on_and_dim_states(self):

        setting = windows.POWERBROADCAST_SETTING()
        setting.PowerSetting = windows.GUID_CONSOLE_DISPLAY_STATE
        setting.DataLength = ctypes.sizeof(wintypes.DWORD)
        message = wintypes.MSG()
        message.message = windows.WM_POWERBROADCAST
        message.wParam = windows.PBT_POWERSETTINGCHANGE
        message.lParam = ctypes.addressof(setting)

        for display_state in (0, 1, 2):
            with self.subTest(display_state=display_state):
                setting.Data = display_state
                self.assertEqual(windows.get_display_state(message), display_state)


    def test_display_notification_ignores_other_messages_and_power_settings(self):

        message = wintypes.MSG()
        self.assertIsNone(windows.get_display_state(message))

        message.message = windows.WM_POWERBROADCAST
        self.assertIsNone(windows.get_display_state(message))

        setting = windows.POWERBROADCAST_SETTING()
        message.wParam = windows.PBT_POWERSETTINGCHANGE
        message.lParam = ctypes.addressof(setting)
        self.assertIsNone(windows.get_display_state(message))


    def test_power_notifications_preserve_64_bit_handles(self):

        hwnd = 2**40 + 1
        handle = 2**40 + 2

        with (
            mock.patch.object(windows.ctypes.windll.user32, "RegisterPowerSettingNotification", return_value=handle) as register,
            mock.patch.object(windows.ctypes.windll.user32, "UnregisterPowerSettingNotification") as unregister,
        ):
            self.assertEqual(windows.register_power_notification(hwnd), handle)
            self.assertIs(register.restype, wintypes.HANDLE)
            self.assertIs(register.argtypes[0], wintypes.HANDLE)
            self.assertEqual(register.call_args.args[0], hwnd)
            power_guid = ctypes.cast(register.call_args.args[1], ctypes.POINTER(windows.GUID)).contents
            self.assertEqual(bytes(power_guid), bytes(windows.GUID_CONSOLE_DISPLAY_STATE))

            windows.unregister_power_notification(handle)
            self.assertIs(unregister.argtypes[0], wintypes.HANDLE)
            unregister.assert_called_once_with(handle)


if __name__ == "__main__":
    unittest.main()
