"""
測試媒體匯入的同名處理, 長檔名, 內容去重, 容量檢查, 進度與取消
只在暫存資料夾操作, 不啟動桌布或修改使用者設定
"""

from contextlib import contextmanager
import errno
from functools import partial
import hashlib
import json
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

from media_import import MediaImportCancelled, import_media


@unittest.skipUnless(os.name == "nt", "需要 Windows 的 rename 不覆寫語意")
class MediaImportTests(unittest.TestCase):
    """
    媒體匯入, 容量檢查與檔名衝突處理
    """

    def setUp(self) -> None:
        """
        建立暫存來源與匯入目錄, 固定可用容量及語言
        """

        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.source_dir = Path(self.temp_dir.name) / "source"
        self.source_dir.mkdir()
        self.media_dir = Path(self.temp_dir.name) / "media"
        self.import_dir = self.media_dir / "_imports"
        self.import_dir.mkdir(parents=True)

        # 模擬可用容量
        self.disk_usage = self.enterContext(mock.patch.object(shutil, "disk_usage"))
        self.disk_usage.return_value.free = 1024 * 1024 * 1024

        self.language = json.loads((ROOT / "lang" / "zh-TW.json").read_text(encoding="utf-8"))
        self.import_media = partial(
            import_media,
            import_dir=self.import_dir,
            translate=lambda key: self.language.get(key, key)
        )


    def test_space_boundary_rejects_before_copy_and_accepts_exact_reserve(self):
        """
        容量不足時在建立暫存檔前拒絕, 剛好保留 100 MB 時可完成匯入
        """

        source = self.source_dir / "sample.mp4"
        contents = b"new"
        source.write_bytes(contents)
        # 來源檔案大小加上 100 MB 保留空間
        self.disk_usage.return_value.free = len(contents) + 100 * 1024 * 1024 - 1

        with mock.patch.object(tempfile, "NamedTemporaryFile") as temporary_file:
            with self.assertRaises(OSError):
                self.import_media(source)
            temporary_file.assert_not_called()
        self.assertEqual(list(self.import_dir.iterdir()), [])
        self.assertEqual(source.read_bytes(), contents)

        self.disk_usage.return_value.free += 1
        destination = self.import_media(source)
        self.assertEqual(destination.read_bytes(), contents)
        self.assertEqual(source.read_bytes(), contents)
        self.assertEqual(list(self.import_dir.iterdir()), [destination])


    def test_existing_file_or_identical_content_is_reused_when_disk_is_full(self):
        """
        磁碟已滿時仍可重用匯入目錄內的來源檔或內容相同的既有檔案
        """

        existing = self.import_dir / "existing.mp4"
        existing.write_bytes(b"same")
        source = self.source_dir / "sample.mp4"
        source.write_bytes(b"same")
        self.disk_usage.return_value.free = 0
        progress = mock.Mock()

        for path in (source, existing):
            with self.subTest(source=path):
                self.assertEqual(self.import_media(path, progress=progress), existing)
                self.assertEqual(list(self.import_dir.iterdir()), [existing])
        progress.assert_not_called()
        self.disk_usage.assert_not_called()


    def test_disk_filling_during_copy_cleans_up_partial_file(self):
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
        self.assertEqual(source.read_bytes(), b"new contents")


    def test_cancel_during_hashing_source_or_candidate(self):
        """
        計算來源或既有檔案的雜湊時可取消
        """

        source = self.source_dir / "sample.mp4"
        source.write_bytes(b"new contents")
        existing = self.import_dir / "existing.mp4"
        existing.write_bytes(b"old contents")
        real_sha256 = hashlib.sha256

        # 依序取消來源與既有檔案的雜湊
        for cancel_hash_number in (1, 2):
            with self.subTest(cancel_hash_number=cancel_hash_number):
                cancelled = False
                hash_count = 0
                progress = mock.Mock()

                def new_digest():
                    """
                    建立雜湊物件, 在指定次數的雜湊計算中加入取消回呼
                    """

                    nonlocal hash_count
                    hash_count += 1
                    digest = real_sha256()

                    if hash_count == cancel_hash_number:

                        def update_and_cancel(chunk):
                            """
                            更新雜湊內容後設定取消旗標
                            """

                            nonlocal cancelled
                            digest.update(chunk)
                            cancelled = True

                        wrapper = mock.Mock(wraps=digest)
                        wrapper.update.side_effect = update_and_cancel
                        return wrapper

                    return digest

                with mock.patch.object(hashlib, "sha256", side_effect=new_digest):
                    with self.assertRaises(MediaImportCancelled):
                        self.import_media(source, progress=progress, cancelled=lambda: cancelled)

                progress.assert_not_called()
                self.assertEqual(hash_count, cancel_hash_number)
                self.assertEqual(list(self.import_dir.iterdir()), [existing])
                self.assertEqual(existing.read_bytes(), b"old contents")
                self.assertEqual(source.read_bytes(), b"new contents")


    def test_cancel_during_copy_or_before_rename_cleans_up_partial_file(self):
        """
        複製中或更名前取消時清除暫存檔
        """

        source = self.source_dir / "sample.mp4"
        contents = b"new" * (1024 * 1024)
        source.write_bytes(contents)
        existing = self.import_dir / source.name
        existing.write_bytes(b"old contents")

        for cancel_percent in (1, 99):
            with self.subTest(cancel_percent=cancel_percent):
                cancelled = False
                updates = []

                def report(percent):
                    """
                    記錄複製進度, 達到指定百分比時確認暫存檔存在並要求取消
                    """

                    nonlocal cancelled
                    updates.append(percent)

                    if percent >= cancel_percent:
                        self.assertTrue(list(self.import_dir.glob(".import-*.tmp")))
                        cancelled = True

                with self.assertRaises(MediaImportCancelled):
                    self.import_media(source, progress=report, cancelled=lambda: cancelled)

                self.assertNotIn(100, updates)
                self.assertEqual(list(self.import_dir.iterdir()), [existing])
                self.assertEqual(existing.read_bytes(), b"old contents")
                self.assertEqual(source.read_bytes(), contents)


    def test_collisions_preserve_files_and_reimport_reuses_truncated_names(self):
        """
        同名時保留既有檔案, 長檔名依 UTF-16 截短, 再次匯入重用相同內容
        """

        cases = (
            ("short", "sample", 2, "sample(2).mp4"),
            ("chinese", "桌" * 251, 1, "桌" * 244 + "(1).mp4"),
            ("emoji", "a" + "🌄" * 125, 1, "a" + "🌄" * 121 + "(1).mp4"),
            ("two-digits", "a" * 248, 10, "a" * 243 + "(10).mp4"),
        )
        for label, stem, count, expected in cases:
            with self.subTest(case=label):
                source = self.source_dir / f"{stem}.mp4"
                source.write_bytes(b"new")
                import_dir = self.import_dir / label
                import_dir.mkdir()
                existing = [import_dir / source.name]
                existing.extend(import_dir / f"{stem}({index}).mp4" for index in range(1, count))
                for path in existing:
                    path.write_bytes(b"old")

                destination = self.import_media(source, import_dir=import_dir)
                self.assertEqual(destination.name, expected)
                self.assertLessEqual(len(destination.name.encode("utf-16-le")) // 2, 255)
                self.assertEqual(destination.read_bytes(), b"new")
                self.assertEqual(self.import_media(source, import_dir=import_dir), destination)
                self.assertEqual(set(import_dir.iterdir()), {*existing, destination})
                for path in existing:
                    self.assertEqual(path.read_bytes(), b"old")


if __name__ == "__main__":
    unittest.main()
