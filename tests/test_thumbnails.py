"""
測試縮圖快取的命名, 驗證與清理
只在暫存資料夾操作, 不啟動桌布或初始化 VLC
"""

import ast
import hashlib
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QImageReader


SOURCE_PATH = Path(__file__).resolve().parents[1] / "bb-video-wallpaper.py"
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")
NAMES = {
    "SUPPORTED_VIDEO_EXTENSIONS",
    "SUPPORTED_IMAGE_EXTENSIONS",
    "SUPPORTED_MEDIA_EXTENSIONS",
    "fast_hash",
    "ensure_thumbnail",
}


def load_thumbnail_functions() -> dict:
    """
    只載入縮圖相關函式與常數, 避免主程式匯入時初始化日誌和 VLC
    """

    tree = ast.parse(SOURCE)
    nodes = []

    for node in tree.body:

        if isinstance(node, ast.FunctionDef) and node.name in NAMES:
            nodes.append(node)

        elif isinstance(node, ast.Assign):

            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in NAMES:
                    nodes.append(node)
                    break


    logger = logging.getLogger("thumbnail_tests")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False

    namespace = {
        "hashlib": hashlib,
        "logger": logger,
        "os": os,
        "Path": Path,
        "tempfile": tempfile,
        "Qt": Qt,
        "QImage": QImage,
        "QImageReader": QImageReader,
    }

    module = ast.Module(body=nodes, type_ignores=[])
    code = compile(module, str(SOURCE_PATH), "exec")
    exec(code, namespace)

    return namespace


class ThumbnailTests(unittest.TestCase):

    def setUp(self) -> None:

        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        self.media_dir = Path(self.temp_dir.name) / "media"
        self.media_dir.mkdir()
        self.thumbnail_dir = Path(self.temp_dir.name) / "thumbnails"
        self.namespace = load_thumbnail_functions()

        self.namespace.update(
            MEDIA_DIR=self.media_dir,
            THUMBNAIL_DIR=self.thumbnail_dir
        )


    def save_image(self, path: Path, color) -> None:

        image = QImage(32, 18, QImage.Format.Format_RGB32)
        image.fill(color)
        self.assertTrue(image.save(str(path)))


    def thumbnail(self, path: Path) -> Path:
        return self.namespace["ensure_thumbnail"](path, lambda: False)


    def test_long_names_with_same_content_have_independent_caches(self):

        first = self.media_dir / ("a" * 239 + "1.bmp")
        second = self.media_dir / ("a" * 239 + "2.bmp")
        self.save_image(first, Qt.GlobalColor.red)
        second.write_bytes(first.read_bytes())

        first_thumbnail = self.thumbnail(first)
        second_thumbnail = self.thumbnail(second)
        self.assertNotEqual(first_thumbnail, second_thumbnail)

        for thumbnail in (first_thumbnail, second_thumbnail):
            self.assertEqual(len(thumbnail.name), 29)
            self.assertTrue(thumbnail.name.isascii())
            self.assertEqual(QImage(str(thumbnail)).width(), 300)

        saved_mtime = first_thumbnail.stat().st_mtime_ns
        self.assertEqual(self.thumbnail(first), first_thumbnail)
        self.assertEqual(first_thumbnail.stat().st_mtime_ns, saved_mtime)

        self.save_image(first, Qt.GlobalColor.blue)
        updated_thumbnail = self.thumbnail(first)
        self.assertNotEqual(updated_thumbnail, first_thumbnail)
        self.assertFalse(first_thumbnail.exists())
        self.assertTrue(second_thumbnail.exists())


    def test_cleanup_preserves_unrelated_files_and_directories(self):

        source = self.media_dir / "sample.bmp"
        self.save_image(source, Qt.GlobalColor.red)
        previous = self.thumbnail(source)
        prefix = previous.name.rsplit("-", 1)[0] + "-"
        unrelated = self.thumbnail_dir / "unrelated.jpg"
        malformed = self.thumbnail_dir / f"{prefix}deadbeeg.jpg"
        directory = self.thumbnail_dir / f"{prefix}deadbeef.jpg"
        directory.mkdir()

        for path in (unrelated, malformed):
            path.write_bytes(b"keep")

        self.save_image(source, Qt.GlobalColor.blue)
        self.assertTrue(self.thumbnail(source).is_file())
        self.assertFalse(previous.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(malformed.exists())
        self.assertTrue(directory.is_dir())


    def test_previous_algorithm_cache_is_replaced(self):

        source = self.media_dir / "sample.bmp"
        self.save_image(source, Qt.GlobalColor.red)
        contents = source.read_bytes()
        old_digest = hashlib.sha256(b"thumbnail-v2\0" + str(len(contents)).encode("ascii") + contents).hexdigest()[:8]
        source_id = hashlib.sha256(source.name.encode("utf-8")).hexdigest()[:16]
        self.thumbnail_dir.mkdir()
        previous = self.thumbnail_dir / f"{source_id}-{old_digest}.jpg"
        self.save_image(previous, Qt.GlobalColor.blue)

        current = self.thumbnail(source)

        self.assertNotEqual(current, previous)
        self.assertFalse(previous.exists())
        self.assertEqual(QImage(str(current)).width(), 300)


    def test_replace_failure_preserves_cache_and_removes_temp(self):

        source = self.media_dir / "sample.bmp"
        self.save_image(source, Qt.GlobalColor.red)
        previous = self.thumbnail(source)
        self.save_image(source, Qt.GlobalColor.blue)

        with mock.patch.object(os, "replace", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                self.thumbnail(source)

        self.assertTrue(previous.is_file())
        self.assertFalse(list(self.thumbnail_dir.glob(".thumbnail-*.tmp")))


if __name__ == "__main__":
    unittest.main()
