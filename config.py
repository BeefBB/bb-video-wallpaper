"""
設定檔驗證, 讀寫與語言載入
路徑由主程式提供
"""

import json
import locale
import logging
import os
from configparser import ConfigParser, Error as ConfigParserError
from pathlib import Path


logger = logging.getLogger(__name__)


CONFIG_DEFAULTS = {
    "autoPauseIfFullscreen": True,
    "autoPauseIfScreenOff": True,
    "autoPauseIfOnBattery": False,
}

SUPPORTED_LANGUAGES = {
    "繁體中文": "zh-TW",
    "简体中文": "zh-CN",
    "日本語": "ja-JP",
    "English": "en-US",
}


class ConfigStore:
    """
    管理設定與語言檔, 建立物件時不讀寫檔案
    """

    def __init__(self, config_path: Path, media_dir: Path, lang_dir: Path):

        self.config_path = config_path
        self.media_dir = media_dir
        self.lang_dir = lang_dir


    def validate(self, raw) -> dict:
        """
        逐欄檢查設定, 異常欄位使用預設值
        """

        if not isinstance(raw, dict):
            logger.warning("設定檔最外層必須是物件, 使用預設設定")
            raw = {}

        config = raw.copy()


        # 自動暫停選項只接受布林值
        for key, default in CONFIG_DEFAULTS.items():

            if key not in config:
                config[key] = default

            elif not isinstance(config[key], bool):
                logger.warning("設定欄位 %s 型別錯誤, 使用預設值", key)
                config[key] = default


        # 未指定語言時優先使用安裝語言, 再使用系統語言與繁體中文
        if "language" not in config:

            installer_config = ConfigParser(interpolation=None)

            try:
                installer_config.read(
                    self.lang_dir.parent / "install-language.ini",
                    encoding="utf-8-sig",
                )
                language = installer_config.get("Setup", "Language", fallback="")

            except (OSError, UnicodeError, ConfigParserError):
                language = ""

            if language not in SUPPORTED_LANGUAGES.values():

                try:
                    language = str(locale.getdefaultlocale()[0]).replace("_", "-")

                except (ValueError, TypeError):
                    language = "zh-TW"

            if language not in SUPPORTED_LANGUAGES.values():
                language = "zh-TW"

            config["language"] = language

        language = config["language"]

        if (
            not isinstance(language, str) or
            language not in SUPPORTED_LANGUAGES.values()
        ):
            logger.warning("設定欄位 language 無效, 使用繁體中文")
            config["language"] = "zh-TW"


        # 相容舊版單筆紀錄, 保留媒體資料夾內的相對路徑
        recent = config.get("recentVideo", [])

        if isinstance(recent, str):
            recent = [recent]

        normalized_recent = []
        seen = set()

        if isinstance(recent, list):

            media_root = self.media_dir.resolve()

            for media_path in recent:

                if not isinstance(media_path, str) or not media_path or "\0" in media_path:
                    continue

                try:
                    path = Path(media_path)

                    if (
                        path.drive or
                        path.root or
                        not path.parts or
                        ".." in path.parts
                    ):
                        continue

                    if not (media_root / path).resolve().is_relative_to(media_root):
                        continue

                except (OSError, ValueError, RuntimeError):
                    continue

                if path not in seen:
                    seen.add(path)
                    normalized_recent.append(path.as_posix())

                    if len(normalized_recent) == 5:
                        break

        if normalized_recent != recent:
            logger.warning("設定欄位 recentVideo 已移除無效或重複項目, 最多保留 5 筆")

        config["recentVideo"] = normalized_recent


        return config


    def load(self) -> dict:
        """
        讀取並驗證設定, 不自動寫回修正內容
        """

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = json.load(f)

        except FileNotFoundError:
            config = {}

        except Exception:

            logger.exception("讀取config時發生錯誤")

            config = {}

        return self.validate(config)


    def save(self, config) -> None:

        temp_path = self.config_path.with_suffix(".tmp")

        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(
                    config,
                    f,
                    indent=2,
                    ensure_ascii=False
                )
                f.flush()
                os.fsync(f.fileno())

            os.replace(temp_path, self.config_path)

        except Exception:
            logger.exception("儲存 config 時發生錯誤")

            try:
                if temp_path.exists():
                    temp_path.unlink()

            except Exception:
                pass


    def load_language(self, language_code: str = "") -> dict:

        config = self.load()

        if language_code:
            config["language"] = language_code
            self.save(config)

        else:
            language_code = config["language"]


        path = self.lang_dir / f"{language_code}.json"

        if not path.is_file():
            path = self.lang_dir / "zh-TW.json"

        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
