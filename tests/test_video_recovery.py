"""
以真正 VLC 與獨立的畫面外父視窗程序驗證影片重掛
不重啟 Explorer, 不讀寫使用者設定, 子程序逾時時讓測試明確失敗
"""

from collections.abc import Callable
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
import unittest
from unittest import mock

from app_loader import load_app_module


ROOT = Path(__file__).resolve().parents[1]
VLC_DIR = ROOT / "VLC-lite"
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


def run_recovery_probe(media: Path, operation: str = "reattach") -> None:
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
    hosts: list[subprocess.Popen[str]] = []
    wallpaper = None
    # 探針逾時時輸出執行緒堆疊並退出
    faulthandler.dump_traceback_later(20, exit=True)

    def create_host() -> tuple[int, subprocess.Popen[str]]:
        """
        啟動畫面外父視窗程序, 等待並回傳視窗代碼與程序物件
        """

        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", __file__, "--host"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        hosts.append(process)
        stdout = process.stdout
        assert stdout is not None, "測試父程序未建立標準輸出管線"
        ready: queue.Queue[str] = queue.Queue()
        # 等待父視窗 HWND, 背景讀取避免阻塞
        threading.Thread(target=lambda: ready.put(stdout.readline()), daemon=True).start()
        return int(ready.get(timeout=8)), process

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

    try:
        host_hwnd, host_process = create_host()
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
        wallpaper.check_auto_pause_timer.stop()
        wallpaper.setGeometry(0, 0, 640, 360)
        wallpaper.show()
        player = wallpaper.instance_vlc_player
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
                host_hwnd, host_process = create_host()
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
            elif operation == "image":
                window = wallpaper.instance_image.window
                assert window is not None and window.isVisible(), "換片後圖片沒有顯示"

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
            host_hwnd, host_process = create_host()
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

    finally:
        # 桌布自行停止輸出並釋放資源, 父程序由測試清理
        if wallpaper is not None:
            wallpaper.shutdown()
        for process in hosts:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        dll_directory.close()
        faulthandler.cancel_dump_traceback_later()


@unittest.skipUnless(sys.platform == "win32" and (VLC_DIR / "libvlc.dll").is_file() and FFMPEG is not None, "需要 Windows, VLC-lite 與 ffmpeg")
class VideoRecoveryTests(unittest.TestCase):
    """
    真正的 VLC 輸出失效與重掛恢復
    """

    def test_playing_and_paused_video_recover_after_host_process_ends(self) -> None:
        """
        在獨立程序中驗證播放與暫停影片失去父視窗後恢復輸出及關閉
        """

        if FFMPEG is None:
            self.skipTest("需要 ffmpeg")

        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "recovery.mp4"
            subprocess.run(
                [FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
                 "-t", "20", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(media)],
                check=True, capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW
            )
            result = subprocess.run(
                [sys.executable, "-X", "utf8", __file__, "--probe", str(media)], cwd=ROOT,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, creationflags=subprocess.CREATE_NO_WINDOW
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for stage in ("play", "pause", "shutdown"):
                self.assertIn(f"PASS {stage}:", result.stdout)


    def test_lost_video_output_stops_and_switches_media_without_qt_reentry(self) -> None:
        """
        失效輸出在播放或暫停時可停止, 亦可切換影片與圖片, 期間不執行 Qt 回呼
        """

        from PySide6.QtGui import QImage

        if FFMPEG is None:
            self.skipTest("需要 ffmpeg")

        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "selection.mp4"
            subprocess.run(
                [FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
                 "-t", "20", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(media)],
                check=True, capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW
            )
            image = QImage(320, 180, QImage.Format.Format_RGB32)
            image.fill(0xff336699)
            self.assertTrue(image.save(str(media.with_suffix(".png"))))

            for operation in ("stop", "pause", "video", "image"):
                with self.subTest(operation=operation):
                    result = subprocess.run(
                        [sys.executable, "-X", "utf8", __file__, "--probe", str(media), operation], cwd=ROOT,
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=30, creationflags=subprocess.CREATE_NO_WINDOW
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(f"PASS {operation}:", result.stdout)



if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--host":
        run_host()
    elif len(sys.argv) > 1 and sys.argv[1] == "--probe":
        run_recovery_probe(Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "reattach")
    else:
        unittest.main()
