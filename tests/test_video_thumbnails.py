"""
使用實際 VLC-lite 與暫存合成影片測試縮圖比例與方向, 不啟動桌布或修改使用者設定
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

        names = {"get_video_thumbnail_sizes", "create_video_thumbnail"}
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

        # 四個象限使用不同顏色, 同時驗證旋轉後的尺寸及實際像素方向
        quadrants = Path(temporary.name) / "quadrants.mp4"
        subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i",
                "color=c=red:s=320x180:r=30,"
                "drawbox=x=160:y=0:w=160:h=90:color=lime:t=fill,"
                "drawbox=x=0:y=90:w=160:h=90:color=blue:t=fill,"
                "drawbox=x=160:y=90:w=160:h=90:color=yellow:t=fill",
                "-t", "3", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(quadrants),
            ],
            check=True, capture_output=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        cls.rotated_media = {}

        for rotation in (90, 180, 270):

            path = Path(temporary.name) / f"rotated-{rotation}.mp4"
            subprocess.run(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-display_rotation:v:0", str(rotation), "-i", str(quadrants),
                    "-c", "copy", str(path),
                ],
                check=True, capture_output=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            cls.rotated_media[rotation] = path


    def test_visible_height_excludes_decoder_padding(self):

        image = self.namespace["create_video_thumbnail"](self.media[1], lambda: False)

        self.assertFalse(image.isNull())
        self.assertEqual((image.width(), image.height()), (300, 169))


    def test_non_square_pixels_use_display_aspect_ratio(self):

        image = self.namespace["create_video_thumbnail"](self.media[2], lambda: False)

        self.assertFalse(image.isNull())
        self.assertEqual((image.width(), image.height()), (300, 84))


    def test_default_video_track_determines_size_and_pixels(self):

        ffmpeg = FFMPEG

        if ffmpeg is None:
            raise unittest.SkipTest("需要 ffmpeg")

        with tempfile.TemporaryDirectory() as directory:

            for selected, height, color in (
                (0, 169, (255, 0, 0)),
                (1, 533, (0, 255, 0)),
            ):

                with self.subTest(default_video_track=selected):

                    path = Path(directory) / f"default-{selected}.mkv"
                    # 音訊放在第一軌, 避免把影像軌 ID 誤當成影像軌索引
                    subprocess.run(
                        [
                            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                            "-f", "lavfi", "-i", "color=red:s=320x180:r=30",
                            "-f", "lavfi", "-i", "color=lime:s=180x320:r=30",
                            "-t", "1", "-map", "0:a", "-map", "1:v", "-map", "2:v",
                            "-c:a", "aac", "-c:v", "libx264", "-threads", "1",
                            "-disposition:v:0", "default" if selected == 0 else "0",
                            "-disposition:v:1", "default" if selected == 1 else "0",
                            str(path),
                        ],
                        check=True, capture_output=True, timeout=30,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    image = self.namespace["create_video_thumbnail"](path, lambda: False)

                    self.assertFalse(image.isNull())
                    self.assertEqual((image.width(), image.height()), (300, height))
                    pixel = image.pixelColor(image.width() // 2, image.height() // 2)

                    for actual, expected in zip((pixel.red(), pixel.green(), pixel.blue()), color):
                        self.assertLessEqual(abs(actual - expected), 30)


    def test_cancellation_stops_media_parsing(self):

        media = mock.Mock()
        media.parse_with_options.return_value = 0
        media.get_parsed_status.return_value = 0
        cancelled = mock.Mock(side_effect=[False, True])

        with self.assertRaises(InterruptedError):
            self.namespace["get_video_thumbnail_sizes"](media, cancelled)

        media.parse_stop.assert_called_once_with()


    def test_rotated_videos_preserve_size_and_pixel_orientation(self):

        red, green, blue, yellow = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)
        expected = {
            90: (533, (green, yellow, red, blue)),
            180: (169, (yellow, blue, green, red)),
            270: (533, (blue, red, yellow, green)),
        }

        for rotation, (height, colors) in expected.items():

            with self.subTest(rotation=rotation):

                image = self.namespace["create_video_thumbnail"](self.rotated_media[rotation], lambda: False)
                self.assertFalse(image.isNull())
                self.assertEqual((image.width(), image.height()), (300, height))

                for (x, y), color in zip(((1, 1), (3, 1), (1, 3), (3, 3)), colors):

                    pixel = image.pixelColor(image.width() * x // 4, image.height() * y // 4)
                    actual = (pixel.red(), pixel.green(), pixel.blue())

                    for actual_channel, expected_channel in zip(actual, color):
                        self.assertLessEqual(abs(actual_channel - expected_channel), 30)


if __name__ == "__main__":
    unittest.main()
