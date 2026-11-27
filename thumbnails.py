"""
媒體縮圖, 桌布背景與快取工作
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

from PySide6.QtCore import QRect, QRectF, QSize, Qt, QThread, Signal
from PySide6.QtGui import QColor, QImage, QImageReader, QImageWriter, QPainter, QRegion

from display_painting import fit_image_rect


logger = logging.getLogger(__name__)


SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi",}
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp",}
SUPPORTED_MEDIA_EXTENSIONS = (SUPPORTED_VIDEO_EXTENSIONS | SUPPORTED_IMAGE_EXTENSIONS)
ImageKind = Literal["thumbnail_orig", "thumbnail_blur", "background_blur", "thumbnail_ray", "background_ray"]
RayMode = Literal["ray1", "ray2"]
ThumbnailRequest = tuple[Path, ImageKind, int | None, tuple[int, int] | None, RayMode, int]
DEFAULT_RAY_MODE: RayMode = "ray2"
DEFAULT_RAY_DECAY = 35
CACHE_NAME = re.compile(
    r"(?P<source>[0-9a-f]{16})-(?P<digest>[0-9a-f]{8})"
    r"(?:-[1-5](?:-blur)?|-blur|(?:-[1-5])?-(?:"
    r"ray[12]-(?:100|[1-9]?[0-9])-[1-9][0-9]*-[1-9][0-9]*"
    r"|ray-[1-9][0-9]*-[1-9][0-9]*(?:-d(?:100|[1-9]?[0-9]))?))\.png"
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


def _opaque_on_black(image: QImage) -> QImage:
    """
    將含透明度的圖片合成到不透明黑底
    """

    if not image.hasAlphaChannel():
        return image

    opaque = QImage(image.size(), QImage.Format.Format_RGB32)
    if opaque.isNull():
        raise RuntimeError("無法配置黑底合成圖")
    opaque.fill(Qt.GlobalColor.black)
    with QPainter(opaque) as painter:
        painter.drawImage(0, 0, image)

    return opaque


def _draw_ray_strip(
    painter: QPainter, target: QRect, band: QImage, *,
    sideways: bool, leading: bool, inner_distance: float, ray_mode: RayMode, ray_decay: int
) -> None:
    """
    依指定的方向將原圖邊緣延伸到留白, 並往外柔化與衰減
    """

    # 清晰和柔化的取樣共用黑底, 過渡時只混合顏色
    band = _opaque_on_black(band)
    transform = Qt.TransformationMode.SmoothTransformation
    ignore_aspect = Qt.AspectRatioMode.IgnoreAspectRatio

    if sideways:
        edge = band.copy(0 if leading else band.width() - 1, 0, 1, band.height())
        soft = band.scaled(1, min(8, band.height()), ignore_aspect, transform)
        depth = target.width()
        steps = min(128, depth)
        layer_size = QSize(steps, target.height())

    else:
        edge = band.copy(0, 0 if leading else band.height() - 1, band.width(), 1)
        soft = band.scaled(min(8, band.width()), 1, ignore_aspect, transform)
        depth = target.height()
        steps = min(128, depth)
        layer_size = QSize(target.width(), steps)

    soft = soft.scaled(edge.size(), ignore_aspect, transform)

    layer = QImage(layer_size, QImage.Format.Format_RGB32)
    if layer.isNull():
        raise RuntimeError("無法配置光暈圖層")
    layer.fill(Qt.GlobalColor.black)

    with QPainter(layer) as overlay:

        overlay.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        for index in range(steps):

            progress = index / (steps - 1)

            sample_ratio = 1.0
            if ray_mode == "ray2":
                # 越往外, 射線與原圖邊緣的交點越靠近邊緣中段
                sample_ratio = inner_distance / (inner_distance + progress * (depth - 1))

            position = steps - index - 1 if leading else index

            if sideways:
                height = edge.height() * sample_ratio
                sample = QRectF(0, (edge.height() - height) / 2, 1, height)
                strip = QRectF(position, 0, 1, layer.height())

            else:
                width = edge.width() * sample_ratio
                sample = QRectF((edge.width() - width) / 2, 0, width, 1)
                strip = QRectF(0, position, layer.width(), 1)

            softness = 1 - max(0.0, 1 - 4 * progress) ** 3
            overlay.setOpacity(1.0)
            if softness < 1:
                overlay.drawImage(strip, edge, sample)
            overlay.setOpacity(softness)
            overlay.drawImage(strip, soft, sample)

            overlay.setOpacity(1.0)
            opacity = round(255 * ray_decay / 100 * progress ** 2)
            overlay.fillRect(strip, QColor(0, 0, 0, opacity))

    painter.drawImage(target, layer)


def _render_ray(
    image: QImage, size: QSize, ray_mode: RayMode, ray_decay: int, *, include_foreground: bool
) -> QImage:
    """
    依指定尺寸繪製光暈, 預覽時將原圖合成到同一張圖上
    """

    result = QImage(size, QImage.Format.Format_RGB32)
    if result.isNull():
        raise RuntimeError("無法配置光暈背景")
    result.fill(Qt.GlobalColor.black)

    source = QRectF(image.rect())
    fitted = fit_image_rect(source.size(), QRectF(result.rect()))

    # 分別對齊繪製矩形的兩端, 避免起點和寬度取整後推算出錯誤終點
    start_pixel = fitted.topLeft().toPoint()
    end_pixel = fitted.bottomRight().toPoint()
    sideways = start_pixel.x() > 0 or end_pixel.x() < size.width()
    extent = size.width() if sideways else size.height()
    leading_gap = start_pixel.x() if sideways else start_pixel.y()
    trailing_gap = extent - (end_pixel.x() if sideways else end_pixel.y())
    if not leading_gap and not trailing_gap and not include_foreground:
        return result

    if include_foreground:
        image = _opaque_on_black(image)

    inner_distance = (fitted.width() if sideways else fitted.height()) / 2
    source_extent = image.width() if sideways else image.height()
    band_depth = max(1, round(source_extent * 0.025))

    with QPainter(result) as painter:

        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if image.hasAlphaChannel():
            # 純背景留給半透明前景的區域維持黑底, 讓前景只在顯示時合成一次
            coverage = QRect(
                start_pixel.x(), start_pixel.y(), end_pixel.x() - start_pixel.x(), end_pixel.y() - start_pixel.y()
            )
            painter.setClipRegion(QRegion(result.rect()).subtracted(QRegion(coverage)))

        for leading, gutter in ((True, leading_gap), (False, trailing_gap)):

            if not gutter:
                continue

            offset = 0 if leading else source_extent - band_depth
            # 繪製矩形朝原圖延伸一個像素, 避免取整時露出黑線
            start = 0 if leading else extent - gutter - 1

            if sideways:
                band = image.copy(offset, 0, band_depth, image.height())
                target = QRect(start, 0, gutter + 1, size.height())

            else:
                band = image.copy(0, offset, image.width(), band_depth)
                target = QRect(0, start, size.width(), gutter + 1)

            _draw_ray_strip(
                painter, target, band, sideways=sideways, leading=leading,
                inner_distance=inner_distance, ray_mode=ray_mode, ray_decay=ray_decay
            )

        if include_foreground:
            painter.drawImage(fitted, image, source)

    return result


def ensure_thumbnail(
    path: Path,
    cancelled: Callable[[], bool],
    *,
    kind: ImageKind,
    media_dir: Path,
    thumbnail_dir: Path,
    background_dir: Path,
    preview_size: int | None = None,
    screen_size: tuple[int, int] | None = None,
    ray_mode: RayMode = DEFAULT_RAY_MODE,
    ray_decay: int = DEFAULT_RAY_DECAY
) -> Path:
    """
    驗證指定成品的快取, 缺少時直接從素材取圖並產生
    """

    ray_size = None
    valid_preview_size = type(preview_size) is int and 1 <= preview_size <= 5

    if kind in ("background_ray", "thumbnail_ray"):

        if screen_size is None or len(screen_size) != 2 or any(type(n) is not int or n <= 0 for n in screen_size):
            raise ValueError("光暈背景需要有效的螢幕尺寸")
        if ray_mode not in ("ray1", "ray2"):
            raise ValueError("光暈模式需為 ray1 或 ray2")
        if type(ray_decay) is not int or not 0 <= ray_decay <= 100:
            raise ValueError("光暈衰減需為 0 到 100 的整數")

        width, height = screen_size
        ray_size = QSize(width, height)
        variant = f"{ray_mode}-{ray_decay}-{width}-{height}"

        if kind == "thumbnail_ray":
            if not valid_preview_size or preview_size is None:
                raise ValueError("光暈縮圖需要有效的預覽尺寸")
            limit = preview_size * 100
            ray_size = ray_size.scaled(limit, limit, Qt.AspectRatioMode.KeepAspectRatio).expandedTo(QSize(1, 1))
            cache_dir, variant = thumbnail_dir, f"{preview_size}-{variant}"

        else:
            if preview_size is not None:
                raise ValueError("桌布光暈背景使用螢幕尺寸")
            cache_dir = background_dir

    elif kind == "background_blur":
        if preview_size is not None:
            raise ValueError("桌布模糊背景使用原尺寸")
        cache_dir, variant = background_dir, "blur"

    else:
        if (
            kind not in ("thumbnail_orig", "thumbnail_blur") or
            not valid_preview_size
        ):
            raise ValueError("縮圖種類或尺寸無效")
        cache_dir = thumbnail_dir
        variant = str(preview_size) if kind == "thumbnail_orig" else f"{preview_size}-blur"

    path = path.resolve(strict=True)
    relative_path = path.relative_to(media_dir.resolve())
    extension = path.suffix.lower()

    if (
        not path.is_file() or
        extension not in (SUPPORTED_IMAGE_EXTENSIONS if ray_size is not None else SUPPORTED_MEDIA_EXTENSIONS)
    ):
        raise ValueError("不支援的縮圖來源")


    original_stat = path.stat()
    digest = fast_hash(path)
    source_id = hashlib.sha256(path.name.encode("utf-8")).hexdigest()[:16]
    directory = cache_dir / relative_path.parent
    destination = directory / f"{source_id}-{digest}-{variant}.png"

    if cancelled():
        raise InterruptedError("縮圖工作已取消")

    if destination.is_file():
        cached = QImage(str(destination))
        if not cached.isNull() and (ray_size is None or cached.size() == ray_size):
            return destination

    # 只清理取圖前已存在的快取, 避免刪除其他工作剛產生的新版本
    old_paths = list(directory.glob(f"{source_id}-*"))
    if extension in SUPPORTED_IMAGE_EXTENSIONS:

        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        image = reader.read()

    else:
        image = _read_video_frame(path, cancelled)

    if image.isNull():
        raise RuntimeError("無法讀取縮圖來源")
    if cancelled():
        raise InterruptedError("縮圖工作已取消")

    if ray_size is not None:
        image = _render_ray(image, ray_size, ray_mode, ray_decay, include_foreground=kind == "thumbnail_ray")
    else:
        if preview_size is not None:
            width = preview_size * 100
            height = max(1, round(width * image.height() / image.width()))
            image = image.scaled(
                width, height, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
        if kind != "thumbnail_orig":
            image = _gaussian_blur(image)

    _save_cache_png(image, destination, path, original_stat, cancelled)

    # 保留相同素材版本的其他尺寸與背景
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
    逐一刪除縮圖與桌布背景圖
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
    ray_thumbnail_ready = Signal(str, int, QSize, str, int, QImage)
    ray_background_ready = Signal(str, QSize, str, int, QImage)


    def __init__(
        self, media_dir: Path, thumbnail_dir: Path, parent=None, *, background_dir: Path | None = None
    ) -> None:

        super().__init__(parent)


        self.media_dir = media_dir
        self.thumbnail_dir = thumbnail_dir
        self.background_dir = background_dir if background_dir is not None else thumbnail_dir.parent / "backgrounds"

        self.queue: deque[ThumbnailRequest] = deque()
        self.pending: set[ThumbnailRequest] = set()
        self.pending_lock = threading.Condition()
        self.stopping = False


    def request(
        self, path: Path, kind: ImageKind, *other_kinds: ImageKind, preview_size: int | None = None,
        screen_size: tuple[int, int] | None = None, ray_mode: RayMode = DEFAULT_RAY_MODE,
        ray_decay: int = DEFAULT_RAY_DECAY, priority: bool = False
    ) -> None:
        """
        按傳入順序排程, 每個種類各自產生和回傳
        """

        kinds = (kind, *other_kinds)

        with self.pending_lock:

            if self.stopping:
                return

            for image_kind in reversed(kinds) if priority else kinds:

                is_ray = image_kind in ("background_ray", "thumbnail_ray")
                size = screen_size if is_ray else None
                mode = ray_mode if is_ray else DEFAULT_RAY_MODE
                decay = ray_decay if is_ray else DEFAULT_RAY_DECAY
                request = (path, image_kind, preview_size, size, mode, decay)
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

            path, kind, preview_size, screen_size, ray_mode, ray_decay = request

            try:

                image_path = ensure_thumbnail(
                    path, self.isInterruptionRequested, kind=kind, media_dir=self.media_dir,
                    thumbnail_dir=self.thumbnail_dir, background_dir=self.background_dir, preview_size=preview_size,
                    screen_size=screen_size, ray_mode=ray_mode, ray_decay=ray_decay
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

                if kind == "background_ray" and screen_size is not None:
                    self.ray_background_ready.emit(str(path), QSize(*screen_size), ray_mode, ray_decay, image)
                elif kind == "thumbnail_ray" and preview_size is not None and screen_size is not None:
                    self.ray_thumbnail_ready.emit(
                        str(path), preview_size, QSize(*screen_size), ray_mode, ray_decay, image
                    )
                elif kind == "background_blur":
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
