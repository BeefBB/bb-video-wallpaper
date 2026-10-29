"""
Windows 桌面掛載, 視窗偵測, 電源通知與開機排程
"""

import ctypes
from ctypes import wintypes
import logging
from pathlib import Path
import subprocess
import sys

import win32gui
import win32con
import win32api
import win32event
import winerror

import psutil

from comtypes import GUID as ComGUID, HRESULT, IUnknown, COMMETHOD
from comtypes.client import CreateObject


logger = logging.getLogger(__name__)


# Windows 工作排程器的工作名稱
TASK_NAME = "BBVideoWallpaper"


# 單一實例
# 唯一的 Mutex 名稱
MUTEX_NAME = "Global\\BBVideoWallpaper_SingleInstance_Mutex"

def is_already_running() -> tuple[bool, object]:
    """
    檢查程式是否已經在執行中
    """

    # 建立全域 Mutex
    mutex = win32event.CreateMutex(None, False, MUTEX_NAME)  # type: ignore


    # 檢查錯誤碼
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        return True, mutex

    return False, mutex


WM_POWERBROADCAST = 0x0218
PBT_POWERSETTINGCHANGE = 0x8013
DEVICE_NOTIFY_WINDOW_HANDLE = 0

class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class POWERBROADCAST_SETTING(ctypes.Structure):
    _fields_ = [
        ("PowerSetting", GUID),
        ("DataLength", wintypes.DWORD),
        ("Data", wintypes.DWORD),
    ]


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("Reserved1", ctypes.c_ubyte),
        ("BatteryLifeTime", wintypes.DWORD),
        ("BatteryFullLifeTime", wintypes.DWORD),
    ]


# 螢幕顯示狀態 (亮暗關) 的 GUID
GUID_CONSOLE_DISPLAY_STATE = GUID(
    0x6FE69556,
    0x704A,
    0x47A0,
    (ctypes.c_ubyte * 8)(0x8F, 0x24, 0xC2, 0x8D, 0x93, 0x6F, 0xDA, 0x47)
)


def register_power_notification(hwnd: int) -> int | None:
    """
    註冊螢幕電源通知, 保留完整的原生 Handle
    """

    # 明確指定型別, 避免 64 位元 Handle 被截斷
    register = ctypes.windll.user32.RegisterPowerSettingNotification
    register.restype = wintypes.HANDLE
    register.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]

    return register(hwnd, ctypes.byref(GUID_CONSOLE_DISPLAY_STATE), DEVICE_NOTIFY_WINDOW_HANDLE)


def unregister_power_notification(handle: int) -> None:
    """
    取消註冊螢幕電源通知
    """

    unregister = ctypes.windll.user32.UnregisterPowerSettingNotification
    unregister.restype = wintypes.BOOL
    unregister.argtypes = [wintypes.HANDLE]
    unregister(handle)


def get_display_state(message: wintypes.MSG) -> int | None:
    """
    讀取原生訊息中的螢幕顯示狀態, 其他通知回傳 None
    """

    if message.message != WM_POWERBROADCAST or message.wParam != PBT_POWERSETTINGCHANGE:
        return None

    setting = ctypes.cast(message.lParam, ctypes.POINTER(POWERBROADCAST_SETTING)).contents

    if bytes(setting.PowerSetting) != bytes(GUID_CONSOLE_DISPLAY_STATE):
        return None

    return setting.Data


def run_as_admin() -> None:
    """
    檢查是否為系統管理員權限
    """

    try:
        is_admin = ctypes.windll.shell32.IsUserAnAdmin()

    except Exception:
        is_admin = False

    if not is_admin:
        logger.error("請以系統管理員執行")
        input("請以系統管理員執行。Press Enter...")

        sys.exit()


def check_desktop_ready() -> bool:
    """
    檢查是否登入且載入桌面
    """

    if not win32gui.FindWindow("Shell_TrayWnd", None):
        return False


    if not win32gui.FindWindow("Progman", None):
        return False


    for proc in psutil.process_iter(["name"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() == "explorer.exe":
                return True

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue


    return False


def is_on_battery() -> bool:
    """
    檢查目前是否使用電池供電
    """

    status = SYSTEM_POWER_STATUS()

    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        return False


    return status.ACLineStatus == 0  # 0 = 沒插電, 1 = 接上 AC 電源, 255 = 未知


def create_task(executable_path: Path) -> None:
    """
    加入 Windows 工作排程器
    """

    cmd = [
        "schtasks",
        "/Create",
        "/TN", TASK_NAME,
        "/TR", f'"{str(executable_path)}" --task',
        "/SC", "ONLOGON",
        "/RL", "HIGHEST",
        "/F"
    ]

    subprocess.run(
        cmd,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW
    )

    cmd = [
        "powershell",
        "-Command",
        f"""
        $task = Get-ScheduledTask -TaskName '{TASK_NAME}'
        $task.Settings.DisallowStartIfOnBatteries = $false
        $task.Settings.StopIfGoingOnBatteries = $false
        $task.Settings.ExecutionTimeLimit = 'PT0S'
        Set-ScheduledTask -InputObject $task
        """.strip()
    ]

    subprocess.run(
        cmd,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW
    )


def delete_task() -> None:
    """
    從 Windows 工作排程器中刪除
    """

    subprocess.run(
        [
            "schtasks",
            "/Delete",
            "/TN", TASK_NAME,
            "/F"
        ],
        creationflags=subprocess.CREATE_NO_WINDOW
    )


def task_exists() -> bool:
    """
    檢查 Windows 工作排程器中是否存在這項工作
    """

    result = subprocess.run(
        [
            "schtasks",
            "/Query",
            "/TN",
            TASK_NAME
        ],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW
    )

    return result.returncode == 0


def create_workerw(parent_hwnd) -> int:
    """
    手動創建一個 WorkerW 視窗
    """

    try:

        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)                                                         # type: ignore
        wc.lpszClassName = "WorkerW"                                                                          # type: ignore
        wc.style = win32con.CS_VREDRAW | win32con.CS_HREDRAW                                                  # type: ignore
        wc.lpfnWndProc = lambda hwnd, msg, wParam, lParam: win32gui.DefWindowProc(hwnd, msg, wParam, lParam)  # type: ignore
        win32gui.RegisterClass(wc)

    except win32gui.error as error:
        # 只忽略已註冊的類別, 其他錯誤保留給掛載流程記錄
        if error.winerror != winerror.ERROR_CLASS_ALREADY_EXISTS:
            raise


    rect = win32gui.GetClientRect(parent_hwnd)
    width = rect[2] - rect[0]
    height = rect[3] - rect[1]

    hwnd = win32gui.CreateWindowEx(
        win32con.WS_EX_LAYERED | win32con.WS_EX_TOOLWINDOW,
        "WorkerW",
        "",
        win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.WS_CLIPSIBLINGS,
        0, 0, width, height,
        parent_hwnd,
        None,
        win32api.GetModuleHandle(None),
        None
    )

    try:
        # Layered 視窗需要設定 alpha 才會顯示, 保持完全不透明
        win32gui.SetLayeredWindowAttributes(hwnd, 0, 255, win32con.LWA_ALPHA)

    except Exception:

        try:
            win32gui.DestroyWindow(hwnd)

        except Exception:
            logger.exception("清理未完成初始化的 WorkerW 失敗 - hwnd=%#x", hwnd)

        raise

    return hwnd


def _log_wallpaper_window(role: str, hwnd: int) -> None:
    """
    記錄掛載相關視窗的結構, 診斷失敗不影響掛載結果
    """

    if not hwnd:
        logger.info("桌布視窗 - %s: hwnd=0", role)
        return

    try:

        logger.info(
            "桌布視窗 - %s: hwnd=%#x class=%s parent=%#x style=%#010x exstyle=%#010x rect=%s visible=%s",
            role,
            hwnd,
            win32gui.GetClassName(hwnd),
            win32gui.GetAncestor(hwnd, win32con.GA_PARENT),
            win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE) & 0xFFFFFFFF,
            win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & 0xFFFFFFFF,
            win32gui.GetWindowRect(hwnd),
            bool(win32gui.IsWindowVisible(hwnd)),
        )

    except Exception:
        logger.warning("讀取桌布視窗診斷失敗 - %s: hwnd=%#x", role, hwnd, exc_info=True)


def set_windows_as_wallpaper(hwnd: int) -> None:
    """
    將視窗掛載到 Windows 桌布層
    """

    progman = 0
    workerw = 0
    mode = "unknown"
    stage = "尋找 Progman"

    logger.info("桌布掛載開始 - hwnd=%#x Windows=%s", hwnd, sys.getwindowsversion())
    _log_wallpaper_window("wallpaper-before", hwnd)

    try:

        progman = win32gui.FindWindow("Progman", "Program Manager")

        if not progman:
            progman = win32gui.FindWindow("Progman", None)

        if not progman:
            raise RuntimeError("找不到 Progman 視窗")

        _log_wallpaper_window("Progman", progman)


        # 發送桌面分裂訊息, 讓桌布與桌面圖示分屬不同的 WorkerW
        for wparam, lparam in ((0, None), (0xD, None), (0xD, 1)):

            stage = f"SendMessageTimeout(0x052C, {wparam}, {lparam})"
            win32gui.SendMessageTimeout(progman, 0x052C, wparam, lparam, 0, 0x03E8)


        stage = "尋找 WorkerW"
        workerw_top = 0
        icon_layer = 0
        workerw_old = 0

        while True:

            workerw_top = win32gui.FindWindowEx(None, workerw_top, "WorkerW", None)

            if not workerw_top:
                break

            icon_layer = win32gui.FindWindowEx(workerw_top, None, "SHELLDLL_DefView", None)

            if not icon_layer:
                continue

            workerw_old = win32gui.FindWindowEx(None, workerw_top, "WorkerW", None)
            break


        if not workerw_old:

            # 新版 Windows 11: 在 Progman 底下找尋或建立 WorkerW
            mode = "progman-workerw"
            workerw = win32gui.FindWindowEx(progman, None, "WorkerW", None)

            if not workerw:

                mode = "created-workerw"
                stage = "建立備援 WorkerW"
                workerw = create_workerw(progman)

        else:

            # 舊版: 掛載到桌面圖示層後方的 WorkerW
            mode = "legacy-workerw"
            workerw = workerw_old


        logger.info("桌布掛載路徑 - mode=%s progman=%#x workerw=%#x", mode, progman, workerw)
        _log_wallpaper_window("WorkerW", workerw)

        if icon_layer:
            _log_wallpaper_window("SHELLDLL_DefView", icon_layer)

        stage = "SetParent"
        win32gui.SetParent(hwnd, workerw)

        if workerw_old:

            stage = "SetWindowPos"
            win32gui.SetWindowPos(
                hwnd, icon_layer, 0, 0, 0, 0,
                win32con.SWP_NOSIZE | win32con.SWP_NOMOVE
            )


        # 父層檢查只供診斷, 不因結構不符或查詢失敗中斷既有流程
        try:
            # 使用實際父層, 避免 GetParent 把頂層視窗的 owner 當成 parent
            actual_parent = win32gui.GetAncestor(hwnd, win32con.GA_PARENT)

        except Exception:
            logger.warning(
                "無法確認桌布父視窗, 繼續既有流程 - hwnd=%#x expected=%#x mode=%s",
                hwnd, workerw, mode, exc_info=True,
            )

        else:

            if not workerw or actual_parent != workerw:
                logger.warning(
                    "桌布掛載父視窗不符, 繼續既有流程 - hwnd=%#x expected=%#x actual=%#x mode=%s",
                    hwnd, workerw, actual_parent, mode,
                )

            else:
                logger.info("桌布掛載完成 - hwnd=%#x parent=%#x mode=%s", hwnd, actual_parent, mode)

        _log_wallpaper_window("wallpaper-after", hwnd)

    except Exception:

        logger.exception(
            "桌布掛載失敗 - stage=%s mode=%s hwnd=%#x progman=%#x workerw=%#x",
            stage, mode, hwnd, progman, workerw,
        )
        _log_wallpaper_window("wallpaper-failed", hwnd)
        raise


CLSID_VIRTUAL_DESKTOP_MANAGER = ComGUID("{AA509086-5CA9-4C25-8F95-589D3C07B48A}")


class IVirtualDesktopManager(IUnknown):

    _iid_ = ComGUID("{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}")
    _methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "IsWindowOnCurrentVirtualDesktop",
            (["in"], wintypes.HWND, "top_level_window"),
            (["out"], ctypes.POINTER(wintypes.BOOL), "on_current_desktop")
        ),
    ]


_virtual_desktop_manager = None


def is_on_current_desktop(hwnd: int) -> bool:
    """
    視窗是否在目前桌面
    """

    global _virtual_desktop_manager

    try:

        if _virtual_desktop_manager is None:

            _virtual_desktop_manager = CreateObject(
                CLSID_VIRTUAL_DESKTOP_MANAGER,
                interface=IVirtualDesktopManager
            )

        return bool(_virtual_desktop_manager.IsWindowOnCurrentVirtualDesktop(hwnd))  # type: ignore

    except Exception:
        return False


# 視窗最大化

# 邊界容許值(沒有人會透過螢幕邊緣幾個像素看桌布)
FULLSCREEN_MARGIN = 4

WINDOW_TITLE_BLACKLIST = {
    "",
    "default ime",
    "msctfime ui",
    "windows 輸入體驗",

    "搜尋",
    "search",
    "開始",
    "start",

    "工作檢視",
    "task view",
}

SHELL_CLASSES = {
    "Progman",
    "WorkerW",
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
}

def is_any_fullscreen(wallpaper_hwnd) -> bool:
    """
    偵測是否有任何視窗最大化
    """

    shell_hwnd = ctypes.windll.user32.GetShellWindow()


    has_fullscreen = False

    def enum_windows_callback(hwnd, _) -> bool:

        nonlocal has_fullscreen

        if has_fullscreen:
            return True


        if (
            hwnd == wallpaper_hwnd or  # 自己不算
            hwnd == shell_hwnd         # Progman不算
        ):
            return True


        # 隱藏視窗不算
        if not win32gui.IsWindowVisible(hwnd):
            return True


        # 最小化視窗不算
        if win32gui.IsIconic(hwnd):
            return True


        # 不在目前桌面不算
        if not is_on_current_desktop(hwnd):
            return True


        # Windows Shell / 桌面相關視窗不算
        class_name = win32gui.GetClassName(hwnd)
        if class_name in SHELL_CLASSES:
            return True


        # 黑名單不算
        window_title = win32gui.GetWindowText(hwnd).casefold().strip()

        if window_title in WINDOW_TITLE_BLACKLIST:
            return True


        try:
            monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)

            info = win32api.GetMonitorInfo(monitor)

            # info["Monitor"]: 整個螢幕, info["Work"]: 不包含工作列
            monitor_left, monitor_top, monitor_right, monitor_bottom = info["Work"]

        except Exception:
            return True

        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)

        except Exception:
            return True


        has_fullscreen = (
            left <= monitor_left + FULLSCREEN_MARGIN and
            top <= monitor_top + FULLSCREEN_MARGIN and
            right >= monitor_right - FULLSCREEN_MARGIN and
            bottom >= monitor_bottom - FULLSCREEN_MARGIN
        )


        if has_fullscreen:
            logger.debug("Fullscreen - %s - %s", hwnd, window_title)


        return True


    win32gui.EnumWindows(enum_windows_callback, None)


    return has_fullscreen
