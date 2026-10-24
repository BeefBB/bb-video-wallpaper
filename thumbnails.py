"""
媒體縮圖產生, 快取與背景工作
匯入時不載入 VLC 或建立資料夾
"""

import ctypes
import hashlib
import logging
import os
from pathlib import Path
from queue import Queue
import tempfile
import threading
import time

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QImage, QImageReader


logger = logging.getLogger(__name__)


SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi",}
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp",}
SUPPORTED_MEDIA_EXTENSIONS = (SUPPORTED_VIDEO_EXTENSIONS | SUPPORTED_IMAGE_EXTENSIONS)


def fast_hash(path: Path) -> str:
    """
    以大小與頭尾各 16KB 計算縮圖識別碼
    """

    digest = hashlib.sha256()
    # 縮圖演算法更新時更換版本, 讓既有快取重新產生並清除舊檔
    digest.update(b"thumbnail-v3\0")

    with path.open("rb") as file:

        size = os.fstat(file.fileno()).st_size
        digest.update(str(size).encode("ascii"))

        if size <= 32 * 1024:
            digest.update(file.read())

        else:

            digest.update(file.read(16 * 1024))
            file.seek(-16 * 1024, os.SEEK_END)
            digest.update(file.read(16 * 1024))


    return digest.hexdigest()[:8]


def get_video_thumbnail_sizes(media, cancelled) -> dict[int, tuple[int, int]]:
    """
    依影像軌 ID 記錄縮圖大小, 使用可見尺寸, 像素比例與方向
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
                width *= video.sar_num
                height *= video.sar_den

            if video.orientation in (
                getattr(vlc.VideoOrient, "left_top"),
                getattr(vlc.VideoOrient, "left_bottom"),
                getattr(vlc.VideoOrient, "right_top"),
                getattr(vlc.VideoOrient, "right_bottom"),
            ):
                width, height = height, width

            sizes[track.id] = (300, max(1, round(300 * height / width)))

    finally:
        vlc.libvlc_media_tracks_release(track_array, count)

    if not sizes:
        raise RuntimeError("影片沒有可用的尺寸資訊")

    return sizes


def create_video_thumbnail(path: Path, cancelled) -> QImage:
    """
    使用獨立的靜音 VLC 播放器, 將第一個解碼畫面複製到記憶體, 不建立視窗
    """

    # 主程式先完成 DLL 設定, 圖片縮圖則不需要載入 VLC
    import vlc

    ready = threading.Event()
    frame = {}
    thumbnail_size = None
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
    def setup(opaque, chroma, width, height, pitches, lines):

        try:

            # AV1 可能在選定影像軌前就要求緩衝區
            # 回呼內不呼叫 VLC 播放器 API, 避免停止播放時卡住
            if thumbnail_size is not None:
                _, scaled_height = thumbnail_size

            else:

                if not width[0] or not height[0]:
                    raise RuntimeError("影片沒有可用的緩衝區尺寸")

                scaled_height = max(1, round(300 * height[0] / width[0]))

            width[0], height[0] = 300, scaled_height
            pitches[0] = (300 * 4 + 31) & ~31
            lines[0] = (scaled_height + 31) & ~31
            ctypes.memmove(chroma, b"RV32", 4)


            # VLC 要求像素平面對齊 32 bytes, 並保留 buffer 至播放器釋放後
            frame["buffer"] = ctypes.create_string_buffer(pitches[0] * lines[0] + 31)
            frame["address"] = (ctypes.addressof(frame["buffer"]) + 31) & ~31
            frame["height"] = scaled_height
            frame["pitch"] = pitches[0]

            return 1

        except Exception:
            logger.exception("初始化影片縮圖緩衝區失敗 - %s", path)
            ready.set()
            return 0


    @vlc.CallbackDecorators.VideoLockCb
    def lock(opaque, planes):

        planes[0] = frame["address"]
        return None


    @vlc.CallbackDecorators.VideoUnlockCb
    def unlock(opaque, picture, planes):

        if not ready.is_set():

            try:

                pixels = ctypes.string_at(frame["address"], frame["pitch"] * frame["height"])
                frame["image"] = QImage(
                    pixels,
                    300,
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
        thumbnail_sizes = get_video_thumbnail_sizes(media, cancelled)

        if len(thumbnail_sizes) == 1:
            thumbnail_size = next(iter(thumbnail_sizes.values()))

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

            if track_id in thumbnail_sizes:
                thumbnail_size = thumbnail_sizes[track_id]

            if ready.is_set():

                image = frame.get("image")

                if image is None or image.isNull():
                    raise RuntimeError("無法取得影片畫面")

                if thumbnail_size is not None:
                    break

            if time.monotonic() >= deadline:
                raise TimeoutError("擷取影片縮圖逾時")

            if player.get_state() in (vlc.State.Error, vlc.State.Ended, vlc.State.Stopped):  # type: ignore
                raise RuntimeError("影片沒有可用的畫面")

            if thumbnail_size is None:
                time.sleep(0.05)

            else:
                ready.wait(0.05)


        if cancelled():
            raise InterruptedError("縮圖工作已取消")

        # 選軌前的緩衝區可能使用暫定比例; 最後以實際影像軌的可見尺寸校正
        return image.scaled(
            *thumbnail_size,
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


def ensure_thumbnail(
    path: Path,
    cancelled,
    *,
    media_dir: Path,
    thumbnail_dir: Path,
) -> Path:
    """
    驗證既有縮圖或產生寬 300px 的 JPEG; 成功後才取代檔案並清除舊縮圖
    """

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
    prefix = f"{source_id}-"
    directory = thumbnail_dir / relative_path.parent
    destination = directory / f"{prefix}{digest}.jpg"

    if cancelled():
        raise InterruptedError("縮圖工作已取消")

    if destination.is_file() and not QImage(str(destination)).isNull():
        return destination


    if path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS:

        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        size = reader.size()

        if size.isValid() and size.width() > 0:

            size.setHeight(max(1, round(300 * size.height() / size.width())))
            size.setWidth(300)
            reader.setScaledSize(size)

        image = reader.read()

    else:
        image = create_video_thumbnail(path, cancelled)

    if image.isNull():
        raise RuntimeError("無法讀取縮圖來源")

    image = image.scaledToWidth(300, Qt.TransformationMode.SmoothTransformation)
    current_stat = path.stat()

    if (
        (original_stat.st_size, original_stat.st_mtime_ns) !=
        (current_stat.st_size, current_stat.st_mtime_ns)
    ):
        raise OSError("產生縮圖期間來源檔案已變更")

    if cancelled():
        raise InterruptedError("縮圖工作已取消")


    directory.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(
        dir=directory,
        prefix=".thumbnail-",
        suffix=".tmp",
        delete=False
    ) as temp:
        temp_path = Path(temp.name)


    try:

        if not image.save(str(temp_path), "JPG", 85):  # type: ignore
            raise OSError("無法儲存縮圖")

        os.replace(temp_path, destination)

    finally:
        temp_path.unlink(missing_ok=True)


    # 成功後只清除同一來源識別碼的舊縮圖
    try:

        for old_path in directory.iterdir():

            suffix = old_path.name.removeprefix(prefix)

            if (
                old_path != destination and
                old_path.name.startswith(prefix) and
                len(suffix) == 12 and
                suffix.endswith(".jpg") and
                all(char in "0123456789abcdef" for char in suffix[:8]) and
                old_path.is_file()
            ):
                old_path.unlink()

    except OSError:
        logger.warning("清除舊縮圖失敗 - %s", path, exc_info=True)


    logger.info("已產生縮圖 - %s", destination)
    return destination


class ThumbnailThread(QThread):
    """
    依序處理縮圖要求, 同一路徑在排隊或處理中時不重複加入
    """

    thumbnail_ready = Signal(str, QImage)


    def __init__(self, media_dir: Path, thumbnail_dir: Path, parent=None):

        super().__init__(parent)


        self.media_dir = media_dir
        self.thumbnail_dir = thumbnail_dir
        self.queue = Queue()
        self.pending = set()
        self.pending_lock = threading.Lock()
        self.stopping = False


    def request(self, path: Path) -> None:

        if self.stopping:
            return

        with self.pending_lock:

            if path in self.pending:
                return

            self.pending.add(path)

        self.queue.put(path)

        if not self.isRunning():
            self.start()


    def run(self) -> None:

        while not self.isInterruptionRequested():

            path = self.queue.get()

            if path is None or self.isInterruptionRequested():
                break

            image = QImage()

            try:

                thumbnail_path = ensure_thumbnail(
                    path,
                    self.isInterruptionRequested,
                    media_dir=self.media_dir,
                    thumbnail_dir=self.thumbnail_dir,
                )
                image = QImage(str(thumbnail_path))

                if image.isNull():
                    raise RuntimeError("無法讀取縮圖")

            except InterruptedError:
                break

            except Exception:
                logger.exception("產生縮圖失敗 - %s", path)

            finally:
                with self.pending_lock:
                    self.pending.discard(path)

            if not image.isNull() and not self.isInterruptionRequested():
                self.thumbnail_ready.emit(str(path), image)


    def stop(self) -> None:

        self.stopping = True
        self.requestInterruption()
        self.queue.put(None)
        self.wait()
