"""
測試媒體匯入的同名處理, 長檔名與內容去重
只在暫存資料夾操作, 不啟動桌布或修改使用者設定
"""

import ast
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "需要 Windows 的 rename 不覆寫語意")
class MediaImportTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:

        # 僅載入匯入函式, 避免主程式初始化使用者目錄, 日誌與 VLC
        source_path = ROOT / "bb-video-wallpaper.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        functions = ast.Module(
            body=[
                node for node in tree.body
                if isinstance(node, ast.FunctionDef)
                and node.name in {"calculate_sha256", "import_media"}
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

        namespace = {
            "Path": Path,
            "hashlib": hashlib,
            "shutil": shutil,
            "tempfile": tempfile,
            "MEDIA_DIR": self.media_dir,
            "SUPPORTED_MEDIA_EXTENSIONS": {".mp4", ".jpeg"},
        }
        exec(self.import_code, namespace)
        self.import_media = namespace["import_media"]


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
