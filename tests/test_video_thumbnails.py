"""
使用實際 VLC-lite 與暫存合成影片測試縮圖比例, 不啟動桌布或修改使用者設定
需要 Windows, 專案內的 VLC-lite 與 PATH 中的 ffmpeg; 缺少時跳過
"""

import ast
import ctypes
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from PySide6.QtGui import QImage


ROOT = Path(__file__).resolve().parents[1]
VLC_DIR = ROOT / "VLC-lite"
FFMPEG = shutil.which("ffmpeg")


@unittest.skipUnless(
    os.name == "nt" and FFMPEG and (VLC_DIR / "libvlc.dll").is_file(),
    "需要 Windows, VLC-lite 與 ffmpeg",
)
class VideoThumbnailTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):

        ffmpeg = FFMPEG

        if ffmpeg is None:
            raise unittest.SkipTest("需要 ffmpeg")

        dll_directory = os.add_dll_directory(str(VLC_DIR))
        cls.addClassCleanup(dll_directory.close)
        environment = mock.patch.dict(os.environ, {
            "PYTHON_VLC_LIB_PATH": str(VLC_DIR / "libvlc.dll"),
            "PYTHON_VLC_MODULE_PATH": str(VLC_DIR),
        })
        environment.start()
        cls.addClassCleanup(environment.stop)
        ctypes.CDLL(str(VLC_DIR / "libvlccore.dll"))
        ctypes.CDLL(str(VLC_DIR / "libvlc.dll"))
        import vlc

        names = {"get_video_thumbnail_size", "create_video_thumbnail"}
        tree = ast.parse((ROOT / "bb-video-wallpaper.py").read_text(encoding="utf-8"))
        nodes: list[ast.stmt] = [
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in names
        ]
        cls.namespace = {
            "ctypes": ctypes,
            "logger": logging.getLogger("video_thumbnail_tests"),
            "Path": Path,
            "QImage": QImage,
            "threading": threading,
            "time": time,
            "vlc": vlc,
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / "bb-video-wallpaper.py"), "exec"), cls.namespace)

        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.media = {}

        for sar in (1, 2):

            path = Path(temporary.name) / f"sar-{sar}.mp4"
            subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
                    "-t", "3", "-vf", f"setsar={sar}/1",
                    "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(path),
                ],
                check=True, capture_output=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            cls.media[sar] = path


    def test_visible_height_excludes_decoder_padding(self):

        image = self.namespace["create_video_thumbnail"](self.media[1], lambda: False)

        self.assertFalse(image.isNull())
        self.assertEqual((image.width(), image.height()), (300, 169))


    def test_non_square_pixels_use_display_aspect_ratio(self):

        image = self.namespace["create_video_thumbnail"](self.media[2], lambda: False)

        self.assertFalse(image.isNull())
        self.assertEqual((image.width(), image.height()), (300, 84))


    def test_cancellation_stops_media_parsing(self):

        media = mock.Mock()
        media.parse_with_options.return_value = 0
        media.get_parsed_status.return_value = 0
        cancelled = mock.Mock(side_effect=[False, True])

        with self.assertRaises(InterruptedError):
            self.namespace["get_video_thumbnail_size"](media, cancelled)

        media.parse_stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
