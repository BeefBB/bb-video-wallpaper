"""
測試設定檔的讀取, 驗證與儲存
只在暫存資料夾操作, 不啟動桌布或修改使用者設定
"""

import ast
import json
import locale
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SOURCE_PATH = Path(__file__).resolve().parents[1] / "bb-video-wallpaper.py"
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")
NAMES = {
    "CONFIG_DEFAULTS",
    "SUPPORTED_LANGUAGES",
    "validate_config",
    "load_config",
    "save_config",
    "load_language",
}


def load_config_functions() -> dict:
    """
    只載入設定相關函式與常數, 避免主程式匯入時初始化日誌和 VLC
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


    logger = logging.getLogger("config_tests")
    logger.addHandler(logging.NullHandler())
    logger.propagate = False

    namespace = {
        "json": json,
        "locale": locale,
        "logger": logger,
        "os": os,
        "Path": Path,
    }

    module = ast.Module(body=nodes, type_ignores=[])
    code = compile(module, str(SOURCE_PATH), "exec")
    exec(code, namespace)

    return namespace


namespace = load_config_functions()


class ConfigTests(unittest.TestCase):

    def setUp(self) -> None:

        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        self.config_path = Path(self.temp_dir.name) / "config.json"
        self.media_dir = Path(self.temp_dir.name) / "video"
        self.media_dir.mkdir()

        namespace.update(
            CONFIG_PATH=self.config_path,
            MEDIA_DIR=self.media_dir,
            LANG_DIR=SOURCE_PATH.parent / "lang"
        )

        patcher = mock.patch.object(
            locale,
            "getdefaultlocale",
            return_value=("en_US", "UTF-8")
        )
        patcher.start()
        self.addCleanup(patcher.stop)


    def test_source_compiles(self):

        compile(SOURCE, str(SOURCE_PATH), "exec")


    def test_missing_file_uses_defaults_without_writing(self):

        config = namespace["load_config"]()
        expected = {
            **namespace["CONFIG_DEFAULTS"],
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

        for key, default in namespace["CONFIG_DEFAULTS"].items():

            for invalid in (None, 0, 1, "false", [], {}):

                with self.subTest(key=key, value=invalid):
                    raw = {**baseline, key: invalid}
                    result = namespace["validate_config"](raw)

                    self.assertEqual(result, {**baseline, key: default})
                    self.assertEqual(raw[key], invalid)


    def test_language_validation(self):

        for language in namespace["SUPPORTED_LANGUAGES"].values():
            result = namespace["validate_config"]({"language": language})
            self.assertEqual(result["language"], language)

        for invalid in (None, False, 1, [], {}, "", "xx-XX", "../other"):

            with self.subTest(value=invalid):
                result = namespace["validate_config"]({"language": invalid})
                self.assertEqual(result["language"], "zh-TW")


    def test_unsupported_or_unavailable_system_locale(self):

        for system_locale in ("fr_FR", None):

            with mock.patch.object(
                locale,
                "getdefaultlocale",
                return_value=(system_locale, None)
            ):
                result = namespace["validate_config"]({})
                self.assertEqual(result["language"], "zh-TW")

        with mock.patch.object(locale, "getdefaultlocale", side_effect=ValueError):
            result = namespace["validate_config"]({})
            self.assertEqual(result["language"], "zh-TW")


    def test_legacy_recent_video_and_invalid_types(self):

        result = namespace["validate_config"]({"recentVideo": "video/old.mp4"})
        self.assertEqual(result["recentVideo"], ["video/old.mp4"])

        for invalid in (None, False, 123, {}):

            with self.subTest(value=invalid):
                result = namespace["validate_config"]({"recentVideo": invalid})
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

        result = namespace["validate_config"]({"recentVideo": recent})
        self.assertEqual(result["recentVideo"], ["video/first.mp4", "missing.mp4"])


    def test_recent_video_deduplicates_and_limits_to_five(self):

        recent = ["first.mp4", "./first.mp4"] + [f"{i}.mp4" for i in range(7)]

        if os.name == "nt":
            recent.insert(1, "FIRST.mp4")

        result = namespace["validate_config"]({"recentVideo": recent})
        self.assertEqual(
            result["recentVideo"],
            ["first.mp4", "0.mp4", "1.mp4", "2.mp4", "3.mp4"]
        )

        result = namespace["validate_config"]({})
        self.assertEqual(result["recentVideo"], [])


    def test_bad_json_encoding_and_root_preserve_original_file(self):

        for contents in (b"", b"{broken", b"\xff", b"[]", b"null", b"1", b'"text"'):

            with self.subTest(contents=contents):
                self.config_path.write_bytes(contents)

                result = namespace["load_config"]()
                expected = namespace["validate_config"]({})

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

        result = namespace["load_config"]()

        self.assertIs(result["autoPauseIfFullscreen"], True)
        self.assertIs(result["autoPauseIfOnBattery"], True)
        self.assertEqual(result["language"], "ja-JP")
        self.assertEqual(result["futureOption"], 9)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), contents)


    def test_read_error_uses_defaults(self):

        with mock.patch.dict(namespace, {"open": mock.Mock(side_effect=PermissionError)}):
            result = namespace["load_config"]()
            expected = namespace["validate_config"]({})
            self.assertEqual(result, expected)


    def test_language_load_uses_validated_config(self):

        path = namespace["LANG_DIR"] / "en-US.json"
        expected = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(namespace["load_language"](), expected)

        self.config_path.write_text('{"language": []}', encoding="utf-8")

        path = namespace["LANG_DIR"] / "zh-TW.json"
        expected = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(namespace["load_language"](), expected)


    def test_save_round_trip_and_replace_failure(self):

        original = namespace["validate_config"]({"language": "zh-TW"})
        namespace["save_config"](original)
        self.assertEqual(namespace["load_config"](), original)

        before = self.config_path.read_bytes()

        with mock.patch.object(os, "replace", side_effect=PermissionError):
            namespace["save_config"]({"language": "ja-JP"})

        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertFalse(self.config_path.with_suffix(".tmp").exists())


if __name__ == "__main__":
    unittest.main()
