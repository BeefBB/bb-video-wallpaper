"""
測試 Windows 桌面掛載, 電源通知與開機排程的模組邊界
模擬外部 API, 不變更桌面, Mutex 或工作排程
備援容器另用隱藏的獨立父視窗驗證原生 alpha 設定
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

    def test_vlc_wakeup_only_posts_null_to_own_video_windows(self):

        error_type = windows.win32gui.error
        with (
            mock.patch.object(windows, "win32gui") as gui,
            mock.patch.object(windows.win32api, "GetCurrentProcessId", return_value=100),
            mock.patch.object(windows.win32process, "GetWindowThreadProcessId") as owner,
        ):
            gui.error = error_type
            owner.side_effect = lambda hwnd: (10, 999 if hwnd == 4 else 100)

            def class_name(hwnd):
                if hwnd == 5:
                    raise error_type(1400, "GetClassName", "window destroyed")
                if hwnd == 6:
                    return None
                return "QtWindow" if hwnd == 3 else "VLC video main 1234"

            gui.GetClassName.side_effect = class_name
            gui.EnumWindows.side_effect = lambda callback, data: [callback(hwnd, data) for hwnd in range(1, 7)]
            windows.wake_vlc_video_windows()

            self.assertEqual(gui.PostMessage.call_args_list, [
                mock.call(1, windows.win32con.WM_NULL, 0, 0),
                mock.call(2, windows.win32con.WM_NULL, 0, 0),
            ])


    def test_taskbar_notification_allows_only_registered_message_and_preserves_handle(self):

        hwnd = 2**40 + 1
        with (
            mock.patch.object(windows.win32gui, "RegisterWindowMessage", return_value=0xC123) as register,
            mock.patch.object(windows.ctypes.windll.user32, "ChangeWindowMessageFilterEx", return_value=1) as change,
        ):
            self.assertEqual(windows.register_taskbar_created_notification(hwnd), 0xC123)
            register.assert_called_once_with("TaskbarCreated")
            change.assert_called_once_with(hwnd, 0xC123, 1, None)
            self.assertIs(change.argtypes[0], wintypes.HWND)
            self.assertIs(change.restype, wintypes.BOOL)


    def test_taskbar_filter_failure_keeps_message_registered(self):

        with (
            mock.patch.object(windows.win32gui, "RegisterWindowMessage", return_value=0xC123),
            mock.patch.object(windows.ctypes.windll.user32, "ChangeWindowMessageFilterEx", return_value=0),
            mock.patch.object(windows.ctypes, "GetLastError", return_value=5),
            self.assertLogs(windows.logger, level="WARNING") as logs,
        ):
            self.assertEqual(windows.register_taskbar_created_notification(100), 0xC123)

        self.assertIn("error=5", logs.output[0])


    def test_taskbar_registration_failure_does_not_allow_null_message(self):

        with (
            mock.patch.object(windows.win32gui, "RegisterWindowMessage", return_value=0),
            mock.patch.object(windows.ctypes.windll.user32, "ChangeWindowMessageFilterEx") as change,
        ):
            with self.assertRaisesRegex(RuntimeError, "TaskbarCreated"):
                windows.register_taskbar_created_notification(100)

        change.assert_not_called()


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
            gui.GetAncestor.return_value = 20
            # 頂層視窗原本沒有父層時, SetParent 成功也可能回傳 0
            gui.SetParent.return_value = 0

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
            gui.GetAncestor.return_value = 20

            windows.set_windows_as_wallpaper(100)

            create.assert_called_once_with(10)
            gui.SetParent.assert_called_once_with(100, 20)


    def test_wallpaper_stays_behind_icons_on_legacy_desktop(self):

        with mock.patch.object(windows, "win32gui") as gui:
            gui.FindWindow.return_value = 10
            gui.FindWindowEx.side_effect = [20, 30, 40]
            gui.GetAncestor.return_value = 40

            windows.set_windows_as_wallpaper(100)

            gui.SetParent.assert_called_once_with(100, 40)
            self.assertEqual(gui.SetWindowPos.call_args.args[:2], (100, 30))


    def test_missing_progman_does_not_change_window_parent(self):

        with mock.patch.object(windows, "win32gui") as gui:
            gui.FindWindow.return_value = 0

            with self.assertLogs(windows.logger, level="ERROR") as logs:
                with self.assertRaisesRegex(RuntimeError, "Progman"):
                    windows.set_windows_as_wallpaper(100)

            gui.SetParent.assert_not_called()
            gui.SendMessageTimeout.assert_not_called()
            self.assertIn("stage=尋找 Progman", logs.output[0])


    def test_wallpaper_warns_about_parent_mismatch_without_interrupting(self):

        for actual_parent in (0, 10, 30):
            with self.subTest(actual_parent=actual_parent), mock.patch.object(windows, "win32gui") as gui:
                gui.FindWindow.return_value = 10
                gui.FindWindowEx.side_effect = [0, 20]
                gui.GetParent.return_value = 20
                gui.GetAncestor.return_value = actual_parent

                with self.assertLogs(windows.logger, level="INFO") as logs:
                    windows.set_windows_as_wallpaper(100)

                output = "\n".join(logs.output)
                self.assertIn("WARNING:windows_integration:桌布掛載父視窗不符, 繼續既有流程", output)
                self.assertIn("expected=0x14", output)
                self.assertIn(f"actual={actual_parent:#x}", output)
                self.assertNotIn("桌布掛載完成", output)
                self.assertNotIn("ERROR:", output)
                gui.SetParent.assert_called_once_with(100, 20)
                gui.GetParent.assert_not_called()


    def test_parent_query_failure_warns_without_interrupting(self):

        error = windows.win32gui.error(1400, "GetAncestor", "invalid window handle")

        with mock.patch.object(windows, "win32gui") as gui:
            gui.FindWindow.return_value = 10
            gui.FindWindowEx.side_effect = [0, 20]
            gui.GetAncestor.side_effect = error

            with self.assertLogs(windows.logger, level="INFO") as logs:
                windows.set_windows_as_wallpaper(100)

            gui.SetParent.assert_called_once_with(100, 20)

        output = "\n".join(logs.output)
        self.assertIn("WARNING:windows_integration:無法確認桌布父視窗, 繼續既有流程", output)
        self.assertIn("hwnd=0x64 expected=0x14", output)
        self.assertNotIn("桌布掛載完成", output)
        self.assertNotIn("ERROR:", output)


    def test_wallpaper_logs_structure_and_selected_path(self):

        with mock.patch.object(windows, "win32gui") as gui:
            gui.FindWindow.return_value = 10
            gui.FindWindowEx.side_effect = [0, 20]
            gui.GetAncestor.return_value = 20
            gui.GetClassName.return_value = "TestWindow"
            gui.GetWindowLong.side_effect = lambda hwnd, index: -2147483648 if index == windows.win32con.GWL_STYLE else 0x200000
            gui.GetWindowRect.return_value = (0, 0, 1920, 1080)

            with self.assertLogs(windows.logger, level="INFO") as logs:
                windows.set_windows_as_wallpaper(100)

        output = "\n".join(logs.output)
        for expected in (
            "Windows=",
            "mode=progman-workerw",
            "progman=0xa workerw=0x14",
            "wallpaper-after: hwnd=0x64 class=TestWindow parent=0x14",
            "style=0x80000000 exstyle=0x00200000",
            "rect=(0, 0, 1920, 1080)",
            "桌布掛載完成",
        ):
            self.assertIn(expected, output)


    def test_diagnostic_read_failure_does_not_prevent_attachment(self):

        with mock.patch.object(windows, "win32gui") as gui:
            gui.FindWindow.return_value = 10
            gui.FindWindowEx.side_effect = [0, 20]
            gui.GetAncestor.return_value = 20
            gui.GetClassName.side_effect = RuntimeError("diagnostic unavailable")

            with self.assertLogs(windows.logger, level="INFO") as logs:
                windows.set_windows_as_wallpaper(100)

            gui.SetParent.assert_called_once_with(100, 20)

        output = "\n".join(logs.output)
        self.assertIn("讀取桌布視窗診斷失敗", output)
        self.assertIn("桌布掛載完成", output)


    def test_split_message_failure_is_logged_and_does_not_attempt_attachment(self):

        error = windows.win32gui.error(1460, "SendMessageTimeout", "timed out")

        with mock.patch.object(windows, "win32gui") as gui:
            gui.FindWindow.return_value = 10
            gui.SendMessageTimeout.side_effect = [None, error]

            with self.assertLogs(windows.logger, level="ERROR") as logs:
                with self.assertRaises(type(error)) as caught:
                    windows.set_windows_as_wallpaper(100)

            self.assertIs(caught.exception, error)
            self.assertIn("stage=SendMessageTimeout(0x052C, 13, None)", logs.output[0])
            gui.SetParent.assert_not_called()
            gui.FindWindowEx.assert_not_called()


    def test_attachment_error_survives_failed_diagnostics(self):

        error = windows.win32gui.error(5, "SetParent", "access denied")

        with mock.patch.object(windows, "win32gui") as gui:
            gui.FindWindow.return_value = 10
            gui.FindWindowEx.side_effect = [0, 20]
            gui.SetParent.side_effect = error
            gui.GetClassName.side_effect = RuntimeError("diagnostic unavailable")

            with self.assertLogs(windows.logger, level="INFO") as logs:
                with self.assertRaises(type(error)) as caught:
                    windows.set_windows_as_wallpaper(100)

        self.assertIs(caught.exception, error)
        output = "\n".join(logs.output)
        self.assertIn("stage=SetParent mode=progman-workerw", output)
        self.assertNotIn("桌布掛載完成", output)


    def test_workerw_registration_does_not_hide_unexpected_errors(self):

        error = windows.win32gui.error(5, "RegisterClass", "access denied")

        with (
            mock.patch.object(windows.win32gui, "RegisterClass", side_effect=error),
            mock.patch.object(windows.win32gui, "CreateWindowEx") as create,
        ):
            with self.assertRaises(type(error)) as caught:
                windows.create_workerw(10)

        self.assertIs(caught.exception, error)
        create.assert_not_called()


    def test_workerw_alpha_failure_cleans_up_and_preserves_original_error(self):

        error_type = windows.win32gui.error
        error = error_type(87, "SetLayeredWindowAttributes", "invalid parameter")

        for cleanup_error in (None, error_type(5, "DestroyWindow", "access denied")):
            with self.subTest(cleanup_error=cleanup_error), mock.patch.object(windows, "win32gui") as gui:
                gui.error = error_type
                gui.GetClientRect.return_value = (0, 0, 1920, 1080)
                gui.CreateWindowEx.return_value = 20
                gui.SetLayeredWindowAttributes.side_effect = error
                gui.DestroyWindow.side_effect = cleanup_error

                with mock.patch.object(windows.logger, "exception") as log:
                    with self.assertRaises(error_type) as caught:
                        windows.create_workerw(10)

                self.assertIs(caught.exception, error)
                gui.DestroyWindow.assert_called_once_with(20)
                self.assertEqual(log.call_count, int(cleanup_error is not None))


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


class WorkerWNativeTests(unittest.TestCase):

    def test_created_workerw_is_opaque_and_class_can_be_reused(self):

        # 父視窗保持隱藏, 不接觸 Explorer 或使用者桌面
        parent = windows.win32gui.CreateWindowEx(
            0, "Static", "", windows.win32con.WS_OVERLAPPED,
            0, 0, 320, 240, 0, 0, windows.win32api.GetModuleHandle(None), None,
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


if __name__ == "__main__":
    unittest.main()
