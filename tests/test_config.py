"""
測試設定讀寫失敗時的資料保護與媒體路徑邊界
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

        self.enterContext(mock.patch.object(locale, "getdefaultlocale", return_value=("en_US", "UTF-8")))


    def test_initialization_write_failure_keeps_defaults_available(self) -> None:
        """
        初始化寫入失敗後仍可讀取預設設定與語言
        """

        expected = {
            **CONFIG_DEFAULTS,
            "fontSize": 11,
            "recentDisplayCount": 6,
            "previewSize": 3,
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


    def test_paths_reject_outside_locations_and_keep_missing_media(self) -> None:
        """
        歷史與各螢幕選擇共用路徑邊界, 不因檔案暫時遺失而移除紀錄
        """

        valid = ["video/old.mp4", "missing.png"]
        paths = valid + [None, "", "../outside.mp4", "video/../../outside.mp4",
                         str(self.media_dir / "absolute.mp4"), "bad\0.mp4"]
        if os.name == "nt":
            paths += ["C:relative.mp4", "\\rooted.mp4", "\\\\server\\share\\file.mp4"]

        selections = {str(index): path for index, path in enumerate(paths)}
        result, _ = self.store.validate({"recentVideo": paths, "screenWallpapers": selections})
        self.assertEqual(result["recentVideo"], valid)
        self.assertEqual(result["screenWallpapers"], {"0": valid[0], "1": valid[1]})


    def test_invalid_selection_keeps_file(self) -> None:
        """
        無效螢幕識別或越界路徑不改寫已保存的設定
        """

        self.store.save({"screenWallpapers": {"screen-a": "a.mp4"}, "futureOption": 9})
        original = self.config_path.read_bytes()
        for screen_id, path in (
            ("", self.media_dir / "b.mp4"),
            ("bad\0id", self.media_dir / "b.mp4"),
            ("screen-a", self.media_dir / ".." / "outside.mp4"),
        ):
            with self.subTest(screen_id=screen_id, path=path):
                with self.assertRaises(ValueError):
                    self.store.remember_wallpaper(screen_id, path)
                self.assertEqual(self.config_path.read_bytes(), original)


    def test_loading_and_initialization_validate_config(self) -> None:
        """
        讀取不改寫原檔, 初始化只對物件設定寫回補齊與修復的欄位
        """

        defaults, _ = self.store.validate({})
        repaired = {**defaults, "autoPauseIfOnBattery": True, "language": "ja-JP", "futureOption": 9}
        invalid_values = json.dumps({
            "autoPauseIfFullscreen": "false",
            "autoPauseIfOnBattery": True,
            "previewSize": True,
            "language": "ja-JP",
            "futureOption": 9,
        }).encode("utf-8")
        cases = [(contents, defaults, False) for contents in (b"", b"{broken", b"\xff", b"[]", b"null")]
        cases.extend([
            (invalid_values, repaired, True),
            (json.dumps(repaired).encode("utf-8"), repaired, True),
        ])
        for contents, expected, write_back in cases:
            with self.subTest(contents=contents):
                self.config_path.write_bytes(contents)
                self.assertEqual(self.store.load(), expected)
                self.assertEqual(self.config_path.read_bytes(), contents)

                self.store.initialize_if_missing()

                if write_back:
                    self.assertEqual(
                        json.loads(self.config_path.read_text(encoding="utf-8")),
                        expected
                    )

                else:
                    self.assertEqual(self.config_path.read_bytes(), contents)


    def test_save_failure_preserves_previous_file(self) -> None:
        """
        寫入或替換失敗時保留原檔並清除暫存檔
        """

        self.assertTrue(self.store.save({"language": "zh-TW"}))
        original = self.config_path.read_bytes()
        for target in ("builtins.open", "config.os.replace"):
            with self.subTest(target=target):
                with mock.patch(target, side_effect=PermissionError):
                    self.assertFalse(self.store.save({"language": "ja-JP"}))
                self.assertEqual(self.config_path.read_bytes(), original)
                self.assertFalse(self.config_path.with_suffix(".tmp").exists())


    def test_punctuation_repairs_preserve_settings(self) -> None:
        """
        缺少逗號或多餘起始括號可修復, 讀取不寫回, 初始化才保存
        """

        cases = [
            ('{"fontSize":18"language":"ja-JP"}', {"fontSize": 18, "language": "ja-JP"}),
            (
                '{"recentVideo":[["folder/[1],{2}.mp4"],"language":"ja-JP"}',
                {"recentVideo": ["folder/[1],{2}.mp4"], "language": "ja-JP"},
            ),
        ]

        for text, expected in cases:
            with self.subTest(text=text):
                self.config_path.write_text(text, encoding="utf-8")
                config = self.store.load()
                for key, value in expected.items():
                    self.assertEqual(config[key], value)
                self.assertEqual(self.config_path.read_text(encoding="utf-8"), text)

                self.store.initialize_if_missing()
                self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8")), config)


    def test_validation_scores_repair_candidates(self) -> None:
        """
        缺少欄位不計異常, 修復優先選擇驗證異常較少的候選
        """

        _, issues = self.store.validate({})
        self.assertEqual(issues, [])
        _, issues = self.store.validate({"fontSize": "large", "language": "invalid"})
        self.assertEqual(len(issues), 2)

        text = '{"screenWallpapers":{"0":"a.mp4","language":"ja-JP"],"fontSize":18}'
        self.config_path.write_text(text, encoding="utf-8")
        config = self.store.load()
        self.assertEqual(config["language"], "ja-JP")
        self.assertEqual(config["screenWallpapers"], {"0": "a.mp4"})
        self.assertEqual(config["fontSize"], 18)


    def test_uncertain_repairs_keep_original_file(self) -> None:
        """
        同分歧義, 改變數值或超過兩次修改的內容不自動寫回
        """

        cases = [
            '{"futureOption": [1}',
            '{"futureOption": 1,2}',
            '{"fontSize" 18 "language" "ja-JP"}',
        ]
        defaults, _ = self.store.validate({})

        for text in cases:
            with self.subTest(text=text):
                self.config_path.write_text(text, encoding="utf-8")
                self.assertEqual(self.store.load(), defaults)
                self.store.initialize_if_missing()
                self.assertEqual(self.config_path.read_text(encoding="utf-8"), text)


if __name__ == "__main__":
    unittest.main()
