"""
測試媒體匯入的同名處理, 長檔名, 內容去重, 容量檢查, 進度與取消
只在暫存資料夾操作, 不啟動桌布或修改使用者設定
"""

import ast
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "需要 Windows 的 rename 不覆寫語意")
class MediaImportTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:

        # 僅載入匯入所需函式與常數, 避免主程式初始化使用者目錄, 日誌與 VLC
        source_path = ROOT / "bb-video-wallpaper.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        functions = ast.Module(
            body=[
                node for node in tree.body
                if (
                    isinstance(node, ast.FunctionDef)
                    and node.name in {"tr", "check_import_cancelled", "calculate_sha256", "import_media"}
                ) or (
                    isinstance(node, ast.ClassDef)
                    and node.name == "MediaImportCancelled"
                ) or (
                    isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "IMPORT_RESERVE_BYTES"
                        for target in node.targets
                    )
                )
            ],
            type_ignores=[],
        )
        cls.import_code = compile(functions, str(source_path), "exec")


    def setUp(self) -> None:

        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.source_dir = Path(self.temp_dir.name) / "source"
        self.source_dir.mkdir()
        self.media_dir = Path(self.temp_dir.name) / "media"
        self.import_dir = self.media_dir / "video"
        self.import_dir.mkdir(parents=True)

        disk_usage_patch = mock.patch.object(shutil, "disk_usage")
        self.disk_usage = disk_usage_patch.start()
        self.addCleanup(disk_usage_patch.stop)
        self.disk_usage.return_value.free = 1024 * 1024 * 1024

        namespace = {
            "Path": Path,
            "hashlib": hashlib,
            "shutil": shutil,
            "tempfile": tempfile,
            "MEDIA_DIR": self.media_dir,
            "SUPPORTED_MEDIA_EXTENSIONS": {".mp4", ".jpeg"},
            "LANG": json.loads((ROOT / "lang" / "zh-TW.json").read_text(encoding="utf-8")),
        }
        exec(self.import_code, namespace)
        self.namespace = namespace
        self.import_media = namespace["import_media"]


    def test_insufficient_space_is_rejected_before_creating_or_copying_a_file(self):

        contents = b"new"

        with mock.patch.object(tempfile, "NamedTemporaryFile") as temporary_file:
            for suffix in (".mp4", ".jpeg"):
                source = self.source_dir / f"sample{suffix}"
                source.write_bytes(contents)

                for free_space in (0, len(contents) - 1, len(contents), 100 * 1024 * 1024 + len(contents) - 1):
                    with self.subTest(suffix=suffix, free_space=free_space):
                        self.disk_usage.return_value.free = free_space

                        with self.assertRaises(OSError):
                            self.import_media(source)

                        self.disk_usage.assert_called_with(self.import_dir)
                        self.assertEqual(source.read_bytes(), contents)
                        self.assertEqual(list(self.import_dir.iterdir()), [])

            temporary_file.assert_not_called()


    def test_import_succeeds_with_exactly_100_mb_reserved_or_more(self):

        for extra_bytes in (0, 1):
            with self.subTest(extra_bytes=extra_bytes):
                source = self.source_dir / f"sample-{extra_bytes}.mp4"
                contents = f"new-{extra_bytes}".encode("ascii")
                source.write_bytes(contents)
                self.disk_usage.return_value.free = len(contents) + 100 * 1024 * 1024 + extra_bytes

                destination = self.import_media(source)

                self.disk_usage.assert_called_with(self.import_dir)
                self.assertEqual(destination, self.import_dir / source.name)
                self.assertEqual(destination.read_bytes(), contents)
                self.assertEqual(source.read_bytes(), contents)
                self.assertFalse(list(self.import_dir.glob(".import-*.tmp")))


    def test_identical_content_is_reused_even_when_disk_is_full(self):

        source = self.source_dir / "sample.mp4"
        source.write_bytes(b"same")
        existing = self.import_dir / "existing.mp4"
        existing.write_bytes(b"same")
        self.disk_usage.return_value.free = 0

        progress = mock.Mock()
        self.assertEqual(self.import_media(source, progress=progress), existing)
        progress.assert_not_called()

        self.disk_usage.assert_not_called()
        self.assertEqual(list(self.import_dir.iterdir()), [existing])


    def test_file_already_in_import_directory_is_reused_even_when_disk_is_full(self):

        source = self.import_dir / "sample.mp4"
        source.write_bytes(b"same")
        self.disk_usage.return_value.free = 0

        progress = mock.Mock()
        self.assertEqual(self.import_media(source, progress=progress), source)
        progress.assert_not_called()

        self.disk_usage.assert_not_called()
        self.assertEqual(list(self.import_dir.iterdir()), [source])


    def test_disk_usage_failure_stops_before_creating_or_copying_a_file(self):

        source = self.source_dir / "sample.mp4"
        source.write_bytes(b"new")
        error = OSError("Cannot query disk space")
        self.disk_usage.side_effect = error

        with mock.patch.object(tempfile, "NamedTemporaryFile") as temporary_file:
            with self.assertRaises(OSError) as caught:
                self.import_media(source)

            self.assertIs(caught.exception, error)
            temporary_file.assert_not_called()

        self.assertEqual(list(self.import_dir.iterdir()), [])
        self.assertEqual(source.read_bytes(), b"new")


    def test_disk_filling_during_copy_cleans_up_partial_file(self):

        source = self.source_dir / "sample.mp4"
        source.write_bytes(b"new contents")
        existing = self.import_dir / source.name
        existing.write_bytes(b"old contents")

        original_open = Path.open

        @contextmanager
        def open_until_full(file_path, mode="r", *args, **kwargs):

            with original_open(file_path, mode, *args, **kwargs) as file:

                if mode == "wb" and file_path.name.startswith(".import-"):

                    def write_until_full(chunk):
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


    def test_copy_reports_increasing_progress_and_finishes_after_rename(self):

        source = self.source_dir / "sample.mp4"
        contents = b"video" * (1024 * 1024)
        source.write_bytes(contents)
        updates = []

        def report(percent):
            updates.append(percent)

            if percent == 100:
                self.assertTrue((self.import_dir / source.name).is_file())
                self.assertFalse(list(self.import_dir.glob(".import-*.tmp")))

        destination = self.import_media(source, progress=report)

        self.assertEqual(updates[0], 0)
        self.assertEqual(updates[-1], 100)
        self.assertGreater(len(updates), 2)
        self.assertEqual(updates, sorted(set(updates)))
        self.assertTrue(all(0 <= percent <= 100 for percent in updates))
        self.assertEqual(destination.read_bytes(), contents)
        self.assertEqual(source.read_bytes(), contents)


    def test_empty_file_reports_zero_and_completion(self):

        source = self.source_dir / "empty.mp4"
        source.touch()
        updates = []

        destination = self.import_media(source, progress=updates.append)

        self.assertEqual(updates, [0, 100])
        self.assertEqual(destination.read_bytes(), b"")


    def test_cancel_before_start_does_not_create_a_file(self):

        source = self.source_dir / "sample.mp4"
        source.write_bytes(b"new contents")
        progress = mock.Mock()

        with self.assertRaises(self.namespace["MediaImportCancelled"]):
            self.import_media(source, progress=progress, cancelled=lambda: True)

        progress.assert_not_called()
        self.disk_usage.assert_not_called()
        self.assertEqual(list(self.import_dir.iterdir()), [])
        self.assertEqual(source.read_bytes(), b"new contents")


    def test_cancel_during_hashing_source_or_candidate(self):

        source = self.source_dir / "sample.mp4"
        source.write_bytes(b"new contents")
        existing = self.import_dir / "existing.mp4"
        existing.write_bytes(b"old contents")
        real_sha256 = hashlib.sha256

        for cancel_hash_number in (1, 2):
            with self.subTest(cancel_hash_number=cancel_hash_number):
                cancelled = False
                hash_count = 0
                progress = mock.Mock()

                def new_digest():
                    nonlocal hash_count
                    hash_count += 1
                    digest = real_sha256()

                    if hash_count == cancel_hash_number:

                        def update_and_cancel(chunk):
                            nonlocal cancelled
                            digest.update(chunk)
                            cancelled = True

                        wrapper = mock.Mock(wraps=digest)
                        wrapper.update.side_effect = update_and_cancel
                        return wrapper

                    return digest

                with mock.patch.object(hashlib, "sha256", side_effect=new_digest):
                    with self.assertRaises(self.namespace["MediaImportCancelled"]):
                        self.import_media(source, progress=progress, cancelled=lambda: cancelled)

                progress.assert_not_called()
                self.assertEqual(hash_count, cancel_hash_number)
                self.assertEqual(list(self.import_dir.iterdir()), [existing])
                self.assertEqual(existing.read_bytes(), b"old contents")
                self.assertEqual(source.read_bytes(), b"new contents")


    def test_cancel_during_copy_or_before_rename_cleans_up_partial_file(self):

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
                    nonlocal cancelled
                    updates.append(percent)

                    if percent >= cancel_percent:
                        self.assertTrue(list(self.import_dir.glob(".import-*.tmp")))
                        cancelled = True

                with self.assertRaises(self.namespace["MediaImportCancelled"]):
                    self.import_media(source, progress=report, cancelled=lambda: cancelled)

                self.assertNotIn(100, updates)
                self.assertEqual(list(self.import_dir.iterdir()), [existing])
                self.assertEqual(existing.read_bytes(), b"old contents")
                self.assertEqual(source.read_bytes(), contents)


    def test_insufficient_space_message_includes_capacity_in_every_language(self):

        source = self.source_dir / "sample.mp4"

        with source.open("wb") as file:
            file.truncate(2 * 1024 * 1024 + 1)

        for language in ("zh-TW", "zh-CN", "en-US", "ja-JP"):
            self.namespace["LANG"] = json.loads(
                (ROOT / "lang" / f"{language}.json").read_text(encoding="utf-8")
            )

            for free_mb, available_mb in ((101, 1), (50, 0)):
                with self.subTest(language=language, free_mb=free_mb):
                    self.disk_usage.return_value.free = free_mb * 1024 * 1024

                    with self.assertRaises(OSError) as caught:
                        self.import_media(source)

                    message = str(caught.exception)
                    self.assertEqual(re.findall(r"-?[\d,]+ MB", message), ["3 MB", f"{available_mb} MB"])
                    self.assertNotIn("media_insufficient_space", message)
                    self.assertNotIn("{", message)


    def test_long_name_without_collision_is_preserved(self):

        source = self.source_dir / ("a" * 251 + ".mp4")
        source.write_bytes(b"new")

        destination = self.import_media(source)

        self.assertEqual(destination.name, source.name)
        self.assertEqual(destination.read_bytes(), b"new")
        self.assertEqual(source.read_bytes(), b"new")


    def test_short_name_collisions_keep_existing_files(self):

        source = self.source_dir / "sample.mp4"
        original = self.import_dir / source.name
        original.write_bytes(b"old")

        for index in (1, 2):
            contents = f"new-{index}".encode("ascii")
            source.write_bytes(contents)

            destination = self.import_media(source)

            self.assertEqual(destination.name, f"sample({index}).mp4")
            self.assertEqual(destination.read_bytes(), contents)

        self.assertEqual(original.read_bytes(), b"old")
        self.assertEqual((self.import_dir / "sample(1).mp4").read_bytes(), b"new-1")
        self.assertFalse(list(self.import_dir.glob(".import-*.tmp")))


    def test_truncation_reserves_four_units_only_when_required(self):

        for suffix in (".mp4", ".jpeg"):
            for length, expected_length in ((252, 255), (253, 251), (255, 251)):
                with self.subTest(suffix=suffix, length=length):
                    prefix = str(length)
                    stem = prefix + "a" * (length - len(suffix) - len(prefix))
                    source = self.source_dir / f"{stem}{suffix}"
                    contents = f"new-{suffix}-{length}".encode("ascii")
                    source.write_bytes(contents)
                    original = self.import_dir / source.name
                    original.write_bytes(b"old")

                    destination = self.import_media(source)

                    expected_stem = stem[:expected_length - len(suffix) - 3]
                    self.assertEqual(destination.name, f"{expected_stem}(1){suffix}")
                    self.assertEqual(destination.read_bytes(), contents)
                    self.assertEqual(original.read_bytes(), b"old")
                    self.assertFalse(list(self.import_dir.glob(".import-*.tmp")))


    def test_chinese_and_emoji_names_use_utf16_without_splitting_characters(self):

        cases = (
            ("chinese", "桌" * 251, "桌" * 244),
            ("emoji", "a" + "🌄" * 125, "a" + "🌄" * 121),
        )

        for label, stem, expected_stem in cases:
            with self.subTest(characters=label):
                source = self.source_dir / f"{stem}.mp4"
                contents = stem.encode("utf-8")
                source.write_bytes(contents)
                original = self.import_dir / source.name
                original.write_bytes(b"old")

                destination = self.import_media(source)

                self.assertEqual(destination.name, f"{expected_stem}(1).mp4")
                self.assertLessEqual(len(destination.name.encode("utf-16-le")) // 2, 251)
                self.assertEqual(destination.read_bytes(), contents)
                self.assertEqual(original.read_bytes(), b"old")


    def test_number_growing_to_two_digits_truncates_and_preserves_collisions(self):

        stem = "a" * 248
        source = self.source_dir / f"{stem}.mp4"
        source.write_bytes(b"new")
        existing = [self.import_dir / source.name]
        existing.extend(self.import_dir / f"{stem}({index}).mp4" for index in range(1, 10))

        for candidate in existing:
            candidate.write_bytes(b"old")

        destination = self.import_media(source)

        self.assertEqual(destination.name, "a" * 243 + "(10).mp4")
        self.assertEqual(destination.read_bytes(), b"new")
        self.assertEqual(len(list(self.import_dir.iterdir())), 11)

        for candidate in existing:
            self.assertEqual(candidate.read_bytes(), b"old")


    def test_reimport_after_truncation_reuses_identical_content(self):

        source = self.source_dir / ("a" * 251 + ".mp4")
        source.write_bytes(b"new")
        original = self.import_dir / source.name
        original.write_bytes(b"old")

        destination = self.import_media(source)

        self.assertEqual(self.import_media(source), destination)
        self.assertEqual(destination.read_bytes(), b"new")
        self.assertEqual(original.read_bytes(), b"old")
        self.assertEqual(len(list(self.import_dir.iterdir())), 2)


if __name__ == "__main__":
    unittest.main()
