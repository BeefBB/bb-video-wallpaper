"""
測試快取檔案保護, 排程時序, 預覽回傳, 特殊影片解碼與取消
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

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QColor, QImage, QImageWriter


ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from app_loader import QtTestCase
import media_preview
from thumbnails import (
    ThumbnailThread, cleanup_orphaned_thumbnails, create_video_thumbnail,
    ensure_thumbnail, get_video_thumbnail_sizes,
)


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


    def thumbnail(self, path: Path, preview_size: int = 3) -> Path:
        """
        使用測試路徑取得或產生縮圖
        """

        return ensure_thumbnail(
            path,
            lambda: False,
            media_dir=self.media_dir,
            thumbnail_dir=self.thumbnail_dir,
            preview_size=preview_size
        )


    def test_long_names_with_same_content_have_independent_caches(self) -> None:
        """
        驗證相同內容的長檔名使用獨立快取, 清理過期快取時保留無關檔案與目錄
        """

        first = self.media_dir / ("a" * 239 + "1.bmp")
        second = self.media_dir / ("a" * 239 + "2.bmp")
        self.save_image(first, Qt.GlobalColor.red)
        second.write_bytes(first.read_bytes())

        first_thumbnail = self.thumbnail(first)
        second_thumbnail = self.thumbnail(second)
        self.assertNotEqual(first_thumbnail, second_thumbnail)

        for thumbnail in (first_thumbnail, second_thumbnail):
            self.assertEqual(QImage(str(thumbnail)).width(), 300)

        other_sizes = [self.thumbnail(first, size) for size in (1, 5)]
        for thumbnail, width in zip(other_sizes, (100, 500)):
            self.assertEqual(QImage(str(thumbnail)).width(), width)
        self.assertTrue(first_thumbnail.exists())

        # 混入無關檔案與目錄, 確認快取清理不會誤刪
        prefix = first_thumbnail.name.split("-", 1)[0] + "-"
        unrelated = self.thumbnail_dir / "unrelated.jpg"
        malformed = self.thumbnail_dir / f"{prefix}deadbeeg-3.jpg"
        directory = self.thumbnail_dir / f"{prefix}deadbeef-3.jpg"
        directory.mkdir()
        for path in (unrelated, malformed):
            path.write_bytes(b"keep")

        saved_mtime = first_thumbnail.stat().st_mtime_ns
        self.assertEqual(self.thumbnail(first), first_thumbnail)
        self.assertEqual(first_thumbnail.stat().st_mtime_ns, saved_mtime)

        self.save_image(first, Qt.GlobalColor.blue)
        updated_thumbnail = self.thumbnail(first, 4)
        self.assertNotEqual(updated_thumbnail, first_thumbnail)
        self.assertFalse(first_thumbnail.exists())
        for thumbnail in other_sizes:
            self.assertFalse(thumbnail.exists())
        self.assertTrue(second_thumbnail.exists())
        self.assertEqual(unrelated.read_bytes(), b"keep")
        self.assertEqual(malformed.read_bytes(), b"keep")
        self.assertTrue(directory.is_dir())


    def test_save_failure_preserves_cache_and_removes_temp(self) -> None:
        """
        寫入或替換失敗時保留舊快取內容並關閉及清除暫存檔
        """

        source = self.media_dir / "sample.bmp"
        self.save_image(source, Qt.GlobalColor.red)
        previous = self.thumbnail(source)
        previous_content = previous.read_bytes()
        self.save_image(source, Qt.GlobalColor.blue)
        original_write = QImageWriter.write

        def fail_after_write(writer, image):
            self.assertTrue(original_write(writer, image))
            return False

        for stage, patcher, expected_error in (
            ("write", mock.patch.object(QImageWriter, "write", fail_after_write),
             self.assertRaisesRegex(OSError, "無法儲存縮圖")),
            ("replace", mock.patch.object(os, "replace", side_effect=PermissionError),
             self.assertRaises(PermissionError)),
        ):
            with self.subTest(stage=stage):
                with patcher, expected_error:
                    self.thumbnail(source)
                self.assertEqual(previous.read_bytes(), previous_content)
                self.assertEqual(list(self.thumbnail_dir.iterdir()), [previous])


    def test_jpeg_upgrade_preserves_old_cache_until_png_is_saved(self) -> None:
        """
        舊 JPEG 不再使用, PNG 寫入或替換失敗時保留舊快取, 成功後才清除
        """

        source = self.media_dir / "transparent.png"
        image = QImage(32, 18, QImage.Format.Format_ARGB32)
        image.fill(QColor(255, 0, 0, 128))
        image.setPixelColor(0, 0, QColor(0, 0, 255, 0))
        self.assertTrue(image.save(str(source)))

        destination = self.thumbnail(source)
        previous = destination.with_suffix(".jpg")
        self.assertTrue(QImage(str(destination)).save(str(previous), "JPG", 85))
        destination.unlink()
        previous_content = previous.read_bytes()
        original_write = QImageWriter.write

        def fail_after_write(writer, image):
            self.assertTrue(original_write(writer, image))
            return False

        for stage, patcher, expected_error in (
            ("write", mock.patch.object(QImageWriter, "write", fail_after_write),
             self.assertRaisesRegex(OSError, "無法儲存縮圖")),
            ("replace", mock.patch.object(os, "replace", side_effect=PermissionError),
             self.assertRaises(PermissionError)),
        ):
            with self.subTest(stage=stage):
                with patcher, expected_error:
                    self.thumbnail(source)
                self.assertEqual(previous.read_bytes(), previous_content)
                self.assertEqual(list(self.thumbnail_dir.iterdir()), [previous])

        self.assertEqual(self.thumbnail(source), destination)
        self.assertFalse(previous.exists())
        self.assertEqual(QImage(str(destination)).pixelColor(0, 0).alpha(), 0)


    def test_cleanup_preserves_directory_after_partial_scan_failure(self) -> None:
        """
        來源清單讀取途中失敗時, 不以不完整清單刪除縮圖
        """

        source = self.media_dir / "sample.bmp"
        self.save_image(source, Qt.GlobalColor.red)
        kept = self.thumbnail(source)
        orphan = self.thumbnail_dir / ("a" * 16 + "-deadbeef-3.jpg")
        orphan.write_bytes(b"keep")
        original_scandir = os.scandir

        def interrupted_entries(entries):
            yield next(entries)
            raise PermissionError("模擬來源清單讀取途中失敗")

        @contextmanager
        def scan_directory(path):
            with original_scandir(path) as entries:
                yield interrupted_entries(entries) if Path(path) == self.media_dir else entries

        with mock.patch("thumbnails.os.scandir", scan_directory), mock.patch("thumbnails.logger") as log:
            cleanup_orphaned_thumbnails(self.media_dir, self.thumbnail_dir)

        self.assertTrue(kept.is_file())
        self.assertEqual(orphan.read_bytes(), b"keep")
        log.warning.assert_called_once()


    def test_cancellation_stops_media_parsing(self) -> None:
        """
        取消時停止進行中的媒體解析
        """

        media = mock.Mock()
        media.parse_with_options.return_value = 0
        media.get_parsed_status.return_value = 0
        cancelled = mock.Mock(side_effect=[False, True])

        with mock.patch.dict(sys.modules, {"vlc": mock.Mock()}):
            with self.assertRaises(InterruptedError):
                get_video_thumbnail_sizes(media, cancelled)

        media.parse_stop.assert_called_once_with()



class ThumbnailThreadTests(QtTestCase):
    """
    指向項目變更與背景工作交錯時的排程
    """


    def test_latest_preview_runs_next_without_duplicate_work(self) -> None:
        """
        首項處理中連續切換指向, 驗證已排隊項目可提前且同項目不重複處理
        """

        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        thumbnail_path = directory / "thumbnail.jpg"
        image = QImage(32, 18, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.blue)
        self.assertTrue(image.save(str(thumbnail_path)))
        first, ordinary, queued, newest, last = (
            directory / f"{name}.mp4" for name in ("first", "ordinary", "queued", "newest", "last")
        )
        started = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        processed: list[tuple[Path, int]] = []

        def blocked_thumbnail(path, cancelled, **kwargs):
            processed.append((path, kwargs["preview_size"]))

            if path == first:

                started.set()

                if not release.wait(5):
                    raise TimeoutError("測試未放行縮圖工作")

            return thumbnail_path

        self.enterContext(mock.patch("thumbnails.ensure_thumbnail", side_effect=blocked_thumbnail))
        worker = ThumbnailThread(directory, directory / "thumbnails")
        self.addCleanup(worker.deleteLater)
        self.addCleanup(worker.stop)
        self.addCleanup(release.set)
        worker.thumbnail_ready.connect(
            lambda path, size, image: completed.set() if Path(path) == last else None,
            Qt.ConnectionType.DirectConnection
        )
        worker.request(first)
        self.assertTrue(started.wait(5), "首項縮圖工作未開始")
        worker.request(ordinary)
        worker.request(queued)
        worker.request(last)
        worker.request(first)
        worker.request(first, priority=True)
        worker.request(first, preview_size=4, priority=True)
        worker.request(first, preview_size=4)
        worker.request(queued, priority=True)
        worker.request(newest, priority=True)
        worker.request(queued, priority=True)
        worker.request(ordinary)
        release.set()
        self.assertTrue(completed.wait(5), "縮圖佇列未處理完成")
        worker.stop()
        self.assertEqual(processed, [
            (first, 3), (queued, 3), (newest, 3), (first, 4), (ordinary, 3), (last, 3)
        ])



class ThumbnailWorker(QObject):
    """
    由測試控制縮圖結果訊號
    """

    thumbnail_ready = Signal(str, int, QImage)


class MediaPreviewTests(QtTestCase):
    """
    縮圖回傳的有效性與 Qt 執行緒邊界
    """

    def setUp(self) -> None:
        """
        建立隱藏預覽選單與縮圖訊號替身, 預設第一個媒體項目正在預覽
        """

        super().setUp()

        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.media_dir = directory / "media"
        self.thumbnail_dir = directory / "thumbnails"
        self.loading_path = directory / "loading.png"
        self.worker = ThumbnailWorker()
        self.enterContext(mock.patch.object(media_preview, "ThumbnailThread", return_value=self.worker))
        self.menu = media_preview.MediaPreviewMenu(
            "Media", media_dir=self.media_dir, thumbnail_dir=self.thumbnail_dir,
            loading_image_path=self.loading_path
        )
        self.menu.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.menu.preview.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.addCleanup(self.dispose_menu)
        self.first = self.add_media_action("first.mp4")
        self.second = self.add_media_action("second.mp4")
        self.menu.preview_action = self.first
        self.menu.preview_mode = True
        self.menu.preview_pixmap = self.menu.loading_pixmap
        self.display_preview_mock = mock.Mock()
        self.menu.display_preview = self.display_preview_mock


    def dispose_menu(self) -> None:
        """
        停止預覽並釋放選單與縮圖工作物件
        """

        self.menu.reset_preview()
        self.menu.close()
        self.menu.deleteLater()
        self.worker.deleteLater()


    def add_media_action(self, name):
        """
        建立帶有媒體路徑的選單項目
        """

        action = QAction(name, self.menu)
        action.setData(str(self.media_dir / name))
        self.menu.addAction(action)
        return action


    def thumbnail(self):
        """
        建立背景回傳的測試縮圖
        """

        image = QImage(120, 60, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.blue)
        return image


    def test_stale_null_and_inactive_results_do_not_replace_preview(self) -> None:
        """
        過期, 空白與預覽結束後的結果不更新畫面
        """

        self.menu.preview_action = self.second
        loading_key = self.menu.preview_pixmap.cacheKey()

        self.worker.thumbnail_ready.emit(self.first.data(), 3, self.thumbnail())
        self.assertEqual(self.menu.preview_pixmap.cacheKey(), loading_key)

        self.worker.thumbnail_ready.emit(self.second.data(), 3, QImage())
        self.assertEqual(self.menu.preview_pixmap.cacheKey(), loading_key)

        self.menu.reset_preview()
        self.worker.thumbnail_ready.emit(self.second.data(), 3, self.thumbnail())
        self.assertEqual(self.menu.preview_pixmap.cacheKey(), loading_key)
        self.assertTrue(self.menu.preview.isHidden())


    def test_size_change_ignores_queued_result_for_same_media(self) -> None:
        """
        同媒體的舊尺寸結果已排入 Qt 佇列時, 切換尺寸後不覆蓋新結果
        """

        old_image = self.thumbnail()
        old_image.fill(Qt.GlobalColor.red)
        thread = threading.Thread(
            target=lambda: self.worker.thumbnail_ready.emit(self.first.data(), 3, old_image)
        )
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

        self.menu.set_preview_size(4)
        self.assertEqual(self.menu.loading_pixmap.width(), 400)
        self.menu.preview_action = self.first
        self.menu.preview_mode = True
        self.worker.thumbnail_ready.emit(self.first.data(), 4, self.thumbnail())
        self.app.processEvents()

        self.display_preview_mock.assert_called_once_with()
        self.assertEqual(self.menu.preview_pixmap.toImage().pixelColor(0, 0), QColor("blue"))


    def test_background_result_updates_preview_on_qt_main_thread(self) -> None:
        """
        背景縮圖結果由 Qt 主執行緒更新預覽
        """

        displayed_on = []
        display = self.menu.display_preview

        def record_display():
            """
            記錄預覽顯示回呼的執行緒, 再呼叫顯示替身
            """

            displayed_on.append(threading.get_ident())
            display()

        self.menu.display_preview = record_display
        image = self.thumbnail()
        media_path = self.first.data()
        thread = threading.Thread(target=lambda: self.worker.thumbnail_ready.emit(media_path, 3, image))
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        # 尚未處理 Qt 事件佇列
        self.assertEqual(displayed_on, [])

        self.app.processEvents()

        self.assertEqual(displayed_on, [threading.get_ident()])
        self.assertEqual(self.menu.preview_pixmap.toImage().pixelColor(0, 0), QColor("blue"))


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
        cls.anamorphic_media = cls.directory / "sar-2.mp4"
        run_ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30",
            "-t", "3", "-vf", "setsar=2/1",
            "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", cls.anamorphic_media
        )

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


    def test_av1_thumbnails_finish_without_hanging(self) -> None:
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


    def test_default_video_track_determines_size_and_pixels(self) -> None:
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


    def test_av1_cancellation_and_setup_failure_finish(self) -> None:
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


    def test_non_square_pixels_use_display_aspect_ratio(self) -> None:
        """
        非正方形像素依實際顯示比例產生縮圖
        """

        # 涵蓋最小與最大寬度, 以及 112.5 的取整邊界
        for width, height in ((100, 28), (400, 112), (500, 141)):

            with self.subTest(width=width):
                image = create_video_thumbnail(self.anamorphic_media, lambda: False, width)
                self.assertFalse(image.isNull())
                self.assertEqual((image.width(), image.height()), (width, height))


    def test_rotated_videos_preserve_size_and_pixel_orientation(self) -> None:
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
