"""
測試 Windows 掛載失敗, 原生視窗生命週期與通知邊界
模擬外部 API, 不變更桌面, Mutex 或工作排程
使用隱藏視窗驗證工作列通知, 獨立監聽視窗與備援容器的原生 alpha 設定
"""

import ctypes
from ctypes import wintypes
from pathlib import Path
import tempfile
import sys
import unittest
from unittest import mock

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QWidget


ROOT = Path(__file__).resolve().parents[1]

# 直接執行測試檔時, 將專案根目錄加入模組搜尋路徑
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from app_loader import QtTestCase, load_app_module
import bb_image_display
import win32con
import win32gui
import windows_integration as windows


class WindowsIntegrationTests(unittest.TestCase):
    """
    使用模擬 API 驗證 Windows 整合行為
    """

    def test_monitor_identity_uses_device_interface_and_survives_display_renumbering(self):
        """
        驗證螢幕識別使用裝置介面, Windows 顯示編號改變後仍保持一致
        """

        device = mock.Mock(StateFlags=1, DeviceID=r"\\?\DISPLAY#Monitor-A")
        with (
            mock.patch.object(windows.win32api, "MonitorFromWindow", return_value=10),
            mock.patch.object(windows.win32api, "GetMonitorInfo") as info,
            mock.patch.object(windows.win32api, "EnumDisplayDevices", return_value=device),
        ):
            info.side_effect = [{"Device": r"\\.\DISPLAY1"}, {"Device": r"\\.\DISPLAY3"}]

            self.assertEqual(windows.get_monitor_id(100), r"device:\\?\display#monitor-a")
            self.assertEqual(windows.get_monitor_id(200), r"device:\\?\display#monitor-a")


    def test_monitor_identity_skips_inactive_or_unidentified_devices(self):
        """
        驗證查找螢幕識別時略過未啟用或沒有裝置識別的項目
        """

        with (
            mock.patch.object(windows.win32api, "MonitorFromWindow", return_value=10),
            mock.patch.object(windows.win32api, "GetMonitorInfo", return_value={"Device": "display"}),
            mock.patch.object(windows.win32api, "EnumDisplayDevices") as devices,
        ):
            devices.side_effect = [
                mock.Mock(StateFlags=0, DeviceID="inactive"),
                mock.Mock(StateFlags=1, DeviceID=""),
                mock.Mock(StateFlags=1, DeviceID="active"),
            ]

            self.assertEqual(windows.get_monitor_id(100), "device:active")


    def test_monitor_identity_falls_back_to_distinct_display_names(self):
        """
        驗證無法列舉裝置介面時以顯示裝置名稱區分螢幕
        """

        with (
            mock.patch.object(windows.win32api, "MonitorFromWindow", return_value=10),
            mock.patch.object(windows.win32api, "GetMonitorInfo") as info,
            mock.patch.object(windows.win32api, "EnumDisplayDevices", side_effect=windows.win32api.error),
        ):
            info.side_effect = [{"Device": "DISPLAY1"}, {"Device": "DISPLAY2"}]
            self.assertEqual(windows.get_monitor_id(100), "display:display1")
            self.assertEqual(windows.get_monitor_id(200), "display:display2")


    def test_vlc_wakeup_only_posts_null_to_own_video_windows(self):
        """
        驗證喚醒操作只向目前程序持有的 VLC 影片視窗傳送 WM_NULL
        """

        error_type = windows.win32gui.error
        with (
            mock.patch.object(windows, "win32gui") as gui,
            mock.patch.object(windows.win32api, "GetCurrentProcessId", return_value=100),
            mock.patch.object(windows.win32process, "GetWindowThreadProcessId") as owner,
        ):
            gui.error = error_type
            owner.side_effect = lambda hwnd: (10, 999 if hwnd == 4 else 100)

            def class_name(hwnd):
                """
                依視窗代碼回傳 Qt 或 VLC 類別, 並模擬視窗失效與缺少類別名稱
                """

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


    def test_taskbar_notification_preserves_handle_and_rejects_unregistered_message(self):
        """
        工作列通知保留完整 HWND, 註冊失敗時不放行編號為零的訊息
        """

        # 超過 32 位元的 HWND
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
            register.return_value = 0
            change.reset_mock()
            with self.assertRaisesRegex(RuntimeError, "TaskbarCreated"):
                windows.register_taskbar_created_notification(hwnd)
            change.assert_not_called()


    def test_wallpaper_attaches_to_existing_fallback_and_legacy_containers(self):
        """
        驗證既有與備援容器的掛載目標, 舊版桌面另將桌布排在圖示後方
        """

        cases = (
            ([0, 20], 20, False, None),
            ([0, 0], 20, True, None),
            ([20, 30, 40], 40, False, 30),
        )
        for windows_found, parent, creates_container, behind in cases:
            with (
                self.subTest(windows_found=windows_found),
                mock.patch.object(windows, "win32gui") as gui,
                mock.patch.object(windows, "create_workerw", return_value=20) as create,
            ):
                gui.FindWindow.return_value = 10
                gui.FindWindowEx.side_effect = windows_found
                gui.GetAncestor.return_value = parent
                # SetParent 成功時也可能回傳 0
                gui.SetParent.return_value = 0
                windows.set_windows_as_wallpaper(100)

                gui.SetParent.assert_called_once_with(100, parent)
                self.assertEqual(create.call_args_list, [mock.call(10)] if creates_container else [])
                if behind is None:
                    gui.SetWindowPos.assert_not_called()
                else:
                    self.assertEqual(gui.SetWindowPos.call_args.args[:2], (100, behind))


    def test_workerw_alpha_failure_cleans_up_and_preserves_original_error(self):
        """
        驗證 WorkerW 透明度設定失敗時銷毀容器, 清理再失敗也保留原始例外
        """

        error_type = windows.win32gui.error
        error = error_type(87, "SetLayeredWindowAttributes", "invalid parameter")

        for cleanup_error in (None, error_type(5, "DestroyWindow", "access denied")):
            with self.subTest(cleanup_error=cleanup_error), mock.patch.object(windows, "win32gui") as gui:
                gui.error = error_type
                gui.GetClientRect.return_value = (0, 0, 1920, 1080)
                gui.CreateWindowEx.return_value = 20
                gui.SetLayeredWindowAttributes.side_effect = error
                gui.DestroyWindow.side_effect = cleanup_error

                with mock.patch.object(windows.logger, "exception"):
                    with self.assertRaises(error_type) as caught:
                        windows.create_workerw(10)

                self.assertIs(caught.exception, error)
                gui.DestroyWindow.assert_called_once_with(20)


    def test_fullscreen_detection_skips_unavailable_wallpaper_monitor(self):
        """
        驗證桌布視窗失效或沒有對應螢幕時, 不列舉其他視窗
        """

        error = windows.win32api.error(1400, "MonitorFromWindow", "window destroyed")

        for result in (None, 0, error):
            with (
                self.subTest(result=result),
                mock.patch.object(windows, "win32gui") as gui,
                mock.patch.object(windows, "win32api") as api,
            ):
                if isinstance(result, Exception):
                    api.MonitorFromWindow.side_effect = result
                else:
                    api.MonitorFromWindow.return_value = result

                self.assertFalse(windows.is_any_fullscreen(100))
                gui.EnumWindows.assert_not_called()


    def test_battery_detection_preserves_unknown_and_failed_status(self):
        """
        驗證電池供電, 插電, 未知與查詢失敗時的供電判斷
        """

        with mock.patch.object(windows.ctypes.windll.kernel32, "GetSystemPowerStatus") as get_status:
            for ac_line, succeeded, expected in ((0, 1, True), (1, 1, False), (255, 1, False), (0, 0, False)):
                with self.subTest(ac_line=ac_line, succeeded=succeeded):

                    def query(pointer):
                        """
                        依目前案例填入電源狀態結構, 並回傳指定的 API 成功旗標
                        """

                        status = ctypes.cast(pointer, ctypes.POINTER(windows.SYSTEM_POWER_STATUS)).contents
                        status.ACLineStatus = ac_line
                        return succeeded

                    get_status.side_effect = query
                    self.assertEqual(windows.is_on_battery(), expected)


    def test_display_notification_ignores_unrelated_messages_and_decodes_all_states(self):
        """
        驗證略過無關訊息與電源設定, 正確解析螢幕關閉, 開啟與變暗狀態
        """

        message = wintypes.MSG()
        self.assertIsNone(windows.get_display_state(message))
        message.message = windows.WM_POWERBROADCAST
        self.assertIsNone(windows.get_display_state(message))
        setting = windows.POWERBROADCAST_SETTING()
        message.wParam = windows.PBT_POWERSETTINGCHANGE
        message.lParam = ctypes.addressof(setting)
        self.assertIsNone(windows.get_display_state(message))

        setting.PowerSetting = windows.GUID_CONSOLE_DISPLAY_STATE
        setting.DataLength = ctypes.sizeof(wintypes.DWORD)
        for display_state in (0, 1, 2):
            with self.subTest(display_state=display_state):
                setting.Data = display_state
                self.assertEqual(windows.get_display_state(message), display_state)


    def test_power_notifications_preserve_64_bit_handles(self):
        """
        驗證電源通知註冊與取消保留完整的 64 位元 handle, 並使用正確的電源 GUID
        """

        # 超過 32 位元的 HWND 與通知 handle
        hwnd = 2**40 + 1
        handle = 2**40 + 2

        with (
            mock.patch.object(windows.ctypes.windll.user32, "RegisterPowerSettingNotification", return_value=handle) as register,
            mock.patch.object(windows.ctypes.windll.user32, "UnregisterPowerSettingNotification", return_value=1) as unregister,
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


class DesktopEventTests(QtTestCase):
    """
    工作列通知與獨立監聽視窗
    """

    def setUp(self) -> None:
        """
        建立獨立的通知監聽視窗
        """

        super().setUp()

        self.listener = load_app_module().DesktopEventWindow()
        self.addCleanup(self.dispose_listener)


    def dispose_listener(self) -> None:
        """
        關閉並釋放通知監聽視窗
        """

        self.listener.close()
        self.listener.deleteLater()


    def test_hidden_top_level_window_receives_only_taskbar_notification(self):
        """
        驗證監聽視窗維持隱藏且沒有父層, 只在收到工作列重建訊息時發出通知
        """

        # 確認監聽視窗保持隱藏且沒有父視窗
        hwnd = int(self.listener.winId())
        events = QSignalSpy(self.listener.taskbar_created)
        self.assertFalse(windows.win32gui.IsWindowVisible(hwnd))
        self.assertEqual(windows.win32gui.GetAncestor(hwnd, windows.win32con.GA_ROOT), hwnd)
        self.assertIsNone(self.listener.parentWidget())

        windows.win32gui.SendMessage(hwnd, windows.win32con.WM_NULL, 0, 0)
        self.assertEqual(events.count(), 0)
        windows.win32gui.SendMessage(hwnd, self.listener.taskbar_created_message, 0, 0)
        self.assertEqual(events.count(), 1)
        self.assertFalse(windows.win32gui.IsWindowVisible(hwnd))


    def test_listener_survives_destruction_of_separate_wallpaper_container(self):
        """
        驗證獨立桌布容器銷毀後, 監聽視窗仍存在且可接收工作列通知
        """

        container = windows.win32gui.CreateWindowEx(
            0, "Static", "", windows.win32con.WS_OVERLAPPED,
            0, 0, 320, 240, 0, 0, windows.win32api.GetModuleHandle(None), None,
        )
        target = QWidget()

        try:
            target_hwnd = int(target.winId())
            windows.win32gui.SetParent(target_hwnd, container)
            # 銷毀父容器與原生子視窗
            windows.win32gui.DestroyWindow(container)
            self.assertFalse(windows.win32gui.IsWindow(target_hwnd))

            hwnd = int(self.listener.winId())
            self.assertTrue(windows.win32gui.IsWindow(hwnd))
            events = QSignalSpy(self.listener.taskbar_created)
            windows.win32gui.SendMessage(hwnd, self.listener.taskbar_created_message, 0, 0)
            self.assertEqual(events.count(), 1)

        finally:
            target.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if windows.win32gui.IsWindow(container):
                windows.win32gui.DestroyWindow(container)


class WorkerWNativeTests(unittest.TestCase):
    """
    使用隱藏父視窗驗證真正的 WorkerW
    """

    def test_created_workerw_is_opaque_and_class_can_be_reused(self):
        """
        驗證連續建立的 WorkerW 保持隱藏且完全不透明, 並可重用視窗類別
        """

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



    def test_native_host_lifecycle_preserves_image_and_external_targets(self):
        """
        驗證圖片原生容器的顯示, 重綁與釋放, 保留圖片與外部目標視窗
        """

        self.display.set_image(self.image_path)
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
        self.display.show()
        self.assertIs(self.display.window, window)
        self.assertEqual(window.image.cacheKey(), image_key)
        self.assertEqual(win32gui.GetParent(window._host_hwnd), target_hwnd)
        self.assertTrue(win32gui.IsWindow(int(self.target.winId())))
        self.assertTrue(window.isVisible())

        host = window._host_hwnd
        # 關閉時釋放圖片容器並保留目標供下次使用
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
