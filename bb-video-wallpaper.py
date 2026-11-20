import ctypes
from collections.abc import Sequence
import sys
import os
from ctypes import wintypes
from pathlib import Path
from stat import S_ISREG
import time
import subprocess
import logging
from logging.handlers import RotatingFileHandler
from typing import cast

from win32con import WM_DESTROY

from PySide6.QtWidgets import QApplication, QWidget, QSystemTrayIcon, QMenu, QFileDialog, QMessageBox
from PySide6.QtCore import QByteArray, Qt, QTimer, QThread, Signal
from PySide6.QtGui import QAction, QActionGroup, QIcon, QFont, QImage, QScreen, QCursor

import bb_image_display
import bb_video_display
from config import CONFIG_DEFAULTS, ConfigStore, SUPPORTED_LANGUAGES, get_scaling_mode
from media_import import MediaImportCancelled, check_import_cancelled, import_media
from media_preview import MediaPreviewMenu
from playback import AutoPauseReason, PlaybackState, update_auto_pause
from screen_follow import AutoPauseHandler, FollowController, RESTORE_DELAY_MS, SYNC_INTERVAL_MS
from thumbnails import (
    SUPPORTED_VIDEO_EXTENSIONS,
    SUPPORTED_IMAGE_EXTENSIONS,
    SUPPORTED_MEDIA_EXTENSIONS,
    ThumbnailThread,
    cleanup_orphaned_thumbnails,
)
from windows_integration import (
    check_desktop_ready,
    create_task,
    delete_task,
    get_display_state,
    get_monitor_id,
    is_already_running,
    is_any_fullscreen,
    is_on_battery,
    register_power_notification,
    register_taskbar_created_notification,
    run_as_admin,
    set_windows_as_wallpaper,
    task_exists,
    unregister_power_notification,
)


logger = logging.getLogger(__name__)


# 由啟動流程設定的路徑
FILE: Path
ROOT_DIR: Path
ICON_PATH: Path
LOADING_IMAGE_PATH: Path
APPDATA_DIR: Path
MEDIA_DIR: Path
SINGLE_IMPORT_DIR: Path
THUMBNAIL_DIR: Path
CONFIG_PATH: Path
LOG_DIR: Path

# 共用物件
config_store: ConfigStore
LANG: dict[str, str] = {}

_vlc_dll_directory: os._AddedDllDirectory | None = None


def initialize_paths() -> None:
    """
    初始化路徑
    """

    global FILE, ROOT_DIR, ICON_PATH, LOADING_IMAGE_PATH
    global APPDATA_DIR, MEDIA_DIR, SINGLE_IMPORT_DIR, THUMBNAIL_DIR, CONFIG_PATH, LOG_DIR

    if getattr(sys, "frozen", False):

        FILE = Path(sys.executable).resolve()
        ROOT_DIR = FILE.parent
        icon_dir = ROOT_DIR / "_internal" / "icon"

    else:

        FILE = Path(__file__).resolve()
        ROOT_DIR = FILE.parent
        icon_dir = ROOT_DIR / "icon"

    ICON_PATH = icon_dir / "bb-video-wallpaper.ico"
    LOADING_IMAGE_PATH = icon_dir / "loading300.jpg"

    APPDATA_DIR = Path(os.environ["LOCALAPPDATA"]) / "BB" / "BB Video Wallpaper"
    MEDIA_DIR = APPDATA_DIR / "video"
    SINGLE_IMPORT_DIR = MEDIA_DIR / "_imports"
    THUMBNAIL_DIR = APPDATA_DIR / "thumbnails"
    CONFIG_PATH = APPDATA_DIR / "config.json"
    LOG_DIR = APPDATA_DIR / "log"

    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)


def initialize_logging() -> None:
    """
    初始化日誌
    """

    handler = RotatingFileHandler(
        LOG_DIR / "log.log",
        maxBytes=2 * 1024 * 1024,  # 每份最多約 2 MB
        backupCount=3,             # 額外保留 3 份舊日誌
        encoding="utf-8"
    )

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[handler]
    )


def initialize_vlc() -> bool:
    """
    設定 VLC DLL 搜尋位置並載入原生函式庫
    """

    global _vlc_dll_directory

    vlc_dir = next(
        (path for path in (ROOT_DIR / "VLC-lite", ROOT_DIR / "VLC") if path.is_dir()),
        None
    )

    if vlc_dir is None:
        logger.error("找不到 VLC-lite/ 或 VLC/")
        return False

    _vlc_dll_directory = os.add_dll_directory(str(vlc_dir))
    os.environ["PYTHON_VLC_LIB_PATH"] = str(vlc_dir / "libvlc.dll")
    os.environ["PYTHON_VLC_MODULE_PATH"] = str(vlc_dir)

    try:
        ctypes.CDLL(str(vlc_dir / "libvlccore.dll"))
        ctypes.CDLL(str(vlc_dir / "libvlc.dll"))

    except OSError:
        logger.exception("VLC 載入失敗")
        return False

    return True


def tr(key: str) -> str:
    """
    文字id -> 顯示文字
    """

    return LANG.get(key, key)


def show_already_running_toast() -> None:
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


class MediaImportThread(QThread):
    """
    匯入媒體的背景執行緒
    """

    progress = Signal(object, int, int, bool)

    def __init__(self, sources: list[Path]) -> None:

        super().__init__()


        self.sources = sources
        self.destination: Path | None = None
        self.destinations: list[Path] = []
        self.imported_count = 0
        self.reused_count = 0
        self.errors: list[str] = []
        self.error = ""
        self.cancelled = False


    def run(self) -> None:

        try:

            sizes: list[int | None] = []

            for source in self.sources:

                check_import_cancelled(self.isInterruptionRequested)

                try:

                    source_stat = source.stat()
                    if not S_ISREG(source_stat.st_mode) or source.suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS:
                        raise ValueError(source.name)
                    sizes.append(source_stat.st_size)

                except Exception as error:

                    sizes.append(None)
                    logger.exception("新增媒體預檢失敗 - %s", source)

                    reason = str(error) or type(error).__name__
                    if len(self.sources) == 1:
                        self.error = reason
                    else:
                        self.errors.append(f"{source.name}: {reason}")

            total_size = sum(size for size in sizes if size is not None)

            check_import_cancelled(self.isInterruptionRequested)

            max_batch_import_bytes = config_store.load()["maxBatchImportKiB"] * 1024
            if len(self.sources) > 1 and total_size > max_batch_import_bytes:
                gigabyte = 1024 * 1024 * 1024
                raise ValueError(tr("media_import_limit_exceeded").format(
                    # 向上取至小數點後兩位, 避免超限時仍顯示與上限相同的大小
                    size=(total_size * 100 + gigabyte - 1) // gigabyte / 100,
                    limit=max_batch_import_bytes / gigabyte
                ))

            completed_size = 0

            for index, (source, size) in enumerate(zip(self.sources, sizes), 1):

                check_import_cancelled(self.isInterruptionRequested)

                if size is None:
                    continue

                self.progress.emit(self, min(99, completed_size * 100 // max(1, total_size)), index, True)

                copying = False

                def report_progress(percent: int, source_size: int = size) -> None:
                    """
                    將單檔複製百分比換算為整批進度
                    """

                    nonlocal copying
                    copying = True

                    overall = min(99, (completed_size * 100 + source_size * percent) // max(1, total_size))
                    self.progress.emit(self, overall, index, False)

                try:

                    destination = import_media(
                        source,
                        SINGLE_IMPORT_DIR,
                        translate=tr,
                        progress=report_progress,
                        cancelled=self.isInterruptionRequested,
                        expected_size=size
                    )
                    self.destinations.append(destination)

                    if copying:
                        self.imported_count += 1
                    else:
                        self.reused_count += 1

                    if len(self.sources) == 1:
                        self.destination = destination

                except MediaImportCancelled:
                    raise

                except Exception as error:

                    logger.exception("新增媒體失敗 - %s", source)

                    reason = str(error) or type(error).__name__
                    if len(self.sources) == 1:
                        self.error = reason
                    else:
                        self.errors.append(f"{source.name}: {reason}")

                completed_size += size
                percent = min(99, completed_size * 100 // max(1, total_size))
                self.progress.emit(self, percent, index, False)

            check_import_cancelled(self.isInterruptionRequested)

            self.progress.emit(self, 100, len(self.sources), False)

        except MediaImportCancelled:
            self.cancelled = True
            logger.info("已取消新增媒體, 已完成 %s/%s", len(self.destinations), len(self.sources))

        except Exception as e:
            logger.exception("新增媒體預檢失敗 - %s", self.sources)
            self.error = str(e) or type(e).__name__

        finally:
            self.cancelled = self.cancelled or self.isInterruptionRequested()


class DesktopEventWindow(QWidget):
    """
    獨立的隱藏頂層視窗, 用以接收通知, 避免隨 Explorer 的桌布容器一起失效
    """

    taskbar_created = Signal()

    def __init__(self) -> None:

        super().__init__()

        self.taskbar_created_message = 0
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)

        try:
            # 建立原生 HWND 但不顯示, 頂層視窗才能接收 TaskbarCreated 廣播
            self.taskbar_created_message = register_taskbar_created_notification(int(self.winId()))

        except Exception:
            self.destroy()
            raise


    def nativeEvent(self, eventType: QByteArray | bytes | bytearray | memoryview, message: int) -> tuple[bool, int]:

        if self.taskbar_created_message and eventType in (b"windows_generic_MSG", "windows_generic_MSG"):
            msg = wintypes.MSG.from_address(int(message))

            if msg.message == self.taskbar_created_message:
                self.taskbar_created.emit()
                return True, 0

        return cast(tuple[bool, int], super().nativeEvent(eventType, message))


class Wallpaper(QWidget):
    """
    桌布視窗
    """

    # 影片開始播放訊號
    playback_started = Signal()
    # 桌布媒體變更訊號
    media_changed = Signal()
    # 重掛完成訊號
    reattach_finished = Signal()


    def __init__(self, target_screen: QScreen | None = None) -> None:

        super().__init__()

        # 正在退出
        self.is_exiting = False
        # 監聽螢幕亮滅的事件
        self.power_notify = None
        # 不掛在桌布視窗或 Explorer 之下的獨立頂層 HWND
        self.desktop_events = None

        try:
            self._initialize(target_screen)

        except Exception:
            self.shutdown()
            self.deleteLater()
            raise


    def _initialize(self, target_screen: QScreen | None) -> None:
        """
        建立桌布資源
        """

        # 目標螢幕(這個桌布將掛載到的螢幕), 重掛時保留同一個目標
        # 未指定時使用建立當下的主螢幕
        if target_screen is None:
            target_screen = QApplication.primaryScreen()
            if target_screen is None:
                raise RuntimeError("找不到可用的目標螢幕")
        self.target_screen = target_screen
        self.target_screen.geometryChanged.connect(self.on_screen_changed)

        # 正在重建
        self.is_recreating = False
        # 重掛失敗重試次數
        self.reattach_retry_count = 0

        # 重掛用計時器
        self.reattach_timer = QTimer(self)
        self.reattach_timer.setSingleShot(True)
        self.reattach_timer.timeout.connect(self.reattach_to_desktop)

        # 目前播放的媒體檔案
        self.current_media: Path | None = None
        # 初始媒體檔案
        initial_media: Path | None = None
        # 媒體清單
        media_files = get_media()

        # 播放狀態: "play", "pause", "stop"
        self.play_state = "stop"
        # 播放意圖與自動暫停狀態
        self.playback_state = PlaybackState()
        # 縮放模式: "fill", "fit", "stretch"
        self.scaling_mode = "fill"
        # 自訂自動暫停檢查函式, 回傳 True 表示已處理, False 表示交給內建檢查
        self.auto_pause_handler: AutoPauseHandler | None = None

        # 全螢幕檢測就緒
        self.fullscreen_detection_ready = False

        # 螢幕亮滅
        self.screen_off = False

        # 設定檔
        config = config_store.load()
        # 自動暫停設定
        self.auto_pause_if_fullscreen = config.get("autoPauseIfFullscreen", True)
        self.auto_pause_if_screen_off = config.get("autoPauseIfScreenOff", True)
        self.auto_pause_if_on_battery = config.get("autoPauseIfOnBattery", False)
        # 每個螢幕的桌布設定, 螢幕ID -> 媒體路徑
        screen_wallpapers = config.get("screenWallpapers", {})

        # 建立視窗
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        # 不接受滑鼠事件
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # 設成螢幕大小
        self.setGeometry(self.target_screen.geometry())

        # 此螢幕ID(視窗定位後再取得(避免讀到預設位置的螢幕))
        self.screen_id = get_monitor_id(int(self.winId()))

        # 此螢幕記憶中的桌布
        saved_media = screen_wallpapers.get(self.screen_id)
        if saved_media and (MEDIA_DIR / saved_media).is_file():
            initial_media = MEDIA_DIR / saved_media
        # 記憶中的桌布不存在, 使用最近使用的影片
        if initial_media is None:
            recent = config.get("recentVideo", [])

            if recent and (MEDIA_DIR / recent[0]).is_file():
                initial_media = MEDIA_DIR / recent[0]
        # 最近使用的影片不存在, 使用媒體清單的第一個
        if initial_media is None and media_files:
            initial_media = media_files[0]

        # 影片顯示物件
        self.instance_video = bb_video_display.Instance(hardware_decoding=config.get("hardwareDecoding", "any"))
        self.instance_video.setParent(self)
        self.instance_video.playback_started.connect(self.playback_started)
        # 圖片顯示物件
        self.instance_image = bb_image_display.Instance()

        # 掛載到桌布
        self.attach_to_desktop()

        # 播放
        if initial_media:

            try:
                self.set_play_media(initial_media)
                self.manual_play()

                if self.screen_id not in screen_wallpapers:
                    config_store.remember_wallpaper(self.screen_id, initial_media)

                logger.info("init - play")

            except Exception:
                logger.exception("載入初始桌布時發生錯誤 - %s", initial_media)

        # 啟用全螢幕檢測用計時器
        self.fullscreen_detection_timer = QTimer(self)
        self.fullscreen_detection_timer.setSingleShot(True)
        self.fullscreen_detection_timer.timeout.connect(self.enable_fullscreen_detection)
        self.fullscreen_detection_timer.start(10000)

        # 自動暫停計時器
        self.check_auto_pause_timer = QTimer(self)
        self.check_auto_pause_timer.timeout.connect(lambda: self.check_auto_pause(from_timer=True))
        self.check_auto_pause_timer.start(500)

        try:
            self.desktop_events = DesktopEventWindow()
            self.desktop_events.taskbar_created.connect(self.on_taskbar_created, Qt.ConnectionType.QueuedConnection)
        except Exception:
            logger.exception("建立工作列重建通知視窗失敗")


    def nativeEvent(self, eventType: QByteArray | bytes | bytearray | memoryview, message: int) -> tuple[bool, int]:
        """
        處理原生 Windows 訊息迴圈
        """

        if eventType in (b"windows_generic_MSG", "windows_generic_MSG"):

            # 解析 MSG 結構
            msg = wintypes.MSG.from_address(int(message))

            display_state = get_display_state(msg)

            if display_state is not None:
                self.screen_off = display_state == 0
                self.check_auto_pause()
                return True, 0

            # 監聽 切換虛擬桌面/用戶 時的 WM_DESTROY 訊息
            if msg.message == WM_DESTROY:

                self.schedule_reattach()
                return True, 0

        return cast(tuple[bool, int], super().nativeEvent(eventType, message))


    def is_image(self) -> bool:
        """
        目前播放的媒體是否為圖片
        """

        return bool(
            self.current_media and
            self.current_media.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
        )


    def apply_playback_state(self, state: PlaybackState | None = None, *, force: bool = False) -> None:
        """
        統一套用手動, 自動暫停與跟隨要求的播放狀態
        """

        if self.is_exiting:
            return

        if state is not None:
            self.playback_state = state

        if self.is_image():
            # 圖片沒有暫停狀態, 清除影片的暫停意圖與原因
            requested = "stop" if self.playback_state.requested_state == "stop" else "play"
            self.playback_state = PlaybackState(requested)

        target = self.playback_state.effective_state

        if not force and target == self.play_state:
            return

        if target == "stop":
            self._stop_media_output()
            return

        if not self.current_media:
            return

        if self.is_image():
            self.instance_image.show()

        elif target == "pause":
            self.instance_video.pause()

        else:
            self.instance_video.play()

        self.play_state = target


    def _stop_media_output(self) -> None:
        """
        停止目前輸出
        """

        if self.is_image():
            self.instance_image.hide()

        else:
            self.instance_video.stop()

        self.play_state = "stop"


    def manual_play(self) -> None:
        """
        手動播放
        """

        if not self.current_media:
            return

        self.apply_playback_state(PlaybackState("play"), force=True)
        logger.info("manual - play")


    def manual_pause(self) -> None:
        """
        手動暫停
        """

        if (
            self.current_media and
            self.current_media.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS and
            self.play_state != "stop"
        ):
            self.apply_playback_state(PlaybackState("pause"))

        logger.info("manual - pause")


    def manual_stop(self) -> None:
        """
        手動停止
        """

        self.apply_playback_state(PlaybackState("stop"), force=True)
        logger.info("manual - stop")


    def get_playback_time(self) -> int | None:
        """
        取得播放進度
        """

        if (
            self.is_exiting or self.is_recreating or self.is_image() or not self.current_media or
            self.play_state == "stop"
        ):
            return None

        return self.instance_video.get_time()


    def get_playback_length(self) -> int:
        """
        取得影片長度
        """

        return self.instance_video.get_length()


    def seek_time(self, position: int) -> bool:
        """
        設定影片進度
        """

        if position < 0 or self.get_playback_time() is None:
            return False

        return self.instance_video.set_time(position)


    def on_taskbar_created(self) -> None:
        """
        工作列重建後啟動, "全新的"重掛嘗試
        """

        if self.is_exiting:
            return

        self.reattach_retry_count = 0
        self.schedule_reattach()
        logger.info("收到 TaskbarCreated, 安排重新掛載桌布")


    def schedule_reattach(self, delay_ms: int = 200) -> None:
        """
        安排一次重掛嘗試, 已有排程時保留原本的執行時間
        """

        if self.is_exiting or self.is_recreating or self.reattach_timer.isActive():
            return

        self.reattach_timer.start(delay_ms)


    def reattach_to_desktop(self) -> None:
        """
        重新建立視窗並掛載桌布
        """

        if self.is_exiting or self.is_recreating:
            return

        # 取消排程
        self.reattach_timer.stop()
        self.is_recreating = True

        try:

            # 桌面尚未就緒時只等待, 保留掛載失敗的重試次數
            if not check_desktop_ready():

                self.is_recreating = False
                logger.debug("桌面尚未就緒, 2 秒後再嘗試重掛")
                self.schedule_reattach(2000)
                return

            logger.info("reattach_to_desktop - start")

            # 解除輸出時保留模組內的播放意圖與進度, 等待期間繼續處理 Qt 事件
            self.instance_video.set_hwnd(0, process_events=True)

            if self.is_exiting:
                self.is_recreating = False
                return

            self.unregister_power_notification()

            self.instance_image.set_hwnd(0)

            # 銷毀失效的 HWND, 重新向 Windows 申請新的 HWND
            self.hide()
            self.destroy(True, True)
            self.create()

            # 建立視窗
            self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint |
                Qt.WindowType.Tool |
                Qt.WindowType.WindowDoesNotAcceptFocus
            )
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self.setGeometry(self.target_screen.geometry())

            self.show()

            # 重新掛載前套用目前螢幕方向的縮放設定
            self.apply_scaling()
            # 掛載到桌布
            self.attach_to_desktop()

            # 恢復進度
            if self.current_media:
                self.apply_playback_state(force=True)

            self.is_recreating = False
            self.reattach_retry_count = 0

            logger.info("reattach_to_desktop - finish")

        except Exception:

            logger.exception("重新掛載桌布時發生錯誤")

            self.is_recreating = False

            if self.reattach_retry_count < 2:

                self.reattach_retry_count += 1
                self.schedule_reattach(2000)

                logger.warning("正在重試重新掛載桌布(第 %s 次)", self.reattach_retry_count)

            else:
                logger.error("重新掛載桌布失敗")

        finally:
            self.is_recreating = False
            self.reattach_finished.emit()


    def on_screen_changed(self, geometry) -> None:
        """
        螢幕解析度變更後啟動, 重掛嘗試
        """

        if self.is_exiting or self.is_recreating:
            return

        self.schedule_reattach()
        logger.info("螢幕解析度變更 - %s, 安排重新掛載桌布", geometry)


    def set_play_media(self, path: Path) -> None:
        """
        設定播放的媒體檔案, 停止目前的輸出, 保留原本的播放或停止狀態
        """

        if self.is_exiting:
            return

        is_image = path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS

        self._stop_media_output()
        logger.debug("設定播放的媒體檔案 - stop")

        if is_image:
            self.instance_image.set_media(path)

        else:
            self.instance_video.set_media(path)
            self.instance_image.close()

        self.current_media = path
        self.apply_scaling()
        logger.info("設定播放的媒體檔案 - %s", path)

        self.apply_playback_state()
        self.media_changed.emit()


    def get_orientation(self) -> str:
        """
        取得螢幕方向(橫向landscape/直向portrait)
        """

        geometry = self.target_screen.geometry()
        return "landscape" if geometry.width() >= geometry.height() else "portrait"


    def apply_scaling(self, config: dict | None = None) -> None:
        """
        套用目前素材與螢幕方向的顯示選項
        """

        if self.is_exiting or self.current_media is None:
            return

        if config is None:
            config = config_store.load()

        media_path = self.current_media.relative_to(MEDIA_DIR).as_posix()
        self.scaling_mode = get_scaling_mode(config, media_path, self.get_orientation())
        color = config.get("backgroundColor", {}).get(media_path, "#000000")
        self.instance_image.set_scaling(self.scaling_mode, color)
        self.instance_video.set_scaling(self.scaling_mode, color)


    def select_media(self, path: Path) -> None:
        """
        選擇桌布, 開始播放
        """

        if self.is_exiting:
            return

        self.set_play_media(path)
        self.manual_play()

        config_store.remember_recent(path)
        config_store.remember_wallpaper(self.screen_id, path)


    def attach_to_desktop(self) -> None:
        """
        將 Qt 視窗移到 Windows 桌布層
        """

        if self.is_exiting:
            return

        hwnd = int(self.winId())

        set_windows_as_wallpaper(hwnd)

        # 重新設定圖片與影片輸出的目標 HWND
        self.instance_video.set_hwnd(hwnd)
        self.instance_image.set_hwnd(hwnd)

        self.power_notify = register_power_notification(hwnd)
        if not self.power_notify:
            logger.error("螢幕電源通知註冊失敗")


    def enable_fullscreen_detection(self) -> None:
        """
        啟動全螢幕視窗偵測
        """

        if self.is_exiting:
            return

        self.fullscreen_detection_ready = True
        logger.debug("啟動全螢幕視窗偵測 - ready")


    def check_auto_pause(self, *, from_timer: bool = False) -> None:
        """
        檢查是否需要自動暫停
        """

        if self.is_exiting or self.is_recreating:
            return

        if self.auto_pause_handler is not None and self.auto_pause_handler(self, from_timer=from_timer):
            return

        if update_auto_pause((self,)):
            logger.info("自動暫停 - %s", self.play_state)


    def get_auto_pause_reason(self) -> AutoPauseReason | None:
        """
        回報不需掃描視窗的暫停原因(螢幕關閉screen_off/筆電電池battery/全螢幕fullscreen)
        """

        if self.auto_pause_if_screen_off and self.screen_off:
            return "screen_off"
        if self.auto_pause_if_on_battery and is_on_battery():
            return "battery"
        if self.auto_pause_if_fullscreen and self.fullscreen_detection_ready and is_any_fullscreen(int(self.winId())):
            return "fullscreen"

        return None


    def unregister_power_notification(self) -> None:
        """
        取消註冊 Windows 螢幕電源通知
        """

        handle = self.power_notify
        self.power_notify = None

        if handle:
            unregister_power_notification(handle)


    def shutdown(self) -> None:
        """
        釋放此桌布的資源
        """

        if self.is_exiting:
            return

        self.is_exiting = True
        self.current_media = None
        self.play_state = "stop"
        self.playback_state = PlaybackState()
        self.auto_pause_handler = None
        self.fullscreen_detection_ready = False

        desktop_events = self.desktop_events
        self.desktop_events = None

        steps = [
            ("停止重掛計時器", "reattach_timer", lambda: self.reattach_timer.stop()),
            ("停止自動暫停計時器", "check_auto_pause_timer", lambda: self.check_auto_pause_timer.stop()),
            ("停止全螢幕偵測等待", "fullscreen_detection_timer", lambda: self.fullscreen_detection_timer.stop()),
            ("解除螢幕訊號", "target_screen", lambda: self.target_screen.geometryChanged.disconnect(self.on_screen_changed)),
            ("解除影片訊號", "instance_video", lambda: self.instance_video.playback_started.disconnect(self.playback_started)),
        ]

        if desktop_events is not None:
            steps.extend([
                ("解除桌面通知訊號", None, lambda: desktop_events.taskbar_created.disconnect(self.on_taskbar_created)),
                ("關閉桌面通知視窗", None, desktop_events.close),
                ("釋放桌面通知視窗", None, desktop_events.deleteLater),
            ])

        steps.extend([
            ("取消電源通知", None, self.unregister_power_notification),
            ("關閉影片輸出", "instance_video", lambda: self.instance_video.close(process_events=True)),
            ("關閉圖片輸出", "instance_image", lambda: self.instance_image.close()),
            ("解除圖片目標視窗", "instance_image", lambda: self.instance_image.set_hwnd(0)),
            ("釋放圖片物件", "instance_image", lambda: self.instance_image.deleteLater()),
            ("隱藏桌布視窗", None, self.hide),
            ("釋放桌布視窗", None, lambda: self.destroy(True, True)),
        ])

        for description, resource, cleanup in steps:

            # 只清理已建立的資源
            if resource is not None and getattr(self, resource, None) is None:
                continue

            try:
                cleanup()

            except Exception:
                logger.exception("釋放桌布資源失敗 - %s", description)


class Tray:

    def __init__(self, app: QApplication) -> None:

        self.app = app
        self.app.aboutToQuit.connect(self.shutdown)

        # 螢幕 ID -> Wallpaper
        self.wallpapers: dict[str, Wallpaper] = {}
        # 螢幕 ID -> MediaPreviewMenu
        self.screen_menus: dict[str, MediaPreviewMenu] = {}

        self.is_exiting = False
        self.restart_requested = False
        self.reset_requested = False

        # 系統匣圖示
        self.tray = QSystemTrayIcon()
        self.tray.setIcon(QIcon(str(ICON_PATH)))
        self.tray.setToolTip(tr("app_name"))

        # 匯入媒體的背景執行緒
        self.import_thread: MediaImportThread | None = None
        # 匯入媒體的目標螢幕 ID, None 表示匯入完成後套用到所有螢幕
        self.import_screen_id: str | None = None
        # 匯入媒體的目標螢幕 ID 集合, 用於取消匯入時避免套用到已停止匯入的螢幕
        self.import_applies_to_all = False
        # 匯入已停止的螢幕 ID 集合
        self.import_stopped_screens: set[str] = set()

        # 處理縮圖生成的背景執行緒
        self.thumbnail_thread = ThumbnailThread(MEDIA_DIR, THUMBNAIL_DIR)
        self.thumbnail_thread.thumbnail_ready.connect(self.apply_background_color, Qt.ConnectionType.QueuedConnection)

        # 設定檔
        config = config_store.load()
        # 語言代碼, 字型大小, 縮圖預覽大小, 硬體解碼, 螢幕跟隨設定
        self.current_language_code = config.get("language", "zh-TW")
        self.font_size = config.get("fontSize", 11)
        self.preview_size = config.get("previewSize", 3)
        self.hardware_decoding = config.get("hardwareDecoding", "any")
        self.saved_follows: dict[str, str] = config.get("screenFollows", {})

        # 螢幕跟隨控制器, 會在桌布建立後啟動, 以避免在桌布尚未就緒時就嘗試跟隨
        self.follow_controller = FollowController(self.wallpapers, config_store)

        # 如果有已保存的螢幕跟隨設定, 在啟動時等待桌布建立完成後再啟動跟隨
        self.waiting_for_screens = bool(self.saved_follows)

        self.menu = menu = QMenu()

        self.playback_actions = self.add_playback_actions(menu)
        exit_action = QAction(tr("exit"), menu)

        menu.aboutToShow.connect(self.refresh_playback_actions)
        menu.aboutToShow.connect(
            # 選單顯示後取得焦點(避免首次開啟時點外部無法關閉)
            lambda: menu.activateWindow() if menu.isVisible() else None,
            Qt.ConnectionType.QueuedConnection
        )

        self.media_menu = MediaPreviewMenu(
            tr("select_video"),
            menu,
            media_dir=MEDIA_DIR,
            thumbnail_dir=THUMBNAIL_DIR,
            loading_image_path=LOADING_IMAGE_PATH,
            preview_size=self.preview_size,
            thumbnail_thread=self.thumbnail_thread
        )
        self.media_menu.aboutToShow.connect(self.refresh_media_menu)

        self.add_media_action = QAction(tr("add_media"), menu)
        self.add_media_action.triggered.connect(self.add_media)

        self.cancel_import_action = QAction(tr("cancel_media_import"), menu)
        self.cancel_import_action.triggered.connect(self.cancel_import)
        self.cancel_import_action.setVisible(False)

        open_media_action = QAction(tr("open_video_folder"), menu)
        open_media_action.triggered.connect(self.open_media_folder)

        startup_action = QAction(tr("startup"), menu)
        startup_action.setCheckable(True)
        startup_action.setChecked(task_exists())

        exit_action.triggered.connect(self.exit)

        startup_action.triggered.connect(self.toggle_startup)

        # 主選單
        # ################
        menu.addSeparator()

        menu.addMenu(self.media_menu)
        menu.addAction(self.add_media_action)
        menu.addAction(self.cancel_import_action)
        menu.addAction(open_media_action)

        menu.addSeparator()

        self.options_menu = menu.addMenu(tr("options"))

        menu.addSeparator()

        self.advanced_menu = menu.addMenu(tr("advanced"))

        menu.addSeparator()

        menu.addAction(exit_action)
        # ################

        # "選項"選單
        # ################
        for text_key, config_key, attribute in (
            ("auto_pause_fullscreen", "autoPauseIfFullscreen", "auto_pause_if_fullscreen"),
            ("auto_pause_screen_off", "autoPauseIfScreenOff", "auto_pause_if_screen_off"),
            ("auto_pause_battery", "autoPauseIfOnBattery", "auto_pause_if_on_battery"),
        ):
            action = QAction(tr(text_key), self.options_menu)
            action.setCheckable(True)
            action.setChecked(config.get(config_key, CONFIG_DEFAULTS[config_key]))

            # triggered 前已切換勾選, 保存失敗時還原
            action.triggered.connect(
                lambda checked, key=config_key, attr=attribute, item=action: item.setChecked(
                    checked if self.set_auto_pause_option(key, attr, checked) else not checked
                )
            )

            self.options_menu.addAction(action)

        self.options_menu.addSeparator()

        font_size_menu = self.options_menu.addMenu(tr("font_size"))
        font_size_group = QActionGroup(font_size_menu)
        self.font_size_actions: dict[int, QAction] = {}

        for size, text_key in (
            (24, "size_largest"), (19, "size_large"),
            (15, "size_normal"), (11, "size_small"), (9, "size_smallest"),
        ):
            action = QAction(tr(text_key), font_size_menu)
            action.setCheckable(True)
            action.setChecked(size == self.font_size)
            font_size_group.addAction(action)

            action.triggered.connect(lambda checked, value=size: self.set_font_size(value))

            font_size_menu.addAction(action)
            self.font_size_actions[size] = action

        preview_size_menu = self.options_menu.addMenu(tr("preview_size"))
        preview_size_group = QActionGroup(preview_size_menu)
        self.preview_size_actions: dict[int, QAction] = {}

        for size, text_key in (
            (5, "size_largest"), (4, "size_large"),
            (3, "size_normal"), (2, "size_small"), (1, "size_smallest"),
        ):
            action = QAction(tr(text_key), preview_size_menu)
            action.setCheckable(True)
            action.setChecked(size == self.preview_size)
            preview_size_group.addAction(action)

            action.triggered.connect(lambda checked, value=size: self.set_preview_size(value))

            preview_size_menu.addAction(action)
            self.preview_size_actions[size] = action

        self.options_menu.addSeparator()

        self.options_language_menu = self.options_menu.addMenu(tr("language"))
        self.options_language_menu.aboutToShow.connect(self.refresh_language_menu)

        self.options_menu.addSeparator()

        self.options_menu.addAction(startup_action)
        # ################

        # "進階"選單
        # ################
        open_config_action = QAction(tr("open_config_file"), self.advanced_menu)
        open_config_action.triggered.connect(self.open_config_file)
        self.advanced_menu.addAction(open_config_action)

        open_log_action = QAction(tr("open_log_folder"), self.advanced_menu)
        open_log_action.triggered.connect(self.open_log_folder)
        self.advanced_menu.addAction(open_log_action)

        hardware_decoding_menu = self.advanced_menu.addMenu(tr("hardware_decoding"))
        hardware_decoding_group = QActionGroup(hardware_decoding_menu)
        self.hardware_decoding_actions: dict[str, QAction] = {}

        for mode, text_key in (
            ("any", "hardware_decoding_auto"), ("d3d11va", "hardware_decoding_d3d11"),
            ("dxva2", "hardware_decoding_dxva2"), ("none", "hardware_decoding_disabled"),
        ):
            action = QAction(tr(text_key), hardware_decoding_menu)
            action.setCheckable(True)
            action.setChecked(mode == self.hardware_decoding)
            hardware_decoding_group.addAction(action)
            action.triggered.connect(lambda checked, value=mode: self.set_hardware_decoding(value))
            hardware_decoding_menu.addAction(action)
            self.hardware_decoding_actions[mode] = action

        reset_menu = self.advanced_menu.addMenu(tr("reset_defaults"))
        reset_action = QAction(tr("confirm_reset_defaults"), reset_menu)
        reset_action.triggered.connect(self.reset_defaults)
        reset_menu.addAction(reset_action)
        # ################

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: menu.popup(QCursor.pos())
            if reason == QSystemTrayIcon.ActivationReason.Trigger
            else None
        )

        # 螢幕跟隨同步計時器
        self.follow_sync_timer = QTimer(menu)
        self.follow_sync_timer.timeout.connect(self.follow_controller.synchronize)
        self.follow_sync_timer.start(SYNC_INTERVAL_MS)

        # 螢幕跟隨還原計時器, 避免螢幕列舉尚未穩定就嘗試還原跟隨關係
        self.follow_restore_timer = QTimer(menu)
        self.follow_restore_timer.setSingleShot(True)
        self.follow_restore_timer.timeout.connect(self.initialize_screens)

        # 新增螢幕, 移除螢幕
        self.app.screenAdded.connect(self.add_screen)
        self.app.screenRemoved.connect(self.remove_screen)

        if self.waiting_for_screens:
            # 等待螢幕列舉穩定後再建立桌布(單純等一小段時間)
            self.follow_restore_timer.start(RESTORE_DELAY_MS)
        else:
            self.initialize_screens()

        self.rebuild_screen_menus()
        self.tray.show()


    def initialize_screens(self) -> None:
        """
        列舉目前螢幕並還原已保存的跟隨關係
        """

        if self.is_exiting:
            return

        self.follow_restore_timer.stop()
        self.waiting_for_screens = False
        for screen in self.app.screens():
            self.add_screen(screen, refresh=False)

        self.follow_controller.restore(self.saved_follows)
        self.saved_follows = {}
        self.rebuild_screen_menus()


    def add_screen(self, screen: QScreen, *, refresh: bool = True) -> None:
        """
        為新螢幕建立桌布
        """

        if self.is_exiting or self.waiting_for_screens or any(w.target_screen is screen for w in self.wallpapers.values()):
            return

        try:
            wallpaper = Wallpaper(target_screen=screen)

        except Exception:
            logger.exception("建立螢幕桌布失敗 - %s", screen.name())
            return

        if self.is_exiting or screen not in self.app.screens() or wallpaper.screen_id in self.wallpapers:
            wallpaper.shutdown()
            wallpaper.deleteLater()
            return

        self.wallpapers[wallpaper.screen_id] = wallpaper
        wallpaper.auto_pause_handler = self.follow_controller.check_auto_pause
        wallpaper.media_changed.connect(self.refresh_tooltip, Qt.ConnectionType.QueuedConnection)
        wallpaper.media_changed.connect(self.request_background_colors, Qt.ConnectionType.QueuedConnection)
        wallpaper.reattach_finished.connect(self.request_background_colors, Qt.ConnectionType.QueuedConnection)

        for signal in (wallpaper.playback_started, wallpaper.reattach_finished):
            signal.connect(
                lambda s=wallpaper.screen_id, w=wallpaper: self.follow_controller.on_ready(s, w),
                Qt.ConnectionType.QueuedConnection
            )
        wallpaper.show()
        self.request_background_colors()

        if refresh:
            self.follow_controller.restore(self.follow_controller.graph.sources)
            self.rebuild_screen_menus()


    def remove_screen(self, screen: QScreen) -> None:
        """
        移除已拔除螢幕的桌布並釋放資源
        """

        if self.is_exiting:
            return

        for screen_id, wallpaper in tuple(self.wallpapers.items()):
            if wallpaper.target_screen is not screen:
                continue

            del self.wallpapers[screen_id]
            self.follow_controller.remove_screen(screen_id)
            self.rebuild_screen_menus()

            try:
                wallpaper.shutdown()

            except Exception:
                logger.exception("釋放已拔除螢幕的桌布失敗 - %s", screen_id)

            finally:
                # 重掛仍持有桌布與子計時器, 等流程離開後才刪除 Qt 物件
                if wallpaper.is_recreating:
                    wallpaper.reattach_finished.connect(
                        wallpaper.deleteLater,
                        Qt.ConnectionType(
                            Qt.ConnectionType.QueuedConnection.value | Qt.ConnectionType.SingleShotConnection.value
                        )
                    )
                else:
                    wallpaper.deleteLater()

            # 匯入結果可能正在等待已拔除桌布的重掛訊號
            if self.import_thread is not None and not self.import_thread.isRunning():
                self.finish_import(self.import_thread)
            break


    def rebuild_screen_menus(self) -> None:
        """
        建立單螢幕選單與各螢幕子選單
        """

        self.media_menu.reset_preview()
        self.menu.removeAction(self.media_menu.menuAction())

        for menu in self.screen_menus.values():
            self.menu.removeAction(menu.menuAction())
            menu.close()
            menu.reset_preview()
            menu.deleteLater()

        self.screen_menus.clear()

        if len(self.wallpapers) == 1:
            self.menu.insertMenu(self.add_media_action, self.media_menu)

        elif len(self.wallpapers) > 1:
            for number, screen_id in enumerate(self.wallpapers, start=1):
                menu = MediaPreviewMenu(
                    f'{tr("monitor")} {number}',
                    self.menu,
                    media_dir=MEDIA_DIR,
                    thumbnail_dir=THUMBNAIL_DIR,
                    loading_image_path=LOADING_IMAGE_PATH,
                    preview_size=self.preview_size,
                    thumbnail_thread=self.thumbnail_thread
                )

                menu.aboutToShow.connect(
                    lambda s=screen_id, m=menu: self.refresh_screen_menu(s, m)
                )

                self.screen_menus[screen_id] = menu
                self.menu.insertMenu(self.add_media_action, menu)

        self.refresh_playback_actions()
        self.refresh_tooltip()


    def refresh_tooltip(self) -> None:
        """
        所有螢幕使用同一媒體時, 在系統匣提示顯示桌布名稱
        """

        if self.is_exiting:
            return

        text = tr("app_name")
        media_paths = {wallpaper.current_media for wallpaper in self.wallpapers.values()}

        if len(media_paths) == 1:
            path = next(iter(media_paths))

            if path is not None:
                name = path.stem

                if len(name) > 40:
                    name = name[:32] + ".."

                text = f"{text} - {name}"

        self.tray.setToolTip(text)


    def refresh_playback_actions(
        self,
        actions: Sequence[tuple[QAction, str]] | None = None,
        screen_id: str | None = None
    ) -> None:
        """
        建立主選單
        """

        wallpapers = self.get_wallpapers(screen_id)
        wallpaper = wallpapers[0] if wallpapers else None

        multiple = screen_id is None and len(wallpapers) > 1

        following = screen_id in self.follow_controller.graph.sources

        has_video = any(
            w.current_media and w.current_media.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
            for w in wallpapers
        )

        actions = self.playback_actions if actions is None else actions
        for action, state in actions:
            action.setText(tr(f"all_{state}" if multiple else state))
            action.setCheckable(not multiple)
            action.setChecked(bool(not multiple and not following and wallpaper and wallpaper.play_state == state))
            action.setEnabled(bool(has_video if state == "pause" else wallpapers))


    def get_wallpapers(self, screen_id: str | None = None) -> tuple[Wallpaper, ...]:
        """
        取得指定螢幕的 Wallpaper, 未指定時回傳所有螢幕
        """

        if self.is_exiting:
            return ()

        if screen_id is None:
            return tuple(self.wallpapers.values())

        wallpaper = self.wallpapers.get(screen_id)
        return (wallpaper,) if wallpaper is not None else ()


    def play(self, screen_id: str | None = None) -> None:

        if screen_id is not None:
            self.follow_controller.detach(screen_id)

        for wallpaper in self.get_wallpapers(screen_id):
            wallpaper.manual_play()

        self.follow_controller.synchronize(screen_id)


    def pause(self, screen_id: str | None = None) -> None:

        if screen_id is not None:
            self.follow_controller.detach(screen_id)

        for wallpaper in self.get_wallpapers(screen_id):
            if (
                screen_id is None and len(self.wallpapers) > 1 and wallpaper.play_state == "stop" and
                wallpaper.current_media and wallpaper.current_media.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
            ):
                wallpaper.manual_play()
            wallpaper.manual_pause()

        self.follow_controller.synchronize(screen_id)


    def stop(self, screen_id: str | None = None) -> None:

        if screen_id is not None:
            self.import_stopped_screens.update(self.follow_controller.graph.subtree(screen_id))
            self.follow_controller.detach(screen_id)

        else:
            self.import_screen_id = None
            self.import_applies_to_all = False

        for wallpaper in self.get_wallpapers(screen_id):
            wallpaper.manual_stop()

        self.follow_controller.synchronize(screen_id)


    def refresh_language_menu(self) -> None:
        """
        建立語言選單
        """

        self.options_language_menu.clear()

        for language_display, language_code in SUPPORTED_LANGUAGES.items():

            action = QAction(language_display, self.options_language_menu)
            action.triggered.connect(
                lambda checked=False, c=language_code: self.set_language(c)
            )
            action.setCheckable(True)
            action.setChecked(language_code == self.current_language_code)

            self.options_language_menu.addAction(action)


    def refresh_screen_menu(self, screen_id: str, menu: MediaPreviewMenu) -> None:
        """
        建立指定螢幕的操作與媒體選單
        """

        menu.reset_preview()

        # clear() 只移除子選單入口, 子選單本體需另外釋放
        for action in menu.actions():
            submenu = action.menu()
            if submenu is not None:
                submenu.deleteLater()

        menu.clear()

        self.add_playback_actions(menu, screen_id)
        menu.addSeparator()

        if len(self.wallpapers) > 1:
            self.add_follow_menu(menu, screen_id)

        self.add_scaling_menu(menu, screen_id)
        menu.addSeparator()

        self.add_media_menu_actions(menu, screen_id)


    def add_scaling_menu(self, menu: QMenu, screen_id: str | None) -> None:

        wallpaper = self.wallpapers.get(screen_id) if screen_id is not None else None
        scaling_menu = menu.addMenu(tr("scaling_mode"))
        scaling_menu.setEnabled(bool(wallpaper and wallpaper.current_media))
        group = QActionGroup(scaling_menu)

        for mode in ("fill", "fit", "stretch"):
            action = scaling_menu.addAction(tr(f"scaling_{mode}"))
            action.setCheckable(True)
            action.setChecked(bool(wallpaper and wallpaper.scaling_mode == mode))
            group.addAction(action)

            action.triggered.connect(
                lambda checked=False, value=mode: self.set_scaling(screen_id, value)
            )

        if wallpaper is None or wallpaper.current_media is None or wallpaper.scaling_mode != "fit":
            return

        scaling_menu.addSeparator()

        color_menu = scaling_menu.addMenu(tr("background_color"))
        color_group = QActionGroup(color_menu)

        colors = {
            None: "background_color_auto",
            "#000000": "color_black",
            "#212322": "color_PMS419_moonless_night",
            "#54575a": "color_PMS425_sesame",
            "#8a8d8e": "color_PMS423_metal",
            "#d0d3d5": "color_PMS427_concrete",
            "#e6e1e7": "color_PMS663_cloud",
            "#ffffff": "color_white",
            "#483729": "color_PMS7533_cocoa",
            "#8f292b": "color_PMS7623_brick",
            "#ffb3bb": "color_PMS176_cherry_blossom",
            "#f98e2c": "color_PMS715_orange",
            "#facfa9": "color_PMS2015_wood_plank",
            "#f7e1a5": "color_PMS7401_sand",
            "#f6cf3f": "color_PMS129_sunflower",
            "#eaeb6c": "color_PMS386_lemon",
            "#75d87e": "color_PMS2268_leaf",
            "#7d9c91": "color_PMS5565_sage",
            "#0e7f6f": "color_PMS2244_forest",
            "#8ab8e9": "color_PMS278_sky",
            "#002554": "color_PMS655_deep_sea",
            "#a8a2c3": "color_PMS7445_taro",
            "#6f40a2": "color_PMS2077_grape",
        }

        media_path = wallpaper.current_media.relative_to(MEDIA_DIR).as_posix()
        color = config_store.load().get("backgroundColor", {}).get(media_path)
        selected_color = color if color in colors else None

        for color, label in colors.items():
            action = color_menu.addAction(tr(label))
            action.setCheckable(True)
            action.setChecked(color == selected_color)
            color_group.addAction(action)

            action.triggered.connect(
                lambda checked=False, value=color: self.set_background_color(screen_id, value)
            )


    def set_scaling(self, screen_id: str | None, mode: str) -> None:
        """
        設定並保存素材的縮放模式
        """

        wallpaper = self.wallpapers.get(screen_id) if screen_id is not None else None
        if self.is_exiting or wallpaper is None or wallpaper.is_exiting or wallpaper.current_media is None:
            return

        path = wallpaper.current_media
        media_path = path.relative_to(MEDIA_DIR).as_posix()
        config = config_store.load()
        modes = config.setdefault("scalingMode", {}).setdefault(media_path, {})
        if not modes:
            modes.update(landscape=mode, portrait=mode)
        else:
            modes[wallpaper.get_orientation()] = mode

        if not config_store.save(config):
            return

        for target in self.wallpapers.values():
            if target.current_media == path:
                target.apply_scaling(config)

        self.request_background_colors()


    def set_background_color(self, screen_id: str | None, color: str | None) -> None:
        """
        保存素材共用的背景色, None 表示重新自動取色
        """

        wallpaper = self.wallpapers.get(screen_id) if screen_id is not None else None
        if (
            self.is_exiting or wallpaper is None or
            wallpaper.is_exiting or wallpaper.current_media is None
        ):
            return


        path = wallpaper.current_media
        media_path = path.relative_to(MEDIA_DIR).as_posix()
        config = config_store.load()
        colors = config.setdefault("backgroundColor", {})

        if color is None:
            colors.pop(media_path, None)

        else:
            colors[media_path] = color


        if not config_store.save(config):
            return


        for menu in (self.media_menu, *self.screen_menus.values()):
            menu.display_config = config


        if color is None:
            self.request_background_colors()

        else:
            for target in self.wallpapers.values():
                if target.current_media == path:
                    target.apply_scaling(config)


    def request_background_colors(self) -> None:
        """
        為尚無背景色的完整顯示桌布請求縮圖
        """

        if self.is_exiting:
            return

        config = config_store.load()
        colors = config.get("backgroundColor", {})

        for wallpaper in self.wallpapers.values():

            path = wallpaper.current_media
            if wallpaper.is_exiting or wallpaper.scaling_mode != "fit" or path is None:
                continue

            if path.relative_to(MEDIA_DIR).as_posix() not in colors:
                self.thumbnail_thread.request(path)


    def apply_background_color(self, media_path: str, preview_size: int, image: QImage) -> None:
        """
        從縮圖取得平均色, 保存並套用為背景色
        """

        if self.is_exiting or image.isNull():
            return

        path = Path(media_path)
        targets = [w for w in self.wallpapers.values() if not w.is_exiting and w.current_media == path]
        if not any(w.scaling_mode == "fit" for w in targets):
            return

        config = config_store.load()
        colors = config.setdefault("backgroundColor", {})
        key = path.relative_to(MEDIA_DIR).as_posix()
        if key in colors:
            return

        color = image.scaled(1, 1, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        colors[key] = color.pixelColor(0, 0).name()
        if not config_store.save(config):
            return

        for wallpaper in targets:
            wallpaper.apply_scaling(config)
        for menu in (self.media_menu, *self.screen_menus.values()):
            menu.display_config = config


    def add_follow_menu(self, menu: QMenu, screen_id: str) -> None:

        follow_menu = menu.addMenu(tr("follow_monitor"))
        sources = self.follow_controller.graph.sources
        follow_menu.menuAction().setCheckable(True)
        follow_menu.menuAction().setChecked(screen_id in sources)

        for number, source_id in enumerate(self.wallpapers, start=1):
            if source_id == screen_id:
                continue

            action = follow_menu.addAction(f'{tr("monitor")} {number}')
            action.setCheckable(True)
            action.setChecked(sources.get(screen_id) == source_id)
            action.setEnabled(self.follow_controller.can_follow(screen_id, source_id))
            action.triggered.connect(
                lambda checked=False, s=screen_id, origin=source_id: self.follow_controller.follow(s, origin)
            )


    def add_playback_actions(self, menu: QMenu, screen_id: str | None = None) -> tuple[tuple[QAction, str], ...]:
        """
        新增播放控制項目並回傳動作與狀態的對應
        """

        actions = []
        for state, command in (
            ("play", self.play),
            ("pause", self.pause),
            ("stop", self.stop),
        ):
            action = QAction(menu)
            action.triggered.connect(
                lambda checked=False, s=screen_id, callback=command: callback(s)
            )
            menu.addAction(action)
            actions.append((action, state))

        self.refresh_playback_actions(actions, screen_id)

        return tuple(actions)


    def refresh_media_menu(self) -> None:
        """
        重新建立媒體選單
        """

        self.media_menu.reset_preview()

        for action in self.media_menu.actions():
            submenu = action.menu()
            if submenu is not None:
                submenu.deleteLater()

        self.media_menu.clear()

        screen_id = next(iter(self.wallpapers)) if len(self.wallpapers) == 1 else None

        self.add_scaling_menu(self.media_menu, screen_id)
        self.media_menu.addSeparator()
        self.add_media_menu_actions(self.media_menu, screen_id)


    def add_media_menu_actions(self, menu: MediaPreviewMenu, screen_id: str | None) -> None:
        """
        新增最近紀錄與媒體清單項目
        """

        wallpaper = self.wallpapers.get(screen_id) if screen_id is not None else None
        menu.target_screen = wallpaper.target_screen if wallpaper is not None else None

        config = config_store.load()
        menu.display_config = config
        recent = config["recentVideo"][:config["recentDisplayCount"]]

        for media_path in recent:
            self.add_media_menu_action(MEDIA_DIR / media_path, screen_id, menu)

        if recent:
            clear_action = QAction(tr("clear_recent_videos"), menu)
            clear_action.triggered.connect(self.clear_recent_media)
            menu.addAction(clear_action)
            menu.addSeparator()

        media_files = get_media()

        if not media_files:
            action = QAction(tr("no_video"), menu)
            action.setEnabled(False)
            menu.addAction(action)

            return

        for media_path in media_files:
            self.add_media_menu_action(media_path, screen_id, menu)


    def clear_recent_media(self) -> None:
        """
        清除最近播放的媒體清單
        """

        config = config_store.load()
        config["recentVideo"] = []
        config_store.save(config)

        logger.info("clear_recent_media")


    def add_media_menu_action(
        self,
        media_path: Path,
        screen_id: str | None = None,
        menu: MediaPreviewMenu | None = None
    ) -> None:
        """
        新增媒體選單項目
        """

        menu = self.media_menu if menu is None else menu
        menu.setToolTipsVisible(True)

        wallpaper = self.wallpapers.get(screen_id) if screen_id is not None else None

        name = media_path.name
        action = QAction(name if len(name) <= 40 else name[:32] + "..", menu)
        action.setToolTip(name)
        action.setData(str(media_path))

        action.triggered.connect(
            lambda checked=False, v=media_path, s=screen_id: self.play_from_menu(v, s)
        )

        action.setCheckable(True)
        action.setChecked(bool(
            wallpaper and media_path == wallpaper.current_media and
            screen_id not in self.follow_controller.graph.sources
        ))

        action.setEnabled(wallpaper is not None and media_path.is_file())

        menu.addAction(action)


    def set_language(self, language_code: str) -> None:

        if self.is_exiting or language_code == self.current_language_code:
            return

        config = config_store.load()
        config["language"] = language_code

        if not config_store.save(config):
            return

        self.current_language_code = language_code

        logger.info("set_language - %s", language_code)
        self.restart()


    def play_from_menu(self, path: Path, screen_id: str | None = None) -> None:

        if screen_id is None and len(self.wallpapers) == 1:
            screen_id = next(iter(self.wallpapers))

        if self.is_exiting or screen_id is None:
            return

        wallpaper = self.wallpapers.get(screen_id)

        if wallpaper is None or wallpaper.is_exiting:
            return

        try:

            wallpaper.select_media(path)
            self.follow_controller.detach(screen_id)
            self.follow_controller.synchronize(screen_id)

        except Exception:

            logger.exception("載入桌布時發生錯誤 - %s", path)
            self.tray.showMessage(
                tr("app_name"),
                tr("media_load_failed"),
                QSystemTrayIcon.MessageIcon.Information,
                3000
            )
            return

        logger.info("play_from_menu - play")


    def add_media(self) -> None:

        if self.is_exiting or self.import_thread is not None:
            return

        self.add_media_action.setEnabled(False)

        patterns = " ".join(
            f"*{ext}"
            for ext in sorted(SUPPORTED_MEDIA_EXTENSIONS)
        )

        filenames, _ = QFileDialog.getOpenFileNames(
            None,
            tr("add_media"),
            "",
            f'{tr("media_files")} ({patterns})'
        )

        if self.is_exiting:
            return

        if not filenames:
            self.add_media_action.setEnabled(True)
            return

        self.add_media_action.setText(tr("checking_media"))

        self.cancel_import_action.setEnabled(True)
        self.cancel_import_action.setVisible(True)

        self.import_screen_id = next(iter(self.wallpapers)) if len(filenames) == 1 and len(self.wallpapers) == 1 else None
        self.import_applies_to_all = len(filenames) == 1 and len(self.wallpapers) > 1 and not get_media()
        self.import_stopped_screens.clear()

        self.import_thread = MediaImportThread([Path(filename) for filename in filenames])
        self.import_thread.progress.connect(self.update_import_progress, Qt.ConnectionType.QueuedConnection)
        self.import_thread.finished.connect(lambda w=self.import_thread: self.finish_import(w))
        self.import_thread.start()


    def update_import_progress(self, worker: MediaImportThread, percent: int, index: int, comparing: bool) -> None:

        if self.import_thread is worker and not worker.isInterruptionRequested():
            key = "comparing_media" if comparing else "importing_media"
            self.add_media_action.setText(tr(key).format(index=index, total=len(worker.sources), percent=percent))


    def cancel_import(self) -> None:

        if self.import_thread is not None and self.import_thread.isRunning():
            self.import_thread.requestInterruption()
            self.cancel_import_action.setEnabled(False)


    def finish_import(self, expected_worker: MediaImportThread | None = None) -> None:

        worker = self.import_thread

        if worker is None or (expected_worker is not None and worker is not expected_worker):
            return

        screen_ids: tuple[str, ...] = ()

        if self.import_applies_to_all:
            screen_ids = tuple(self.wallpapers)

        elif self.import_screen_id is not None and self.import_screen_id in self.wallpapers:
            screen_ids = (self.import_screen_id,)

        screen_ids = tuple(screen_id for screen_id in screen_ids if screen_id not in self.import_stopped_screens)

        # 巢狀事件迴圈仍會收到匯入完成訊號, 等當次重掛離開後才切換媒體
        if (
            screen_ids and not self.is_exiting and
            not worker.cancelled and worker.destination is not None
        ):
            for screen_id in screen_ids:
                wallpaper = self.wallpapers.get(screen_id)

                if wallpaper is not None and wallpaper.is_recreating and not wallpaper.is_exiting:
                    wallpaper.reattach_finished.connect(
                        lambda w=worker: self.finish_import(w),
                        Qt.ConnectionType(
                            Qt.ConnectionType.QueuedConnection.value | Qt.ConnectionType.SingleShotConnection.value
                        )
                    )
                    return

        self.import_thread = None
        worker.deleteLater()

        self.add_media_action.setText(tr("add_media"))
        self.add_media_action.setEnabled(True)
        self.cancel_import_action.setVisible(False)

        if self.is_exiting:
            return

        if worker.destinations:

            for destination in dict.fromkeys(worker.destinations):
                self.thumbnail_thread.request(destination, preview_size=self.preview_size)

            if not worker.cancelled and worker.destination is not None:
                config_store.remember_recent(worker.destination)

                for screen_id in screen_ids:
                    self.play_from_menu(worker.destination, screen_id)

            self.refresh_media_menu()

        if worker.error:
            self.tray.showMessage(
                tr("app_name"),
                tr("media_add_failed").format(error=worker.error),
                QSystemTrayIcon.MessageIcon.Information,
                3000
            )

        elif worker.errors:
            message = QMessageBox()
            message.setWindowTitle(tr("app_name"))
            message.setIcon(QMessageBox.Icon.Warning)
            message.setText(tr("media_import_summary").format(
                imported=worker.imported_count, reused=worker.reused_count, failed=len(worker.errors)
            ))
            message.setDetailedText("\n".join(worker.errors))
            message.exec()


    def wait_for_import(self) -> None:
        """
        等待匯入媒體的背景執行緒結束
        """

        if self.import_thread is not None:
            self.import_thread.requestInterruption()
            self.import_thread.wait()


    def open_media_folder(self) -> None:

        os.startfile(str(MEDIA_DIR))


    def open_config_file(self) -> None:

        os.startfile(str(CONFIG_PATH))


    def open_log_folder(self) -> None:

        os.startfile(str(LOG_DIR))


    def reset_defaults(self) -> None:
        """
        標記還原要求並執行重啟流程
        """

        if self.is_exiting:
            return

        self.reset_requested = True
        self.restart()


    def set_hardware_decoding(self, mode: str) -> None:

        if self.is_exiting or mode == self.hardware_decoding:
            self.hardware_decoding_actions[self.hardware_decoding].setChecked(True)
            return

        try:
            config = config_store.load()
        except OSError:
            self.hardware_decoding_actions[self.hardware_decoding].setChecked(True)
            return

        config["hardwareDecoding"] = mode
        if not config_store.save(config):
            self.hardware_decoding_actions[self.hardware_decoding].setChecked(True)
            return

        self.hardware_decoding = mode
        logger.info("set_hardware_decoding - %s", mode)
        self.restart()


    def set_auto_pause_option(self, config_key: str, attribute: str, checked: bool) -> bool:

        if self.is_exiting:
            return False

        try:
            config = config_store.load()
        except OSError:
            return False

        config[config_key] = checked
        if not config_store.save(config):
            return False

        for wallpaper in self.get_wallpapers():
            setattr(wallpaper, attribute, checked)

        return True


    def set_font_size(self, font_size: int) -> None:
        """
        保存文字大小並即時套用到應用程式
        """

        if not self.is_exiting and font_size != self.font_size:
            config = config_store.load()
            config["fontSize"] = font_size

            if config_store.save(config):
                self.font_size = font_size
                font = self.app.font()
                font.setPointSize(font_size)
                self.app.setFont(font)

        for size, action in self.font_size_actions.items():
            action.setChecked(size == self.font_size)


    def set_preview_size(self, preview_size: int) -> None:
        """
        保存預覽大小並套用到所有螢幕選單
        """

        if self.is_exiting or preview_size == self.preview_size:
            self.preview_size_actions[self.preview_size].setChecked(True)
            return

        config = config_store.load()
        config["previewSize"] = preview_size

        if not config_store.save(config):
            self.preview_size_actions[self.preview_size].setChecked(True)
            return

        self.preview_size = preview_size
        for menu in (self.media_menu, *self.screen_menus.values()):
            menu.set_preview_size(preview_size)


    def toggle_startup(self, checked: bool) -> None:

        if checked:
            create_task(FILE)
        else:
            delete_task()


    def shutdown(self) -> None:
        """
        停止共用背景工作並逐台釋放桌布, 重複呼叫不重複清理
        """

        if self.is_exiting:
            return

        self.is_exiting = True
        self.follow_sync_timer.stop()
        self.follow_restore_timer.stop()
        self.follow_controller.shutdown()
        self.wait_for_import()

        if self.import_thread is not None:
            self.import_thread.deleteLater()
            self.import_thread = None

        try:
            self.thumbnail_thread.stop()

        except Exception:
            logger.exception("退出時停止縮圖工作失敗")

        for menu in self.screen_menus.values():
            menu.reset_preview()

        self.media_menu.reset_preview()
        wallpapers = tuple(self.wallpapers.items())
        self.wallpapers.clear()

        for screen_id, wallpaper in wallpapers:
            try:
                wallpaper.shutdown()

            except Exception:
                logger.exception("退出時釋放桌布失敗 - %s", screen_id)


    def restart(self) -> None:
        """
        標記重啟要求並執行退出流程
        """

        if self.is_exiting:
            return

        self.restart_requested = True
        self.exit()


    def exit(self) -> None:

        self.shutdown()

        self.app.quit()
        logger.info("exit")


def main() -> int:

    global config_store, LANG

    already_running, mutex = is_already_running()

    initialize_paths()

    config_store = ConfigStore(CONFIG_PATH, MEDIA_DIR, ROOT_DIR / "lang")

    if already_running:

        mutex.Close()  # type: ignore

        LANG = config_store.load_language()
        show_already_running_toast()

        return 0

    initialize_logging()

    if not initialize_vlc():
        return 1

    # 修復設定檔
    try:
        if not config_store.save(config_store.load()):
            return 1

        LANG = config_store.load_language()

    except OSError:
        return 1

    cleanup_orphaned_thumbnails(MEDIA_DIR, THUMBNAIL_DIR)

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
    font.setPointSize(config_store.load()["fontSize"])
    app.setFont(font)

    # 建立系統匣
    tray = Tray(app)

    # Qt 主迴圈
    exit_code = app.exec()

    if tray.restart_requested:

        tray.shutdown()

        if tray.reset_requested:

            # 除了語言設定, 其他都清除
            config = {"language": config_store.load()["language"]}
            if not config_store.save(config):
                return 1

            delete_task()

            logger.info("reset_defaults")

        logging.shutdown()

        mutex.Close()  # type: ignore

        command = [sys.executable]
        if not getattr(sys, "frozen", False):
            command.append(str(FILE))
        command.extend(sys.argv[1:])

        try:
            subprocess.Popen(
                command,
                cwd=ROOT_DIR,
                env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
                creationflags=subprocess.DETACHED_PROCESS
            )

        except OSError:
            logger.exception("重新啟動程式失敗")
            return 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
