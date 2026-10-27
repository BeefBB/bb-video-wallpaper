import sys
import os
import subprocess
import hashlib
import shutil
import tempfile
from pathlib import Path
import time
import logging
from logging.handlers import RotatingFileHandler

import win32gui
import win32con
import win32api
import win32event
import winerror

import ctypes
from ctypes import wintypes
import psutil

from PySide6.QtWidgets import QApplication, QWidget, QSystemTrayIcon, QMenu, QFileDialog, QLabel
from PySide6.QtCore import Qt, QTimer, QThread, Slot, Signal
from PySide6.QtGui import QAction, QIcon, QFont, QPixmap, QCursor, QImage

from comtypes import GUID as ComGUID, HRESULT, IUnknown, COMMETHOD
from comtypes.client import CreateObject

from config import ConfigStore, SUPPORTED_LANGUAGES
from thumbnails import (
    SUPPORTED_VIDEO_EXTENSIONS,
    SUPPORTED_IMAGE_EXTENSIONS,
    SUPPORTED_MEDIA_EXTENSIONS,
    ThumbnailThread,
)


if getattr(sys, "frozen", False):

    FILE = Path(sys.executable).resolve()
    ROOT_DIR = FILE.parent
    ICON_PATH = ROOT_DIR / "_internal" / "icon" / "bb-video-wallpaper.ico"
    LOADING_IMAGE_PATH = ROOT_DIR / "_internal" / "icon" / "loading.jpg"

else:

    FILE = Path(__file__).resolve()
    ROOT_DIR = FILE.parent
    ICON_PATH = ROOT_DIR / "icon" / "bb-video-wallpaper.ico"
    LOADING_IMAGE_PATH = ROOT_DIR / "icon" / "loading.jpg"


TASK_NAME = "BBVideoWallpaper"
IMPORT_RESERVE_BYTES = 100 * 1024 * 1024


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


# 開啟日誌前先確認單一實例, 避免重複啟動時鎖住日誌檔
already_running = False
if __name__ == "__main__":
    already_running, mutex = is_already_running()


APPDATA_DIR = Path(os.environ["LOCALAPPDATA"]) / "BB" / "BB Video Wallpaper"
APPDATA_DIR.mkdir(parents=True, exist_ok=True)

MEDIA_DIR = APPDATA_DIR / "video"
MEDIA_DIR.mkdir(exist_ok=True)

THUMBNAIL_DIR = APPDATA_DIR / "thumbnails"

CONFIG_PATH = APPDATA_DIR / "config.json"

LOG_DIR = APPDATA_DIR / "log"

if not already_running:
    LOG_DIR.mkdir(exist_ok=True)

    handler = RotatingFileHandler(
        LOG_DIR / "log.log",
        maxBytes=2 * 1024 * 1024,  # 每份最多約 2 MB
        backupCount=3,             # 額外保留 3 份舊日誌
        encoding="utf-8",
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[handler],
    )

logger = logging.getLogger(__name__)


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
        logger.exception("VLC 載入失敗")
        sys.exit(1)
else:
    logger.error("找不到 VLC-lite/ 或 VLC/")
    sys.exit(1)

import vlc


# 設定與語言
config_store = ConfigStore(CONFIG_PATH, MEDIA_DIR, ROOT_DIR / "lang")
LANG = config_store.load_language()

def tr(key: str) -> str:
    """
    文字id -> 顯示文字
    """

    return LANG.get(key, key)


def show_already_running_toast():
    """
    跳出 Windows 系統右下角通知並退出
    """

    app = QApplication(sys.argv)

    tray = QSystemTrayIcon(QIcon(str(ICON_PATH)))
    tray.show()

    # 標題, 內容, 圖示類型, 顯示毫秒數
    tray.showMessage(
        tr("app_name"),
        tr("already_running_message"),
        QSystemTrayIcon.MessageIcon.Information,
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


def get_media() -> list[Path]:
    """
    從 MEDIA_DIR 取得媒體
    """

    return [
        p
        for p in sorted(MEDIA_DIR.rglob("*"), key=lambda p: p.relative_to(MEDIA_DIR).as_posix().casefold())
        if (
            p.is_file() and
            p.suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS
        )
    ]


class MediaImportCancelled(Exception):
    """使用者取消媒體匯入"""


def check_import_cancelled(cancelled) -> None:

    if cancelled():
        raise MediaImportCancelled()


def calculate_sha256(path: Path, cancelled=lambda: False) -> bytes:
    """
    分塊讀取檔案並計算 SHA-256
    """

    digest = hashlib.sha256()

    with path.open("rb") as file:

        while True:

            check_import_cancelled(cancelled)
            chunk = file.read(1024 * 1024)

            if not chunk:
                break

            digest.update(chunk)


    return digest.digest()


def import_media(source: Path, *, progress=None, cancelled=lambda: False) -> Path:
    """
    比對重複內容後分塊複製至 video/video, 支援進度回報與取消
    """

    check_import_cancelled(cancelled)
    source = source.resolve(strict=True)

    if (
        not source.is_file() or
        source.suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS
    ):
        raise ValueError(source.name)


    import_dir = MEDIA_DIR / "video"
    import_dir.mkdir(parents=True, exist_ok=True)

    source_size = source.stat().st_size
    source_hash = None


    for candidate in sorted(import_dir.iterdir()):

        check_import_cancelled(cancelled)

        if (
            not candidate.is_file() or
            candidate.suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS
        ):
            continue

        if candidate.samefile(source):
            return candidate

        if candidate.stat().st_size != source_size:
            continue

        if source_hash is None:
            source_hash = calculate_sha256(source, cancelled)

        if calculate_sha256(candidate, cancelled) == source_hash:
            return candidate


    # 重複檔案不需額外容量; 確定要複製時才檢查並預留 100 MB
    check_import_cancelled(cancelled)
    free_space = shutil.disk_usage(import_dir).free
    required_space = source_size + IMPORT_RESERVE_BYTES

    if free_space < required_space:
        megabyte = 1024 * 1024
        raise OSError(
            tr("media_insufficient_space").format(
                required=(source_size + megabyte - 1) // megabyte,
                available=max(0, free_space - IMPORT_RESERVE_BYTES) // megabyte,
            )
        )


    # 暫存檔不會出現在媒體清單, 完成複製後才改成正式檔名
    with tempfile.NamedTemporaryFile(
        dir=import_dir,
        prefix=".import-",
        suffix=".tmp",
        delete=False
    ) as temp:
        temp_path = Path(temp.name)


    try:

        copied = 0
        last_percent = 0

        if progress is not None:
            progress(0)

        with source.open("rb") as source_file, temp_path.open("wb") as destination_file:

            while True:

                check_import_cancelled(cancelled)
                chunk = source_file.read(1024 * 1024)

                if not chunk:
                    break

                destination_file.write(chunk)
                copied += len(chunk)
                # 正式檔名建立後才回報 100%, 包含關閉檔案時的寫入失敗
                percent = min(99, copied * 100 // max(1, source_size))

                if progress is not None and percent != last_percent:
                    progress(percent)
                    last_percent = percent

        destination = import_dir / source.name
        index = 1

        while True:

            check_import_cancelled(cancelled)

            try:
                # Windows 的 rename 不會覆蓋已存在的檔案
                temp_path.rename(destination)

                if progress is not None:
                    progress(100)

                return destination

            except FileExistsError:
                suffix = f"({index}){source.suffix}"
                stem = source.stem
                encoded_stem = stem.encode("utf-16-le")
                # Windows 檔名上限以 UTF-16 單位計算
                max_stem_units = 255 - len(suffix.encode("utf-16-le")) // 2

                if len(encoded_stem) // 2 > max_stem_units:
                    # 截斷時額外預留 4 個單位, 並避免留下不完整的雙單位字元
                    stem = encoded_stem[:(max_stem_units - 4) * 2].decode("utf-16-le", errors="ignore")

                destination = import_dir / f"{stem}{suffix}"
                index += 1

    finally:
        temp_path.unlink(missing_ok=True)


class MediaImportThread(QThread):

    progress = Signal(int)

    def __init__(self, source: Path):

        super().__init__()


        self.source = source
        self.destination: Path | None = None
        self.error = ""
        self.cancelled = False


    def run(self) -> None:

        try:
            self.destination = import_media(
                self.source,
                progress=self.progress.emit,
                cancelled=self.isInterruptionRequested,
            )

        except MediaImportCancelled:
            self.cancelled = True
            logger.info("已取消新增媒體 - %s", self.source)

        except Exception as e:
            logger.exception("新增媒體失敗 - %s", self.source)
            self.error = str(e)


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
            logger.debug("Fullscreen - %s - %s", hwnd, window_title)


        return True


    win32gui.EnumWindows(enum_windows_callback, None)


    return has_fullscreen


class Wallpaper(QWidget):

    playback_started = Signal()


    def __init__(self):

        super().__init__()


        # 正在重建或退出
        self.is_recreating = False
        self.reattach_retry_count = 0
        self.is_exiting = False


        self.current_media: Path | None = None
        media_files = get_media()


        config = config_store.load()

        recent = config.get("recentVideo")

        if recent:
            recent_path = MEDIA_DIR / recent[0]

            if recent_path.is_file():
                self.current_media = recent_path

        if not self.current_media and media_files:
            self.current_media = media_files[0]


        # 建立視窗
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )

        # 不接受滑鼠事件
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

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


        # VLC 回呼只發送訊號, 播放器操作回到 Qt 主執行緒處理
        self.playback_started.connect(
            self.apply_pending_pause,
            Qt.ConnectionType.QueuedConnection
        )
        self.player_events = self.player.event_manager()
        self.player_events.event_attach(
            vlc.EventType.MediaPlayerPlaying,  # type: ignore
            lambda event: self.playback_started.emit()
        )


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
        if self.current_media:

            self.set_play_media(self.current_media)

            self.media_play()
            logger.info("init - play")


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

        self.play_state = "pause"
        self.apply_pending_pause()


    @Slot()
    def apply_pending_pause(self) -> None:

        # 載入完成後依最新狀態補套暫停, 不覆蓋後續的播放或停止要求
        if not self.is_exiting and self.play_state == "pause":
            self.player.set_pause(1)


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

            logger.info("reattach_to_desktop - start")


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
                Qt.WindowType.FramelessWindowHint |
                Qt.WindowType.Tool |
                Qt.WindowType.WindowDoesNotAcceptFocus
            )

            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self.show()


            # 掛載到桌布
            self.attach_to_desktop()


            # 恢復播放與時間進度
            if self.current_media:

                self.set_play_media(self.current_media)

                if curr_time > 0:
                    self.player.set_time(curr_time)


            logger.info("reattach_to_desktop - finish")
            self.is_recreating = False
            self.reattach_retry_count = 0

        except Exception:

            logger.exception("重新掛載桌布時發生錯誤")

            self.is_recreating = False

            if self.reattach_retry_count < 2:

                self.reattach_retry_count += 1

                logger.warning("正在重試重新掛載桌布（第 %s 次）", self.reattach_retry_count)
                QTimer.singleShot(2000, self.reattach_to_desktop)

            else:
                logger.error("重新掛載桌布失敗")


    def on_screen_changed(self, geometry) -> None:
        """
        螢幕解析度變更時重整
        """

        if self.is_recreating:
            return


        logger.info("screen_changed - %s", geometry)
        QTimer.singleShot(200, self.reattach_to_desktop)


    def set_play_media(self, path: Path) -> None:
        """
        把媒體檔案給VLC
        """

        self.current_media = path

        config = config_store.load()
        media_path = path.relative_to(MEDIA_DIR).as_posix()
        recent = config.get("recentVideo", [])
        config["recentVideo"] = [media_path] + [
            item for item in recent if item != media_path
        ][:4]
        config_store.save(config)


        previous_play_state = self.play_state


        self.media_stop()
        logger.debug("set_play_media - stop")


        media = self.instance.media_new(str(path))  # type: ignore
        self.player.set_media(media)
        media.release()
        logger.info("set_play_media - %s", path)

        # 裁切填滿螢幕
        self.player.video_set_crop_geometry(self.get_screen_aspect_ratio())


        # 讓畫面跑出來
        self.media_play()
        logger.debug("set_play_media - play")


        # 回復原樣
        if previous_play_state == "pause":

            self.media_pause()
            logger.debug("set_play_media - pause")

        elif previous_play_state == "stop":

            self.media_stop()
            logger.debug("set_play_media - stop")


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
            logger.error("螢幕電源通知註冊失敗")


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
        logger.debug("fullscreen detection - ready")


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
        if self.current_media and self.current_media.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS:
            return


        need_auto_pause = (
            (self.auto_pause_if_screen_off and self.screen_off) or
            (self.auto_pause_if_on_battery and is_on_battery()) or
            (self.auto_pause_if_fullscreen and self.fullscreen_detection_ready and is_any_fullscreen(int(self.winId())))
        )


        if need_auto_pause:
            if self.play_state == "play":

                self.media_pause()
                logger.info("auto_pause - pause")

        else:
            if self.play_state == "pause":

                self.media_play()
                logger.info("auto_pause - play")


    def unregister_power_notification(self) -> None:
        """
        取消註冊 Windows 螢幕電源通知
        """

        if self.power_notify:

            try:
                ctypes.windll.user32.UnregisterPowerSettingNotification(self.power_notify)

            except Exception:
                logger.exception("取消註冊電源通知時發生錯誤")

            finally:
                self.power_notify = None


class MediaPreviewMenu(QMenu):
    """
    桌布選單的懸停預覽, 在背景驗證並載入對應縮圖
    """

    def __init__(self, title: str, parent=None):

        super().__init__(title, parent)
        self.setMouseTracking(True)


        # 預覽視窗不搶焦點, 也不攔截滑鼠事件
        self.preview = QLabel(self, Qt.WindowType.ToolTip)
        self.preview.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.preview.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.preview.setStyleSheet("background: #202020; padding: 4px;")


        self.loading_pixmap = QPixmap(LOADING_IMAGE_PATH)

        if self.loading_pixmap.isNull():

            self.loading_pixmap = QPixmap(300, 169)
            self.loading_pixmap.fill(Qt.GlobalColor.darkGray)

        self.loading_pixmap = self.loading_pixmap.scaledToWidth(
            300, Qt.TransformationMode.SmoothTransformation
        )

        self.preview_pixmap = self.loading_pixmap
        self.preview_mode = False
        self.preview_action = None


        self.thumbnail_thread = ThumbnailThread(MEDIA_DIR, THUMBNAIL_DIR, self)
        self.thumbnail_thread.thumbnail_ready.connect(self.update_preview)


        # 首次懸停 0.5 秒後顯示預覽
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(500)
        self.preview_timer.timeout.connect(self.show_preview)


        # 離開所有桌布選項滿 1 秒後退出預覽模式
        self.reset_timer = QTimer(self)
        self.reset_timer.setSingleShot(True)
        self.reset_timer.setInterval(1000)
        self.reset_timer.timeout.connect(self.reset_preview)


    def mouseMoveEvent(self, event):

        super().mouseMoveEvent(event)
        action = self.actionAt(event.position().toPoint())

        if (
            action is None or
            not action.isEnabled() or
            action.data() is None
        ):

            self.leave_preview()
            return


        self.reset_timer.stop()

        if action == self.preview_action:
            return

        self.preview_action = action

        if self.preview_mode:
            self.show_preview()

        else:
            self.preview_timer.start()


    def leaveEvent(self, event):

        self.leave_preview()
        super().leaveEvent(event)


    def hideEvent(self, event):

        self.reset_preview()
        super().hideEvent(event)


    def leave_preview(self) -> None:

        self.preview_timer.stop()
        self.preview_action = None
        self.preview.hide()

        if self.preview_mode and not self.reset_timer.isActive():
            self.reset_timer.start()


    def reset_preview(self) -> None:

        self.preview_timer.stop()
        self.reset_timer.stop()
        self.preview_action = None
        self.preview_mode = False
        self.preview.hide()


    def show_preview(self) -> None:

        action = self.preview_action
        cursor = QCursor.pos()

        if (
            not self.isVisible() or
            action is None or
            self.actionAt(self.mapFromGlobal(cursor)) != action
        ):
            return

        # 切換項目時先換成載入圖片, 不保留上一個項目的縮圖
        self.preview_pixmap = self.loading_pixmap
        self.display_preview()

        self.thumbnail_thread.request(Path(action.data()))


    @Slot(str, QImage)
    def update_preview(self, media_path: str, image: QImage) -> None:

        action = self.preview_action

        if (
            not self.preview_mode or
            action is None or
            Path(action.data()) != Path(media_path) or
            image.isNull()
        ):
            return

        # 背景只傳遞 QImage, QPixmap 在主執行緒建立
        self.preview_pixmap = QPixmap.fromImage(image)
        self.display_preview()


    def display_preview(self) -> None:

        action = self.preview_action
        cursor = QCursor.pos()

        if (
            not self.isVisible() or
            action is None or
            self.actionAt(self.mapFromGlobal(cursor)) != action or
            self.preview_pixmap.isNull()
        ):
            return


        # 等比例縮小預覽圖片, 避免超出目前螢幕的可用範圍
        screen = QApplication.screenAt(cursor) or self.screen()
        available = screen.availableGeometry()

        pixmap = self.preview_pixmap.scaled(
            min(300, max(1, available.width() - 8)),
            max(1, available.height() - 8),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )
        self.preview.setPixmap(pixmap)
        self.preview.adjustSize()


        # 優先放在選單右側, 空間不足時改放左側
        menu_rect = self.frameGeometry()
        x = menu_rect.right() + 8

        if x + self.preview.width() > available.right() + 1:
            x = menu_rect.left() - self.preview.width() - 8


        y = self.mapToGlobal(self.actionGeometry(action).topLeft()).y()
        x = max(available.left(), min(x, available.right() - self.preview.width() + 1))
        y = max(available.top(), min(y, available.bottom() - self.preview.height() + 1))


        self.preview.move(x, y)
        self.preview.show()
        self.preview_mode = True


class Tray:

    def __init__(self, app, wallpaper):

        self.app = app
        self.wallpaper = wallpaper
        self.import_thread: MediaImportThread | None = None
        self.app.aboutToQuit.connect(self.wait_for_import)


        config = config_store.load()


        self.current_language_code = config.get("language", "zh-TW")


        self.tray = QSystemTrayIcon()
        self.tray.setIcon(QIcon(str(ICON_PATH)))
        self.tray.setToolTip(tr("app_name"))


        menu = QMenu()


        play_action = QAction(tr("play"), menu)
        pause_action = QAction(tr("pause"), menu)
        stop_action = QAction(tr("stop"), menu)
        exit_action = QAction(tr("exit"), menu)


        for action, state in (
            (play_action, "play"),
            (pause_action, "pause"),
            (stop_action, "stop"),
        ):
            action.setCheckable(True)
            menu.aboutToShow.connect(
                lambda a=action, s=state: a.setChecked(self.wallpaper.play_state == s)
            )

        menu.aboutToShow.connect(
            lambda: pause_action.setEnabled(
                bool(self.wallpaper.current_media)
                and self.wallpaper.current_media.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
            )
        )


        self.add_media_action = QAction(tr("add_media"), menu)
        self.add_media_action.triggered.connect(self.add_media)

        self.cancel_import_action = QAction(tr("cancel_media_import"), menu)
        self.cancel_import_action.triggered.connect(self.cancel_import)
        self.cancel_import_action.setVisible(False)

        open_media_action = QAction(tr("open_video_folder"), menu)
        open_media_action.triggered.connect(self.open_media_folder)


        auto_pause_if_fullscreen_action = QAction(tr("auto_pause_fullscreen"), menu)
        auto_pause_if_fullscreen_action.setCheckable(True)
        auto_pause_if_fullscreen_action.setChecked(
            config.get("autoPauseIfFullscreen", True)
        )

        auto_pause_if_screen_off_action = QAction(tr("auto_pause_screen_off"), menu)
        auto_pause_if_screen_off_action.setCheckable(True)
        auto_pause_if_screen_off_action.setChecked(
            config.get("autoPauseIfScreenOff", True)
        )

        auto_pause_if_on_battery_action = QAction(tr("auto_pause_battery"), menu)
        auto_pause_if_on_battery_action.setCheckable(True)
        auto_pause_if_on_battery_action.setChecked(
            config.get("autoPauseIfOnBattery", False)
        )

        startup_action = QAction(tr("startup"), menu)
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


        self.media_menu = MediaPreviewMenu(tr("select_video"), menu)
        self.app.aboutToQuit.connect(self.media_menu.thumbnail_thread.stop)
        menu.addMenu(self.media_menu)
        self.media_menu.aboutToShow.connect(
            self.refresh_media_menu
        )

        menu.addAction(self.add_media_action)
        menu.addAction(self.cancel_import_action)
        menu.addAction(open_media_action)


        menu.addSeparator()


        self.options_menu = menu.addMenu(tr("options"))

        self.options_menu.addAction(auto_pause_if_fullscreen_action)
        self.options_menu.addAction(auto_pause_if_screen_off_action)
        self.options_menu.addAction(auto_pause_if_on_battery_action)

        self.options_menu.addSeparator()

        self.options_language_menu = self.options_menu.addMenu(tr("language"))
        self.options_language_menu.aboutToShow.connect(
            self.refresh_language_menu
        )

        self.options_menu.addSeparator()

        self.options_menu.addAction(startup_action)


        menu.addSeparator()


        menu.addAction(exit_action)


        self.tray.setContextMenu(menu)

        self.tray.show()


    def play(self) -> None:
        if self.wallpaper.current_media:

            self.wallpaper.media_play()
            self.wallpaper.enable_auto_pause = True
            logger.info("manual - play")


    def pause(self) -> None:
        if self.wallpaper.current_media:

            if self.wallpaper.current_media.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS:

                if self.wallpaper.play_state == "play":

                    self.wallpaper.media_pause()

        self.wallpaper.enable_auto_pause = False
        logger.info("manual - pause")


    def stop(self) -> None:

        self.wallpaper.media_stop()
        self.wallpaper.enable_auto_pause = False
        logger.info("manual - stop")


    def refresh_language_menu(self) -> None:

        self.options_language_menu.clear()

        for language_display, language_code in SUPPORTED_LANGUAGES.items():

            action = QAction(language_display, self.options_language_menu)

            action.triggered.connect(
                lambda checked=False, c=language_code: self.set_language(c)
            )

            action.setCheckable(True)
            action.setChecked(language_code == self.current_language_code)

            self.options_language_menu.addAction(action)


    def refresh_media_menu(self) -> None:

        self.media_menu.reset_preview()
        self.media_menu.clear()

        recent = config_store.load().get("recentVideo", [])[:5]

        for media_path in recent:
            self.add_media_menu_action(MEDIA_DIR / media_path)

        if recent:
            clear_action = QAction(tr("clear_recent_videos"), self.media_menu)
            clear_action.setEnabled(len(recent) > 1)
            clear_action.triggered.connect(self.clear_recent_media)
            self.media_menu.addAction(clear_action)
            self.media_menu.addSeparator()

        media_files = get_media()

        if not media_files:
            action = QAction(tr("no_video"), self.media_menu)
            action.setEnabled(False)
            self.media_menu.addAction(action)
            return

        for media_path in media_files:
            self.add_media_menu_action(media_path)


    def clear_recent_media(self) -> None:

        config = config_store.load()
        config["recentVideo"] = config.get("recentVideo", [])[:1]
        config_store.save(config)

        logger.info("clear_recent_media")


    def add_media_menu_action(self, media_path: Path) -> None:

        action = QAction(media_path.name, self.media_menu)
        action.setData(str(media_path))

        action.triggered.connect(
            lambda checked=False, v=media_path: self.play_from_menu(v)
        )

        action.setCheckable(True)
        action.setChecked(media_path == self.wallpaper.current_media)
        action.setEnabled(media_path.is_file())

        self.media_menu.addAction(action)


    def set_language(self, language_code: str):

        if language_code == self.current_language_code:
            return

        self.current_language_code = language_code
        config_store.load_language(language_code)

        logger.info("set_language - %s", language_code)


    def play_from_menu(self, path: Path):

        self.wallpaper.set_play_media(path)
        self.wallpaper.media_play()
        self.wallpaper.enable_auto_pause = True
        logger.info("play_from_menu - play")


    def add_media(self) -> None:

        if self.import_thread is not None:
            return

        self.add_media_action.setEnabled(False)

        patterns = " ".join(
            f"*{ext}"
            for ext in sorted(SUPPORTED_MEDIA_EXTENSIONS)
        )

        filename, _ = QFileDialog.getOpenFileName(
            None,
            tr("add_media"),
            "",
            f'{tr("media_files")} ({patterns})',
        )

        if not filename:
            self.add_media_action.setEnabled(True)
            return

        self.add_media_action.setText(tr("comparing_media"))
        self.cancel_import_action.setEnabled(True)
        self.cancel_import_action.setVisible(True)

        self.import_thread = MediaImportThread(Path(filename))
        self.import_thread.progress.connect(self.update_import_progress, Qt.ConnectionType.QueuedConnection)
        self.import_thread.finished.connect(self.finish_import)
        self.import_thread.start()


    def update_import_progress(self, percent: int) -> None:

        if self.import_thread is not None and not self.import_thread.isInterruptionRequested():
            self.add_media_action.setText(tr("importing_media").format(percent=percent))


    def cancel_import(self) -> None:

        if self.import_thread is not None and self.import_thread.isRunning():
            self.import_thread.requestInterruption()
            self.cancel_import_action.setEnabled(False)


    def finish_import(self) -> None:

        worker = self.import_thread

        if worker is None:
            return

        self.import_thread = None
        worker.deleteLater()

        self.add_media_action.setText(tr("add_media"))
        self.add_media_action.setEnabled(True)
        self.cancel_import_action.setVisible(False)

        if self.wallpaper.is_exiting or worker.cancelled:
            return

        if worker.destination is not None:

            self.media_menu.thumbnail_thread.request(worker.destination)
            self.play_from_menu(worker.destination)
            self.refresh_media_menu()

        else:
            self.tray.showMessage(
                tr("app_name"),
                tr("media_add_failed").format(error=worker.error),
                QSystemTrayIcon.MessageIcon.Information,
                3000
            )


    def wait_for_import(self) -> None:

        if self.import_thread is not None:
            self.import_thread.requestInterruption()
            self.import_thread.wait()


    def open_media_folder(self) -> None:
        os.startfile(str(MEDIA_DIR))


    def set_auto_pause_option(self, config_key: str, attribute: str, checked: bool) -> None:

        config = config_store.load()
        config[config_key] = checked
        config_store.save(config)

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
            self.wallpaper.player_events.event_detach(vlc.EventType.MediaPlayerPlaying)  # type: ignore

            self.wallpaper.media_stop()

            self.wallpaper.player.release()
            self.wallpaper.instance.release()

        except Exception:
            logger.exception("退出時發生錯誤")

        self.app.quit()
        logger.info("exit")


if __name__ == "__main__":

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

    # 系統匣程式在關閉選檔視窗後仍需繼續執行
    app.setQuitOnLastWindowClosed(False)


    font = QFont()
    font.setPointSize(11)
    app.setFont(font)


    # 建立播放器
    w = Wallpaper()
    w.show()


    tray = Tray(app, w)


    # Qt 主迴圈
    sys.exit(app.exec())
