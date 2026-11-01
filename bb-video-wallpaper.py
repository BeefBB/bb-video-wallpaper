import ctypes
from collections.abc import Sequence
import sys
import os
from ctypes import wintypes
from pathlib import Path
import time
import logging
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import vlc

from win32con import WM_DESTROY

from PySide6.QtWidgets import QApplication, QWidget, QSystemTrayIcon, QMenu, QFileDialog
from PySide6.QtCore import QEventLoop, Qt, QTimer, QThread, Slot, Signal
from PySide6.QtGui import QAction, QIcon, QFont, QScreen

import bb_image_display
from config import CONFIG_DEFAULTS, ConfigStore, SUPPORTED_LANGUAGES
from media_import import MediaImportCancelled, import_media
from media_preview import MediaPreviewMenu
from playback import AutoPauseReason, PlaybackState, update_auto_pause
from screen_follow import AutoPauseHandler, FollowController, RESTORE_DELAY_MS, SYNC_INTERVAL_MS
from thumbnails import (
    SUPPORTED_VIDEO_EXTENSIONS,
    SUPPORTED_IMAGE_EXTENSIONS,
    SUPPORTED_MEDIA_EXTENSIONS,
    ThumbnailThread,
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
    wake_vlc_video_windows,
)


logger = logging.getLogger(__name__)


# 由啟動流程設定的路徑與共用物件
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
config_store: ConfigStore
LANG: dict[str, str] = {}
_vlc_dll_directory: os._AddedDllDirectory | None = None


def initialize_paths() -> None:
    """
    依原始碼或打包位置設定執行路徑
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
    LOADING_IMAGE_PATH = icon_dir / "loading.jpg"

    APPDATA_DIR = Path(os.environ["LOCALAPPDATA"]) / "BB" / "BB Video Wallpaper"
    MEDIA_DIR = APPDATA_DIR / "video"
    SINGLE_IMPORT_DIR = MEDIA_DIR / "_imports"
    THUMBNAIL_DIR = APPDATA_DIR / "thumbnails"
    CONFIG_PATH = APPDATA_DIR / "config.json"
    LOG_DIR = APPDATA_DIR / "log"


def initialize_logging() -> None:
    """
    建立日誌資料夾並設定輪替日誌
    """

    LOG_DIR.mkdir(exist_ok=True)

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
    先設定 DLL 搜尋位置與載入原生函式庫, 再匯入 VLC
    """

    global vlc, _vlc_dll_directory

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

    import vlc

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
    匯入媒體的背景執行緒, 支援進度回報
    """

    progress = Signal(int)

    def __init__(self, source: Path) -> None:

        super().__init__()


        self.source = source
        self.destination: Path | None = None
        self.error = ""
        self.cancelled = False


    def run(self) -> None:

        try:
            self.destination = import_media(
                self.source,
                SINGLE_IMPORT_DIR,
                translate=tr,
                progress=self.progress.emit,
                cancelled=self.isInterruptionRequested
            )

        except MediaImportCancelled:
            self.cancelled = True
            logger.info("已取消新增媒體 - %s", self.source)

        except Exception as e:
            logger.exception("新增媒體失敗 - %s", self.source)
            self.error = str(e)


class VlcStopThread(QThread):
    """
    停止影片輸出時讓 Qt 主執行緒仍能處理原生視窗訊息
    """

    def __init__(self, player, parent=None) -> None:

        super().__init__(parent)
        self.player = player
        self.error = None


    def run(self) -> None:

        try:
            self.player.stop()

        except Exception as error:
            self.error = error


class DesktopEventWindow(QWidget):
    """
    用獨立的隱藏頂層視窗接收通知, 避免隨 Explorer 的桌布容器一起失效
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


    def nativeEvent(self, eventType, message) -> tuple[bool, int]:

        if self.taskbar_created_message and eventType in (b"windows_generic_MSG", "windows_generic_MSG"):
            msg = wintypes.MSG.from_address(int(message))

            if msg.message == self.taskbar_created_message:
                self.taskbar_created.emit()
                return True, 0

        return super().nativeEvent(eventType, message)  # type: ignore


class Wallpaper(QWidget):
    """
    桌布視窗
    """

    # 影片開始播放訊號
    playback_started = Signal()
    # 重掛完成訊號
    reattach_finished = Signal()


    def __init__(self, target_screen: QScreen | None = None) -> None:

        super().__init__()


        # 未指定時使用建立當下的主螢幕, 重掛時保留同一個目標
        if target_screen is None:
            target_screen = QApplication.primaryScreen()

        if target_screen is None:
            raise RuntimeError("找不到可用的目標螢幕")

        self.target_screen = target_screen


        # 設定檔
        config = config_store.load()

        # 正在重建
        self.is_recreating = False
        # 重掛失敗重試次數
        self.reattach_retry_count = 0
        # 正在退出
        self.is_exiting = False
        self._shutdown_started = False
        self._video_stop_thread: VlcStopThread | None = None

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
        self.playback_state = PlaybackState()
        self.pending_reattach_time: int | None = None
        # 重掛載時的媒體與進度
        self._reattach_position: tuple[Path, int] | None = None
        self.auto_pause_handler: AutoPauseHandler | None = None

        # 全螢幕檢測就緒
        self.fullscreen_detection_ready = False

        # 螢幕亮滅
        self.screen_off = False
        # 監聽螢幕亮滅的事件
        self.power_notify = None

        # 自動暫停設定
        self.auto_pause_if_fullscreen = config.get("autoPauseIfFullscreen", True)
        self.auto_pause_if_screen_off = config.get("autoPauseIfScreenOff", True)
        self.auto_pause_if_on_battery = config.get("autoPauseIfOnBattery", False)


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


        # 在掛入桌面前辨識目標螢幕
        self.screen_id = get_monitor_id(int(self.winId()))
        screen_wallpapers = config.get("screenWallpapers", {})
        saved_media = screen_wallpapers.get(self.screen_id)

        if saved_media and (MEDIA_DIR / saved_media).is_file():
            initial_media = MEDIA_DIR / saved_media

        if initial_media is None:
            recent = config.get("recentVideo", [])

            if recent and (MEDIA_DIR / recent[0]).is_file():
                initial_media = MEDIA_DIR / recent[0]

        if initial_media is None and media_files:
            initial_media = media_files[0]


        # 螢幕解析度變更事件
        self.target_screen.geometryChanged.connect(self.on_screen_changed)


        # 建立 VLC 播放器物件
        self.instance_vlc = vlc.Instance(
            "--no-video-title-show",
            "--input-repeat=65535",
            "--no-audio"
        )
        self.instance_vlc_player = self.instance_vlc.media_player_new()  # type: ignore

        # 建立圖片顯示物件
        # 圖片共用主程式的 QApplication 與桌布 HWND
        self.instance_image = bb_image_display.Instance()


        # VLC 回呼只發送訊號, 播放器操作回到 Qt 主執行緒處理
        self.playback_started.connect(self.apply_pending_reattach_time, Qt.ConnectionType.QueuedConnection)
        self.playback_started.connect(self.apply_pending_pause, Qt.ConnectionType.QueuedConnection)

        self.player_events = self.instance_vlc_player.event_manager()
        self.player_events.event_attach(
            vlc.EventType.MediaPlayerPlaying,  # type: ignore
            lambda event: self.playback_started.emit()
        )


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


        self.fullscreen_detection_timer = QTimer(self)
        self.fullscreen_detection_timer.setSingleShot(True)
        self.fullscreen_detection_timer.timeout.connect(self.enable_fullscreen_detection)
        self.fullscreen_detection_timer.start(10000)

        self.check_auto_pause_timer = QTimer(self)
        self.check_auto_pause_timer.timeout.connect(lambda: self.check_auto_pause(from_timer=True))
        self.check_auto_pause_timer.start(500)

        # 保持獨立頂層 HWND, 不掛在桌布視窗或 Explorer 之下
        self.desktop_events = None

        try:
            self.desktop_events = DesktopEventWindow()
            self.desktop_events.taskbar_created.connect(self.on_taskbar_created, Qt.ConnectionType.QueuedConnection)

        except Exception:
            logger.exception("建立工作列重建通知視窗失敗")


    def nativeEvent(self, eventType, message) -> tuple[bool, int]:
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


        return super().nativeEvent(eventType, message)  # type: ignore


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

        elif target == "play" or self.play_state == "stop" or force:
            self.instance_vlc_player.play()

        self.play_state = target
        if target == "pause":
            self.apply_pending_pause()


    def _stop_media_output(self, *, preserve_reattach_position: bool = False) -> None:
        """
        停止目前輸出, 媒體切換時保留播放意圖
        """

        if self.is_exiting:
            return

        self.pending_reattach_time = None
        if not preserve_reattach_position:
            self._reattach_position = None

        if self.is_image():
            self.instance_image.hide()

        else:
            self.instance_vlc_player.stop()

        self.play_state = "stop"


    def manual_play(self) -> None:
        """
        手動播放, 清除先前的自動暫停判斷
        """

        if self.is_exiting or not self.current_media:
            return

        self.apply_playback_state(PlaybackState("play"), force=True)
        logger.info("manual - play")


    def manual_pause(self) -> None:
        """
        手動暫停, 不由自動暫停條件恢復
        """

        if self.is_exiting:
            return

        if (
            self.current_media and
            self.current_media.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS and
            self.play_state != "stop"
        ):
            self.apply_playback_state(PlaybackState("pause"))

        logger.info("manual - pause")


    def manual_stop(self) -> None:
        """
        手動停止, 清除進度恢復與自動暫停
        """

        if self.is_exiting:
            return

        self.apply_playback_state(PlaybackState("stop"), force=True)
        logger.info("manual - stop")


    @Slot()
    def apply_pending_reattach_time(self) -> None:
        """
        新影片輸出開始後補套重掛前的播放進度
        """

        if self.is_exiting or self.is_recreating or self.is_image() or self.play_state == "stop":
            return

        if self.pending_reattach_time is not None:
            self.instance_vlc_player.set_time(self.pending_reattach_time)
            self.pending_reattach_time = None


    @Slot()
    def apply_pending_pause(self) -> None:
        """
        VLC 播放器在 MediaPlayerPlaying 事件發生前不會接受暫停指令, 所以要等到播放開始後再套用暫停
        """

        # 載入完成後依最新狀態補套暫停, 不覆蓋後續的播放或停止要求
        if not self.is_exiting and not self.is_recreating and not self.is_image() and self.play_state == "pause":
            self.instance_vlc_player.set_pause(1)


    def get_playback_time(self) -> int | None:
        """
        取得播放中或暫停中的進度, 尚未就緒時回傳 None
        """

        if (
            self.is_exiting or self.is_recreating or self.is_image() or not self.current_media or
            self.play_state == "stop" or self.pending_reattach_time is not None or
            self._reattach_position is not None
        ):
            return None

        # python-vlc 在執行時建立列舉成員
        playing_state: vlc.State = getattr(vlc.State, "Playing")
        paused_state: vlc.State = getattr(vlc.State, "Paused")
        if self.instance_vlc_player.get_state() not in (playing_state, paused_state):
            return None

        position = self.instance_vlc_player.get_time()
        return position if position >= 0 else None


    def get_playback_length(self) -> int:

        return self.instance_vlc_player.get_length()


    def seek_time(self, position: int) -> bool:

        if position < 0 or self.get_playback_time() is None or not self.instance_vlc_player.is_seekable():
            return False

        self.instance_vlc_player.set_time(position)
        return True


    @Slot()
    def on_taskbar_created(self) -> None:
        """
        工作列重建後啟動新的恢復嘗試, 實際掛載交給共用計時器
        """

        if self.is_exiting:
            return

        logger.info("收到 TaskbarCreated, 安排重新掛載桌布")
        self.reattach_retry_count = 0
        self.schedule_reattach()


    def schedule_reattach(self, delay_ms: int = 200) -> None:
        """
        安排一次重掛, 已有排程時保留原本的執行時間
        """

        if self.is_exiting or self.is_recreating or self.reattach_timer.isActive():
            return

        self.reattach_timer.start(delay_ms)


    def stop_video_output(self) -> None:
        """
        等待舊影片輸出停止, 期間處理視窗訊息並暫緩使用者輸入
        """

        loop = QEventLoop(self)
        # 重掛等待期間可能收到關閉要求, 共用同一次停止工作
        worker = self._video_stop_thread
        owns_worker = worker is None

        if worker is None:
            worker = VlcStopThread(self.instance_vlc_player, self)
            self._video_stop_thread = worker

        worker.finished.connect(loop.quit)
        wake_timer = QTimer(self)
        wake_timer.timeout.connect(wake_vlc_video_windows)
        wake_timer.start(50)
        if owns_worker:
            worker.start()

        if worker.isRunning():
            loop.exec(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)

        worker.wait()
        wake_timer.stop()
        wake_timer.deleteLater()
        error = worker.error
        worker.finished.disconnect(loop.quit)

        if owns_worker:
            self._video_stop_thread = None
            worker.deleteLater()

        loop.deleteLater()

        if error is not None:
            raise error


    def reattach_to_desktop(self) -> None:
        """
        重新建立視窗並掛載桌布
        """

        if self.is_exiting:
            return

        if self.is_recreating:
            return

        # 直接呼叫重掛時, 也取消尚未執行的排程
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


            # 先停止舊影片輸出再換綁 HWND, 避免 VLC 在換綁後停止時卡住
            # 直接停止播放器, 保留 play_state 供重掛後恢復
            if not self.is_image():
                if self.current_media and self._reattach_position is None:
                    # 尚未完成的 seek 比新輸出回報的起始進度優先
                    curr_time = self.pending_reattach_time
                    if curr_time is None:
                        curr_time = self.instance_vlc_player.get_time()
                    if curr_time >= 0:
                        self._reattach_position = (self.current_media, curr_time)

                # 舊輸出的 Playing 通知不能消耗等待重試的進度
                self.pending_reattach_time = None
                self.stop_video_output()

            # 停止輸出的事件迴圈可能已處理關閉要求
            if self.is_exiting:
                self.is_recreating = False
                return


            self.unregister_power_notification()
            self.instance_image.set_hwnd(0)
            self.hide()


            # 銷毀失效的 HWND, 重新向 Windows 申請新的 HWND
            self.destroy(True, True)
            self.create()


            # 建立視窗
            self.setGeometry(self.target_screen.geometry())

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

                if self.is_image():

                    if self.play_state == "play":
                        self.apply_playback_state(force=True)

                else:

                    self.set_play_media(self.current_media, preserve_reattach_position=True)

                    if self._reattach_position is not None and self._reattach_position[0] == self.current_media:
                        curr_time = self._reattach_position[1]
                    else:
                        curr_time = -1

                    if curr_time > 0:
                        self.instance_vlc_player.set_time(curr_time)
                        # VLC 尚在啟動時可能略過 seek, 等 Playing 事件再補套
                        if self.play_state != "stop":
                            self.pending_reattach_time = curr_time


            self._reattach_position = None
            logger.info("reattach_to_desktop - finish")
            self.is_recreating = False
            self.reattach_retry_count = 0

        except Exception:

            logger.exception("重新掛載桌布時發生錯誤")

            self.is_recreating = False

            if self.reattach_retry_count < 2:

                self.reattach_retry_count += 1

                logger.warning("正在重試重新掛載桌布（第 %s 次）", self.reattach_retry_count)
                self.schedule_reattach(2000)

            else:
                logger.error("重新掛載桌布失敗")

        finally:
            self.is_recreating = False
            self.reattach_finished.emit()


    def on_screen_changed(self, geometry) -> None:
        """
        螢幕解析度變更時重整
        """

        if self.is_exiting or self.is_recreating:
            return


        logger.info("screen_changed - %s", geometry)
        self.schedule_reattach()


    def set_play_media(self, path: Path, *, preserve_reattach_position: bool = False) -> None:
        """
        圖片交給 Qt 顯示, 影片交給 VLC, 保留原本的播放或停止狀態
        """

        if self.is_exiting:
            return

        is_image = path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS

        # 先讀取圖片, 失敗時保留原本的輸出與播放狀態
        if is_image:
            self.instance_image.set_image(path)


        self._stop_media_output(preserve_reattach_position=preserve_reattach_position)
        logger.debug("set_play_media - stop")


        if not is_image:

            media = self.instance_vlc.media_new(str(path))  # type: ignore

            try:
                self.instance_vlc_player.set_media(media)

            finally:
                media.release()

            # 裁切填滿螢幕, 並釋放已不再使用的圖片
            self.instance_vlc_player.video_set_crop_geometry(self.get_screen_aspect_ratio())
            self.instance_image.close()


        self.current_media = path
        logger.info("set_play_media - %s", path)

        self.apply_playback_state()


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
        self.instance_vlc_player.set_hwnd(hwnd)
        self.instance_image.set_hwnd(hwnd)


        self.power_notify = register_power_notification(hwnd)

        if not self.power_notify:
            logger.error("螢幕電源通知註冊失敗")


    def get_screen_aspect_ratio(self) -> str:
        """
        取得螢幕解析度的寬高比
        """

        geometry = self.target_screen.geometry()

        width = geometry.width()
        height = geometry.height()

        return f"{width}:{height}"


    def enable_fullscreen_detection(self) -> None:
        """
        啟動全螢幕視窗偵測
        """

        if self.is_exiting:
            return

        self.fullscreen_detection_ready = True
        logger.debug("fullscreen detection - ready")


    def check_auto_pause(self, *, from_timer: bool = False) -> None:
        """
        檢查是否需要自動暫停
        """

        if self.is_exiting or self.is_recreating:
            return

        if self.auto_pause_handler is not None and self.auto_pause_handler(self, from_timer=from_timer):
            return

        if update_auto_pause((self,)):
            logger.info("auto_pause - %s", self.play_state)


    def get_auto_pause_reason(self) -> AutoPauseReason | None:
        """
        優先回報不需掃描視窗的暫停原因
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

        if self.power_notify:

            try:
                unregister_power_notification(self.power_notify)

            except Exception:
                logger.exception("取消註冊電源通知時發生錯誤")

            finally:
                self.power_notify = None


    def shutdown(self) -> None:
        """
        釋放此桌布的資源, 可重複呼叫且不退出 QApplication
        """

        if self._shutdown_started:
            return

        self._shutdown_started = True
        self.is_exiting = True
        self.auto_pause_handler = None
        self.playback_state = PlaybackState()
        self.fullscreen_detection_ready = False
        self.pending_reattach_time = None
        self._reattach_position = None
        self.play_state = "stop"
        self.current_media = None

        desktop_events = self.desktop_events
        self.desktop_events = None

        steps = [
            ("停止重掛計時器", self.reattach_timer.stop),
            ("停止自動暫停計時器", self.check_auto_pause_timer.stop),
            ("停止全螢幕偵測等待", self.fullscreen_detection_timer.stop),
            ("解除螢幕訊號", lambda: self.target_screen.geometryChanged.disconnect(self.on_screen_changed)),
            ("解除 VLC 回呼", lambda: self.player_events.event_detach(vlc.EventType.MediaPlayerPlaying)),  # type: ignore
            ("解除進度恢復訊號", lambda: self.playback_started.disconnect(self.apply_pending_reattach_time)),
            ("解除暫停恢復訊號", lambda: self.playback_started.disconnect(self.apply_pending_pause)),
        ]

        if desktop_events is not None:
            steps.extend([
                ("解除桌面通知訊號", lambda: desktop_events.taskbar_created.disconnect(self.on_taskbar_created)),
                ("關閉桌面通知視窗", desktop_events.close),
                ("釋放桌面通知視窗", desktop_events.deleteLater),
            ])

        steps.extend([
            ("取消電源通知", self.unregister_power_notification),
            ("停止影片輸出", self.stop_video_output),
            ("關閉圖片輸出", self.instance_image.close),
            ("解除圖片目標視窗", lambda: self.instance_image.set_hwnd(0)),
            ("釋放圖片物件", self.instance_image.deleteLater),
            ("解除影片目標視窗", lambda: self.instance_vlc_player.set_hwnd(0)),
            ("釋放 VLC 播放器", self.instance_vlc_player.release),
            ("釋放 VLC 實例", self.instance_vlc.release),  # type: ignore
            ("隱藏桌布視窗", self.hide),
            ("釋放桌布視窗", lambda: self.destroy(True, True)),
        ])

        # 單一步驟失敗時仍繼續釋放其餘資源
        for description, cleanup in steps:
            try:
                cleanup()

            except Exception:
                logger.exception("釋放桌布資源失敗 - %s", description)


class Tray:

    def __init__(self, app: "QApplication") -> None:

        self.app = app
        self.wallpapers: dict[str, Wallpaper] = {}
        self.screen_menus: dict[str, MediaPreviewMenu] = {}
        self.is_exiting = False

        # 系統匣圖示與選單
        self.tray = QSystemTrayIcon()
        self.tray.setIcon(QIcon(str(ICON_PATH)))
        self.tray.setToolTip(tr("app_name"))

        # 匯入媒體的背景執行緒
        self.import_thread: MediaImportThread | None = None
        self.import_screen_id: str | None = None
        self.import_applies_to_all = False
        self.app.aboutToQuit.connect(self.shutdown)

        # 共用縮圖工作
        self.thumbnail_thread = ThumbnailThread(MEDIA_DIR, THUMBNAIL_DIR)

        # 設定檔
        config = config_store.load()

        self.follow_controller = FollowController(self.wallpapers, config_store)
        self.saved_follows: dict[str, str] = config.get("screenFollows", {})
        self.waiting_for_screens = bool(self.saved_follows)

        # 語言代碼
        self.current_language_code = config.get("language", "zh-TW")


        self.menu = menu = QMenu()

        self.playback_actions = self.add_playback_actions(menu)
        exit_action = QAction(tr("exit"), menu)


        menu.aboutToShow.connect(self.refresh_playback_actions)


        self.media_menu = MediaPreviewMenu(
            tr("select_video"),
            menu,
            media_dir=MEDIA_DIR,
            thumbnail_dir=THUMBNAIL_DIR,
            loading_image_path=LOADING_IMAGE_PATH,
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


        # 主要選單
        # ################
        menu.addSeparator()

        menu.addMenu(self.media_menu)
        menu.addAction(self.add_media_action)
        menu.addAction(self.cancel_import_action)
        menu.addAction(open_media_action)

        menu.addSeparator()

        self.options_menu = menu.addMenu(tr("options"))

        menu.addSeparator()

        menu.addAction(exit_action)
        # ################


        # 選項子選單
        # ################
        for text_key, config_key, attribute in (
            ("auto_pause_fullscreen", "autoPauseIfFullscreen", "auto_pause_if_fullscreen"),
            ("auto_pause_screen_off", "autoPauseIfScreenOff", "auto_pause_if_screen_off"),
            ("auto_pause_battery", "autoPauseIfOnBattery", "auto_pause_if_on_battery"),
        ):
            action = QAction(tr(text_key), self.options_menu)
            action.setCheckable(True)
            action.setChecked(config.get(config_key, CONFIG_DEFAULTS[config_key]))
            action.triggered.connect(
                lambda checked, key=config_key, attr=attribute: self.set_auto_pause_option(key, attr, checked)
            )
            self.options_menu.addAction(action)

        self.options_menu.addSeparator()

        self.options_language_menu = self.options_menu.addMenu(tr("language"))

        self.options_menu.addSeparator()

        self.options_menu.addAction(startup_action)
        # ################


        # 每次開啟選單時, 重新建立語言選單
        self.options_language_menu.aboutToShow.connect(self.refresh_language_menu)

        self.tray.setContextMenu(menu)

        self.follow_sync_timer = QTimer(menu)
        self.follow_sync_timer.timeout.connect(self.follow_controller.synchronize)
        self.follow_sync_timer.start(SYNC_INTERVAL_MS)

        self.follow_restore_timer = QTimer(menu)
        self.follow_restore_timer.setSingleShot(True)
        self.follow_restore_timer.timeout.connect(self.initialize_screens)

        self.app.screenAdded.connect(self.add_screen)
        self.app.screenRemoved.connect(self.remove_screen)

        if self.waiting_for_screens:
            # 等待螢幕列舉穩定後再建立桌布
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
        為新螢幕建立桌布, 重複通知不重建既有桌布
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
        for signal in (wallpaper.playback_started, wallpaper.reattach_finished):
            signal.connect(
                lambda s=wallpaper.screen_id, w=wallpaper: self.follow_controller.on_ready(s, w),
                Qt.ConnectionType.QueuedConnection,
            )
        wallpaper.show()

        if refresh:
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
                        ),
                    )
                else:
                    wallpaper.deleteLater()

            # 匯入結果可能正在等待已拔除桌布的重掛訊號
            if self.import_thread is not None and not self.import_thread.isRunning():
                self.finish_import(self.import_thread)
            break


    def rebuild_screen_menus(self) -> None:
        """
        依目前螢幕數切換單螢幕選單與各螢幕子選單
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
                    thumbnail_thread=self.thumbnail_thread
                )
                menu.aboutToShow.connect(
                    lambda s=screen_id, m=menu: self.refresh_screen_menu(s, m)
                )
                self.screen_menus[screen_id] = menu
                self.menu.insertMenu(self.add_media_action, menu)

        self.refresh_playback_actions()


    def refresh_playback_actions(
        self,
        actions: Sequence[tuple[QAction, str]] | None = None,
        screen_id: str | None = None,
    ) -> None:

        wallpapers = self.get_wallpapers(screen_id)
        multiple = screen_id is None and len(wallpapers) > 1
        wallpaper = wallpapers[0] if wallpapers else None
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


    def get_wallpapers(self, screen_id: str | None = None) -> tuple["Wallpaper", ...]:

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
            self.follow_controller.detach(screen_id)

        for wallpaper in self.get_wallpapers(screen_id):
            wallpaper.manual_stop()

        self.follow_controller.synchronize(screen_id)


    def refresh_language_menu(self) -> None:
        """
        重新建立語言選單
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


    def refresh_screen_menu(self, screen_id: str, menu: "MediaPreviewMenu") -> None:
        """
        重新建立指定螢幕的操作與媒體選單
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
        self.add_media_menu_actions(menu, screen_id)
        if len(self.wallpapers) > 1:
            menu.addSeparator()
            self.add_follow_menu(menu, screen_id)


    def add_follow_menu(self, menu: "QMenu", screen_id: str) -> None:

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


    def add_playback_actions(self, menu: "QMenu", screen_id: str | None = None) -> tuple[tuple[QAction, str], ...]:
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
        重新建立單螢幕的媒體選單
        """

        self.media_menu.reset_preview()
        self.media_menu.clear()
        screen_id = next(iter(self.wallpapers)) if len(self.wallpapers) == 1 else None
        self.add_media_menu_actions(self.media_menu, screen_id)


    def add_media_menu_actions(self, menu: "MediaPreviewMenu", screen_id: str | None) -> None:
        """
        新增最近紀錄與媒體清單項目
        """

        recent = config_store.load().get("recentVideo", [])[:5]

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
        menu: "MediaPreviewMenu | None" = None
    ) -> None:
        """
        新增媒體選單項目
        """

        menu = self.media_menu if menu is None else menu
        wallpaper = self.wallpapers.get(screen_id) if screen_id is not None else None
        action = QAction(media_path.name, menu)
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

        if language_code == self.current_language_code:
            return

        self.current_language_code = language_code
        config_store.load_language(language_code)

        logger.info("set_language - %s", language_code)


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

        except Exception as error:

            logger.exception("載入桌布時發生錯誤 - %s", path)
            self.tray.showMessage(
                tr("app_name"),
                tr("media_load_failed").format(error=error),
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

        filename, _ = QFileDialog.getOpenFileName(
            None,
            tr("add_media"),
            "",
            f'{tr("media_files")} ({patterns})'
        )

        if not filename:
            self.add_media_action.setEnabled(True)
            return

        self.add_media_action.setText(tr("comparing_media"))
        self.cancel_import_action.setEnabled(True)
        self.cancel_import_action.setVisible(True)
        self.import_screen_id = next(iter(self.wallpapers)) if len(self.wallpapers) == 1 else None
        self.import_applies_to_all = len(self.wallpapers) > 1 and not get_media()

        self.import_thread = MediaImportThread(Path(filename))
        self.import_thread.progress.connect(self.update_import_progress, Qt.ConnectionType.QueuedConnection)
        self.import_thread.finished.connect(lambda w=self.import_thread: self.finish_import(w))
        self.import_thread.start()


    def update_import_progress(self, percent: int) -> None:

        if self.import_thread is not None and not self.import_thread.isInterruptionRequested():
            self.add_media_action.setText(tr("importing_media").format(percent=percent))


    def cancel_import(self) -> None:

        if self.import_thread is not None and self.import_thread.isRunning():
            self.import_thread.requestInterruption()
            self.cancel_import_action.setEnabled(False)


    def finish_import(self, expected_worker: "MediaImportThread | None" = None) -> None:

        worker = self.import_thread

        if worker is None or (expected_worker is not None and worker is not expected_worker):
            return

        screen_ids: tuple[str, ...] = ()

        if self.import_applies_to_all:
            screen_ids = tuple(self.wallpapers)

        elif self.import_screen_id is not None and self.import_screen_id in self.wallpapers:
            screen_ids = (self.import_screen_id,)

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
                        ),
                    )
                    return

        self.import_thread = None
        worker.deleteLater()

        self.add_media_action.setText(tr("add_media"))
        self.add_media_action.setEnabled(True)
        self.cancel_import_action.setVisible(False)

        if self.is_exiting or worker.cancelled:
            return

        if worker.destination is not None:

            self.thumbnail_thread.request(worker.destination)
            config_store.remember_recent(worker.destination)

            for screen_id in screen_ids:
                self.play_from_menu(worker.destination, screen_id)

            self.refresh_media_menu()

        else:
            self.tray.showMessage(
                tr("app_name"),
                tr("media_add_failed").format(error=worker.error),
                QSystemTrayIcon.MessageIcon.Information,
                3000
            )


    def wait_for_import(self) -> None:
        """
        等待匯入媒體的背景執行緒結束
        """

        if self.import_thread is not None:
            self.import_thread.requestInterruption()
            self.import_thread.wait()


    def open_media_folder(self) -> None:

        os.startfile(str(MEDIA_DIR))


    def set_auto_pause_option(self, config_key: str, attribute: str, checked: bool) -> None:

        config = config_store.load()
        config[config_key] = checked
        config_store.save(config)

        for wallpaper in self.get_wallpapers():
            setattr(wallpaper, attribute, checked)


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


    def exit(self) -> None:

        self.shutdown()

        self.app.quit()
        logger.info("exit")


def main() -> int:

    global config_store, LANG

    initialize_paths()
    already_running, mutex = is_already_running()
    config_store = ConfigStore(CONFIG_PATH, MEDIA_DIR, ROOT_DIR / "lang")

    if already_running:

        LANG = config_store.load_language()
        show_already_running_toast()

        return 0


    APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(exist_ok=True)
    initialize_logging()

    if not initialize_vlc():
        return 1

    config_store.initialize_if_missing()
    LANG = config_store.load_language()


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


    # 建立系統匣
    tray = Tray(app)


    # Qt 主迴圈
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
