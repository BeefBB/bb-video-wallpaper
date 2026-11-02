"""
測試快取檔案保護, 縮圖工作釋放與影片解碼的比例, 方向及取消
影片案例需要 Windows, VLC-lite 與 ffmpeg; 缺少時顯示跳過
只在暫存資料夾操作, 不啟動桌布或修改使用者設定
"""

from contextlib import contextmanager, nullcontext
import ctypes
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage


ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from thumbnails import ThumbnailThread, create_video_thumbnail, ensure_thumbnail, get_video_thumbnail_sizes


VLC_DIR = ROOT / "VLC-lite"
FFMPEG = shutil.which("ffmpeg")


def run_ffmpeg(*args, timeout: int = 30) -> subprocess.CompletedProcess:
    """
    執行 FFmpeg 並擷取輸出, 缺少工具時跳過測試
    """

    if FFMPEG is None:
        raise unittest.SkipTest("需要 ffmpeg")

    return subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
        check=True, capture_output=True, timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW
    )



@contextmanager
def video_thumbnail_environment():
    """
    暫設 VLC 的 DLL 與模組搜尋路徑, 替換縮圖日誌並提供 VLC 模組
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
    依指定模式擷取縮圖, 將圖片尺寸與顏色或例外資訊送回管線
    """

    cancelled = threading.Event()
    # 保留 C 回呼參考, 避免被回收
    callback_refs = []

    with video_thumbnail_environment() as vlc:

        original_set_callbacks = vlc.MediaPlayer.video_set_format_callbacks

        def cancel_after_setup(player, setup, cleanup):
            """
            包裝並註冊影像格式設定回呼, 保留其參考以免被回收
            """

            def wrapped(*args):
                """
                執行原影像格式設定回呼後設定取消事件, 並回傳原結果
                """

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



class ThumbnailTests(unittest.TestCase):
    """
    縮圖快取與背景工作
    """

    def setUp(self) -> None:
        """
        建立暫存媒體目錄與縮圖路徑
        """

        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        self.media_dir = Path(self.temp_dir.name) / "media"
        self.media_dir.mkdir()
        self.thumbnail_dir = Path(self.temp_dir.name) / "thumbnails"


    def save_image(self, path: Path, color) -> None:
        """
        建立指定顏色的測試圖片
        """

        image = QImage(32, 18, QImage.Format.Format_RGB32)
        image.fill(color)
        self.assertTrue(image.save(str(path)))


    def thumbnail(self, path: Path) -> Path:
        """
        使用測試路徑取得或產生縮圖
        """

        return ensure_thumbnail(
            path,
            lambda: False,
            media_dir=self.media_dir,
            thumbnail_dir=self.thumbnail_dir
        )


    def test_background_worker_uses_supplied_directories(self):
        """
        背景縮圖工作使用指定的媒體與快取目錄
        """

        source = self.media_dir / "nested" / "sample.bmp"
        source.parent.mkdir()
        self.save_image(source, Qt.GlobalColor.red)


        ready = threading.Event()
        results = []
        worker = ThumbnailThread(self.media_dir, self.thumbnail_dir)

        def receive(media_path, image):
            """
            保存背景回傳的媒體路徑與縮圖, 並通知等待中的測試
            """

            results.append((media_path, image))
            ready.set()


        # 直接接收結果並以 Event 等待
        worker.thumbnail_ready.connect(receive, Qt.ConnectionType.DirectConnection)

        try:
            worker.request(source)
            self.assertTrue(ready.wait(5), "背景縮圖工作未回傳結果")

        finally:
            worker.stop()


        self.assertFalse(worker.isRunning())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], str(source))
        self.assertEqual(results[0][1].width(), 300)
        self.assertEqual(len(list((self.thumbnail_dir / "nested").glob("*.jpg"))), 1)


    def test_long_names_with_same_content_have_independent_caches(self):
        """
        相同內容的長檔名各自使用獨立快取
        """

        first = self.media_dir / ("a" * 239 + "1.bmp")
        second = self.media_dir / ("a" * 239 + "2.bmp")
        self.save_image(first, Qt.GlobalColor.red)
        second.write_bytes(first.read_bytes())

        first_thumbnail = self.thumbnail(first)
        second_thumbnail = self.thumbnail(second)
        self.assertNotEqual(first_thumbnail, second_thumbnail)

        for thumbnail in (first_thumbnail, second_thumbnail):
            self.assertEqual(len(thumbnail.name), 29)
            self.assertTrue(thumbnail.name.isascii())
            self.assertEqual(QImage(str(thumbnail)).width(), 300)

        saved_mtime = first_thumbnail.stat().st_mtime_ns
        self.assertEqual(self.thumbnail(first), first_thumbnail)
        self.assertEqual(first_thumbnail.stat().st_mtime_ns, saved_mtime)

        self.save_image(first, Qt.GlobalColor.blue)
        updated_thumbnail = self.thumbnail(first)
        self.assertNotEqual(updated_thumbnail, first_thumbnail)
        self.assertFalse(first_thumbnail.exists())
        self.assertTrue(second_thumbnail.exists())


    def test_cleanup_preserves_unrelated_files_and_directories(self):
        """
        清理過期快取時保留無關檔案與目錄
        """

        source = self.media_dir / "sample.bmp"
        self.save_image(source, Qt.GlobalColor.red)
        previous = self.thumbnail(source)
        prefix = previous.name.rsplit("-", 1)[0] + "-"
        unrelated = self.thumbnail_dir / "unrelated.jpg"
        malformed = self.thumbnail_dir / f"{prefix}deadbeeg.jpg"
        directory = self.thumbnail_dir / f"{prefix}deadbeef.jpg"
        directory.mkdir()

        for path in (unrelated, malformed):
            path.write_bytes(b"keep")

        self.save_image(source, Qt.GlobalColor.blue)
        self.assertTrue(self.thumbnail(source).is_file())
        self.assertFalse(previous.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(malformed.exists())
        self.assertTrue(directory.is_dir())


    def test_replace_failure_preserves_cache_and_removes_temp(self):
        """
        替換快取失敗時保留舊檔並清除暫存檔
        """

        source = self.media_dir / "sample.bmp"
        self.save_image(source, Qt.GlobalColor.red)
        previous = self.thumbnail(source)
        self.save_image(source, Qt.GlobalColor.blue)

        with mock.patch.object(os, "replace", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                self.thumbnail(source)

        self.assertTrue(previous.is_file())
        self.assertFalse(list(self.thumbnail_dir.glob(".thumbnail-*.tmp")))


@unittest.skipUnless(
    os.name == "nt" and FFMPEG and (VLC_DIR / "libvlc.dll").is_file(),
    "需要 Windows, VLC-lite 與 ffmpeg"
)
class VideoThumbnailTests(unittest.TestCase):
    """
    使用 VLC 與合成影片驗證縮圖
    """

    @classmethod
    def setUpClass(cls):
        """
        設定 VLC 路徑並建立比例與旋轉測試影片
        """

        cls.enterClassContext(video_thumbnail_environment())
        cls.directory = Path(cls.enterClassContext(tempfile.TemporaryDirectory()))
        cls.has_av1_encoder = b"libaom-av1" in run_ffmpeg("-encoders", timeout=10).stdout
        cls.media = {}

        # 建立像素長寬比為 1:1 與 2:1 的影片
        for sar in (1, 2):

            path = cls.directory / f"sar-{sar}.mp4"
            run_ffmpeg(
                "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
                "-t", "3", "-vf", f"setsar={sar}/1",
                "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", path
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
            "-t", "3", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", quadrants
        )
        cls.rotated_media = {}

        for rotation in (90, 180, 270):

            path = cls.directory / f"rotated-{rotation}.mp4"
            run_ffmpeg(
                "-display_rotation:v:0", rotation, "-i", quadrants,
                "-c", "copy", path
            )
            cls.rotated_media[rotation] = path


    def require_av1_encoder(self) -> None:
        """
        缺少 AV1 編碼器時跳過案例
        """

        if not self.has_av1_encoder:
            self.skipTest("目前 FFmpeg 不支援測試使用的 AV1 編碼器")


    def create_av1_video(self, path: Path) -> None:
        """
        建立 AV1 編碼的測試影片
        """

        run_ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
            "-t", "2", "-c:v", "libaom-av1", "-cpu-used", "8", "-threads", "1", path
        )


    def isolated_thumbnail(self, path: Path, mode: str = "render") -> dict:
        """
        在獨立程序中擷取縮圖並檢查逾時
        """

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
        """
        AV1 的 MP4 與 MKV 縮圖皆可正常完成
        """

        self.require_av1_encoder()

        for extension in ("mp4", "mkv"):

            with self.subTest(extension=extension):

                path = self.directory / f"av1.{extension}"
                self.create_av1_video(path)
                result = self.isolated_thumbnail(path)

                self.assertNotIn("error", result)
                self.assertEqual(result["size"], (300, 169))


    def test_default_video_track_determines_size_and_pixels(self):
        """
        依預設影像軌決定縮圖尺寸與內容
        """

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
                            path
                        )
                        result = self.isolated_thumbnail(path)

                        self.assertNotIn("error", result)
                        self.assertEqual(result["size"], (300, height))

                        for actual, expected in zip(result["color"], color):
                            self.assertLessEqual(abs(actual - expected), 30)


    def test_av1_cancellation_and_setup_failure_finish(self):
        """
        AV1 取消或設定失敗後可正常結束
        """

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
        """
        縮圖高度不包含解碼器的補齊範圍
        """

        image = create_video_thumbnail(self.media[1], lambda: False)

        self.assertFalse(image.isNull())
        self.assertEqual((image.width(), image.height()), (300, 169))


    def test_non_square_pixels_use_display_aspect_ratio(self):
        """
        非正方形像素依實際顯示比例產生縮圖
        """

        image = create_video_thumbnail(self.media[2], lambda: False)

        self.assertFalse(image.isNull())
        self.assertEqual((image.width(), image.height()), (300, 84))


    def test_cancellation_stops_media_parsing(self):
        """
        取消時停止進行中的媒體解析
        """

        media = mock.Mock()
        media.parse_with_options.return_value = 0
        media.get_parsed_status.return_value = 0
        cancelled = mock.Mock(side_effect=[False, True])

        with self.assertRaises(InterruptedError):
            get_video_thumbnail_sizes(media, cancelled)

        media.parse_stop.assert_called_once_with()


    def test_rotated_videos_preserve_size_and_pixel_orientation(self):
        """
        旋轉影片的縮圖尺寸與像素方向正確
        """

        red, green, blue, yellow = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)
        # 預期色塊順序: 左上, 右上, 左下, 右下
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

                    # 容許影片壓縮造成的色偏
                    for actual_channel, expected_channel in zip(actual, color):
                        self.assertLessEqual(abs(actual_channel - expected_channel), 30)



if __name__ == "__main__":
    unittest.main()
