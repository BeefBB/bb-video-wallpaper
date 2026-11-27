"""
測試縮圖排程, 預覽回傳, AV1 解碼與取消, 以及預設影像軌
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

from PySide6.QtCore import QObject, QRect, QSize, Qt, Signal
from PySide6.QtGui import QAction, QColor, QImage, QPainter


ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from app_loader import QtTestCase
import media_preview
from thumbnails import DEFAULT_RAY_DECAY, ImageKind, ThumbnailThread, ensure_thumbnail


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

            source = Path(path)
            with patch, tempfile.TemporaryDirectory(dir=source.parent) as directory:
                cache_dir = Path(directory)
                thumbnail = ensure_thumbnail(
                    source, cancelled.is_set, kind="thumbnail_orig", media_dir=source.parent,
                    thumbnail_dir=cache_dir / "thumbnails", background_dir=cache_dir / "backgrounds", preview_size=3
                )
                image = QImage(str(thumbnail))

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



class ThumbnailCacheTests(QtTestCase):

    def test_missing_results_read_the_video_and_cache_only_the_result(self) -> None:
        """
        驗證缺少的成品各自從影片取圖, 命中成品快取時略過取圖
        """

        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        media_dir = directory / "media"
        path = media_dir / "subdirectory" / "clip.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"video")
        thumbnail_dir = directory / "thumbnails"
        background_dir = directory / "backgrounds"
        cancelled = threading.Event().is_set
        frame = QImage(800, 400, QImage.Format.Format_RGB32)
        frame.fill(Qt.GlobalColor.blue)
        decode = self.enterContext(mock.patch("thumbnails._read_video_frame", return_value=frame))
        blur_sizes = []

        def request_thumbnail(kind: ImageKind, *, preview_size: int | None = None) -> Path:
            """
            用同一份素材與快取目錄請求指定成品
            """

            return ensure_thumbnail(
                path, cancelled, kind=kind, media_dir=media_dir, thumbnail_dir=thumbnail_dir,
                background_dir=background_dir, preview_size=preview_size
            )

        def blur(image: QImage) -> QImage:
            """
            記錄送進模糊處理的尺寸
            """

            blur_sizes.append(image.size().toTuple())
            return image

        self.enterContext(mock.patch("thumbnails._gaussian_blur", side_effect=blur))

        blurred = request_thumbnail("thumbnail_blur", preview_size=3)
        self.assertEqual(list(thumbnail_dir.rglob("*.png")), [blurred])

        clear = request_thumbnail("thumbnail_orig", preview_size=5)
        background = request_thumbnail("background_blur")
        self.assertEqual(decode.call_args_list, [mock.call(path.resolve(), cancelled)] * 3)
        self.assertEqual(blur_sizes, [(300, 150), (800, 400)])
        self.assertEqual(QImage(str(clear)).size().toTuple(), (500, 250))
        self.assertEqual(QImage(str(background)).size().toTuple(), (800, 400))
        self.assertEqual(set(directory.rglob("*.png")), {blurred, clear, background})

        decode.reset_mock()
        cached_blur = request_thumbnail("thumbnail_blur", preview_size=3)
        self.assertEqual(cached_blur, blurred)
        decode.assert_not_called()


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
        clear_ready = threading.Event()
        blur_ready = threading.Event()
        processed: list[tuple[Path, str, int]] = []
        clear_ready_before_blur = []

        def blocked_thumbnail(path, cancelled, **kwargs):
            kind = kwargs["kind"]
            processed.append((path, kind, kwargs["preview_size"]))

            if path == queued and kind == "thumbnail_blur":
                clear_ready_before_blur.append(clear_ready.is_set())

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
        worker.thumbnail_ready.connect(
            lambda path, size, image: clear_ready.set() if Path(path) == queued else None,
            Qt.ConnectionType.DirectConnection
        )
        worker.blurred_thumbnail_ready.connect(
            lambda path, size, image: blur_ready.set() if Path(path) == queued else None,
            Qt.ConnectionType.DirectConnection
        )
        worker.request(first, "thumbnail_orig", preview_size=3)
        self.assertTrue(started.wait(5), "首項縮圖工作未開始")
        worker.request(ordinary, "thumbnail_orig", preview_size=3)
        worker.request(queued, "thumbnail_blur", preview_size=3)
        worker.request(last, "thumbnail_orig", preview_size=3)
        worker.request(first, "thumbnail_orig", preview_size=3)
        worker.request(first, "thumbnail_orig", preview_size=3, priority=True)
        worker.request(first, "thumbnail_orig", preview_size=4, priority=True)
        worker.request(first, "thumbnail_orig", preview_size=4)
        worker.request(queued, "thumbnail_orig", preview_size=3, priority=True)
        worker.request(newest, "thumbnail_orig", preview_size=3, priority=True)
        worker.request(queued, "thumbnail_orig", "thumbnail_blur", preview_size=3, priority=True)
        worker.request(ordinary, "thumbnail_orig", preview_size=3)
        release.set()
        self.assertTrue(completed.wait(5), "縮圖佇列未處理完成")
        worker.stop()
        self.assertTrue(blur_ready.is_set())
        self.assertEqual(clear_ready_before_blur, [True])
        self.assertEqual(processed, [
            (first, "thumbnail_orig", 3), (queued, "thumbnail_orig", 3), (queued, "thumbnail_blur", 3),
            (newest, "thumbnail_orig", 3), (first, "thumbnail_orig", 4), (ordinary, "thumbnail_orig", 3),
            (last, "thumbnail_orig", 3)
        ])



class ThumbnailWorker(QObject):
    """
    由測試控制縮圖結果訊號
    """

    thumbnail_ready = Signal(str, int, QImage)
    blurred_thumbnail_ready = Signal(str, int, QImage)
    ray_thumbnail_ready = Signal(str, int, QSize, str, int, QImage)

    def __init__(self) -> None:
        super().__init__()
        self.request = mock.Mock()


class MediaPreviewTests(QtTestCase):
    """
    縮圖回傳的有效性與 Qt 執行緒邊界
    """

    def setUp(self) -> None:
        """
        建立隱藏預覽選單與縮圖訊號替身
        """

        super().setUp()

        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.worker = ThumbnailWorker()
        self.enterContext(mock.patch.object(media_preview, "ThumbnailThread", return_value=self.worker))
        self.menu = media_preview.MediaPreviewMenu(
            "Media", media_dir=directory / "media", thumbnail_dir=directory / "thumbnails",
            loading_image_path=directory / "loading.png"
        )
        self.menu.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.menu.preview.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.addCleanup(self.dispose_menu)
        self.action = QAction("sample.mp4", self.menu)
        self.action.setData(str(directory / "media" / "sample.mp4"))
        self.menu.addAction(self.action)


    def dispose_menu(self) -> None:
        """
        停止預覽並釋放選單與縮圖工作物件
        """

        self.menu.reset_preview()
        self.menu.close()
        self.menu.deleteLater()
        self.worker.deleteLater()


    def queue_thumbnail(self, size: int, color: Qt.GlobalColor, *, blurred: bool = False) -> None:
        """
        從背景執行緒送出縮圖, 讓呼叫端控制何時處理 Qt 佇列
        """

        image = QImage(120, 60, QImage.Format.Format_RGB32)
        image.fill(color)
        path = self.action.data()
        signal = self.worker.blurred_thumbnail_ready if blurred else self.worker.thumbnail_ready
        thread = threading.Thread(target=signal.emit, args=(path, size, image))
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())


    def test_queued_preview_respects_size_changes_and_updates_on_main_thread(self) -> None:
        """
        忽略尺寸切換或停用前的佇列結果, 新結果由 Qt 主執行緒套用
        """

        displayed_on = []
        self.menu.display_preview = lambda: displayed_on.append(threading.get_ident())

        for size in (4, 0):
            with self.subTest(size=size):
                displayed_on.clear()
                self.menu.set_preview_size(3)
                self.menu.preview_action = self.action
                self.menu.preview_mode = True
                self.menu.preview_timer.start()
                self.queue_thumbnail(3, Qt.GlobalColor.red)
                self.queue_thumbnail(3, Qt.GlobalColor.yellow, blurred=True)
                self.assertEqual(displayed_on, [])

                self.menu.set_preview_size(size)
                if size:
                    self.menu.preview_action = self.action
                    self.menu.preview_mode = True
                else:
                    self.assertFalse(self.menu.preview_mode)
                    self.assertTrue(self.menu.preview.isHidden())
                self.assertFalse(self.menu.preview_timer.isActive())
                self.app.processEvents()
                self.assertEqual(displayed_on, [])
                self.assertIsNone(self.menu.blurred_thumbnail)

                if not size:
                    self.menu.set_preview_size(3)
                    self.menu.preview_action = self.action
                    self.menu.preview_mode = True
                self.queue_thumbnail(self.menu.preview_size, Qt.GlobalColor.blue)
                self.assertEqual(displayed_on, [])
                self.app.processEvents()

                self.assertEqual(displayed_on, [threading.get_ident()])
                self.assertEqual(self.menu.preview_pixmap.toImage().pixelColor(0, 0), QColor("blue"))


    def test_hovered_blur_uses_target_orientation_and_ignores_late_results(self) -> None:
        """
        直向預覽以模糊小圖置中裁切鋪底, 切換項目或關閉後忽略先前排隊的結果
        """

        path = Path(self.action.data())
        other = self.menu.addAction("other.png")
        other.setData(str(path.with_name("other.png")))
        screen = mock.Mock()
        screen.geometry.return_value = QRect(0, 0, 200, 400)
        self.menu.target_screen = screen
        self.menu.display_config = {
            "scalingMode": {path.name: {"portrait": "fit", "landscape": "fill"}},
            "backgroundColor": {path.name: "blur"}
        }
        self.enterContext(mock.patch.object(self.menu, "actionAt", side_effect=lambda pos: self.menu.preview_action))
        self.menu.show()
        self.menu.preview_action = self.action
        self.menu.show_preview()
        self.worker.request.assert_called_with(path, "thumbnail_orig", "thumbnail_blur", preview_size=3, priority=True)

        self.queue_thumbnail(3, Qt.GlobalColor.red)
        self.app.processEvents()
        self.assertEqual(self.menu.preview.pixmap().toImage().pixelColor(5, 5), QColor("black"))

        background = QImage(120, 60, QImage.Format.Format_RGB32)
        background.fill(Qt.GlobalColor.yellow)
        painter = QPainter(background)
        painter.fillRect(45, 0, 30, 60, Qt.GlobalColor.blue)
        painter.end()
        self.worker.blurred_thumbnail_ready.emit(str(path), 3, background)
        result = self.menu.preview.pixmap().toImage()
        self.assertEqual(result.pixelColor(5, 5), QColor("blue"))
        self.assertEqual(result.pixelColor(result.width() // 2, result.height() // 2), QColor("red"))

        self.queue_thumbnail(3, Qt.GlobalColor.yellow, blurred=True)
        self.menu.preview_action = other
        self.menu.show_preview()
        self.app.processEvents()
        self.assertIsNone(self.menu.blurred_thumbnail)
        self.worker.request.assert_called_with(Path(other.data()), "thumbnail_orig", preview_size=3, priority=True)

        screen.geometry.return_value = QRect(0, 0, 400, 200)
        self.menu.preview_action = self.action
        self.menu.show_preview()
        self.worker.request.assert_called_with(path, "thumbnail_orig", preview_size=3, priority=True)
        self.queue_thumbnail(3, Qt.GlobalColor.yellow, blurred=True)
        self.menu.hide()
        self.app.processEvents()
        self.assertIsNone(self.menu.blurred_thumbnail)


    def test_ray_preview_ignores_delayed_modes_and_resolutions(self) -> None:
        """
        滑鼠停在同一張圖時, 只顯示目前光暈模式與目標螢幕的合成預覽
        """

        path = Path(self.action.data()).with_name("sample.PNG")
        self.action.setData(str(path))
        screen = mock.Mock()
        screen.geometry.return_value = QRect(0, 0, 200, 400)
        self.enterContext(mock.patch.object(media_preview, "screen_pixel_size", return_value=QSize(300, 600)))
        self.menu.target_screen = screen
        self.menu.display_config = {
            "scalingMode": {path.name: {"portrait": "fit"}},
            "backgroundColor": {path.name: "ray1"}
        }
        self.enterContext(mock.patch.object(self.menu, "actionAt", return_value=self.action))
        self.menu.show()
        self.menu.preview_action = self.action
        self.menu.show_preview()
        self.worker.request.assert_called_with(
            path, "thumbnail_ray", preview_size=3, screen_size=(300, 600), ray_mode="ray1", priority=True
        )

        image = QImage(150, 300, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.blue)
        self.menu.display_config["backgroundColor"][path.name] = "ray2"
        emit = self.worker.ray_thumbnail_ready.emit
        emit(str(path), 3, QSize(300, 600), "ray1", DEFAULT_RAY_DECAY, image)
        emit(str(path), 3, QSize(200, 400), "ray2", DEFAULT_RAY_DECAY, image)
        self.assertIsNone(self.menu.ray_thumbnail)

        emit(str(path), 3, QSize(300, 600), "ray2", DEFAULT_RAY_DECAY, image)
        self.queue_thumbnail(3, Qt.GlobalColor.red)
        self.app.processEvents()
        result = self.menu.preview.pixmap().toImage()
        self.assertEqual(result.pixelColor(5, 5), QColor("blue"))
        self.assertEqual(result.pixelColor(result.width() // 2, result.height() // 2), QColor("blue"))


@unittest.skipUnless(
    os.name == "nt" and FFMPEG and (VLC_DIR / "libvlc.dll").is_file(),
    "需要 Windows, VLC-lite 與 ffmpeg"
)
class VideoThumbnailTests(unittest.TestCase):
    """
    使用 VLC 與合成影片驗證縮圖
    """

    @classmethod
    def setUpClass(cls) -> None:
        """
        建立暫存目錄並檢查 FFmpeg 是否可編碼 AV1
        """

        cls.directory = Path(cls.enterClassContext(tempfile.TemporaryDirectory()))
        cls.has_av1_encoder = b"libaom-av1" in run_ffmpeg("-encoders", timeout=10).stdout


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


    def test_av1_thumbnails_finish_on_success_cancellation_and_setup_failure(self) -> None:
        """
        AV1 縮圖在產圖, 取消與緩衝區配置失敗時皆能結束
        """

        self.require_av1_encoder()

        for extension in ("mp4", "mkv"):

            with self.subTest(extension=extension):

                path = self.directory / f"av1.{extension}"
                self.create_av1_video(path)
                result = self.isolated_thumbnail(path)

                self.assertNotIn("error", result)
                self.assertEqual(result["size"], (300, 169))

        path = self.directory / "av1.mp4"
        for mode, error in (("cancel", "InterruptedError"), ("setup_failure", "RuntimeError")):
            with self.subTest(mode=mode):
                result = self.isolated_thumbnail(path, mode)
                self.assertEqual(result.get("error"), error)
                self.assertEqual(result["cancelled"], mode == "cancel")


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


if __name__ == "__main__":
    unittest.main()
