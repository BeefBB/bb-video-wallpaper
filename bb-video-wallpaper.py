import sys
import os
import subprocess
import json
from pathlib import Path
import time

import win32gui
import win32con
import win32api
import win32event
import winerror

import ctypes
from ctypes import wintypes
import psutil

from PySide6.QtWidgets import QApplication, QWidget, QSystemTrayIcon, QMenu
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QFont

from comtypes import GUID as ComGUID, HRESULT, IUnknown, COMMETHOD
from comtypes.client import CreateObject


if getattr(sys, "frozen", False):

    FILE = Path(sys.executable).resolve()
    ROOT_DIR = FILE.parent
    ICON_PATH = ROOT_DIR / "_internal" / "icon" / "bb-video-wallpaper.ico"

else:

    FILE = Path(__file__).resolve()
    ROOT_DIR = FILE.parent
    ICON_PATH = ROOT_DIR / "icon" / "bb-video-wallpaper.ico"


TASK_NAME = "BBVideoWallpaper"


APPDATA_DIR = (Path(os.getenv("LOCALAPPDATA")) / "BB" / "BB Video Wallpaper")  # type: ignore
APPDATA_DIR.mkdir(parents=True, exist_ok=True)

VIDEO_DIR = APPDATA_DIR / "video"
VIDEO_DIR.mkdir(exist_ok=True)

CONFIG_PATH = APPDATA_DIR / "config.json"


VLC_DIR = next(
    (
        path
        for path in (ROOT_DIR / "VLC-lite", ROOT_DIR / "VLC")
        if path.is_dir()
    ),
    None
)

if VLC_DIR:
    os.add_dll_directory(str(VLC_DIR))
    os.environ["PYTHON_VLC_LIB_PATH"] = str(VLC_DIR / "libvlc.dll")
    os.environ["PYTHON_VLC_MODULE_PATH"] = str(VLC_DIR)

    try:
        ctypes.CDLL(str(VLC_DIR / "libvlccore.dll"))
        ctypes.CDLL(str(VLC_DIR / "libvlc.dll"))
    except OSError:
        print("VLC 載入失敗")
        sys.exit(1)
else:
    print("找不到 VLC-lite/ 或 VLC/")
    sys.exit(1)

import vlc


# 唯一的 Mutex 名稱
MUTEX_NAME = "Global\\BBVideoWallpaper_SingleInstance_Mutex"

def is_already_running():
    """
    檢查程式是否已經在執行中
    """

    # 建立全域 Mutex
    mutex = win32event.CreateMutex(None, False, MUTEX_NAME)  # type: ignore


    # 檢查錯誤碼
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        return True, mutex

    return False, mutex


def show_already_running_toast():
    """
    跳出 Windows 系統右下角通知並退出
    """

    app = QApplication(sys.argv)

    tray = QSystemTrayIcon(QIcon(str(ICON_PATH)))
    tray.show()

    # 標題, 內容, 圖示類型, 顯示毫秒數
    tray.showMessage(
        "BB Video Wallpaper",
        "程式已經在背景執行中!!\n從系統匣查看它",
        QSystemTrayIcon.Information,  # type: ignore
        3000
    )

    # 延遲後退出
    QTimer.singleShot(3000, app.quit)
    app.exec()


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


# 明確指定 Windows API 的參數與回傳值型別, 避免 64 位元 Handle 被截斷
ctypes.windll.user32.RegisterPowerSettingNotification.restype = wintypes.HANDLE
ctypes.windll.user32.RegisterPowerSettingNotification.argtypes = [
    wintypes.HANDLE,
    ctypes.c_void_p,
    wintypes.DWORD
]

ctypes.windll.user32.UnregisterPowerSettingNotification.restype = wintypes.BOOL
ctypes.windll.user32.UnregisterPowerSettingNotification.argtypes = [
    wintypes.HANDLE
]


SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi",}
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp",}
SUPPORTED_MEDIA_EXTENSIONS = (SUPPORTED_VIDEO_EXTENSIONS | SUPPORTED_IMAGE_EXTENSIONS)


def run_as_admin() -> None:
    """
    檢查是否為系統管理員權限
    """

    try:
        is_admin = ctypes.windll.shell32.IsUserAnAdmin()

    except Exception:
        is_admin = False

    if not is_admin:
        print()
        print("請以系統管理員執行")
        input("Press Enter...")

        sys.exit()


def check_desktop_ready() -> bool:
    """
    檢查是否登入且載入桌面
    """

    if not win32gui.FindWindow("Shell_TrayWnd", None):
        return False


    if not win32gui.FindWindow("Progman", None):
        return False


    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] and proc.info['name'].lower() == 'explorer.exe':
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


    # 0 = 沒插電
    # 1 = 接上 AC 電源
    # 255 = 未知
    return status.ACLineStatus == 0


def load_config() -> dict:

    if not CONFIG_PATH.exists():
        return {}

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    except Exception as e:

        print(f"讀取config時發生錯誤")
        print(f" > {e}")

        return {}


def save_config(config) -> None:

    temp_path = CONFIG_PATH.with_suffix(".tmp")

    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(
                config,
                f,
                indent=2,
                ensure_ascii=False
            )
            f.flush()
            os.fsync(f.fileno())

        os.replace(temp_path, CONFIG_PATH)

    except Exception as e:
        print(f"儲存 config 時發生錯誤")
        print(f" > {e}")

        try:
            if temp_path.exists():
                temp_path.unlink()

        except Exception:
            pass


def create_task() -> None:
    """
    加入 Windows 工作排程器
    """

    cmd = [
        "schtasks",
        "/Create",
        "/TN", TASK_NAME,
        "/TR", f'"{str(FILE)}" --task',
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


def get_media() -> list[Path]:
    """
    從 VIDEO_DIR 取得媒體
    """

    return [
        p
        for p in sorted(VIDEO_DIR.rglob("*"), key=lambda p: p.relative_to(VIDEO_DIR).as_posix().casefold())
        if (
            p.is_file() and
            p.suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS
        )
    ]


def create_workerw(parent_hwnd):
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

    except Exception:
        # WorkerW 類別可能已經註冊, 直接忽略即可
        pass


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

    return hwnd


def set_windows_as_wallpaper(hwnd) -> None:
    """
    將視窗放到桌布
    """

    progman = win32gui.FindWindow("Progman", "Program Manager")

    if not progman:
        progman = win32gui.FindWindow("Progman", None)

    if not progman:
        raise RuntimeError("找不到 Progman 視窗")


    # 發送舊版 0x052C 訊息讓桌面分裂
    # 把本來黏在一起的 "桌面圖示" 與 "背景桌布" 分離開來, 希望在中間炸出一個獨立的 WorkerW 視窗
    win32gui.SendMessageTimeout(progman, 0x052C, 0, None, 0, 0x03E8)
    win32gui.SendMessageTimeout(progman, 0x052C, 0xD, None, 0, 0x03E8)
    win32gui.SendMessageTimeout(progman, 0x052C, 0xD, 1, 0, 0x03E8)


    WorkerW_top = None
    SHELLDLL_DefView = None
    WorkerW_first = None
    WorkerW_old = None


    # 嘗試搜尋舊版分裂產生的 WorkerW
    while True:

        WorkerW_top = win32gui.FindWindowEx(None, WorkerW_top, "WorkerW", None)
        if WorkerW_top == WorkerW_first:
            # 直到找不到分裂的桌面圖示層
            break

        if WorkerW_first is None:
            WorkerW_first = WorkerW_top

        if not WorkerW_top:
            continue

        SHELLDLL_DefView = win32gui.FindWindowEx(WorkerW_top, None, "SHELLDLL_DefView", None)
        if not SHELLDLL_DefView:
            continue

        WorkerW_old = win32gui.FindWindowEx(None, WorkerW_top, "WorkerW", None)
        break


    # 判斷使用新版或舊版機制
    if not WorkerW_old:

        # 新版 Windows 11: 直接在 Progman 底下找尋或創建 WorkerW
        new_workerw = win32gui.FindWindowEx(progman, None, "WorkerW", None)
        if not new_workerw:
            new_workerw = create_workerw(progman)

        ctypes.windll.user32.SetParent(hwnd, new_workerw)

    else:

        # 舊版: 直接掛載到分裂後的 WorkerW
        ctypes.windll.user32.SetParent(hwnd, WorkerW_old)
        ctypes.windll.user32.SetWindowPos(
            hwnd,

            SHELLDLL_DefView,

            0, 0, 0, 0,

            0x0001 | 0x0002
        )


CLSID_VIRTUAL_DESKTOP_MANAGER = ComGUID(
    "{AA509086-5CA9-4C25-8F95-589D3C07B48A}"
)


class IVirtualDesktopManager(IUnknown):
    _iid_ = ComGUID("{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}")
    _methods_ = [
        COMMETHOD(
            [],
            HRESULT,
            "IsWindowOnCurrentVirtualDesktop",
            (["in"], wintypes.HWND, "top_level_window"),
            (["out"], ctypes.POINTER(wintypes.BOOL), "on_current_desktop"),
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
                interface=IVirtualDesktopManager,
            )

        return bool(_virtual_desktop_manager.IsWindowOnCurrentVirtualDesktop(hwnd))  # type: ignore

    except Exception:
        return False


# 邊界容許值
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

    def enum_windows_callback(hwnd, _):

        nonlocal has_fullscreen

        if has_fullscreen:
            return True


        if (
            hwnd == wallpaper_hwnd or                  # 自己不算
            hwnd == shell_hwnd                         # Progman不算
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


        # 黑名單 不算
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
            print(f"Fullscreen - {hwnd} - {window_title}")


        return True


    win32gui.EnumWindows(enum_windows_callback, None)


    return has_fullscreen


class Wallpaper(QWidget):

    def __init__(self):

        super().__init__()


        # 正在重建或退出
        self.is_recreating = False
        self.reattach_retry_count = 0
        self.is_exiting = False


        self.current_video: Path | None = None
        videos = get_media()


        config = load_config()

        recent = config.get("recentVideo")

        if recent:
            recent_path = VIDEO_DIR / recent

            if recent_path.is_file():
                self.current_video = recent_path

        if not self.current_video and videos:
            self.current_video = videos[0]


        # 建立視窗
        self.setWindowFlags(
            Qt.FramelessWindowHint |     # type: ignore
            Qt.Tool |                    # type: ignore
            Qt.WindowDoesNotAcceptFocus  # type: ignore
        )

        # 不接受滑鼠事件
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)  # type: ignore

        # 設成螢幕大小
        screen = QApplication.primaryScreen()
        self.setGeometry(screen.geometry())


        # 螢幕解析度變更事件
        if screen:
            screen.geometryChanged.connect(self.on_screen_changed)


        # VLC
        self.instance = vlc.Instance(
            "--no-video-title-show",
            "--input-repeat=65535",
            "--no-audio"
        )

        # 建立 VLC 播放器物件
        self.player = self.instance.media_player_new()  # type: ignore


        self.enable_auto_pause = True
        self.play_state = "stop"  # "play", "pause", "stop"


        self.fullscreen_detection_ready = False
        QTimer.singleShot(10000, self.enable_fullscreen_detection)


        # 螢幕亮滅
        self.screen_off = False
        # 監聽螢幕亮滅的事件
        self.power_notify = None


        self.auto_pause_if_fullscreen = config.get("autoPauseIfFullscreen", True)
        self.auto_pause_if_screen_off = config.get("autoPauseIfScreenOff", True)
        self.auto_pause_if_on_battery = config.get("autoPauseIfOnBattery", False)


        # 掛載到桌布
        self.attach_to_desktop()


        # 播放
        if self.current_video:

            self.set_play_media(self.current_video)

            self.media_play()
            print("init - play")


        self.timer = QTimer(self)
        self.timer.timeout.connect(self.check_auto_pause)
        self.timer.start(500)


    def nativeEvent(self, eventType, message):
        """
        處理原生 Windows 訊息迴圈
        """

        if eventType in (b"windows_generic_MSG", "windows_generic_MSG"):

            # 解析 MSG 結構
            msg = wintypes.MSG.from_address(int(message))

            if msg.message == WM_POWERBROADCAST and msg.wParam == PBT_POWERSETTINGCHANGE:

                setting = ctypes.cast(msg.lParam, ctypes.POINTER(POWERBROADCAST_SETTING)).contents

                # 比對 GUID 是否為螢幕顯示狀態變更
                if bytes(setting.PowerSetting) == bytes(GUID_CONSOLE_DISPLAY_STATE):

                    if setting.Data == 0:
                        self.screen_off = True

                    else:
                        self.screen_off = False

                    self.check_auto_pause()

                    return True, 0


            # 監聽 切換虛擬桌面/用戶 時的 WM_DESTROY 訊息
            if msg.message == win32con.WM_DESTROY:

                if not self.is_recreating:

                    # 重新掛載
                    QTimer.singleShot(200, self.reattach_to_desktop)

                return True, 0


        return super().nativeEvent(eventType, message)  # type: ignore


    def media_play(self) -> None:

        self.player.play()
        self.play_state = "play"


    def media_pause(self) -> None:

        self.player.pause()
        self.play_state = "pause"


    def media_stop(self) -> None:

        self.player.stop()
        self.play_state = "stop"


    def reattach_to_desktop(self) -> None:
        """
        重新建立視窗並掛載桌布
        """

        if self.is_exiting:
            return

        if self.is_recreating:
            return

        self.is_recreating = True


        try:

            print("reattach_to_desktop - start")


            # 紀錄當前播放時間進度
            curr_time = self.player.get_time()


            self.unregister_power_notification()
            self.hide()


            # 銷毀失效的 HWND , 重新向 Windows 申請新的 HWND
            self.destroy(True, True)
            self.create()


            # 建立視窗
            screen = QApplication.primaryScreen()

            self.setGeometry(screen.geometry())

            self.setWindowFlags(
                Qt.FramelessWindowHint |     # type: ignore
                Qt.Tool |                    # type: ignore
                Qt.WindowDoesNotAcceptFocus  # type: ignore
            )

            self.setAttribute(Qt.WA_TransparentForMouseEvents, True)  # type: ignore
            self.show()


            # 掛載到桌布
            self.attach_to_desktop()


            # 恢復播放與時間進度
            if self.current_video:

                self.set_play_media(self.current_video)

                if curr_time > 0:
                    self.player.set_time(curr_time)


            print("reattach_to_desktop - finish")
            self.is_recreating = False
            self.reattach_retry_count = 0

        except Exception as e:

            print(f"重新掛載桌布時發生錯誤")
            print(f" > {e}")

            self.is_recreating = False

            if self.reattach_retry_count < 2:

                self.reattach_retry_count += 1

                print("正在重試")
                QTimer.singleShot(2000, self.reattach_to_desktop)

            else:
                print("重新掛載桌布失敗")


    def on_screen_changed(self, geometry) -> None:
        """
        螢幕解析度變更時重整
        """

        if self.is_recreating:
            return


        print(f"screen_changed - {geometry}")
        QTimer.singleShot(200, self.reattach_to_desktop)


    def set_play_media(self, path: Path) -> None:
        """
        把媒體檔案給VLC
        """

        self.current_video = path

        config = load_config()
        config["recentVideo"] = path.relative_to(VIDEO_DIR).as_posix()
        save_config(config)


        previous_play_state = self.play_state


        self.media_stop()
        print("set_play_media - stop")


        media = self.instance.media_new(str(path))  # type: ignore
        self.player.set_media(media)
        print(f"set_play_media - {path}")

        # 裁切填滿螢幕
        self.player.video_set_crop_geometry(self.get_screen_aspect_ratio())


        # 讓畫面跑出來
        self.media_play()
        print("set_play_media - play")


        # 回復原樣
        if previous_play_state == "pause":

            self.media_pause()
            print("set_play_media - pause")

        elif previous_play_state == "stop":

            self.media_stop()
            print("set_play_media - stop")


    def attach_to_desktop(self) -> None:
        """
        將 Qt 視窗移到 Windows 桌布層
        """

        hwnd = int(self.winId())


        set_windows_as_wallpaper(hwnd)


        # 重新設定 HWND 給 VLC 指向
        self.player.set_hwnd(hwnd)


        self.power_notify = ctypes.windll.user32.RegisterPowerSettingNotification(
            hwnd,
            ctypes.byref(GUID_CONSOLE_DISPLAY_STATE),
            DEVICE_NOTIFY_WINDOW_HANDLE
        )

        if not self.power_notify:
            print("螢幕電源通知註冊失敗")


    def get_screen_aspect_ratio(self) -> str:

        screen = QApplication.primaryScreen()
        geometry = screen.geometry()

        width = geometry.width()
        height = geometry.height()

        return f"{width}:{height}"


    def enable_fullscreen_detection(self) -> None:
        """
        啟動全螢幕視窗偵測
        """

        self.fullscreen_detection_ready = True
        print("fullscreen detection - ready")


    def check_auto_pause(self) -> None:
        """
        檢查是否需要自動暫停
        """

        # 若正在重建視窗則跳過
        if self.is_recreating:
            return

        if not self.enable_auto_pause:
            return

        # 圖片不需要暫停
        if self.current_video and self.current_video.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS:
            return


        any_fullscreen = False

        if self.fullscreen_detection_ready:
            hwnd = int(self.winId())
            any_fullscreen = is_any_fullscreen(hwnd)

        need_auto_pause = (
            (self.auto_pause_if_fullscreen and any_fullscreen) or
            (self.auto_pause_if_screen_off and self.screen_off) or
            (self.auto_pause_if_on_battery and is_on_battery())
        )


        if need_auto_pause:
            if self.play_state == "play":

                self.media_pause()
                print("auto_pause - pause")

        else:
            if self.play_state == "pause":

                self.media_play()
                print("auto_pause - play")


    def unregister_power_notification(self) -> None:
        """
        取消註冊 Windows 螢幕電源通知
        """

        if self.power_notify:

            try:
                ctypes.windll.user32.UnregisterPowerSettingNotification(self.power_notify)

            except Exception as e:
                print(f"取消註冊電源通知時發生錯誤")
                print(f" > {e}")

            finally:
                self.power_notify = None


class Tray:

    def __init__(self, app, wallpaper):

        self.app = app
        self.wallpaper = wallpaper


        config = load_config()


        self.tray = QSystemTrayIcon()
        self.tray.setIcon(QIcon(str(ICON_PATH)))
        self.tray.setToolTip("BB Video Wallpaper")


        menu = QMenu()


        play_action = QAction("播放", menu)
        pause_action = QAction("暫停", menu)
        stop_action = QAction("停止", menu)
        exit_action = QAction("退出", menu)


        open_video_action = QAction("開啟影片資料夾", menu)
        open_video_action.triggered.connect(self.open_video_folder)


        auto_pause_if_fullscreen_action = QAction("視窗最大化時自動暫停", menu)
        auto_pause_if_fullscreen_action.setCheckable(True)
        auto_pause_if_fullscreen_action.setChecked(
            config.get("autoPauseIfFullscreen", True)
        )

        auto_pause_if_screen_off_action = QAction("螢幕關閉時自動暫停", menu)
        auto_pause_if_screen_off_action.setCheckable(True)
        auto_pause_if_screen_off_action.setChecked(
            config.get("autoPauseIfScreenOff", True)
        )

        auto_pause_if_on_battery_action = QAction("沒插電時自動暫停", menu)
        auto_pause_if_on_battery_action.setCheckable(True)
        auto_pause_if_on_battery_action.setChecked(
            config.get("autoPauseIfOnBattery", False)
        )

        startup_action = QAction("開機自動啟動", menu)
        startup_action.setCheckable(True)
        startup_action.setChecked(task_exists())


        play_action.triggered.connect(self.play)
        pause_action.triggered.connect(self.pause)
        stop_action.triggered.connect(self.stop)
        exit_action.triggered.connect(self.exit)


        auto_pause_if_fullscreen_action.triggered.connect(
            lambda checked: self.set_auto_pause_option(
                "autoPauseIfFullscreen",
                "auto_pause_if_fullscreen",
                checked,
            )
        )
        auto_pause_if_screen_off_action.triggered.connect(
            lambda checked: self.set_auto_pause_option(
                "autoPauseIfScreenOff",
                "auto_pause_if_screen_off",
                checked,
            )
        )
        auto_pause_if_on_battery_action.triggered.connect(
            lambda checked: self.set_auto_pause_option(
                "autoPauseIfOnBattery",
                "auto_pause_if_on_battery",
                checked,
            )
        )

        startup_action.triggered.connect(self.toggle_startup)


        menu.addAction(play_action)
        menu.addAction(pause_action)
        menu.addAction(stop_action)


        menu.addSeparator()


        self.video_menu = menu.addMenu("選擇影片")
        self.video_menu.aboutToShow.connect(
            self.refresh_video_menu
        )

        menu.addAction(open_video_action)


        menu.addSeparator()


        self.options_menu = menu.addMenu("選項")

        self.options_menu.addAction(auto_pause_if_fullscreen_action)
        self.options_menu.addAction(auto_pause_if_screen_off_action)
        self.options_menu.addAction(auto_pause_if_on_battery_action)

        self.options_menu.addSeparator()

        self.options_menu.addAction(startup_action)


        menu.addSeparator()


        menu.addAction(exit_action)


        self.tray.setContextMenu(menu)

        self.tray.show()


    def play(self) -> None:
        if self.wallpaper.current_video:

            self.wallpaper.media_play()
            self.wallpaper.enable_auto_pause = True
            print("manual - play")


    def pause(self) -> None:
        if self.wallpaper.current_video:

            if self.wallpaper.current_video.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS:

                if self.wallpaper.play_state == "play":

                    self.wallpaper.media_pause()

        self.wallpaper.enable_auto_pause = False
        print("manual - pause")


    def stop(self) -> None:

        self.wallpaper.media_stop()
        self.wallpaper.enable_auto_pause = False
        print("manual - stop")


    def refresh_video_menu(self) -> None:

        self.video_menu.clear()

        videos = get_media()

        if not videos:
            action = QAction("沒有影片", self.video_menu)
            action.setEnabled(False)
            self.video_menu.addAction(action)
            return

        for video in videos:
            action = QAction(video.stem, self.video_menu)

            action.triggered.connect(
                lambda checked=False, v=video: self.play_from_menu(v)
            )

            action.setCheckable(True)
            action.setChecked(video == self.wallpaper.current_video)

            self.video_menu.addAction(action)


    def play_from_menu(self, path: Path):

        self.wallpaper.set_play_media(path)
        self.wallpaper.media_play()
        self.wallpaper.enable_auto_pause = True
        print("play_from_menu - play")


    def open_video_folder(self) -> None:
        os.startfile(str(VIDEO_DIR))


    def set_auto_pause_option(self, config_key: str, attribute: str, checked: bool) -> None:

        config = load_config()
        config[config_key] = checked
        save_config(config)

        setattr(self.wallpaper, attribute, checked)


    def toggle_startup(self, checked) -> None:
        if checked:
            create_task()
        else:
            delete_task()


    def exit(self) -> None:
        try:
            self.wallpaper.is_exiting = True

            self.wallpaper.unregister_power_notification()

            self.wallpaper.media_stop()

            self.wallpaper.player.release()
            self.wallpaper.instance.release()

        except Exception as e:
            print(f"退出時發生錯誤")
            print(f" > {e}")

        self.app.quit()
        print("exit")


if __name__ == "__main__":

    # 檢查是否重複執行
    already_running, mutex = is_already_running()

    if already_running:

        show_already_running_toast()

        sys.exit(0)


    while True:
        time.sleep(1)

        if check_desktop_ready():
            break


    if "--task" not in sys.argv:
        run_as_admin()


    # 建立 Qt 應用程式
    app = QApplication(sys.argv)


    font = QFont()
    font.setPointSize(11)
    app.setFont(font)


    # 建立播放器
    w = Wallpaper()
    w.show()


    tray = Tray(app, w)


    # Qt 主迴圈
    sys.exit(app.exec())