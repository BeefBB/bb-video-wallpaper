"""
媒體縮圖, 模糊背景與快取工作
匯入時不載入 VLC 或建立資料夾
"""

from collections import deque
from collections.abc import Callable, Iterator
from contextlib import ExitStack
import ctypes
import errno
import hashlib
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Literal
from uuid import UUID

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QImage, QImageReader, QImageWriter


logger = logging.getLogger(__name__)


SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi",}
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp",}
SUPPORTED_MEDIA_EXTENSIONS = (SUPPORTED_VIDEO_EXTENSIONS | SUPPORTED_IMAGE_EXTENSIONS)
ImageKind = Literal["thumbnail_orig", "thumbnail_blur", "background_blur"]
CACHE_NAME = re.compile(
    r"(?P<source>[0-9a-f]{16})-(?P<digest>[0-9a-f]{8})(?:-[1-5](?:-blur)?|-blur)\.png"
)


def cleanup_orphaned_thumbnails(media_dir: Path, thumbnail_dir: Path) -> None:
    """
    清除失去媒體來源的縮圖與空的快取子目錄
    """

    cache_subdirectories = []

    for root, subdirectories, filenames in os.walk(thumbnail_dir):

        directory = Path(root)
        source_directory = media_dir / directory.relative_to(thumbnail_dir)

        try:

            # 略過連結目錄, 避免清理其指向的其他位置
            if directory.resolve() != directory.absolute():
                subdirectories.clear()
                continue

            if directory != thumbnail_dir:
                cache_subdirectories.append(directory)

            with os.scandir(source_directory) as entries:
                source_ids = {
                    hashlib.sha256(entry.name.encode("utf-8")).hexdigest()[:16]
                    for entry in entries
                    if Path(entry.name).suffix.lower() in SUPPORTED_MEDIA_EXTENSIONS and entry.is_file()
                }

        except (FileNotFoundError, NotADirectoryError):
            source_ids = set()

        except OSError:
            logger.warning("讀取縮圖來源目錄失敗 - %s", source_directory, exc_info=True)
            continue

        for name in filenames:

            match = CACHE_NAME.fullmatch(name)
            if match is None or match["source"] in source_ids:
                continue

            path = directory / name

            try:
                path.unlink(missing_ok=True)

            except OSError:
                logger.warning("清除失效縮圖失敗 - %s", path, exc_info=True)

    # 先刪除子目錄, 父目錄變空時才能一併清理
    for directory in reversed(cache_subdirectories):
        try:
            directory.rmdir()
        except OSError as error:
            if error.errno not in (errno.ENOENT, errno.ENOTEMPTY):
                logger.warning("清除空快取目錄失敗 - %s", directory, exc_info=True)


def fast_hash(path: Path) -> str:
    """
    以大小, 修改時間與頭尾各 16 KiB 計算縮圖識別碼
    """

    digest = hashlib.sha256()
    # 將縮圖演算法版本納入識別碼計算
    digest.update(b"thumbnail-v5\0")

    with path.open("rb") as file:

        file_stat = os.fstat(file.fileno())
        size = file_stat.st_size
        digest.update(f"{size}:{file_stat.st_mtime_ns}\0".encode("ascii"))

        if size <= 32 * 1024:
            digest.update(file.read())

        else:

            digest.update(file.read(16 * 1024))
            file.seek(-16 * 1024, os.SEEK_END)
            digest.update(file.read(16 * 1024))


    return digest.hexdigest()[:8]


def _get_video_frame_sizes(media, cancelled: Callable[[], bool]) -> dict[int, tuple[int, int]]:
    """
    依影像軌 ID 記錄原尺寸畫面大小, 使用可見尺寸, 像素比例與方向
    """

    if cancelled():
        raise InterruptedError("縮圖工作已取消")

    import vlc

    # python-vlc 在類別外動態建立列舉成員, 以 getattr 讀取這些執行期屬性
    if media.parse_with_options(getattr(vlc.MediaParseFlag, "local"), 5000) == -1:
        raise RuntimeError("無法解析影片資訊")

    deadline = time.monotonic() + 5

    try:

        while media.get_parsed_status() != getattr(vlc.MediaParsedStatus, "done"):

            if cancelled():
                raise InterruptedError("縮圖工作已取消")

            if time.monotonic() >= deadline:
                raise TimeoutError("解析影片資訊逾時")

            if media.get_parsed_status() in (
                getattr(vlc.MediaParsedStatus, "failed"),
                getattr(vlc.MediaParsedStatus, "skipped"),
                getattr(vlc.MediaParsedStatus, "timeout"),
            ):
                raise RuntimeError("無法解析影片資訊")

            time.sleep(0.05)

    finally:
        media.parse_stop()


    # python-vlc 的 tracks_get 不會釋放原生陣列, 因此直接取得並成對釋放
    tracks = ctypes.POINTER(vlc.MediaTrack)()
    count = vlc.libvlc_media_tracks_get(media, ctypes.byref(tracks))
    track_array = ctypes.cast(tracks, ctypes.POINTER(ctypes.POINTER(vlc.MediaTrack)))
    sizes = {}

    try:

        for index in range(count):

            track = track_array[index].contents

            if track.type != getattr(vlc.TrackType, "video") or not track.audio:
                continue

            # LibVLC 的 audio/video 共用 union; python-vlc 將它們展開成獨立欄位,
            # 因此從第一個槽位 (audio) 轉型, 才能取得正確的 video 指標
            video = ctypes.cast(track.audio, ctypes.POINTER(vlc.VideoTrack)).contents

            if not video.width or not video.height:
                continue

            width, height = video.width, video.height

            if video.sar_num and video.sar_den:
                width = width * video.sar_num / video.sar_den

            if video.orientation in (
                getattr(vlc.VideoOrient, "left_top"),
                getattr(vlc.VideoOrient, "left_bottom"),
                getattr(vlc.VideoOrient, "right_top"),
                getattr(vlc.VideoOrient, "right_bottom"),
            ):
                width, height = height, width

            sizes[track.id] = (max(1, round(width)), max(1, round(height)))

    finally:
        vlc.libvlc_media_tracks_release(track_array, count)

    if not sizes:
        raise RuntimeError("影片沒有可用的尺寸資訊")

    return sizes


def _read_video_frame(path: Path, cancelled: Callable[[], bool]) -> QImage:
    """
    使用獨立的靜音 VLC 播放器, 將第一個解碼畫面複製到記憶體, 不建立視窗
    """

    # 主程式先完成 DLL 設定, 圖片縮圖則不需要載入 VLC
    import vlc

    ready = threading.Event()
    frame = {}
    frame_size = None
    uint_pointer = ctypes.POINTER(ctypes.c_uint)


    # chroma 是可寫入的四位元組指標; 不使用會轉成 Python bytes 的 c_char_p
    @ctypes.CFUNCTYPE(
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        uint_pointer,
        uint_pointer,
        uint_pointer,
        uint_pointer
    )
    def setup(opaque, chroma, width, height, pitches, lines) -> int:

        try:

            # AV1 可能在選定影像軌前就要求緩衝區
            # 回呼內不呼叫 VLC 播放器 API, 避免停止播放時卡住
            if frame_size is not None:
                frame_width, frame_height = frame_size

            else:

                if not width[0] or not height[0]:
                    raise RuntimeError("影片沒有可用的緩衝區尺寸")

                frame_width, frame_height = width[0], height[0]

            pitch = (frame_width * 4 + 31) & ~31
            padded_height = (frame_height + 31) & ~31
            allocation_limit = QImageReader.allocationLimit()
            if allocation_limit > 0 and pitch * padded_height > allocation_limit * 1024 * 1024:
                raise MemoryError("影片畫面超出 Qt 圖片記憶體上限")

            width[0], height[0] = frame_width, frame_height
            pitches[0], lines[0] = pitch, padded_height
            ctypes.memmove(chroma, b"RV32", 4)


            # VLC 要求像素平面對齊 32 bytes, 並保留 buffer 至播放器釋放後
            frame["buffer"] = ctypes.create_string_buffer(pitches[0] * lines[0] + 31)
            frame["address"] = (ctypes.addressof(frame["buffer"]) + 31) & ~31
            frame["width"] = frame_width
            frame["height"] = frame_height
            frame["pitch"] = pitches[0]

            return 1

        except Exception:
            logger.exception("初始化影片縮圖緩衝區失敗 - %s", path)
            ready.set()
            return 0


    @vlc.CallbackDecorators.VideoLockCb
    def lock(opaque, planes) -> None:

        planes[0] = frame["address"]
        return None


    @vlc.CallbackDecorators.VideoUnlockCb
    def unlock(opaque, picture, planes) -> None:

        if not ready.is_set():

            try:

                pixels = ctypes.string_at(frame["address"], frame["pitch"] * frame["height"])
                frame["image"] = QImage(
                    pixels,
                    frame["width"],
                    frame["height"],
                    frame["pitch"],
                    QImage.Format.Format_RGB32
                ).copy()

            except Exception:
                logger.exception("複製影片縮圖畫面失敗 - %s", path)

            finally:
                ready.set()


    instance = vlc.Instance(
        "--no-audio",
        "--no-video-title-show",
        "--no-osd",
        "--avcodec-hw=none",
        "--quiet"
    )

    if instance is None:
        raise RuntimeError("無法建立縮圖播放器")

    player: vlc.MediaPlayer | None = None
    media = None


    try:

        player = instance.media_player_new()

        if player is None:
            raise RuntimeError("無法建立縮圖播放器")

        media = instance.media_new(str(path))
        frame_sizes = _get_video_frame_sizes(media, cancelled)

        if len(frame_sizes) == 1:
            frame_size = next(iter(frame_sizes.values()))

        player.set_media(media)
        player.video_set_callbacks(lock, unlock, None, None)
        player.video_set_format_callbacks(setup, None)

        if player.play() == -1:
            raise RuntimeError("無法開啟影片")

        deadline = time.monotonic() + 10

        while True:

            if cancelled():
                raise InterruptedError("縮圖工作已取消")

            # 在縮圖工作執行緒查詢選軌, 不從 VLC 的原生回呼呼叫播放器 API
            track_id = player.video_get_track()

            if track_id in frame_sizes:
                frame_size = frame_sizes[track_id]

            if ready.is_set():

                image = frame.get("image")

                if image is None or image.isNull():
                    raise RuntimeError("無法取得影片畫面")

                if frame_size is not None:
                    break

            if time.monotonic() >= deadline:
                raise TimeoutError("擷取影片縮圖逾時")

            if player.get_state() in (
                getattr(vlc.State, "Error"),
                getattr(vlc.State, "Ended"),
                getattr(vlc.State, "Stopped"),
            ):
                raise RuntimeError("影片沒有可用的畫面")

            if frame_size is None:
                time.sleep(0.05)

            else:
                ready.wait(0.05)


        if cancelled():
            raise InterruptedError("縮圖工作已取消")

        # 選軌前的緩衝區可能使用暫定比例; 最後以實際影像軌的可見尺寸校正
        return image.scaled(
            *frame_size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation
        )

    finally:

        # 先停止並釋放 VLC, 再讓 Python 回收上方的回呼函式和像素緩衝區
        if player is not None:

            player.stop()
            player.release()

        if media is not None:
            media.release()

        instance.release()


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32), ("debug_callback", ctypes.c_void_p),
        ("suppress_background_thread", ctypes.c_int), ("suppress_external_codecs", ctypes.c_int)
    ]


class _EffectGuid(ctypes.Structure):
    _fields_ = [("data", ctypes.c_uint32 * 4)]


class _BlurParams(ctypes.Structure):
    _fields_ = [("radius", ctypes.c_float), ("expand_edge", ctypes.c_int)]


def _gaussian_blur(image: QImage) -> QImage:
    """
    在原尺寸的預乘 BGRA 像素上套用 Windows GDI+ 高斯模糊
    """

    result = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    if result.isNull():
        raise ValueError("無法取得模糊來源的像素")

    gdip = ctypes.WinDLL("gdiplus")
    gdip.GdiplusShutdown.restype = None
    # GdipCreateEffect 的 GUID 以值傳入, 其餘指標使用 ctypes 的指標物件
    gdip.GdipCreateEffect.argtypes = [_EffectGuid, ctypes.POINTER(ctypes.c_void_p)]
    blur_guid = _EffectGuid.from_buffer_copy(UUID("633c80a4-1843-482b-9ef2-be2834c5fdd4").bytes_le)
    token = ctypes.c_size_t()
    bitmap = ctypes.c_void_p()
    effect = ctypes.c_void_p()
    pixels = ctypes.c_ubyte.from_buffer(memoryview(result.bits()))

    def check(status: int) -> None:
        if status != 0:
            raise RuntimeError(f"GDI+ 高斯模糊失敗 (status={status})")

    with ExitStack() as resources:
        check(gdip.GdiplusStartup(ctypes.byref(token), ctypes.byref(_GdiplusStartupInput(1)), None))
        resources.callback(gdip.GdiplusShutdown, token)

        # PixelFormat32bppPARGB 與 Qt 的預乘 BGRA 共用同一塊記憶體
        check(gdip.GdipCreateBitmapFromScan0(
            result.width(), result.height(), result.bytesPerLine(), 0x000E200B,
            ctypes.byref(pixels), ctypes.byref(bitmap)
        ))
        resources.callback(gdip.GdipDisposeImage, bitmap)
        check(gdip.GdipCreateEffect(blur_guid, ctypes.byref(effect)))
        resources.callback(gdip.GdipDeleteEffect, effect)

        radius = min(255.0, max(result.width(), result.height()) * 0.03)
        params = _BlurParams(radius, False)
        check(gdip.GdipSetEffectParameters(effect, ctypes.byref(params), ctypes.sizeof(params)))
        check(gdip.GdipBitmapApplyEffect(bitmap, effect, None, False, None, None))

    return result


def ensure_thumbnail(
    path: Path,
    cancelled: Callable[[], bool],
    *,
    kind: ImageKind,
    media_dir: Path,
    thumbnail_dir: Path,
    background_dir: Path,
    preview_size: int | None = None
) -> Path:
    """
    驗證指定成品的快取, 缺少時直接從素材取圖並產生
    """

    if kind == "background_blur":
        if preview_size is not None:
            raise ValueError("桌布模糊背景使用原尺寸")
        cache_dir, variant = background_dir, "blur"
    else:
        if (
            kind not in ("thumbnail_orig", "thumbnail_blur") or
            type(preview_size) is not int or not 1 <= preview_size <= 5
        ):
            raise ValueError("縮圖種類或尺寸無效")
        cache_dir = thumbnail_dir
        variant = str(preview_size) if kind == "thumbnail_orig" else f"{preview_size}-blur"

    path = path.resolve(strict=True)
    relative_path = path.relative_to(media_dir.resolve())

    if (
        not path.is_file() or
        path.suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS
    ):
        raise ValueError("不支援的縮圖來源")


    original_stat = path.stat()
    digest = fast_hash(path)
    source_id = hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:16]
    directory = cache_dir / relative_path.parent
    destination = directory / f"{source_id}-{digest}-{variant}.png"

    if cancelled():
        raise InterruptedError("縮圖工作已取消")

    if destination.is_file() and not QImage(str(destination)).isNull():
        return destination

    # 只清理取圖前已存在的快取, 避免刪除其他工作剛產生的新版本
    old_paths = list(directory.glob(f"{source_id}-*"))
    if path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS:

        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        image = reader.read()

    else:
        image = _read_video_frame(path, cancelled)

    if image.isNull():
        raise RuntimeError("無法讀取縮圖來源")
    if cancelled():
        raise InterruptedError("縮圖工作已取消")

    if preview_size is not None:
        width = preview_size * 100
        height = max(1, round(width * image.height() / image.width()))
        image = image.scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
    if kind != "thumbnail_orig":
        image = _gaussian_blur(image)

    _save_cache_png(image, destination, path, original_stat, cancelled)

    # 保留相同素材版本的其他尺寸與其模糊圖
    try:
        for old_path in old_paths:
            match = CACHE_NAME.fullmatch(old_path.name)
            if match is None or not old_path.is_file():
                continue
            if match["digest"] != digest:
                old_path.unlink(missing_ok=True)

    except OSError:
        logger.warning("清除舊快取圖片失敗 - %s", path, exc_info=True)

    return destination


def _save_cache_png(
    image: QImage, destination: Path, source: Path, original_stat: os.stat_result, cancelled: Callable[[], bool]
) -> None:

    if cancelled():
        raise InterruptedError("縮圖工作已取消")


    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        dir=destination.parent,
        prefix=".thumbnail-",
        suffix=".tmp",
        delete=False
    ) as temp:
        temp_path = Path(temp.name)


    try:

        writer = QImageWriter(str(temp_path), b"PNG")

        try:
            if not writer.write(image):
                raise OSError("無法儲存縮圖")

        finally:
            writer.device().close()

        current_stat = source.stat()
        if (
            (original_stat.st_size, original_stat.st_mtime_ns) !=
            (current_stat.st_size, current_stat.st_mtime_ns)
        ):
            raise OSError("產生快取圖片期間來源檔案已變更")
        if cancelled():
            raise InterruptedError("縮圖工作已取消")
        os.replace(temp_path, destination)

    finally:
        temp_path.unlink(missing_ok=True)


    logger.info("已產生快取圖片 - %s", destination)


class ImageCacheClearThread(QThread):
    """
    逐一刪除縮圖與模糊背景圖
    """

    def __init__(self, thumbnail_dir: Path, background_dir: Path) -> None:

        super().__init__()

        self.cache_dirs = (thumbnail_dir, background_dir)

    def iter_png_files(self) -> Iterator[Path]:
        """
        遍歷快取目錄下的 PNG
        """

        for cache_dir in self.cache_dirs:
            for root, subdirectories, filenames in os.walk(cache_dir):

                directory = Path(root)

                try:
                    # 略過連結目錄, 避免清理其指向的其他位置
                    if directory.resolve() != directory.absolute():
                        subdirectories.clear()
                        continue

                except OSError:
                    subdirectories.clear()
                    continue

                for name in filenames:
                    if name.lower().endswith(".png"):
                        yield directory / name

    def run(self) -> None:
        """
        逐一刪除快取目錄下的 PNG
        """

        for path in self.iter_png_files():

            if self.isInterruptionRequested():
                return

            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.debug("清除快取圖片失敗 - %s", path, exc_info=True)


class ThumbnailThread(QThread):
    """
    依序處理縮圖要求, 預覽項目可插隊, 同一路徑, 種類與尺寸不重複加入
    """

    thumbnail_ready = Signal(str, int, QImage)
    blurred_thumbnail_ready = Signal(str, int, QImage)
    background_ready = Signal(str, QImage)


    def __init__(
        self, media_dir: Path, thumbnail_dir: Path, parent=None, *, background_dir: Path | None = None
    ) -> None:

        super().__init__(parent)


        self.media_dir = media_dir
        self.thumbnail_dir = thumbnail_dir
        self.background_dir = background_dir if background_dir is not None else thumbnail_dir.parent / "backgrounds"

        self.queue: deque[tuple[Path, ImageKind, int | None]] = deque()
        self.pending: set[tuple[Path, ImageKind, int | None]] = set()
        self.pending_lock = threading.Condition()
        self.stopping = False


    def request(
        self, path: Path, kind: ImageKind, *other_kinds: ImageKind, preview_size: int | None = None,
        priority: bool = False
    ) -> None:
        """
        按傳入順序排程, 每個種類各自產生和回傳
        """

        kinds = (kind, *other_kinds)

        with self.pending_lock:

            if self.stopping:
                return

            for image_kind in reversed(kinds) if priority else kinds:

                request = (path, image_kind, preview_size)
                if request in self.pending:

                    if not priority:
                        continue

                    try:
                        self.queue.remove(request)

                    except ValueError:
                        # 已取出的工作正在處理, 不重複排入
                        continue

                else:
                    self.pending.add(request)

                if priority:
                    self.queue.appendleft(request)
                else:
                    self.queue.append(request)

            if not self.queue:
                return
            self.pending_lock.notify()

            if not self.isRunning():
                self.start()


    def run(self) -> None:
        """
        逐一產生佇列中的成品, 依種類送出對應訊號
        """

        while not self.isInterruptionRequested():

            with self.pending_lock:

                self.pending_lock.wait_for(lambda: bool(self.queue) or self.stopping)

                if self.stopping or self.isInterruptionRequested():
                    break

                request = self.queue.popleft()

            path, kind, preview_size = request

            try:

                image_path = ensure_thumbnail(
                    path, self.isInterruptionRequested, kind=kind, media_dir=self.media_dir,
                    thumbnail_dir=self.thumbnail_dir, background_dir=self.background_dir, preview_size=preview_size
                )
                image = QImage(str(image_path))
                if image.isNull():
                    raise RuntimeError("無法讀取快取圖片")

            except InterruptedError:
                break

            except Exception:
                logger.exception("產生快取圖片失敗 (%s) - %s", kind, path)
                continue

            finally:
                with self.pending_lock:
                    self.pending.discard(request)

            if not self.isInterruptionRequested():
                if kind == "background_blur":
                    self.background_ready.emit(str(path), image)
                elif preview_size is not None:
                    if kind == "thumbnail_blur":
                        self.blurred_thumbnail_ready.emit(str(path), preview_size, image)
                    else:
                        self.thumbnail_ready.emit(str(path), preview_size, image)


    def stop(self) -> None:

        with self.pending_lock:

            self.stopping = True
            self.requestInterruption()
            self.pending_lock.notify_all()

        self.wait()
