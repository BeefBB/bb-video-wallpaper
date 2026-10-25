"""
使用實際 VLC-lite 與暫存合成影片測試縮圖比例, 方向, 影像軌選擇及錯誤處理
不啟動桌布或修改使用者設定
需要 Windows, 專案內的 VLC-lite 與 PATH 中的 ffmpeg; 缺少時跳過
"""

from contextlib import contextmanager, nullcontext
import ctypes
import multiprocessing
import os
import sys
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]

# 直接執行測試檔時, 將專案根目錄加入模組搜尋路徑
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from thumbnails import create_video_thumbnail, get_video_thumbnail_sizes


VLC_DIR = ROOT / "VLC-lite"
FFMPEG = shutil.which("ffmpeg")


def run_ffmpeg(*args, timeout: int = 30) -> subprocess.CompletedProcess:
    """以共用選項執行 FFmpeg, 保留輸出供編碼器檢查使用"""

    if FFMPEG is None:
        raise unittest.SkipTest("需要 ffmpeg")

    return subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
        check=True, capture_output=True, timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


@contextmanager
def video_thumbnail_environment():
    """
    設定測試用 VLC 路徑, 直接測試縮圖模組
    """

    with os.add_dll_directory(str(VLC_DIR)), mock.patch.dict(os.environ, {
        "PYTHON_VLC_LIB_PATH": str(VLC_DIR / "libvlc.dll"),
        "PYTHON_VLC_MODULE_PATH": str(VLC_DIR),
    }), mock.patch("thumbnails.logger"):

        ctypes.CDLL(str(VLC_DIR / "libvlccore.dll"))
        ctypes.CDLL(str(VLC_DIR / "libvlc.dll"))
        import vlc

        yield vlc


def thumbnail_probe(path: str, mode: str, connection) -> None:
    """
    在獨立程序中擷取縮圖, 檢查取消或錯誤後 VLC 是否能正常停止
    """

    cancelled = threading.Event()
    callback_refs = []

    with video_thumbnail_environment() as vlc:

        original_set_callbacks = vlc.MediaPlayer.video_set_format_callbacks

        def cancel_after_setup(player, setup, cleanup):

            def wrapped(*args):

                result = setup(*args)
                cancelled.set()
                return result

            callback = type(setup)(wrapped)
            callback_refs.append(callback)
            return original_set_callbacks(player, callback, cleanup)

        if mode == "cancel":

            patch = mock.patch.object(
                vlc.MediaPlayer,
                "video_set_format_callbacks",
                cancel_after_setup
            )

        elif mode == "setup_failure":

            patch = mock.patch.object(
                ctypes,
                "create_string_buffer",
                side_effect=MemoryError("模擬緩衝區配置失敗")
            )

        else:
            patch = nullcontext()

        try:

            with patch:
                image = create_video_thumbnail(Path(path), cancelled.is_set)

            color = image.pixelColor(image.width() // 2, image.height() // 2)
            connection.send({
                "size": (image.width(), image.height()),
                "color": (color.red(), color.green(), color.blue()),
            })

        except Exception as error:

            connection.send({
                "error": type(error).__name__,
                "cancelled": cancelled.is_set(),
            })

        finally:
            connection.close()


@unittest.skipUnless(
    os.name == "nt" and FFMPEG and (VLC_DIR / "libvlc.dll").is_file(),
    "需要 Windows, VLC-lite 與 ffmpeg",
)
class VideoThumbnailTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):

        cls.enterClassContext(video_thumbnail_environment())
        cls.directory = Path(cls.enterClassContext(tempfile.TemporaryDirectory()))
        cls.has_av1_encoder = b"libaom-av1" in run_ffmpeg("-encoders", timeout=10).stdout
        cls.media = {}

        for sar in (1, 2):

            path = cls.directory / f"sar-{sar}.mp4"
            run_ffmpeg(
                "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
                "-t", "3", "-vf", f"setsar={sar}/1",
                "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", path,
            )
            cls.media[sar] = path

        # 四個象限使用不同顏色, 同時驗證旋轉後的尺寸及實際像素方向
        quadrants = cls.directory / "quadrants.mp4"
        run_ffmpeg(
            "-f", "lavfi", "-i",
            "color=c=red:s=320x180:r=30,"
            "drawbox=x=160:y=0:w=160:h=90:color=lime:t=fill,"
            "drawbox=x=0:y=90:w=160:h=90:color=blue:t=fill,"
            "drawbox=x=160:y=90:w=160:h=90:color=yellow:t=fill",
            "-t", "3", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", quadrants,
        )
        cls.rotated_media = {}

        for rotation in (90, 180, 270):

            path = cls.directory / f"rotated-{rotation}.mp4"
            run_ffmpeg(
                "-display_rotation:v:0", rotation, "-i", quadrants,
                "-c", "copy", path,
            )
            cls.rotated_media[rotation] = path


    def require_av1_encoder(self) -> None:

        if not self.has_av1_encoder:
            self.skipTest("目前 FFmpeg 不支援測試使用的 AV1 編碼器")


    def create_av1_video(self, path: Path) -> None:

        run_ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
            "-t", "2", "-c:v", "libaom-av1", "-cpu-used", "8", "-threads", "1", path,
        )


    def isolated_thumbnail(self, path: Path, mode: str = "render") -> dict:

        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=thumbnail_probe, args=(str(path), mode, sender))
        process.start()
        sender.close()

        try:

            process.join(15)
            self.assertFalse(
                process.is_alive(),
                f"縮圖工作未在 15 秒內結束: {path.name}, {mode}"
            )
            self.assertEqual(process.exitcode, 0)
            self.assertTrue(receiver.poll(), "縮圖子程序未回傳結果")
            return receiver.recv()

        finally:

            if process.is_alive():
                process.terminate()
                process.join()

            receiver.close()
            process.close()


    def test_av1_thumbnails_finish_without_hanging(self):

        self.require_av1_encoder()

        for extension in ("mp4", "mkv"):

            with self.subTest(extension=extension):

                path = self.directory / f"av1.{extension}"
                self.create_av1_video(path)
                result = self.isolated_thumbnail(path)

                self.assertNotIn("error", result)
                self.assertEqual(result["size"], (300, 169))


    def test_default_video_track_determines_size_and_pixels(self):

        for codec in ("libx264", "libaom-av1"):

            with self.subTest(codec=codec):

                if codec == "libaom-av1":
                    self.require_av1_encoder()

                encoder_options = ("-cpu-used", "8") if codec == "libaom-av1" else ()

                for selected, height, color in (
                    (0, 169, (255, 0, 0)),
                    (1, 533, (0, 255, 0)),
                ):

                    with self.subTest(default_video_track=selected):

                        path = self.directory / f"default-{codec}-{selected}.mkv"
                        # 音訊放在第一軌, 避免把影像軌 ID 誤當成影像軌索引
                        run_ffmpeg(
                            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
                            "-f", "lavfi", "-i", "color=red:s=320x180:r=30",
                            "-f", "lavfi", "-i", "color=lime:s=180x320:r=30",
                            "-t", "2", "-map", "0:a", "-map", "1:v", "-map", "2:v",
                            "-c:a", "aac", "-c:v", codec, *encoder_options, "-threads", "1",
                            "-disposition:v:0", "default" if selected == 0 else "0",
                            "-disposition:v:1", "default" if selected == 1 else "0",
                            path,
                        )
                        result = self.isolated_thumbnail(path)

                        self.assertNotIn("error", result)
                        self.assertEqual(result["size"], (300, height))

                        for actual, expected in zip(result["color"], color):
                            self.assertLessEqual(abs(actual - expected), 30)


    def test_av1_cancellation_and_setup_failure_finish(self):

        self.require_av1_encoder()

        path = self.directory / "av1-errors.mp4"
        self.create_av1_video(path)

        for mode, error in (
            ("cancel", "InterruptedError"),
            ("setup_failure", "RuntimeError"),
        ):

            with self.subTest(mode=mode):

                result = self.isolated_thumbnail(path, mode)

                self.assertEqual(result.get("error"), error)
                self.assertEqual(result["cancelled"], mode == "cancel")


    def test_visible_height_excludes_decoder_padding(self):

        image = create_video_thumbnail(self.media[1], lambda: False)

        self.assertFalse(image.isNull())
        self.assertEqual((image.width(), image.height()), (300, 169))


    def test_non_square_pixels_use_display_aspect_ratio(self):

        image = create_video_thumbnail(self.media[2], lambda: False)

        self.assertFalse(image.isNull())
        self.assertEqual((image.width(), image.height()), (300, 84))


    def test_cancellation_stops_media_parsing(self):

        media = mock.Mock()
        media.parse_with_options.return_value = 0
        media.get_parsed_status.return_value = 0
        cancelled = mock.Mock(side_effect=[False, True])

        with self.assertRaises(InterruptedError):
            get_video_thumbnail_sizes(media, cancelled)

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

                image = create_video_thumbnail(self.rotated_media[rotation], lambda: False)
                self.assertFalse(image.isNull())
                self.assertEqual((image.width(), image.height()), (300, height))

                for (x, y), color in zip(((1, 1), (3, 1), (1, 3), (3, 3)), colors):

                    pixel = image.pixelColor(image.width() * x // 4, image.height() * y // 4)
                    actual = (pixel.red(), pixel.green(), pixel.blue())

                    for actual_channel, expected_channel in zip(actual, color):
                        self.assertLessEqual(abs(actual_channel - expected_channel), 30)


if __name__ == "__main__":
    unittest.main()
