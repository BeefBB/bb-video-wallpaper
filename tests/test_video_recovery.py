"""
以真正 VLC 與獨立的畫面外父視窗程序驗證影片重掛
不重啟 Explorer, 不讀寫使用者設定, 子程序逾時時讓測試明確失敗
"""

import ast
from collections.abc import Callable
import ctypes
from ctypes import wintypes
import faulthandler
import logging
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
VLC_DIR = ROOT / "VLC-lite"
FFMPEG = shutil.which("ffmpeg")


def run_host() -> None:

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


def run_recovery_probe(media: Path) -> None:

    import win32con
    import win32gui
    from PySide6.QtCore import QEventLoop, Qt, QThread, QTimer, Signal, Slot
    from PySide6.QtWidgets import QApplication, QWidget

    sys.path.insert(0, str(ROOT))
    import bb_image_display
    from windows_integration import register_taskbar_created_notification, wake_vlc_video_windows

    dll_directory = os.add_dll_directory(str(VLC_DIR))
    os.environ["PYTHON_VLC_LIB_PATH"] = str(VLC_DIR / "libvlc.dll")
    os.environ["PYTHON_VLC_MODULE_PATH"] = str(VLC_DIR)
    ctypes.CDLL(str(VLC_DIR / "libvlccore.dll"))
    ctypes.CDLL(str(VLC_DIR / "libvlc.dll"))
    import vlc

    # python-vlc 動態建立列舉成員, 以名稱查取並註明型別
    playing_state: vlc.State = getattr(vlc.State, "Playing")
    paused_state: vlc.State = getattr(vlc.State, "Paused")
    playing_event: vlc.EventType = getattr(vlc.EventType, "MediaPlayerPlaying")

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    logging.basicConfig(level=logging.INFO)
    hosts: list[subprocess.Popen[str]] = []
    wallpaper = None
    faulthandler.dump_traceback_later(20, exit=True)

    def create_host() -> tuple[int, subprocess.Popen[str]]:
        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", __file__, "--host"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        hosts.append(process)
        stdout = process.stdout
        assert stdout is not None, "測試父程序未建立標準輸出管線"
        ready: queue.Queue[str] = queue.Queue()
        threading.Thread(target=lambda: ready.put(stdout.readline()), daemon=True).start()
        return int(ready.get(timeout=8)), process

    def wait_until(predicate: Callable[[], bool]) -> None:
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
        source_path = ROOT / "bb-video-wallpaper.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        nodes = ast.Module(
            body=[node for node in tree.body if isinstance(node, ast.ClassDef) and node.name in {"VlcStopThread", "DesktopEventWindow", "Wallpaper"}],
            type_ignores=[],
        )
        # exec 使用的命名空間混合類別, 模組與測試替身
        namespace: dict[str, Any] = {
            "QWidget": QWidget, "QApplication": QApplication, "Qt": Qt, "QTimer": QTimer,
            "QEventLoop": QEventLoop, "QThread": QThread,
            "Signal": Signal, "Slot": Slot, "Path": Path, "wintypes": wintypes,
            "WM_DESTROY": win32con.WM_DESTROY, "vlc": vlc, "bb_image_display": bb_image_display,
            "config_store": config, "get_media": lambda: [media], "MEDIA_DIR": media.parent,
            "SUPPORTED_IMAGE_EXTENSIONS": {".png"}, "SUPPORTED_VIDEO_EXTENSIONS": {".mp4"},
            "register_taskbar_created_notification": register_taskbar_created_notification,
            "wake_vlc_video_windows": wake_vlc_video_windows,
            "check_desktop_ready": lambda: True,
            "set_windows_as_wallpaper": lambda hwnd: win32gui.SetParent(hwnd, host_hwnd),
            "register_power_notification": lambda hwnd: 1,
            "unregister_power_notification": lambda handle: None,
            "get_display_state": lambda message: None, "is_on_battery": lambda: False,
            "is_any_fullscreen": lambda hwnd: False, "logger": logging.getLogger("video-recovery-test"),
        }
        exec(compile(nodes, str(source_path), "exec"), namespace)
        wallpaper = namespace["Wallpaper"]()
        wallpaper.check_auto_pause_timer.stop()
        wallpaper.setGeometry(0, 0, 640, 360)
        wallpaper.show()
        player = wallpaper.instance_vlc_player
        wait_until(lambda: player.has_vout() > 0 and player.get_time() > 1000)
        # 重建途中不顯示尚未掛入測試容器的頂層視窗
        wallpaper.show = lambda: None

        for state, vlc_state in (("play", playing_state), ("pause", paused_state)):
            if state == "pause":
                wallpaper.media_pause()
                wait_until(lambda: player.get_state() == vlc_state)
            saved_time = player.get_time()
            old_hwnd = int(wallpaper.winId())
            assert win32gui.GetAncestor(old_hwnd, win32con.GA_PARENT) == host_hwnd, "影片未掛入測試父視窗"
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

    finally:
        # 先停止仍綁定於有效測試 HWND 的播放器, 再清理原生視窗與父程序
        if wallpaper is not None:
            wallpaper.is_exiting = True
            wallpaper.reattach_timer.stop()
            wallpaper.player_events.event_detach(playing_event)
            wallpaper.instance_vlc_player.stop()
            wallpaper.instance_image.close()
            wallpaper.desktop_events.destroy()
            wallpaper.destroy()
            wallpaper.instance_vlc_player.release()
            wallpaper.instance_vlc.release()
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

    def test_playing_and_paused_video_recover_after_host_process_ends(self):

        if FFMPEG is None:
            self.skipTest("需要 ffmpeg")

        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "recovery.mp4"
            subprocess.run(
                [FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
                 "-t", "20", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(media)],
                check=True, capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            result = subprocess.run(
                [sys.executable, "-X", "utf8", __file__, "--probe", str(media)], cwd=ROOT,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS play:", result.stdout)
            self.assertIn("PASS pause:", result.stdout)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--host":
        run_host()
    elif len(sys.argv) > 1 and sys.argv[1] == "--probe":
        run_recovery_probe(Path(sys.argv[2]))
    else:
        unittest.main()
