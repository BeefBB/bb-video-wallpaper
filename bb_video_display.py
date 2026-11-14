"""
Qt 影片顯示模組, 接收主程式提供的 HWND 並共用既有 QApplication

呼叫端先設定 VLC DLL 搜尋位置, 再於 Qt 主執行緒使用:
    import bb_video_display

    display = bb_video_display.Instance()
    display.set_hwnd(int(target_window.winId()))
    display.set_scaling("fit", "#202020")
    display.set_media("video.mp4")
    display.play()

預設為 fill 與黑色背景, 靜音並循環播放
show/hide 只改變可見性, stop 隱藏輸出並停止播放
set_hwnd(0) 解除掛載但保留進度與播放意圖, 重新綁定後恢復
close 釋放媒體及內部資源, 保留 HWND 與顯示選項供重新載入
重掛或結束時可傳入 process_events=True, 在等待停止期間處理 Qt 事件
"""

from collections.abc import Callable
import ctypes
from ctypes import wintypes
import logging
from pathlib import Path
import threading
from typing import TYPE_CHECKING
import weakref

import win32api
import win32con
import win32gui

from PySide6.QtCore import (
    QByteArray, QAbstractNativeEventFilter, QEventLoop, QObject, QRectF, QSizeF,
    Qt, QThread, Signal, Slot,
)
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QRasterWindow, QWindow
from PySide6.QtWidgets import QApplication

from windows_integration import wake_vlc_video_windows

if TYPE_CHECKING:
    import vlc


__all__ = ["Instance"]


logger = logging.getLogger(__name__)
_VLC_VAR_STRING = 0x0040


class _VlcValue(ctypes.Union):
    """VLC 3 在 64 位元 Windows 使用的變數值 union"""

    _fields_ = [("integer", ctypes.c_int64), ("string", ctypes.c_char_p)]


class _TargetEvents(QAbstractNativeEventFilter):
    """
    將目標視窗的尺寸與銷毀訊息轉交 Qt 事件佇列
    """

    def __init__(self, display: "Instance") -> None:

        super().__init__()
        self._display = weakref.ref(display)


    def nativeEventFilter(
        self, event_type: QByteArray | bytes | bytearray | memoryview, message: int
    ) -> tuple[bool, int]:

        display = self._display()
        if display is None or event_type != b"windows_generic_MSG":
            return False, 0

        msg = wintypes.MSG.from_address(int(message))

        if display._hwnd and msg.hWnd == display._hwnd:
            if msg.message in (win32con.WM_SIZE, win32con.WM_WINDOWPOSCHANGED, win32con.WM_DESTROY):
                # 離開原生訊息處理後再停止或重排 VLC, 避免在銷毀回呼內等待播放器
                display._target_event.emit(display._hwnd, msg.message == win32con.WM_DESTROY)

        return False, 0


class _StopThread(QThread):
    """
    重掛時在背景停止 VLC, 讓 Qt 主執行緒繼續處理視窗訊息
    """

    def __init__(self, stop: Callable[[], None], parent: QObject) -> None:

        super().__init__(parent)
        self._stop = stop
        self.error: Exception | None = None


    def run(self) -> None:

        try:
            self._stop()
        except Exception as error:
            self.error = error


class _VideoWindow(QRasterWindow):
    """
    不透明背景與影片子視窗, VLC 停止後才可釋放
    """

    def __init__(self, target_hwnd: int, color: QColor) -> None:

        super().__init__()

        self.target_hwnd = target_hwnd
        self.host_hwnd = 0
        self.video_hwnd = 0
        self.host_window: QWindow | None = None
        self.background_color = QColor(color)

        self.setTitle("Qt video display")
        self.setFlags(
            Qt.WindowType.SubWindow |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowDoesNotAcceptFocus |
            Qt.WindowType.WindowTransparentForInput
        )


    def show(self) -> None:
        """
        第一次顯示時建立不透明容器及影片子視窗
        """

        if not win32gui.IsWindow(self.target_hwnd):
            raise RuntimeError("必須先設定有效的目標視窗")

        try:
            if not self.host_hwnd:
                self.create()
                self.host_hwnd = win32gui.CreateWindowEx(
                    win32con.WS_EX_LAYERED |
                    win32con.WS_EX_TRANSPARENT |
                    win32con.WS_EX_NOACTIVATE |
                    win32con.WS_EX_TOOLWINDOW,
                    "Static",
                    "",
                    win32con.WS_CHILD | win32con.WS_CLIPCHILDREN | win32con.WS_CLIPSIBLINGS,
                    0, 0, 1, 1,
                    self.target_hwnd,
                    None,
                    win32api.GetModuleHandle(None),
                    None
                )
                win32gui.SetLayeredWindowAttributes(self.host_hwnd, 0, 255, win32con.LWA_ALPHA)
                self.host_window = QWindow.fromWinId(self.host_hwnd)

                if self.host_window is None:
                    raise RuntimeError("Qt 無法連接影片容器")

                self.setParent(self.host_window)
                self.setPosition(0, 0)

            super().show()
            hwnd = int(self.winId())
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE) & 0xFFFFFFFF
            win32gui.SetWindowLong(
                hwnd,
                win32con.GWL_STYLE,
                (style & ~win32con.WS_POPUP) | win32con.WS_CHILD |
                win32con.WS_CLIPCHILDREN | win32con.WS_CLIPSIBLINGS
            )

            if not self.video_hwnd:
                self.video_hwnd = win32gui.CreateWindowEx(
                    win32con.WS_EX_TRANSPARENT | win32con.WS_EX_NOACTIVATE,
                    "Static",
                    "",
                    win32con.WS_CHILD | win32con.WS_CLIPCHILDREN | win32con.WS_CLIPSIBLINGS,
                    0, 0, 1, 1,
                    hwnd,
                    None,
                    win32api.GetModuleHandle(None),
                    None
                )

            self.sync_geometry()
            win32gui.ShowWindow(self.host_hwnd, win32con.SW_SHOWNOACTIVATE)

        except Exception:
            self.close()
            raise


    def sync_geometry(self) -> None:
        """
        依目標客戶區與 DPI 更新背景視窗尺寸
        """

        if not win32gui.IsWindow(self.target_hwnd) or not win32gui.IsWindow(self.host_hwnd):
            return

        left, top, right, bottom = win32gui.GetClientRect(self.target_hwnd)
        width, height = right - left, bottom - top
        win32gui.SetWindowPos(
            self.host_hwnd, 0, 0, 0, width, height,
            win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
        )
        ratio = self.devicePixelRatio()
        self.resize(round(width / ratio), round(height / ratio))
        self.update()


    def paintEvent(self, event: QPaintEvent) -> None:

        painter = QPainter(self)
        painter.fillRect(QRectF(0, 0, self.width(), self.height()), self.background_color)


    def hide(self) -> None:

        if self.host_hwnd and win32gui.IsWindow(self.host_hwnd):
            win32gui.ShowWindow(self.host_hwnd, win32con.SW_HIDE)

        super().hide()


    def close(self) -> bool:

        self.hide()
        self.setParent(None)
        self.destroy()
        self.host_window = None

        if self.host_hwnd and win32gui.IsWindow(self.host_hwnd):
            win32gui.DestroyWindow(self.host_hwnd)
        self.host_hwnd = self.video_hwnd = 0
        return True


class Instance(QObject):
    """
    在 Qt 主執行緒使用, 建立物件時不載入 VLC 或建立視窗
    """

    playback_started = Signal()
    error = Signal(str)
    _player_event = Signal(int, str)
    _target_event = Signal(object, bool)

    def __init__(self) -> None:

        app = QApplication.instance()
        if app is None:
            raise RuntimeError("必須先建立 QApplication")

        super().__init__(app)


        self._hwnd = 0
        self._window: _VideoWindow | None = None
        self._target_events: _TargetEvents | None = None

        self._instance: vlc.Instance | None = None
        self._player: vlc.MediaPlayer | None = None
        self._media: vlc.Media | None = None
        self._events: vlc.EventManager | None = None
        self._event_types: list[vlc.EventType] = []

        self._stop_thread: _StopThread | None = None
        self._closing = False
        self._generation = 0

        self._state = "stop"
        self._visible = False
        self._pending_time: int | None = None
        self._video_size: QSizeF | None = None
        self._video_transposed = False
        self._output_transposed = False
        self._set_output: Callable | None = None

        self._scaling_mode = "fill"
        self._background_color = QColor(Qt.GlobalColor.black)
        self.current_media: Path | None = None

        self._player_event.connect(self._on_player_event, Qt.ConnectionType.QueuedConnection)
        self._target_event.connect(self._on_target_event, Qt.ConnectionType.QueuedConnection)
        app.aboutToQuit.connect(self.close)


    def _ensure_player(self) -> None:
        """
        延後到載入媒體時才建立 VLC 資源
        """

        if self._player is not None:
            return

        import vlc

        instance = vlc.Instance("--no-video-title-show", "--input-repeat=65535", "--no-audio")
        if instance is None:
            raise RuntimeError("無法建立 VLC 實例")

        try:
            player = instance.media_player_new()
            if player is None:
                raise RuntimeError("無法建立 VLC 播放器")

        except Exception:
            instance.release()
            raise

        self._instance = instance
        self._player = player
        self._events = player.event_manager()
        player.video_set_key_input(False)
        player.video_set_mouse_input(False)


    def _detach_events(self) -> None:
        """
        解除 VLC 回呼, 讓先前排入 Qt 佇列的通知失效
        """

        self._generation += 1

        if self._events is not None:
            for event_type in self._event_types:
                self._events.event_detach(event_type)

        self._event_types.clear()


    def _stop_player(self, *, process_events: bool = False) -> bool:
        """
        重掛時共用停止工作, 若等待期間被較新的操作取代則回傳 False
        """

        self._detach_events()
        generation = self._generation
        player = self._player

        if player is None:
            return True

        if not process_events and self._stop_thread is None:
            self._stop_sync(player)
            return True

        loop = QEventLoop(self)
        worker = self._stop_thread
        owns_worker = worker is None

        if worker is None:
            worker = _StopThread(lambda: self._stop_sync(player), self)
            self._stop_thread = worker

        worker.finished.connect(loop.quit)
        if owns_worker:
            worker.start()

        if worker.isRunning():
            loop.exec(QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        worker.wait()

        worker.finished.disconnect(loop.quit)
        error = worker.error

        if owns_worker:
            self._stop_thread = None
            worker.deleteLater()

        loop.deleteLater()

        if error is not None:
            raise error

        return generation == self._generation


    def _stop_sync(self, player: "vlc.MediaPlayer") -> None:
        """
        同步停止輸出, 背景喚醒 VLC 視窗並在返回前回收執行緒
        """

        finished = threading.Event()

        def wake() -> None:

            while not finished.wait(0.05):
                try:
                    wake_vlc_video_windows()
                except Exception:
                    logger.exception("喚醒 VLC 原生視窗失敗")

        worker = threading.Thread(target=wake, daemon=True)
        worker.start()

        try:
            player.stop()
        finally:
            finished.set()
            worker.join()


    def set_media(self, path: Path | str) -> None:
        """
        換片保留播放意圖, 清除舊進度; 非同步解碼失敗由 error 通知
        """

        source = Path(path).resolve(strict=True)
        if not source.is_file():
            raise ValueError("影片來源必須是檔案")

        self._ensure_player()
        assert self._instance is not None and self._player is not None
        media = self._instance.media_new(str(source))
        if media is None:
            raise RuntimeError("無法建立 VLC 媒體")

        try:
            if not self._stop_player():
                media.release()
                return
            self._player.set_media(media)
        except Exception:
            media.release()
            raise

        if self._media is not None:
            self._media.release()
        self._media = media
        self.current_media = source
        self._pending_time = None
        self._video_size = None
        self._video_transposed = False
        self._layout()

        if self._state != "stop" and self._hwnd:
            self._start_player()


    def set_hwnd(self, hwnd: int, *, process_events: bool = False) -> None:
        """
        停止舊輸出後換綁目標, 保留進度與播放意圖, 0 表示解除掛載
        """

        hwnd = int(hwnd)
        if hwnd and not win32gui.IsWindow(hwnd):
            raise ValueError("無效的目標視窗控制代碼")

        if hwnd == self._hwnd and (self._window is None or win32gui.IsWindow(self._window.video_hwnd)):
            self._layout()
            return

        position = self._pending_time
        if position is None and self._player is not None and self._state != "stop":
            # 輸出失效時 VLC 可能已離開 Playing/Paused, 仍保留最後可取得的進度
            current = self._player.get_time()
            position = current if current > 0 else None

        # 停止或釋放視窗失敗時, 下次重試仍使用原先的進度
        self._pending_time = position
        needs_stop = self._stop_thread is not None
        if not needs_stop and self._player is not None:
            import vlc
            needs_stop = self._player.get_state() not in (
                getattr(vlc.State, "Stopped"), getattr(vlc.State, "NothingSpecial")
            )

        if needs_stop:
            if not self._stop_player(process_events=process_events):
                return
        else:
            self._detach_events()

        if self._player is not None:
            self._player.set_hwnd(0)
        if self._window is not None:
            self._window.close()
            self._window = None
        self._hwnd = hwnd

        if hwnd and self._state != "stop":
            self._start_player()
        elif hwnd and self._visible and self._media is not None:
            self.show()


    def get_hwnd(self) -> int:

        return self._hwnd


    def set_scaling(self, mode: str, background_color: QColor | str = "#000000") -> None:
        """
        設定 fill, fit 或 stretch 與不透明背景色, 可在載入前設定
        """

        if mode not in ("fill", "fit", "stretch"):
            raise ValueError("無效的縮放模式")

        color = QColor(background_color)
        if not color.isValid() or color.alpha() != 255:
            raise ValueError("背景色必須是有效的不透明顏色")

        self._scaling_mode = mode
        self._background_color = color
        self._layout()


    def show(self) -> None:
        """
        顯示影片與背景容器, 不改變播放狀態
        """

        if self._media is None or self._player is None:
            raise RuntimeError("尚未設定影片")

        if not self._hwnd or not win32gui.IsWindow(self._hwnd):
            raise RuntimeError("必須先設定有效的目標視窗")

        if self._window is not None and not win32gui.IsWindow(self._window.video_hwnd):
            raise RuntimeError("影片輸出已失效, 請重新設定目標視窗")

        self._visible = True

        if self._window is None:
            window = _VideoWindow(self._hwnd, self._background_color)
            window.show()
            self._window = window
            self._player.set_hwnd(window.video_hwnd)

            if self._target_events is None:
                self._target_events = _TargetEvents(self)
                app = QApplication.instance()
                assert app is not None
                app.installNativeEventFilter(self._target_events)

        else:
            self._window.show()

        self._layout()


    def hide(self) -> None:
        """
        隱藏整個輸出, 保留播放狀態與內部資源
        """

        self._visible = False
        if self._window is not None:
            self._window.hide()


    def _start_player(self) -> None:
        """
        綁定這次輸出的回呼, 保留目前可見性並開始載入影片
        """

        import vlc

        visible = self._visible
        self.show()
        if not visible:
            self.hide()

        assert self._player is not None and self._events is not None
        if self._video_transposed or self._output_transposed:
            # VLC 3 綁定 HWND 時會清空 vout, 旋轉影片改用可正確裁切的原生 OpenGL 輸出
            if self._set_output is None:
                set_output = ctypes.CDLL("libvlccore.dll").var_SetChecked
                set_output.argtypes = (ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, _VlcValue)
                set_output.restype = ctypes.c_int
                self._set_output = set_output
            output = b"glwin32" if self._video_transposed else b""
            if self._set_output(self._player, b"vout", _VLC_VAR_STRING, _VlcValue(string=output)) != 0:
                raise RuntimeError("無法設定影片輸出")
        self._output_transposed = self._video_transposed

        self._detach_events()

        for name in ("Playing", "Vout", "SeekableChanged", "ESSelected", "EncounteredError"):
            event_type = getattr(vlc.EventType, f"MediaPlayer{name}")
            self._events.event_attach(
                event_type,
                lambda event, generation=self._generation, kind=name: self._player_event.emit(generation, kind)
            )
            self._event_types.append(event_type)

        if self._player.play() == -1:
            self.stop()
            raise RuntimeError("無法開始播放影片")


    def play(self) -> None:
        """
        顯示輸出並播放, 載入期間以最新的播放意圖為準
        """

        self._request_playback("play")


    def pause(self) -> None:
        """
        顯示輸出並暫停, 尚未就緒時等播放通知後套用
        """

        self._request_playback("pause")


    def _request_playback(self, state: str) -> None:
        """
        等待重掛時只更新播放意圖, 有效目標綁定後再恢復輸出
        """

        if self._media is None:
            raise RuntimeError("尚未設定影片")

        if self._hwnd:
            self.show()
        else:
            self._visible = True

        self._state = state
        if not self._hwnd:
            return

        assert self._player is not None
        if self._is_ready():
            self._player.set_pause(int(state == "pause"))
        elif not self._event_types:
            self._start_player()


    def stop(self, *, process_events: bool = False) -> None:
        """
        隱藏輸出並停止播放, 清除等待中的進度要求
        """

        self._state = "stop"
        self._pending_time = None
        self.hide()
        self._stop_player(process_events=process_events)


    def _is_ready(self) -> bool:

        if self._player is None:
            return False

        import vlc

        return self._player.get_state() in (getattr(vlc.State, "Playing"), getattr(vlc.State, "Paused"))


    def get_time(self) -> int | None:
        """
        停止, 載入中或等待定位時回傳 None
        """

        if (
            self._player is None or self._state == "stop" or
            self._pending_time is not None or not self._is_ready()
        ):
            return None

        position = self._player.get_time()
        return position if position >= 0 else None


    def get_length(self) -> int:

        return max(0, self._player.get_length()) if self._player is not None else 0


    def set_time(self, position: int) -> bool:
        """
        以毫秒設定進度, 尚未就緒時保留要求, 無法定位時回傳 False
        """

        if position < 0 or self._media is None or self._player is None:
            return False

        if self._is_ready() and not self._player.is_seekable():
            return False

        self._pending_time = position
        self._apply_pending()
        return True


    def _apply_pending(self) -> None:

        if self._player is None or not self._is_ready() or self._state == "stop":
            return

        if self._pending_time is not None and self._player.is_seekable():
            self._player.set_time(self._pending_time)
            self._pending_time = None

        if self._state == "pause":
            self._player.set_pause(1)


    @Slot(int, str)
    def _on_player_event(self, generation: int, kind: str) -> None:

        if generation != self._generation or self._state == "stop" or self._player is None:
            return

        if self._hwnd and not win32gui.IsWindow(self._hwnd):
            self.set_hwnd(0)
            return

        if kind == "EncounteredError":
            path = self.current_media
            self.stop()
            self.error.emit(f"無法播放影片: {path}")
            return

        self._read_video_size()
        if self._video_transposed != self._output_transposed:
            if self._pending_time is None:
                position = self._player.get_time()
                self._pending_time = position if position > 0 else None
            if self._stop_player():
                self._start_player()
            return
        self._layout()
        self._apply_pending()

        if kind == "Playing":
            self.playback_started.emit()


    def _read_video_size(self) -> None:
        """
        讀取目前影像軌的顯示比例, 包含像素比例與旋轉方向
        """

        if self._player is None or self._media is None:
            return

        import vlc

        selected = self._player.video_get_track()
        tracks = ctypes.POINTER(vlc.MediaTrack)()
        count = vlc.libvlc_media_tracks_get(self._media, ctypes.byref(tracks))
        track_array = ctypes.cast(tracks, ctypes.POINTER(ctypes.POINTER(vlc.MediaTrack)))

        try:
            for index in range(count):
                track = track_array[index].contents
                if track.id != selected or track.type != getattr(vlc.TrackType, "video") or not track.audio:
                    continue

                # python-vlc 將 union 展開成獨立欄位, video 資料實際位於第一個指標槽位
                video = ctypes.cast(track.audio, ctypes.POINTER(vlc.VideoTrack)).contents
                width, height = video.width, video.height
                if not width or not height:
                    continue

                if video.sar_num and video.sar_den:
                    width *= video.sar_num
                    height *= video.sar_den
                self._video_transposed = video.orientation in (
                    getattr(vlc.VideoOrient, "left_top"), getattr(vlc.VideoOrient, "left_bottom"),
                    getattr(vlc.VideoOrient, "right_top"), getattr(vlc.VideoOrient, "right_bottom")
                )
                if self._video_transposed:
                    width, height = height, width
                self._video_size = QSizeF(width, height)
                break

        finally:
            vlc.libvlc_media_tracks_release(track_array, count)


    def _layout(self) -> None:
        """
        依縮放模式調整影片子視窗, 外圍由 Qt 背景覆蓋
        """

        window = self._window
        if window is None or self._player is None or not win32gui.IsWindow(window.video_hwnd):
            return

        window.sync_geometry()
        window.background_color = QColor(self._background_color)
        window.update()

        _, _, width, height = win32gui.GetClientRect(int(window.winId()))
        if width <= 0 or height <= 0:
            return

        video_width, video_height = width, height
        # 旋轉影片避開 VLC 的原生裁切, 由父視窗裁切放大的輸出
        clip_video = self._scaling_mode == "fill" and self._video_transposed
        if self._video_size is not None and (self._scaling_mode == "fit" or clip_video):
            aspect = Qt.AspectRatioMode.KeepAspectRatioByExpanding if clip_video else Qt.AspectRatioMode.KeepAspectRatio
            size = self._video_size.scaled(QSizeF(width, height), aspect)
            video_width, video_height = max(1, round(size.width())), max(1, round(size.height()))

        win32gui.SetWindowPos(
            window.video_hwnd, 0, (width - video_width) // 2, (height - video_height) // 2,
            video_width, video_height, win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
        )

        self._player.video_set_scale(0)
        # VLC 的拉伸比例使用旋轉前的影像方向
        ratio = f"{height}:{width}" if self._video_transposed else f"{width}:{height}"
        self._player.video_set_crop_geometry(ratio if self._scaling_mode == "fill" and not clip_video else None)
        self._player.video_set_aspect_ratio(ratio if self._scaling_mode == "stretch" else None)

        show_video = self._video_size is not None and self._state != "stop" and self._player.has_vout() > 0
        win32gui.ShowWindow(window.video_hwnd, win32con.SW_SHOWNOACTIVATE if show_video else win32con.SW_HIDE)


    @Slot(object, bool)
    def _on_target_event(self, hwnd: int, destroyed: bool) -> None:

        if hwnd != self._hwnd:
            return

        if destroyed and not win32gui.IsWindow(hwnd):
            self.set_hwnd(0)
        else:
            self._layout()


    @Slot()
    def close(self, *, process_events: bool = False) -> None:
        """
        停止輸出並釋放內部資源, 保留外部 HWND 與顯示選項
        """

        if self._closing:
            return

        self._closing = True
        error: Exception | None = None
        steps: list[Callable[[], object]] = [lambda: self.stop(process_events=process_events)]

        app = QApplication.instance()
        if app is not None and self._target_events is not None:
            events = self._target_events
            steps.append(lambda: app.removeNativeEventFilter(events))

        if self._player is not None:
            player = self._player
            steps.extend((lambda: player.set_hwnd(0), player.release))

        if self._media is not None:
            steps.append(self._media.release)

        if self._instance is not None:
            steps.append(self._instance.release)

        if self._window is not None:
            window = self._window
            steps.append(lambda: window.close())

        try:
            for cleanup in steps:
                try:
                    cleanup()

                except Exception as failure:
                    if error is None:
                        error = failure

                    else:
                        logger.exception("釋放影片資源失敗")

        finally:
            self._target_events = None
            self._player = None
            self._events = None
            self._event_types.clear()
            self._media = None
            self._instance = None
            self._window = None

            self.current_media = None
            self._video_size = None
            self._video_transposed = False
            self._output_transposed = False

            self._closing = False

        if error is not None:
            raise error
