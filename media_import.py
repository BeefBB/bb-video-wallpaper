"""
媒體匯入, 內容去重, 容量檢查與取消
匯入目錄與翻譯函式由呼叫端提供, 匯入模組時不建立資料夾或載入 VLC
"""

import hashlib
from pathlib import Path
import shutil
import tempfile

from thumbnails import SUPPORTED_MEDIA_EXTENSIONS


# 匯入媒體時預留的額外空間
IMPORT_RESERVE_BYTES = 100 * 1024 * 1024


class MediaImportCancelled(Exception):
    """使用者取消媒體匯入"""


def check_import_cancelled(cancelled) -> None:

    if cancelled():
        raise MediaImportCancelled()


def calculate_sha256(path: Path, cancelled=lambda: False) -> bytes:
    """
    分塊讀取檔案並計算 SHA-256
    """

    digest = hashlib.sha256()

    with path.open("rb") as file:

        while True:

            check_import_cancelled(cancelled)
            chunk = file.read(1024 * 1024)

            if not chunk:
                break

            digest.update(chunk)


    return digest.digest()


def import_media(
        source: Path,
        import_dir: Path,
        *,
        translate,
        progress=None,
        cancelled=lambda: False
    ) -> Path:
    """
    比對重複內容後分塊複製至指定目錄, 支援進度回報與取消
    """

    check_import_cancelled(cancelled)
    source = source.resolve(strict=True)

    if (
        not source.is_file() or
        source.suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS
    ):
        raise ValueError(source.name)


    import_dir.mkdir(parents=True, exist_ok=True)


    source_size = source.stat().st_size
    source_hash = None


    for candidate in sorted(import_dir.iterdir()):

        check_import_cancelled(cancelled)

        if (
            not candidate.is_file() or
            candidate.suffix.lower() not in SUPPORTED_MEDIA_EXTENSIONS
        ):
            continue

        if candidate.samefile(source):
            return candidate

        if candidate.stat().st_size != source_size:
            continue

        if source_hash is None:
            source_hash = calculate_sha256(source, cancelled)

        if calculate_sha256(candidate, cancelled) == source_hash:
            return candidate


    check_import_cancelled(cancelled)


    # 重複檔案不需額外空間; 確定要複製時才檢查並預留
    # 可用空間是給使用者看的, 是扣除預留空間後的數值, 也就是說會比真實可用空間小一些
    free_space = shutil.disk_usage(import_dir).free
    required_space = source_size + IMPORT_RESERVE_BYTES

    if free_space < required_space:
        megabyte = 1024 * 1024
        raise OSError(
            translate("media_insufficient_space").format(
                required=(source_size + megabyte - 1) // megabyte,
                available=max(0, free_space - IMPORT_RESERVE_BYTES) // megabyte
            )
        )


    # 暫存檔不會出現在媒體清單, 完成複製後才改成正式檔名
    with tempfile.NamedTemporaryFile(
        dir=import_dir,
        prefix=".import-",
        suffix=".tmp",
        delete=False
    ) as temp:
        temp_path = Path(temp.name)


    try:

        copied = 0
        last_percent = 0

        if progress is not None:
            progress(0)

        with source.open("rb") as source_file, temp_path.open("wb") as destination_file:

            while True:

                check_import_cancelled(cancelled)

                chunk = source_file.read(1024 * 1024)

                if not chunk:
                    break

                destination_file.write(chunk)
                copied += len(chunk)
                # 正式檔名建立後才回報 100%, 包含關閉檔案時的寫入失敗
                percent = min(99, copied * 100 // max(1, source_size))

                if progress is not None and percent != last_percent:
                    progress(percent)
                    last_percent = percent

        destination = import_dir / source.name
        index = 1

        while True:

            check_import_cancelled(cancelled)

            try:

                # Windows 的 rename 不會覆蓋已存在的檔案
                temp_path.rename(destination)

                if progress is not None:
                    progress(100)

                return destination

            except FileExistsError:

                suffix = f"({index}){source.suffix}"
                stem = source.stem
                encoded_stem = stem.encode("utf-16-le")

                # Windows 檔名上限以 UTF-16 單位計算
                max_stem_units = 255 - len(suffix.encode("utf-16-le")) // 2

                if len(encoded_stem) // 2 > max_stem_units:
                    # 截斷時額外預留 4 個單位, 並避免留下不完整的雙單位字元
                    stem = encoded_stem[:(max_stem_units - 4) * 2].decode("utf-16-le", errors="ignore")

                destination = import_dir / f"{stem}{suffix}"
                index += 1

    finally:
        temp_path.unlink(missing_ok=True)
