"""
測試設定檔的讀取, 驗證與儲存
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

from config import CONFIG_DEFAULTS, SUPPORTED_LANGUAGES, ConfigStore


class ConfigTests(unittest.TestCase):

    def setUp(self) -> None:

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


    def test_source_compiles(self):

        for name in ("bb-video-wallpaper.py", "config.py", "thumbnails.py"):

            source_path = ROOT / name

            with self.subTest(module=name):
                compile(source_path.read_text(encoding="utf-8"), str(source_path), "exec")


    def test_missing_file_uses_defaults_without_writing(self):

        config = self.store.load()
        expected = {
            **CONFIG_DEFAULTS,
            "language": "en-US",
            "recentVideo": [],
        }

        self.assertEqual(config, expected)
        self.assertFalse(self.config_path.exists())


    def test_only_invalid_boolean_is_replaced(self):

        baseline = {
            "autoPauseIfFullscreen": False,
            "autoPauseIfScreenOff": False,
            "autoPauseIfOnBattery": True,
            "language": "ja-JP",
            "recentVideo": ["missing.mp4"],
            "futureOption": {"enabled": True},
        }

        for key, default in CONFIG_DEFAULTS.items():

            for invalid in (None, 0, 1, "false", [], {}):

                with self.subTest(key=key, value=invalid):
                    raw = {**baseline, key: invalid}
                    result = self.store.validate(raw)

                    self.assertEqual(result, {**baseline, key: default})
                    self.assertEqual(raw[key], invalid)


    def test_language_validation(self):

        for language in SUPPORTED_LANGUAGES.values():
            result = self.store.validate({"language": language})
            self.assertEqual(result["language"], language)

        for invalid in (None, False, 1, [], {}, "", "xx-XX", "../other"):

            with self.subTest(value=invalid):
                result = self.store.validate({"language": invalid})
                self.assertEqual(result["language"], "zh-TW")


    def test_unsupported_or_unavailable_system_locale(self):

        for system_locale in ("fr_FR", None):

            with mock.patch.object(
                locale,
                "getdefaultlocale",
                return_value=(system_locale, None)
            ):
                result = self.store.validate({})
                self.assertEqual(result["language"], "zh-TW")

        with mock.patch.object(locale, "getdefaultlocale", side_effect=ValueError):
            result = self.store.validate({})
            self.assertEqual(result["language"], "zh-TW")


    def test_legacy_recent_video_and_invalid_types(self):

        result = self.store.validate({"recentVideo": "video/old.mp4"})
        self.assertEqual(result["recentVideo"], ["video/old.mp4"])

        for invalid in (None, False, 123, {}):

            with self.subTest(value=invalid):
                result = self.store.validate({"recentVideo": invalid})
                self.assertEqual(result["recentVideo"], [])


    def test_recent_video_filters_paths_and_keeps_missing_files(self):

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


    def test_recent_video_deduplicates_and_limits_to_five(self):

        recent = ["first.mp4", "./first.mp4"] + [f"{i}.mp4" for i in range(7)]

        if os.name == "nt":
            recent.insert(1, "FIRST.mp4")

        result = self.store.validate({"recentVideo": recent})
        self.assertEqual(
            result["recentVideo"],
            ["first.mp4", "0.mp4", "1.mp4", "2.mp4", "3.mp4"]
        )

        result = self.store.validate({})
        self.assertEqual(result["recentVideo"], [])


    def test_bad_json_encoding_and_root_preserve_original_file(self):

        for contents in (b"", b"{broken", b"\xff", b"[]", b"null", b"1", b'"text"'):

            with self.subTest(contents=contents):
                self.config_path.write_bytes(contents)

                result = self.store.load()
                expected = self.store.validate({})

                self.assertEqual(result, expected)
                self.assertEqual(self.config_path.read_bytes(), contents)


    def test_load_repairs_only_in_memory(self):

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


    def test_read_error_uses_defaults(self):

        with mock.patch("builtins.open", side_effect=PermissionError):
            result = self.store.load()
            expected = self.store.validate({})
            self.assertEqual(result, expected)


    def test_language_load_uses_validated_config(self):

        path = self.store.lang_dir / "en-US.json"
        expected = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(self.store.load_language(), expected)

        self.config_path.write_text('{"language": []}', encoding="utf-8")

        path = self.store.lang_dir / "zh-TW.json"
        expected = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(self.store.load_language(), expected)


    def test_save_round_trip_and_replace_failure(self):

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
