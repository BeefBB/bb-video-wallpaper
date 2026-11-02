"""
測試設定檔的初始化, 讀取, 驗證與儲存
只在暫存資料夾操作, 不啟動桌布或修改使用者設定
"""

import json
import locale
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]

# 直接執行測試檔時, 將專案根目錄加入模組搜尋路徑
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from config import CONFIG_DEFAULTS, ConfigStore


class ConfigTests(unittest.TestCase):
    """
    設定檔的初始化, 驗證與儲存
    """

    def setUp(self) -> None:
        """
        建立暫存設定檔路徑並固定系統語系
        """

        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        self.config_path = Path(self.temp_dir.name) / "config.json"
        self.media_dir = Path(self.temp_dir.name) / "video"
        self.media_dir.mkdir()

        self.store = ConfigStore(self.config_path, self.media_dir, ROOT / "lang")
        self.enterContext(mock.patch("config.logger"))

        patcher = mock.patch.object(
            locale,
            "getdefaultlocale",
            return_value=("en_US", "UTF-8")
        )
        patcher.start()
        self.addCleanup(patcher.stop)


    def test_initialization_preserves_existing_files(self):
        """
        保留已存在的設定檔, 包含損壞或空白檔案
        """

        existing = json.dumps({
            "autoPauseIfFullscreen": False,
            "language": "ja-JP",
            "recentVideo": ["custom.mp4"],
            "futureOption": 9,
        }).encode("utf-8")

        for contents in (existing, b"", b"{broken", b"\xff", b"[]", b"null"):

            with self.subTest(contents=contents):
                self.config_path.write_bytes(contents)

                with mock.patch.object(self.store, "save") as save:
                    self.store.initialize_if_missing()
                    self.store.initialize_if_missing()

                save.assert_not_called()
                self.assertEqual(self.config_path.read_bytes(), contents)


    def test_initialization_write_failure_keeps_defaults_available(self):
        """
        初始化寫入失敗後仍可讀取預設設定與語言
        """

        expected = {
            **CONFIG_DEFAULTS,
            "language": "en-US",
            "recentVideo": [],
            "screenWallpapers": {},
            "screenFollows": {},
        }
        language = json.loads(
            (self.store.lang_dir / "en-US.json").read_text(encoding="utf-8")
        )

        for target in ("builtins.open", "config.os.replace"):

            with self.subTest(target=target):

                with mock.patch(target, side_effect=PermissionError):
                    self.store.initialize_if_missing()

                self.assertFalse(self.config_path.exists())
                self.assertFalse(self.config_path.with_suffix(".tmp").exists())
                self.assertEqual(self.store.load(), expected)
                self.assertEqual(self.store.load_language(), language)


    def test_only_invalid_boolean_is_replaced(self):
        """
        只修復無效的布林值, 保留其他設定
        """

        baseline = {
            "autoPauseIfFullscreen": False,
            "autoPauseIfScreenOff": False,
            "autoPauseIfOnBattery": True,
            "language": "ja-JP",
            "recentVideo": ["missing.mp4"],
            "screenWallpapers": {},
            "futureOption": {"enabled": True},
            "screenFollows": {},
        }

        for key, default in CONFIG_DEFAULTS.items():

            for invalid in (None, 0, 1, "false", [], {}):

                with self.subTest(key=key, value=invalid):
                    raw = {**baseline, key: invalid}
                    result = self.store.validate(raw)

                    self.assertEqual(result, {**baseline, key: default})
                    self.assertEqual(raw[key], invalid)


    def test_legacy_recent_video_and_invalid_types(self):
        """
        相容舊版最近紀錄格式, 排除無效型別
        """

        result = self.store.validate({"recentVideo": "video/old.mp4"})
        self.assertEqual(result["recentVideo"], ["video/old.mp4"])

        for invalid in (None, False, 123, {}):

            with self.subTest(value=invalid):
                result = self.store.validate({"recentVideo": invalid})
                self.assertEqual(result["recentVideo"], [])


    def test_recent_video_filters_paths_and_keeps_missing_files(self):
        """
        排除無效或越界路徑, 保留尚不存在的媒體紀錄
        """

        recent = [
            "video/first.mp4",
            None,
            3,
            "",
            ".",
            "../outside.mp4",
            "video/../../outside.mp4",
            str(self.media_dir / "absolute.mp4"),
            "bad\0.mp4",
            "video/first.mp4",
            "missing.mp4",
        ]

        if os.name == "nt":
            recent.extend([
                "C:relative.mp4",
                "\\rooted.mp4",
                "\\\\server\\share\\file.mp4",
            ])

        result = self.store.validate({"recentVideo": recent})
        self.assertEqual(result["recentVideo"], ["video/first.mp4", "missing.mp4"])


    def test_screen_wallpapers_validate_paths_and_preserve_missing_or_disconnected_entries(self):
        """
        各螢幕路徑使用相同驗證, 保留暫時不存在的檔案與未連接螢幕
        """

        paths = {
            "screen-a": "video/first.mp4",
            "disconnected-screen": "missing.png",
            "normalized": "./video/second.mp4",
            "outside": "../outside.mp4",
            "absolute": str(self.media_dir / "absolute.mp4"),
            "bad-value": None,
            "bad-nul": "bad\0.png",
            "": "empty-id.mp4",
            "bad\0id": "bad-id.mp4",
            123: "numeric-id.mp4",
        }
        result = self.store.validate({"screenWallpapers": paths, "recentVideo": ["recent.mp4"]})

        self.assertEqual(result["screenWallpapers"], {
            "screen-a": "video/first.mp4",
            "disconnected-screen": "missing.png",
            "normalized": "video/second.mp4",
        })
        self.assertEqual(result["recentVideo"], ["recent.mp4"])
        self.assertEqual(paths["normalized"], "./video/second.mp4")

        for invalid in (None, False, 123, [], "file.mp4"):
            with self.subTest(value=invalid):
                self.assertEqual(self.store.validate({"screenWallpapers": invalid})["screenWallpapers"], {})


    def test_initial_selection_preserves_shared_history_and_other_settings(self):
        """
        初始選擇保存時保留共用最近紀錄, 其他螢幕與無關設定
        """

        self.store.save({
            "recentVideo": ["recent.mp4", "older.png"],
            "screenWallpapers": {"screen-a": "a.mp4"},
            "futureOption": {"enabled": True},
        })
        self.store.remember_wallpaper("screen-b", self.media_dir / "older.png")

        config = self.store.load()
        self.assertEqual(config["screenWallpapers"], {"screen-a": "a.mp4", "screen-b": "older.png"})
        self.assertEqual(config["recentVideo"], ["recent.mp4", "older.png"])
        self.assertEqual(config["futureOption"], {"enabled": True})


    def test_invalid_selection_does_not_overwrite_saved_config(self):
        """
        無效螢幕識別或越界路徑不改寫原有選擇
        """

        self.store.remember_wallpaper("screen-a", self.media_dir / "a.mp4")
        original = self.config_path.read_bytes()

        for screen_id, path in (
            ("", self.media_dir / "b.mp4"),
            ("bad\0id", self.media_dir / "b.mp4"),
            ("screen-a", self.media_dir.parent / "outside.mp4"),
            ("screen-a", self.media_dir / ".." / "outside.mp4"),
        ):
            with self.subTest(screen_id=screen_id, path=path):
                with self.assertRaises(ValueError):
                    self.store.remember_wallpaper(screen_id, path)
                self.assertEqual(self.config_path.read_bytes(), original)


    def test_bad_json_encoding_and_root_preserve_original_file(self):
        """
        設定檔格式損壞時使用預設值並保留原檔
        """

        for contents in (b"", b"{broken", b"\xff", b"[]", b"null", b"1", b'"text"'):

            with self.subTest(contents=contents):
                self.config_path.write_bytes(contents)

                result = self.store.load()
                expected = self.store.validate({})

                self.assertEqual(result, expected)
                self.assertEqual(self.config_path.read_bytes(), contents)


    def test_load_repairs_only_in_memory(self):
        """
        讀取時只修復記憶體中的設定
        """

        contents = json.dumps({
            "autoPauseIfFullscreen": "false",
            "autoPauseIfOnBattery": True,
            "language": "ja-JP",
            "futureOption": 9,
        })
        self.config_path.write_text(contents, encoding="utf-8")

        result = self.store.load()

        self.assertIs(result["autoPauseIfFullscreen"], True)
        self.assertIs(result["autoPauseIfOnBattery"], True)
        self.assertEqual(result["language"], "ja-JP")
        self.assertEqual(result["futureOption"], 9)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), contents)


    def test_save_round_trip_and_replace_failure(self):
        """
        確認儲存結果可回讀, 更名失敗時保留原檔
        """

        original = self.store.validate({"language": "zh-TW"})
        self.store.save(original)
        self.assertEqual(self.store.load(), original)

        before = self.config_path.read_bytes()

        with mock.patch.object(os, "replace", side_effect=PermissionError):
            self.store.save({"language": "ja-JP"})

        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.config_path.with_suffix(".tmp").exists())


if __name__ == "__main__":
    unittest.main()
