"""
測試媒體匯入期間的來源保護與部分寫入失敗後的清理
只在暫存資料夾操作, 不啟動桌布或修改使用者設定
"""

from contextlib import contextmanager
import errno
from functools import partial
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


# 直接執行測試檔時, 將專案根目錄加入模組搜尋路徑
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from media_import import calculate_sha256, import_media


@unittest.skipUnless(os.name == "nt", "需要 Windows 的檔案共用模式")
class MediaImportTests(unittest.TestCase):
    """
    Windows 來源鎖定與匯入失敗後的檔案保護
    """

    def setUp(self) -> None:
        """
        建立暫存來源與匯入目錄, 固定可用容量
        """

        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source_dir = directory / "source"
        self.source_dir.mkdir()
        self.import_dir = directory / "media" / "_imports"
        self.import_dir.mkdir(parents=True)

        # 模擬可用容量
        disk_usage = self.enterContext(mock.patch.object(shutil, "disk_usage"))
        disk_usage.return_value.free = 1024 * 1024 * 1024

        self.import_media = partial(
            import_media,
            import_dir=self.import_dir,
            translate=lambda key: key
        )


    def test_source_is_protected_during_hashing_and_copy_and_released_on_success(self) -> None:
        """
        驗證雜湊與複製期間可讀取來源但不能寫入, 更名或刪除, 完成後釋放保護
        """

        source = self.source_dir / "sample.mp4"
        contents = b"new" * (1024 * 1024)
        source.write_bytes(contents)
        existing = self.import_dir / "existing.mp4"
        existing.write_bytes(b"old" * (1024 * 1024))

        def check_source_protection(percent=None):
            """
            使用真正的 Windows 檔案操作驗證共用模式
            """

            self.assertEqual(source.read_bytes(), contents)
            with self.assertRaises(PermissionError):
                with source.open("r+b"):
                    pass
            with self.assertRaises(PermissionError):
                source.rename(source.with_name("renamed.mp4"))
            with self.assertRaises(PermissionError):
                source.unlink()

        def hash_protected_source(path, cancelled):
            """
            在每次雜湊開始前確認來源保護仍有效
            """

            check_source_protection()
            return calculate_sha256(path, cancelled)

        with mock.patch("media_import.calculate_sha256", side_effect=hash_protected_source) as calculate:
            destination = self.import_media(source, progress=check_source_protection)

        self.assertEqual(calculate.call_count, 2)
        self.assertEqual(destination.read_bytes(), contents)
        with source.open("r+b") as file:
            self.assertEqual(file.read(), contents)
        renamed = source.with_name("renamed.mp4")
        source.rename(renamed)
        renamed.rename(source)


    def test_disk_filling_during_copy_cleans_up_partial_file(self) -> None:
        """
        複製途中磁碟已滿時清除暫存檔並保留既有檔案
        """

        source = self.source_dir / "sample.mp4"
        source.write_bytes(b"new contents")
        existing = self.import_dir / source.name
        existing.write_bytes(b"old contents")

        original_open = Path.open

        @contextmanager
        def open_until_full(file_path, mode="r", *args, **kwargs):
            """
            開啟來源或暫存檔, 只將匯入暫存檔的寫入替換為磁碟已滿的行為
            """

            with original_open(file_path, mode, *args, **kwargs) as file:

                if mode == "wb" and file_path.name.startswith(".import-"):

                    def write_until_full(chunk):
                        """
                        先寫入部分資料, 再拋出磁碟空間不足例外
                        """

                        # 寫入部分資料後模擬磁碟已滿
                        file.write(chunk[:3])
                        raise OSError(errno.ENOSPC, "No space left on device")

                    writer = mock.Mock(wraps=file)
                    writer.write.side_effect = write_until_full
                    yield writer

                else:
                    yield file

        progress = mock.Mock()

        with mock.patch.object(Path, "open", open_until_full):
            with self.assertRaises(OSError) as caught:
                self.import_media(source, progress=progress)

        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertNotIn(mock.call(100), progress.call_args_list)
        self.assertEqual(list(self.import_dir.iterdir()), [existing])
        self.assertEqual(existing.read_bytes(), b"old contents")
        with source.open("r+b") as file:
            self.assertEqual(file.read(), b"new contents")


if __name__ == "__main__":
    unittest.main()
