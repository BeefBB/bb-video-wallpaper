"""
以隔離程序驗證 VLC 的非同步操作, 桌布重掛與原生輸出恢復
使用測試專用影片及父視窗, 不重啟 Explorer 或修改使用者設定
"""

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
import faulthandler
import logging
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import TracebackType
import unittest
from unittest import mock

from app_loader import load_app_module


ROOT = Path(__file__).resolve().parents[1]
FFMPEG = shutil.which("ffmpeg")


def run_host() -> None:
    """
    在獨立程序中建立影片輸出的父視窗
    """

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QWidget

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    window = QWidget()
    window.setGeometry(-10000, -10000, 640, 360)
    window.show()
    print(int(window.winId()), flush=True)
    # 即使測試主程序卡住或退出, 父視窗程序也會自行結束
    QTimer.singleShot(25000, app.quit)
    app.exec()


@contextmanager
def open_host() -> Iterator[tuple[int, subprocess.Popen[str]]]:
    """
    啟動畫面外的父視窗, 等待 HWND 並在探針結束時回收程序
    """

    with subprocess.Popen(
        [sys.executable, "-X", "utf8", __file__, "--host"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        creationflags=subprocess.CREATE_NO_WINDOW
    ) as process:
        try:
            stdout = process.stdout
            assert stdout is not None
            ready: queue.Queue[str] = queue.Queue()
            threading.Thread(target=lambda: ready.put(stdout.readline()), daemon=True).start()
            line = ready.get(timeout=8).strip()
            assert line.isdecimal(), f"父視窗未回傳 HWND: {line!r}"
            yield int(line), process
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


def run_display_probe(directory: Path, operation: str) -> None:
    """
    在畫面外的獨立父視窗中驗證影片輸出, 逾時時結束探針程序
    """

    sys.path.insert(0, str(ROOT))
    import win32con
    import win32gui
    from PySide6.QtCore import QEventLoop, QTimer, Qt
    from PySide6.QtWidgets import QApplication, QWidget
    import bb_video_display

    module = load_app_module()
    module.initialize_paths()
    assert module.initialize_vlc()
    import vlc

    paused_state: vlc.State = getattr(vlc.State, "Paused")

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    display = bb_video_display.Instance()
    errors: list[str] = []
    display.error.connect(errors.append)

    def record_error(kind: type[BaseException], error: BaseException, traceback: TracebackType | None) -> None:

        errors.append(str(error))
        sys.__excepthook__(kind, error, traceback)

    sys.excepthook = record_error
    hosts = ExitStack()
    targets = ExitStack()
    faulthandler.dump_traceback_later(20, exit=True)


    def wait(predicate: Callable[[], bool]) -> None:

        deadline = time.monotonic() + 6
        while not predicate():
            assert not errors, errors
            assert time.monotonic() < deadline, "等待影片輸出逾時"
            loop = QEventLoop()
            QTimer.singleShot(50, loop.quit)
            loop.exec()


    def create_target() -> tuple[QWidget, subprocess.Popen[str]]:

        host, process = hosts.enter_context(open_host())
        win32gui.SetWindowPos(
            host, 0, 0, 0, 640, 640,
            win32con.SWP_NOMOVE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
        )

        target = QWidget()
        targets.callback(target.close)
        target.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        target.setGeometry(0, 0, 400, 400)
        win32gui.SetParent(int(target.winId()), host)
        target.show()
        assert win32gui.GetAncestor(int(target.winId()), win32con.GA_PARENT) == host, "測試目標未保留外部父視窗"
        return target, process


    def paused_at(position: int) -> bool:

        current = display.get_time()
        player = display._player
        return (
            current is not None and abs(current - position) < 250 and
            player is not None and player.get_state() == paused_state
        )


    with ExitStack() as cleanup:
        cleanup.callback(faulthandler.cancel_dump_traceback_later)
        cleanup.callback(module._vlc_dll_directory.close)
        cleanup.callback(hosts.close)
        cleanup.callback(targets.close)
        cleanup.callback(display.close)

        target, host = create_target()
        display.set_hwnd(int(target.winId()))
        display.set_scaling("fit", "#123456")

        media = directory / ("rotated.mp4" if operation == "lifecycle-rotated" else "base.mp4")
        display.set_media(media)
        display.play()
        display.set_time(3000)
        display.pause()
        wait(lambda: paused_at(3000))

        # 不處理事件佇列就再次換片與停止, 舊 Playing 通知不能重新啟動輸出
        display.set_media(media)
        display.set_time(1000)
        display.stop()
        app.processEvents()
        assert display.get_time() is None and display._state == "stop"
        assert display._window is not None and not display._window.isVisible()
        display.pause()
        display.set_time(3000)
        wait(lambda: paused_at(3000))

        old_hwnd = int(target.winId())
        host.terminate()
        host.wait(timeout=5)
        wait(lambda: not win32gui.IsWindow(old_hwnd))
        # 外部父程序退出不保證送出 WM_DESTROY, 呼叫端依桌面通知解除掛載
        display.set_hwnd(0)
        target, host = create_target()
        display.set_hwnd(int(target.winId()))
        wait(lambda: paused_at(3000))
        display.play()
        wait(lambda: (display.get_time() or 0) > 3300)

        if operation == "lifecycle-rotated":
            display.set_media(directory / "base.mp4")
            wait(lambda: (display.get_time() or 0) > 500)

        assert not errors, errors
        print(f"PASS {operation}", flush=True)


def run_recovery_probe(media: Path, operation: str) -> None:
    """
    驗證真正 VLC 在父視窗失效後恢復, 停止或切換媒體, 並可正常關閉
    """

    import win32con
    import win32gui
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    sys.path.insert(0, str(ROOT))

    module = load_app_module()
    module.initialize_paths()
    assert module.initialize_vlc(), "主程式的 VLC 初始化失敗"
    dll_directory = module._vlc_dll_directory
    import vlc

    # python-vlc 動態建立列舉成員, 以名稱查取並註明型別
    playing_state: vlc.State = getattr(vlc.State, "Playing")
    paused_state: vlc.State = getattr(vlc.State, "Paused")

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    logging.basicConfig(level=logging.INFO)
    hosts = ExitStack()
    # 探針逾時時輸出執行緒堆疊並退出
    faulthandler.dump_traceback_later(20, exit=True)

    def wait_until(predicate: Callable[[], bool]) -> None:
        """
        持續處理 Qt 事件直到指定條件成立, 超過六秒則回報逾時
        """

        deadline = time.monotonic() + 6
        while not predicate():
            if time.monotonic() >= deadline:
                raise AssertionError("等待 VLC 輸出或重掛完成逾時")
            # 使用事件迴圈釋放 GIL, 讓 VLC 的 Python 回呼與 Qt 訊號都能執行
            loop = QEventLoop()
            QTimer.singleShot(50, loop.quit)
            loop.exec()

    with ExitStack() as cleanup:
        cleanup.callback(faulthandler.cancel_dump_traceback_later)
        cleanup.callback(dll_directory.close)
        cleanup.callback(hosts.close)

        host_hwnd, host_process = hosts.enter_context(open_host())
        config = mock.Mock()
        config.load.return_value = {}
        namespace = module.__dict__
        namespace.update({
            "config_store": config, "get_media": lambda: [media], "MEDIA_DIR": media.parent,
            "SUPPORTED_IMAGE_EXTENSIONS": {".png"}, "SUPPORTED_VIDEO_EXTENSIONS": {".mp4"},
            "check_desktop_ready": lambda: True,
            "set_windows_as_wallpaper": lambda hwnd: win32gui.SetParent(hwnd, host_hwnd),
            "register_power_notification": lambda hwnd: 1,
            "unregister_power_notification": lambda handle: None,
            "get_display_state": lambda message: None, "is_on_battery": lambda: False,
            "is_any_fullscreen": lambda hwnd: False, "logger": logging.getLogger("video-recovery-test"),
            "tr": lambda key: key,
        })
        wallpaper = namespace["Wallpaper"]()
        cleanup.callback(wallpaper.shutdown)
        wallpaper.check_auto_pause_timer.stop()
        wallpaper.setGeometry(0, 0, 640, 360)
        wallpaper.show()
        player = wallpaper.instance_video._player
        assert player is not None, "影片模組沒有建立播放器"
        wait_until(lambda: player.has_vout() > 0 and player.get_time() > 1000)
        # 重建途中不顯示尚未掛入測試容器的頂層視窗
        wallpaper.show = lambda: None

        if operation != "reattach":
            if operation == "pause":
                wallpaper.manual_pause()
                wait_until(lambda: player.get_state() == paused_state)

            old_hwnd = int(wallpaper.winId())
            host_process.terminate()
            host_process.wait(timeout=5)
            wait_until(lambda: not win32gui.IsWindow(old_hwnd))
            wallpaper.reattach_timer.stop()

            if operation in ("video", "image"):
                host_hwnd, host_process = hosts.enter_context(open_host())
                # 恢復 Qt 目標但保留舊 VLC 輸出, 讓換片仍需停止失效的輸出
                wallpaper.is_recreating = True
                try:
                    wallpaper.unregister_power_notification()
                    wallpaper.instance_image.set_hwnd(0)
                    wallpaper.hide()
                    wallpaper.destroy(True, True)
                    wallpaper.create()
                    wallpaper.setGeometry(0, 0, 640, 360)
                    wallpaper.attach_to_desktop()
                finally:
                    wallpaper.is_recreating = False

            delivered = []
            QTimer.singleShot(0, lambda: delivered.append(True))
            if operation in ("stop", "pause"):
                wallpaper.manual_stop()
                assert wallpaper.play_state == "stop" and player.get_state() == getattr(vlc.State, "Stopped")
            else:
                selected = media if operation == "video" else media.with_suffix(".png")
                wallpaper.select_media(selected)
                assert wallpaper.current_media == selected and wallpaper.play_state == "play"
                assert config.remember_wallpaper.call_args.args[1] == selected
            assert not delivered, "一般停止或換片期間不可處理排隊的 Qt 操作"

            if operation == "video":
                wait_until(lambda: player.has_vout() > 0 and player.get_state() == playing_state)
            elif operation in ("image", "image-before-reattach"):
                window = wallpaper.instance_image.window
                assert window is not None
                if operation == "image-before-reattach":
                    assert not window.isVisible()
                    host_hwnd, host_process = hosts.enter_context(open_host())
                    wallpaper.reattach_to_desktop()
                    assert win32gui.GetAncestor(int(wallpaper.winId()), win32con.GA_PARENT) == host_hwnd
                assert window.isVisible(), "換圖後圖片沒有顯示"

            print(f"PASS {operation}: lost output, synchronous command", flush=True)
            return

        # 分別驗證播放與暫停狀態
        for state, vlc_state in (("play", playing_state), ("pause", paused_state)):
            if state == "pause":
                wallpaper.manual_pause()
                wait_until(lambda: player.get_state() == vlc_state)
            saved_time = player.get_time()
            old_hwnd = int(wallpaper.winId())
            assert win32gui.GetAncestor(old_hwnd, win32con.GA_PARENT) == host_hwnd, "影片未掛入測試父視窗"
            # 結束父視窗程序, 讓影片輸出失效
            host_process.terminate()
            host_process.wait(timeout=5)
            wait_until(lambda: not win32gui.IsWindow(old_hwnd))
            host_hwnd, host_process = hosts.enter_context(open_host())
            listener = wallpaper.desktop_events
            win32gui.SendMessage(int(listener.winId()), listener.taskbar_created_message, 0, 0)
            app.processEvents()
            assert wallpaper.reattach_timer.isActive(), "通知沒有排定重掛"
            wait_until(lambda: not wallpaper.is_recreating and not wallpaper.reattach_timer.isActive())
            wait_until(lambda: player.has_vout() > 0 and player.get_state() == vlc_state)
            wait_until(lambda: player.get_time() >= saved_time - 250)
            assert win32gui.GetAncestor(int(wallpaper.winId()), win32con.GA_PARENT) == host_hwnd
            assert wallpaper.play_state == state
            assert player.get_time() >= saved_time - 250, "播放進度未恢復"
            print(f"PASS {state}: vout={player.has_vout()}, time={player.get_time()}", flush=True)

        # 父視窗失效後關閉桌布
        old_hwnd = int(wallpaper.winId())
        host_process.terminate()
        host_process.wait(timeout=5)
        wait_until(lambda: not win32gui.IsWindow(old_hwnd))
        with mock.patch.object(namespace["logger"], "exception") as cleanup_error:
            app.aboutToQuit.connect(wallpaper.shutdown)
            QTimer.singleShot(0, app.quit)
            app.exec()
            wallpaper.shutdown()
            cleanup_error.assert_not_called()
        assert wallpaper.is_exiting and wallpaper.desktop_events is None
        assert not wallpaper.reattach_timer.isActive()
        assert not wallpaper.check_auto_pause_timer.isActive()
        assert not wallpaper.fullscreen_detection_timer.isActive()
        print("PASS shutdown: lost host, repeated cleanup", flush=True)


@unittest.skipUnless(
    sys.platform == "win32" and FFMPEG and (ROOT / "VLC-lite" / "libvlc.dll").is_file(),
    "需要 Windows, VLC-lite 與 ffmpeg"
)
class VideoDisplayTests(unittest.TestCase):
    """
    真正 VLC 的非同步生命週期與原生輸出
    """

    directory: Path

    @classmethod
    def setUpClass(cls) -> None:
        """
        建立重掛與顯示探針所需的暫存影片
        """

        cls.directory = Path(cls.enterClassContext(tempfile.TemporaryDirectory()))

        def ffmpeg(*args: str | Path) -> None:

            if FFMPEG is None:
                raise unittest.SkipTest("需要 ffmpeg")

            subprocess.run(
                [FFMPEG, "-v", "error", *map(str, args)], check=True, capture_output=True,
                timeout=20, creationflags=subprocess.CREATE_NO_WINDOW
            )

        cls.recovery_media = cls.directory / "recovery.mp4"
        ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-t", "20",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", cls.recovery_media
        )
        base = cls.directory / "base.mp4"
        ffmpeg(
            "-f", "lavfi", "-i", "color=c=red:s=320x180:r=30", "-t", "8",
            "-c:v", "libx264", "-preset", "ultrafast", base
        )
        ffmpeg("-display_rotation:v:0", "90", "-i", base, "-c", "copy", cls.directory / "rotated.mp4")


    def run_probe(self, probe: str, operation: str) -> str:
        """
        在獨立程序執行探針, 驗證結束狀態並回傳輸出
        """

        result = subprocess.run(
            [sys.executable, "-X", "utf8", __file__, probe, str(self.directory), operation],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, creationflags=subprocess.CREATE_NO_WINDOW
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout


    def test_pending_commands_and_lost_parent_preserve_state_without_hanging(self) -> None:
        """
        載入中指令與失效父視窗不造成卡住, 重綁後恢復暫停進度
        """

        for operation in ("lifecycle", "lifecycle-rotated"):
            with self.subTest(operation=operation):
                self.assertIn(f"PASS {operation}", self.run_probe("--display", operation))


    def test_playing_and_paused_video_recover_after_host_process_ends(self) -> None:
        """
        在獨立程序中驗證播放與暫停影片失去父視窗後恢復輸出及關閉
        """

        output = self.run_probe("--recovery", "reattach")
        for stage in ("play", "pause", "shutdown"):
            self.assertIn(f"PASS {stage}:", output)


    def test_lost_video_output_stops_and_switches_media_without_qt_reentry(self) -> None:
        """
        失效輸出可停止或換片, 重掛前選擇的圖片會保存並在重掛後顯示
        """

        from PySide6.QtGui import QImage

        image = QImage(320, 180, QImage.Format.Format_RGB32)
        image.fill(0xff336699)
        self.assertTrue(image.save(str(self.recovery_media.with_suffix(".png"))))

        for operation in ("stop", "pause", "video", "image", "image-before-reattach"):
            with self.subTest(operation=operation):
                self.assertIn(f"PASS {operation}:", self.run_probe("--recovery", operation))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--host":
        run_host()
    elif len(sys.argv) > 1 and sys.argv[1] == "--display":
        run_display_probe(Path(sys.argv[2]), sys.argv[3])
    elif len(sys.argv) > 1 and sys.argv[1] == "--recovery":
        run_recovery_probe(Path(sys.argv[2]) / "recovery.mp4", sys.argv[3])
    else:
        unittest.main()
