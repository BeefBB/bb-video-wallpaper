"""
以隔離程序驗證 VLC 的非同步操作, 父視窗失效與原生輸出幾何
所有影片與視窗皆為測試專用, 不接入主程式或修改使用者設定
"""

from collections.abc import Callable
import ctypes
import faulthandler
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

from app_loader import load_app_module


ROOT = Path(__file__).resolve().parents[1]
FFMPEG = shutil.which("ffmpeg")


def run_probe(directory: Path, operation: str) -> None:
    """
    在畫面外的獨立父視窗中驗證影片輸出, 逾時時結束探針程序
    """

    sys.path.insert(0, str(ROOT))
    import win32con
    import win32gui
    import win32ui
    from PySide6.QtCore import QEventLoop, QTimer, Qt
    from PySide6.QtGui import QImage, QPainter, QTransform
    from PySide6.QtWidgets import QApplication, QWidget
    import bb_video_display

    module = load_app_module()
    module.initialize_paths()
    assert module.initialize_vlc()
    import vlc

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    display = bb_video_display.Instance()
    errors: list[str] = []
    display.error.connect(errors.append)

    def record_error(kind: type[BaseException], error: BaseException, traceback: TracebackType | None) -> None:

        errors.append(str(error))
        sys.__excepthook__(kind, error, traceback)

    sys.excepthook = record_error
    hosts: list[subprocess.Popen[str]] = []
    targets: list[QWidget] = []
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

        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", str(ROOT / "tests" / "test_video_recovery.py"), "--host"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        hosts.append(process)
        stdout = process.stdout
        assert stdout is not None
        ready: queue.Queue[str] = queue.Queue()
        threading.Thread(target=lambda: ready.put(stdout.readline()), daemon=True).start()
        host = int(ready.get(timeout=6))
        win32gui.SetWindowPos(
            host, 0, 0, 0, 640, 640,
            win32con.SWP_NOMOVE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
        )

        target = QWidget()
        target.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        target.setGeometry(0, 0, 400, 400)
        win32gui.SetParent(int(target.winId()), host)
        target.show()
        assert win32gui.GetAncestor(int(target.winId()), win32con.GA_PARENT) == host, "測試目標未保留外部父視窗"
        targets.append(target)
        return target, process


    def paused_at(position: int) -> bool:

        current = display.get_time()
        player = display._player
        return (
            current is not None and abs(current - position) < 250 and
            player is not None and player.get_state() == getattr(vlc.State, "Paused")
        )


    def capture() -> QImage:

        # 擷取原生背景視窗, 不以繪圖替身代替 Qt 與 Windows 的輸出
        window = display._window
        assert window is not None
        hwnd = window.host_hwnd
        _, _, width, height = win32gui.GetClientRect(hwnd)
        handle = win32gui.GetDC(hwnd)
        dc = win32ui.CreateDCFromHandle(handle)
        buffer = dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(dc, width, height)
        original = buffer.SelectObject(bitmap)

        try:
            assert ctypes.windll.user32.PrintWindow(hwnd, buffer.GetSafeHdc(), 2)
            pixels = bitmap.GetBitmapBits(True)
            return QImage(pixels, width, height, QImage.Format.Format_RGB32).copy()

        finally:
            buffer.SelectObject(original)
            buffer.DeleteDC()
            dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, handle)
            win32gui.DeleteObject(bitmap.GetHandle())


    def corner_pixel() -> tuple[int, int, int]:

        color = capture().pixelColor(5, 5)
        return color.red(), color.green(), color.blue()


    try:
        target, host = create_target()
        display.set_hwnd(int(target.winId()))
        display.set_scaling("fit", "#123456")
        if operation.startswith("lifecycle"):
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
                wait(lambda: capture().pixelColor(200, 200).red() > 180)

            # 輸出再次失效後立即關閉, 延遲回呼不得操作已釋放播放器
            host.terminate()
            host.wait(timeout=5)
            display.close()
            app.processEvents()
            display.close()
            assert display._player is None and display._window is None

        elif operation.startswith("scaling-"):
            rotation = int(operation.removeprefix("scaling-"))
            source = QImage(str(directory / "pattern.png"))
            for sar in (1, 2):
                image = source.scaled(320 * sar, 180).transformed(QTransform().rotate(-rotation))
                display.set_media(directory / f"pattern-{sar}-{rotation}.mp4")
                display.play()
                wait(lambda: display._video_size is not None and (display.get_time() or 0) > 500)
                display.pause()
                player = display._player
                assert player is not None
                wait(lambda: player.get_state() == getattr(vlc.State, "Paused"))

                for width, height in ((300, 500), (500, 300)):
                    win32gui.SetWindowPos(
                        int(target.winId()), 0, 0, 0, width, height,
                        win32con.SWP_NOMOVE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
                    )
                    for mode in ("fit", "stretch", "fill", "fit"):
                        position = display.get_time()
                        display.set_scaling(mode, "#123456")
                        aspect = {
                            "fit": Qt.AspectRatioMode.KeepAspectRatio,
                            "stretch": Qt.AspectRatioMode.IgnoreAspectRatio,
                            "fill": Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        }[mode]
                        scaled = image.scaled(width, height, aspect)
                        expected = QImage(width, height, QImage.Format.Format_RGB32)
                        expected.fill("#123456")
                        painter = QPainter(expected)
                        painter.drawImage((width - scaled.width()) // 2, (height - scaled.height()) // 2, scaled)
                        painter.end()

                        # 多點比對實際畫面, 略過色塊邊界並容許影片壓縮色偏
                        samples = []
                        for x in (5, *range(width // 10, width, width // 10), width - 6):
                            for y in (5, *range(height // 10, height, height // 10), height - 6):
                                color = expected.pixelColor(x, y)
                                if all(expected.pixelColor(x + dx, y + dy) == color
                                       for dx, dy in ((-3, 0), (3, 0), (0, -3), (0, 3))):
                                    samples.append((x, y, color))

                        def matches() -> bool:

                            actual = capture()
                            return actual.size() == expected.size() and all(
                                max(abs(a - b) for a, b in zip(
                                    actual.pixelColor(x, y).getRgb()[:3], color.getRgb()[:3]
                                )) <= 30
                                for x, y, color in samples
                            )

                        try:
                            wait(matches)
                        except AssertionError:
                            raise AssertionError((rotation, sar, width, height, mode)) from None
                        assert display.get_time() == position, "縮放模式切換不應改變暫停進度"

        else:
            for name, dimensions in (("sar.mp4", (640, 180)), ("rotated.mp4", (180, 320))):
                display.set_media(directory / name)
                display.play()
                wait(lambda: display._video_size is not None and (display.get_time() or 0) > 500)
                display.pause()
                player = display._player
                window = display._window
                assert player is not None and window is not None
                wait(lambda: player.get_state() == getattr(vlc.State, "Paused"))  # type: ignore

                for size in ((400, 400), (300, 500)):
                    # Qt 邏輯上仍視為頂層視窗, 原生調整尺寸才不會重套父層的位置偏移
                    win32gui.SetWindowPos(
                        int(target.winId()), 0, 0, 0, *size,
                        win32con.SWP_NOMOVE | win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
                    )

                    def fitted() -> bool:

                        _, _, width, height = win32gui.GetClientRect(int(target.winId()))
                        scale = min(width / dimensions[0], height / dimensions[1])
                        expected = (0, 0, round(dimensions[0] * scale), round(dimensions[1] * scale))
                        return win32gui.GetClientRect(window.video_hwnd) == expected  # type: ignore

                    wait(fitted)
                    wait(lambda: corner_pixel() == (18, 52, 86))

        assert not errors, errors
        print(f"PASS {operation}", flush=True)

    finally:
        display.close()
        for target in targets:
            target.close()
        for process in hosts:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        module._vlc_dll_directory.close()
        faulthandler.cancel_dump_traceback_later()


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
        建立供探針使用的暫存影片, 含像素比例與旋轉標記
        """

        cls.directory = Path(cls.enterClassContext(tempfile.TemporaryDirectory()))

        def ffmpeg(*args: str | Path) -> None:

            if FFMPEG is None:
                raise unittest.SkipTest("需要 ffmpeg")

            subprocess.run(
                [FFMPEG, "-v", "error", *map(str, args)], check=True, capture_output=True,
                timeout=20, creationflags=subprocess.CREATE_NO_WINDOW
            )

        base = cls.directory / "base.mp4"
        ffmpeg(
            "-f", "lavfi", "-i", "color=c=red:s=320x180:r=30", "-t", "8",
            "-c:v", "libx264", "-preset", "ultrafast", base
        )
        ffmpeg(
            "-i", base, "-vf", "setsar=2", "-c:v", "libx264",
            "-preset", "ultrafast", cls.directory / "sar.mp4"
        )
        ffmpeg("-display_rotation:v:0", "90", "-i", base, "-c", "copy", cls.directory / "rotated.mp4")

        pattern = cls.directory / "pattern.mp4"
        ffmpeg(
            "-f", "lavfi", "-i",
            "color=c=red:s=320x180:r=30,"
            "drawbox=x=160:y=0:w=160:h=90:color=lime:t=fill,"
            "drawbox=x=0:y=90:w=160:h=90:color=blue:t=fill,"
            "drawbox=x=160:y=90:w=160:h=90:color=yellow:t=fill,"
            "drawbox=x=80:y=48:w=80:h=48:color=white:t=fill",
            "-t", "8", "-c:v", "libx264", "-preset", "ultrafast", pattern
        )
        ffmpeg("-i", pattern, "-frames:v", "1", cls.directory / "pattern.png")
        for sar in (1, 2):
            anamorphic = cls.directory / f"pattern-{sar}.mp4"
            ffmpeg("-i", pattern, "-vf", f"setsar={sar}", "-c:v", "libx264", "-preset", "ultrafast", anamorphic)
            for rotation in (0, 90, 180, 270):
                ffmpeg("-display_rotation:v:0", str(rotation), "-i", anamorphic, "-c", "copy",
                       cls.directory / f"pattern-{sar}-{rotation}.mp4")


    def probe(self, operation: str) -> None:
        """
        在獨立程序執行指定探針並驗證完成標記
        """

        result = subprocess.run(
            [sys.executable, "-X", "utf8", __file__, "--probe", str(self.directory), operation],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=25, creationflags=subprocess.CREATE_NO_WINDOW
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"PASS {operation}", result.stdout)


    def test_pending_commands_and_lost_parent_preserve_state_without_hanging(self) -> None:
        """
        載入中指令與失效父視窗不造成卡住, 重綁後恢復暫停進度
        """

        for operation in ("lifecycle", "lifecycle-rotated"):
            with self.subTest(operation=operation):
                self.probe(operation)


    def test_native_fit_background_tracks_sar_rotation_and_resize(self) -> None:
        """
        原生背景與影片區域依旋轉, 像素比例及目標尺寸正確更新
        """

        self.probe("geometry")


    def test_rotated_scaling_matches_rendered_pixels_while_paused(self) -> None:
        """
        實際輸出符合旋轉與像素比例, 暫停時切換縮放或尺寸也更新畫面
        """

        for rotation in (90, 270, 180, 0):
            with self.subTest(rotation=rotation):
                self.probe(f"scaling-{rotation}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--probe":
        run_probe(Path(sys.argv[2]), sys.argv[3])
    else:
        unittest.main()
