import ctypes
import sys
import os
from ctypes import wintypes
from pathlib import Path
import time
import logging
from logging.handlers import RotatingFileHandler

from win32con import WM_DESTROY

from PySide6.QtWidgets import QApplication, QWidget, QSystemTrayIcon, QMenu, QFileDialog
from PySide6.QtCore import QEventLoop, Qt, QTimer, QThread, Slot, Signal
from PySide6.QtGui import QAction, QIcon, QFont, QScreen

import bb_image_display
from config import ConfigStore, SUPPORTED_LANGUAGES
from media_import import MediaImportCancelled, import_media
from media_preview import MediaPreviewMenu
from thumbnails import (
    SUPPORTED_VIDEO_EXTENSIONS,
    SUPPORTED_IMAGE_EXTENSIONS,
    SUPPORTED_MEDIA_EXTENSIONS,
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


# 路徑

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


APPDATA_DIR = Path(os.environ["LOCALAPPDATA"]) / "BB" / "BB Video Wallpaper"
APPDATA_DIR.mkdir(parents=True, exist_ok=True)

MEDIA_DIR = APPDATA_DIR / "video"
MEDIA_DIR.mkdir(exist_ok=True)

SINGLE_IMPORT_DIR = MEDIA_DIR / "_imports"

THUMBNAIL_DIR = APPDATA_DIR / "thumbnails"

CONFIG_PATH = APPDATA_DIR / "config.json"

LOG_DIR = APPDATA_DIR / "log"

VLC_DIR = next(
    (
        path
        for path in (ROOT_DIR / "VLC-lite", ROOT_DIR / "VLC")
        if path.is_dir()
    ),
    None
)


already_running = False

if __name__ == "__main__":
    already_running, mutex = is_already_running()


if not already_running:

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

logger = logging.getLogger(__name__)


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


# 設定檔
config_store = ConfigStore(CONFIG_PATH, MEDIA_DIR, ROOT_DIR / "lang")

if __name__ == "__main__" and not already_running:
    config_store.initialize_if_missing()


LANG = config_store.load_language()

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
        self.pending_reattach_time: int | None = None
        # 重掛載時的媒體與進度
        self._reattach_position: tuple[Path, int] | None = None
        # 啟用自動暫停
        self.enable_auto_pause = True

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
                self.media_play()

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
        self.check_auto_pause_timer.timeout.connect(self.check_auto_pause)
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


    def media_play(self) -> None:
        """
        播放
        """

        if not self.current_media or self.is_exiting:
            return

        if self.is_image():
            self.instance_image.show()

        else:
            self.instance_vlc_player.play()

        self.play_state = "play"


    def media_pause(self) -> None:
        """
        暫停播放
        """

        if self.is_exiting or not self.current_media or self.is_image():
            return

        self.play_state = "pause"
        self.apply_pending_pause()


    def media_stop(self, *, preserve_reattach_position: bool = False) -> None:
        """
        停止播放
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
        手動播放, 恢復自動暫停
        """

        if self.is_exiting or not self.current_media:
            return

        self.media_play()
        self.enable_auto_pause = True
        logger.info("manual - play")


    def manual_pause(self) -> None:
        """
        手動暫停, 停用自動暫停
        """

        if self.is_exiting:
            return

        if (
            self.current_media and
            self.current_media.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS and
            self.play_state == "play"
        ):
            self.media_pause()

        self.enable_auto_pause = False
        logger.info("manual - pause")


    def manual_stop(self) -> None:
        """
        手動停止, 停用自動暫停
        """

        if self.is_exiting:
            return

        self.media_stop()
        self.enable_auto_pause = False
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
                        self.media_play()

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

        previous_play_state = self.play_state
        is_image = path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS

        # 先讀取圖片, 失敗時保留原本的輸出與播放狀態
        if is_image:
            self.instance_image.set_image(path)


        self.media_stop(preserve_reattach_position=preserve_reattach_position)
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

        if is_image:

            # 圖片沒有暫停狀態, 從暫停中的影片切換時直接顯示
            if previous_play_state != "stop":
                self.media_play()

        else:

            # 讓影片畫面出現後再回復原本的狀態
            self.media_play()
            logger.debug("set_play_media - play")

            if previous_play_state == "pause":

                self.media_pause()
                logger.debug("set_play_media - pause")

            elif previous_play_state == "stop":

                self.media_stop(preserve_reattach_position=preserve_reattach_position)
                logger.debug("set_play_media - stop")


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


    def check_auto_pause(self) -> None:
        """
        檢查是否需要自動暫停
        """

        if self.is_exiting or self.is_recreating:
            return

        if not self.enable_auto_pause:
            return

        # 圖片不需要暫停
        if self.is_image():
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
        self.enable_auto_pause = False
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

    def __init__(self, app, wallpaper) -> None:

        self.app = app
        self.wallpaper = wallpaper

        # 系統匣圖示與選單
        self.tray = QSystemTrayIcon()
        self.tray.setIcon(QIcon(str(ICON_PATH)))
        self.tray.setToolTip(tr("app_name"))

        # 匯入媒體的背景執行緒
        self.import_thread: MediaImportThread | None = None
        self.app.aboutToQuit.connect(self.wait_for_import)

        # 設定檔
        config = config_store.load()

        # 語言代碼
        self.current_language_code = config.get("language", "zh-TW")


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

        # 播放圖片時停用暫停按鈕
        menu.aboutToShow.connect(
            lambda: pause_action.setEnabled(
                bool(self.wallpaper.current_media) and
                self.wallpaper.current_media.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
            )
        )


        self.media_menu = MediaPreviewMenu(
            tr("select_video"),
            menu,
            media_dir=MEDIA_DIR,
            thumbnail_dir=THUMBNAIL_DIR,
            loading_image_path=LOADING_IMAGE_PATH
        )
        self.media_menu.aboutToShow.connect(self.refresh_media_menu)

        self.app.aboutToQuit.connect(self.media_menu.thumbnail_thread.stop)


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
                checked
            )
        )
        auto_pause_if_screen_off_action.triggered.connect(
            lambda checked: self.set_auto_pause_option(
                "autoPauseIfScreenOff",
                "auto_pause_if_screen_off",
                checked
            )
        )
        auto_pause_if_on_battery_action.triggered.connect(
            lambda checked: self.set_auto_pause_option(
                "autoPauseIfOnBattery",
                "auto_pause_if_on_battery",
                checked
            )
        )

        startup_action.triggered.connect(self.toggle_startup)


        # 主要選單
        # ################
        menu.addAction(play_action)
        menu.addAction(pause_action)
        menu.addAction(stop_action)

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
        self.options_menu.addAction(auto_pause_if_fullscreen_action)
        self.options_menu.addAction(auto_pause_if_screen_off_action)
        self.options_menu.addAction(auto_pause_if_on_battery_action)

        self.options_menu.addSeparator()

        self.options_language_menu = self.options_menu.addMenu(tr("language"))

        self.options_menu.addSeparator()

        self.options_menu.addAction(startup_action)
        # ################


        # 每次開啟選單時, 重新建立語言選單
        self.options_language_menu.aboutToShow.connect(self.refresh_language_menu)

        self.tray.setContextMenu(menu)
        self.tray.show()


    def play(self) -> None:

        self.wallpaper.manual_play()


    def pause(self) -> None:

        self.wallpaper.manual_pause()


    def stop(self) -> None:

        self.wallpaper.manual_stop()


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


    def refresh_media_menu(self) -> None:
        """
        重新建立媒體選單
        """

        self.media_menu.reset_preview()
        self.media_menu.clear()

        recent = config_store.load().get("recentVideo", [])[:5]

        for media_path in recent:
            self.add_media_menu_action(MEDIA_DIR / media_path)

        if recent:
            clear_action = QAction(tr("clear_recent_videos"), self.media_menu)
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
        """
        清除最近播放的媒體清單
        """

        config = config_store.load()
        config["recentVideo"] = []
        config_store.save(config)

        logger.info("clear_recent_media")


    def add_media_menu_action(self, media_path: Path) -> None:
        """
        新增媒體選單項目
        """

        action = QAction(media_path.name, self.media_menu)
        action.setData(str(media_path))

        action.triggered.connect(
            lambda checked=False, v=media_path: self.play_from_menu(v)
        )

        action.setCheckable(True)
        action.setChecked(media_path == self.wallpaper.current_media)
        action.setEnabled(media_path.is_file())

        self.media_menu.addAction(action)


    def set_language(self, language_code: str) -> None:

        if language_code == self.current_language_code:
            return

        self.current_language_code = language_code
        config_store.load_language(language_code)

        logger.info("set_language - %s", language_code)


    def play_from_menu(self, path: Path) -> None:

        try:

            self.wallpaper.select_media(path)

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
            f'{tr("media_files")} ({patterns})'
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

        # 巢狀事件迴圈仍會收到匯入完成訊號, 等當次重掛離開後才切換媒體
        if self.wallpaper.is_recreating:
            self.wallpaper.reattach_finished.connect(
                self.finish_import,
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

        setattr(self.wallpaper, attribute, checked)


    def toggle_startup(self, checked) -> None:

        if checked:
            create_task(FILE)
        else:
            delete_task()


    def exit(self) -> None:

        try:
            self.wallpaper.shutdown()

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
    w = Wallpaper(target_screen=app.primaryScreen())
    app.aboutToQuit.connect(w.shutdown)
    w.show()


    tray = Tray(app, w)


    # Qt 主迴圈
    sys.exit(app.exec())
