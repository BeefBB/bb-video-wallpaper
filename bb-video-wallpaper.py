import ctypes
import sys
import os
from ctypes import wintypes
from pathlib import Path
import time
import logging
from logging.handlers import RotatingFileHandler

from win32con import WM_DESTROY

from PySide6.QtWidgets import QApplication, QMessageBox, QWidget, QSystemTrayIcon, QMenu, QFileDialog, QLabel
from PySide6.QtCore import Qt, QTimer, QThread, Slot, Signal
from PySide6.QtGui import QAction, QIcon, QFont, QPixmap, QCursor, QImage

import bb_image_display
from config import ConfigStore, SUPPORTED_LANGUAGES
from media_import import MediaImportCancelled, import_media
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
    is_already_running,
    is_any_fullscreen,
    is_on_battery,
    register_power_notification,
    run_as_admin,
    set_windows_as_wallpaper,
    task_exists,
    unregister_power_notification,
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


class Wallpaper(QWidget):
    """
    桌布視窗
    """

    playback_started = Signal()


    def __init__(self) -> None:

        super().__init__()


        # 設定檔
        config = config_store.load()

        # 正在重建
        self.is_recreating = False
        self.reattach_retry_count = 0
        # 正在退出
        self.is_exiting = False

        # 目前播放的媒體檔案
        self.current_media: Path | None = None
        # 初始媒體檔案
        initial_media: Path | None = None
        # 媒體清單
        media_files = get_media()

        # 播放狀態: "play", "pause", "stop"
        self.play_state = "stop"
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


        recent = config.get("recentVideo")

        if recent:
            recent_path = MEDIA_DIR / recent[0]

            if recent_path.is_file():
                initial_media = recent_path

        if not initial_media and media_files:
            initial_media = media_files[0]


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
                logger.info("init - play")

            except Exception:
                logger.exception("載入初始桌布時發生錯誤 - %s", initial_media)


        QTimer.singleShot(10000, self.enable_fullscreen_detection)

        self.check_auto_pause_timer = QTimer(self)
        self.check_auto_pause_timer.timeout.connect(self.check_auto_pause)
        self.check_auto_pause_timer.start(500)


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

                if not self.is_recreating:

                    # 重新掛載
                    QTimer.singleShot(200, self.reattach_to_desktop)

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

        if not self.current_media or self.is_image():
            return

        self.play_state = "pause"
        self.apply_pending_pause()


    def media_stop(self) -> None:
        """
        停止播放
        """

        if self.is_image():
            self.instance_image.hide()

        else:
            self.instance_vlc_player.stop()

        self.play_state = "stop"


    @Slot()
    def apply_pending_pause(self) -> None:
        """
        VLC 播放器在 MediaPlayerPlaying 事件發生前不會接受暫停指令, 所以要等到播放開始後再套用暫停
        """

        # 載入完成後依最新狀態補套暫停, 不覆蓋後續的播放或停止要求
        if not self.is_exiting and not self.is_image() and self.play_state == "pause":
            self.instance_vlc_player.set_pause(1)


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
            curr_time = self.instance_vlc_player.get_time() if not self.is_image() else -1


            self.unregister_power_notification()
            self.instance_image.set_hwnd(0)
            self.hide()


            # 銷毀失效的 HWND, 重新向 Windows 申請新的 HWND
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

                if self.is_image():

                    if self.play_state == "play":
                        self.media_play()

                else:

                    self.set_play_media(self.current_media)

                    if curr_time > 0:
                        self.instance_vlc_player.set_time(curr_time)


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
        圖片交給 Qt 顯示, 影片交給 VLC, 保留原本的播放或停止狀態
        """

        media_path = path.relative_to(MEDIA_DIR).as_posix()
        previous_play_state = self.play_state
        is_image = path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS

        # 先讀取圖片, 失敗時保留原本的輸出, 播放狀態與最近紀錄
        if is_image:
            self.instance_image.set_image(path)


        self.media_stop()
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

                self.media_stop()
                logger.debug("set_play_media - stop")


        config = config_store.load()
        recent = config.get("recentVideo", [])
        config["recentVideo"] = [media_path] + [
            item for item in recent if item != media_path
        ][:4]
        config_store.save(config)


    def attach_to_desktop(self) -> None:
        """
        將 Qt 視窗移到 Windows 桌布層
        """

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

        if self.is_recreating:
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


class MediaPreviewMenu(QMenu):
    """
    桌布選單的懸停預覽, 在背景驗證並載入對應縮圖
    """

    def __init__(self, title: str, parent=None) -> None:

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


    def mouseMoveEvent(self, event) -> None:

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


    def leaveEvent(self, event) -> None:

        self.leave_preview()
        super().leaveEvent(event)


    def hideEvent(self, event) -> None:

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


        self.media_menu = MediaPreviewMenu(tr("select_video"), menu)
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
        """
        清除最近播放的媒體清單, 只保留目前播放的
        """

        config = config_store.load()
        config["recentVideo"] = config.get("recentVideo", [])[:1]
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

            self.wallpaper.set_play_media(path)
            self.wallpaper.media_play()

        except Exception as error:

            logger.exception("載入桌布時發生錯誤 - %s", path)
            self.tray.showMessage(
                tr("app_name"),
                tr("media_load_failed").format(error=error),
                QSystemTrayIcon.MessageIcon.Information,
                3000
            )
            return

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
            self.wallpaper.is_exiting = True

            self.wallpaper.unregister_power_notification()
            self.wallpaper.player_events.event_detach(vlc.EventType.MediaPlayerPlaying)  # type: ignore

            self.wallpaper.media_stop()
            self.wallpaper.instance_image.close()

            self.wallpaper.instance_vlc_player.release()
            self.wallpaper.instance_vlc.release()

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
