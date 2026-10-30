"""
測試縮圖快取的命名, 驗證與清理
只在暫存資料夾操作, 不啟動桌布或初始化 VLC
"""

import hashlib
import os
import sys
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage


ROOT = Path(__file__).resolve().parents[1]

# 直接執行測試檔時, 將專案根目錄加入模組搜尋路徑
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from thumbnails import ThumbnailThread, ensure_thumbnail


class ThumbnailTests(unittest.TestCase):
    """
    縮圖快取與背景工作
    """

    def setUp(self) -> None:
        """
        建立暫存媒體目錄與縮圖路徑
        """

        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        self.media_dir = Path(self.temp_dir.name) / "media"
        self.media_dir.mkdir()
        self.thumbnail_dir = Path(self.temp_dir.name) / "thumbnails"


    def save_image(self, path: Path, color) -> None:
        """
        建立指定顏色的測試圖片
        """

        image = QImage(32, 18, QImage.Format.Format_RGB32)
        image.fill(color)
        self.assertTrue(image.save(str(path)))


    def thumbnail(self, path: Path) -> Path:
        """
        使用測試路徑取得或產生縮圖
        """

        return ensure_thumbnail(
            path,
            lambda: False,
            media_dir=self.media_dir,
            thumbnail_dir=self.thumbnail_dir
        )


    def test_background_worker_uses_supplied_directories(self):
        """
        背景縮圖工作使用指定的媒體與快取目錄
        """

        source = self.media_dir / "nested" / "sample.bmp"
        source.parent.mkdir()
        self.save_image(source, Qt.GlobalColor.red)


        ready = threading.Event()
        results = []
        worker = ThumbnailThread(self.media_dir, self.thumbnail_dir)

        def receive(media_path, image):

            results.append((media_path, image))
            ready.set()


        # 直接接收結果並以 Event 等待
        worker.thumbnail_ready.connect(receive, Qt.ConnectionType.DirectConnection)

        try:
            worker.request(source)
            self.assertTrue(ready.wait(5), "背景縮圖工作未回傳結果")

        finally:
            worker.stop()


        self.assertFalse(worker.isRunning())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], str(source))
        self.assertEqual(results[0][1].width(), 300)
        self.assertEqual(len(list((self.thumbnail_dir / "nested").glob("*.jpg"))), 1)


    def test_long_names_with_same_content_have_independent_caches(self):
        """
        相同內容的長檔名各自使用獨立快取
        """

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
        """
        清理過期快取時保留無關檔案與目錄
        """

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
        """
        使用目前演算法替換舊版縮圖快取
        """

        source = self.media_dir / "sample.bmp"
        self.save_image(source, Qt.GlobalColor.red)
        contents = source.read_bytes()
        # 建立舊版演算法的快取
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
        """
        替換快取失敗時保留舊檔並清除暫存檔
        """

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
