"""
測試媒體匯入的同名處理, 長檔名, 內容去重與磁碟容量檢查
只在暫存資料夾操作, 不啟動桌布或修改使用者設定
"""

import ast
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
                    and node.name in {"tr", "calculate_sha256", "import_media"}
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

        with (
            mock.patch.object(tempfile, "NamedTemporaryFile") as temporary_file,
            mock.patch.object(shutil, "copyfile") as copy_file,
        ):
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
            copy_file.assert_not_called()


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

        with mock.patch.object(shutil, "copyfile") as copy_file:
            self.assertEqual(self.import_media(source), existing)
            copy_file.assert_not_called()

        self.disk_usage.assert_not_called()
        self.assertEqual(list(self.import_dir.iterdir()), [existing])


    def test_file_already_in_import_directory_is_reused_even_when_disk_is_full(self):

        source = self.import_dir / "sample.mp4"
        source.write_bytes(b"same")
        self.disk_usage.return_value.free = 0

        with mock.patch.object(shutil, "copyfile") as copy_file:
            self.assertEqual(self.import_media(source), source)
            copy_file.assert_not_called()

        self.disk_usage.assert_not_called()
        self.assertEqual(list(self.import_dir.iterdir()), [source])


    def test_disk_usage_failure_stops_before_creating_or_copying_a_file(self):

        source = self.source_dir / "sample.mp4"
        source.write_bytes(b"new")
        error = OSError("Cannot query disk space")
        self.disk_usage.side_effect = error

        with (
            mock.patch.object(tempfile, "NamedTemporaryFile") as temporary_file,
            mock.patch.object(shutil, "copyfile") as copy_file,
        ):
            with self.assertRaises(OSError) as caught:
                self.import_media(source)

            self.assertIs(caught.exception, error)
            temporary_file.assert_not_called()
            copy_file.assert_not_called()

        self.assertEqual(list(self.import_dir.iterdir()), [])
        self.assertEqual(source.read_bytes(), b"new")


    def test_disk_filling_during_copy_cleans_up_partial_file(self):

        source = self.source_dir / "sample.mp4"
        source.write_bytes(b"new contents")
        existing = self.import_dir / source.name
        existing.write_bytes(b"old contents")

        def copy_until_full(source_path, destination):
            Path(destination).write_bytes(b"partial")
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch.object(shutil, "copyfile", side_effect=copy_until_full):
            with self.assertRaises(OSError) as caught:
                self.import_media(source)

        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertEqual(list(self.import_dir.iterdir()), [existing])
        self.assertEqual(existing.read_bytes(), b"old contents")
        self.assertEqual(source.read_bytes(), b"new contents")


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
