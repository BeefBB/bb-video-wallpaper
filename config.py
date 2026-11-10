"""
設定檔初始化, 驗證, 讀寫與語言載入
路徑由主程式提供
"""

import json
import locale
import logging
import os
import re
from configparser import ConfigParser, Error as ConfigParserError
from pathlib import Path

from screen_follow import FollowGraph, valid_screen_id


logger = logging.getLogger(__name__)


CONFIG_DEFAULTS = {
    "autoPauseIfFullscreen": True,
    "autoPauseIfScreenOff": True,
    "autoPauseIfOnBattery": False,
}

CONFIG_KEYS = set(CONFIG_DEFAULTS) | {
    "fontSize", "recentDisplayCount", "language", "recentVideo",
    "screenWallpapers", "screenFollows",
}

SUPPORTED_LANGUAGES = {
    "繁體中文": "zh-TW",
    "简体中文": "zh-CN",
    "日本語": "ja-JP",
    "한국어": "ko-KR",
    "English": "en-US",
}


def _punctuation_candidates(text: str, position: int) -> set[str]:
    """
    在錯誤, 未閉合括號與巢狀設定欄位附近嘗試修改, 不更動字串內的內容
    """

    punctuation = ",:{}[]"
    positions = {position}
    previous = position - 1

    while previous >= 0 and text[previous] in " \t\r\n":
        previous -= 1

    if previous >= 0 and text[previous] in punctuation:
        positions.add(previous)

    edit_positions = set()
    in_string = False
    escaped = False
    openings: list[int] = []
    field_pattern = re.compile(r'\s*"([^"\\]*)"\s*:')

    # 多掃描一個位置, 讓檔尾也能補入括號
    for index, char in enumerate(text + " "):

        # 解析錯誤可能出現在多餘起始括號之後, 也嘗試修改當時尚未閉合的括號
        if index == position and not in_string:
            edit_positions.update(openings)

        # 已知設定欄位出現在巢狀物件時, 也嘗試在它前面補上結尾括號
        if not in_string and char == "," and len(openings) > 1:
            field = field_pattern.match(text, index + 1)
            if field and field[1] in CONFIG_KEYS:
                edit_positions.add(index)

        if index in positions and not in_string:
            edit_positions.add(index)

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False

        elif char == '"':
            in_string = True

        elif char in "{[":
            openings.append(index)

        elif char in "}]" and openings:
            openings.pop()

    candidates = set()

    for index in edit_positions:
        prefix, suffix = text[:index], text[index:]

        for mark in punctuation:
            candidates.add(prefix + mark + suffix)

        if suffix and suffix[0] in punctuation:
            candidates.add(prefix + suffix[1:])
            for mark in punctuation:
                candidates.add(prefix + mark + suffix[1:])

    return candidates


class ConfigStore:
    """
    管理設定與語言檔, 建立物件時不讀寫檔案
    """

    def __init__(self, config_path: Path, media_dir: Path, lang_dir: Path) -> None:

        self.config_path = config_path
        self.media_dir = media_dir
        self.lang_dir = lang_dir


    def _read_config(self):
        """
        最多修復兩個標點, 依驗證異常數與修改數選擇唯一結果
        """

        with open(self.config_path, "r", encoding="utf-8") as f:
            text = f.read()

        try:
            return json.loads(text)

        except json.JSONDecodeError as error:
            original_error = error

        if not text.strip():
            raise original_error

        def unique_object(pairs):
            config = dict(pairs)
            if len(config) != len(pairs):
                raise ValueError("修復結果包含重複欄位")
            return config

        pending = [(text, original_error)]
        seen = {text}
        repaired = {}
        tokens = r'"(?:\\.|[^"\\])*"|[^"\s,:{}\[\]]+'
        original_tokens = re.findall(tokens, text)

        # 搜尋完兩層候選再評分, 讓異常較少的兩次修改也能被選中
        for count in range(1, 3):
            next_pending = []

            for source, error in pending:
                for candidate in _punctuation_candidates(source, error.pos):
                    if candidate in seen:
                        continue
                    seen.add(candidate)

                    try:
                        config = json.loads(candidate, object_pairs_hook=unique_object)

                    except json.JSONDecodeError as candidate_error:
                        next_pending.append((candidate, candidate_error))
                        continue

                    except ValueError:
                        continue

                    if not isinstance(config, dict):
                        continue

                    # 保留字串與原始值的邊界, 避免刪除標點後將 1,2 合併為 12
                    if re.findall(tokens, candidate) != original_tokens:
                        continue

                    key = json.dumps(config, ensure_ascii=False, sort_keys=True)
                    if key not in repaired:
                        _, issues = self.validate(config, log_issues=False)
                        repaired[key] = ((len(issues), count), config)

            pending = next_pending

        if repaired:
            best_score = min(result[0] for result in repaired.values())
            best = [result for result in repaired.values() if result[0] == best_score]

            if len(best) == 1:
                score, config = best[0]
                logger.warning(
                    "設定檔已在記憶體修復 %s 個標點, 驗證異常 %s 處",
                    score[1], score[0],
                )
                return config

        raise original_error


    def _normalize_media_path(self, value) -> str | None:
        """
        檢查並標準化媒體資料夾內的相對路徑, 不允許絕對路徑或跳出資料夾的路徑
        失敗時回傳 None
        """

        if not isinstance(value, str) or not value or "\0" in value:
            return None

        try:
            path = Path(value)

            if path.drive or path.root or not path.parts or ".." in path.parts:
                return None

            media_root = self.media_dir.resolve()

            if not (media_root / path).resolve().is_relative_to(media_root):
                return None

        except (OSError, ValueError, RuntimeError):
            return None

        return path.as_posix()


    def _relative_media_path(self, path: Path) -> str:
        """
        將媒體路徑轉為可保存的相對路徑, 無效時拒絕保存
        """

        media_path = self._normalize_media_path(path.relative_to(self.media_dir).as_posix())

        if media_path is None:
            raise ValueError("桌布路徑必須位於媒體資料夾內")

        return media_path


    def validate(self, raw, *, log_issues: bool = True) -> tuple[dict, list[str]]:
        """
        回傳修正後設定與異常清單, 缺少欄位與相容性轉換不計異常
        """

        issues: list[str] = []

        def report(message: str, *args) -> None:
            issues.append(message % args if args else message)
            if log_issues:
                logger.warning(message, *args)

        if not isinstance(raw, dict):
            report("設定檔最外層必須是物件, 使用預設設定")
            raw = {}

        config = raw.copy()


        # 自動暫停選項只接受布林值
        for key, default in CONFIG_DEFAULTS.items():

            if key not in config:
                config[key] = default

            elif not isinstance(config[key], bool):
                report("設定欄位 %s 型別錯誤, 使用預設值", key)
                config[key] = default


        # 字體大小使用整數 pt, 超出範圍時限制在 6 到 48
        font_size = config.get("fontSize", 11)
        if type(font_size) is not int:
            report("設定欄位 fontSize 無效, 使用預設值")
            font_size = 11
        elif not 6 <= font_size <= 48:
            report("設定欄位 fontSize 超出 6 到 48, 已限制範圍")
        config["fontSize"] = max(6, min(48, font_size))


        # 最近紀錄顯示筆數使用整數, 0 表示隱藏, 最多顯示 20 筆
        recent_display_count = config.get("recentDisplayCount", 6)
        if type(recent_display_count) is not int:
            report("設定欄位 recentDisplayCount 無效, 使用預設值")
            recent_display_count = 6
        elif not 0 <= recent_display_count <= 20:
            report("設定欄位 recentDisplayCount 超出 0 到 20, 已限制範圍")
        config["recentDisplayCount"] = max(0, min(20, recent_display_count))


        # 未指定語言時優先使用安裝語言, 再使用系統語言與繁體中文
        if "language" not in config:

            installer_config = ConfigParser(interpolation=None)

            try:
                installer_config.read(
                    self.lang_dir.parent / "install-language.ini",
                    encoding="utf-8-sig"
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
            report("設定欄位 language 無效, 使用繁體中文")
            config["language"] = "zh-TW"


        # 相容舊版單筆紀錄, 保留媒體資料夾內的相對路徑
        recent = config.get("recentVideo", [])

        if isinstance(recent, str):
            recent = [recent]

        normalized_recent = []
        seen = set()

        if isinstance(recent, list):

            for index, media_path in enumerate(recent):

                normalized = self._normalize_media_path(media_path)

                if normalized is None:
                    report("設定欄位 recentVideo[%s] 無效, 已移除", index)
                    continue

                path = Path(normalized)

                if path in seen:
                    report("設定欄位 recentVideo[%s] 重複, 已移除", index)

                elif len(normalized_recent) >= 20:
                    report("設定欄位 recentVideo[%s] 超過 20 筆上限, 已移除", index)

                else:
                    seen.add(path)
                    normalized_recent.append(path.as_posix())

        else:
            report("設定欄位 recentVideo 必須是清單, 使用空清單")

        config["recentVideo"] = normalized_recent


        # 各螢幕的桌布選擇
        screen_wallpapers = config.get("screenWallpapers", {})
        normalized_wallpapers = {}

        if isinstance(screen_wallpapers, dict):
            for screen_id, media_path in screen_wallpapers.items():
                if not valid_screen_id(screen_id):
                    report("設定欄位 screenWallpapers.%s 螢幕識別無效, 已移除", screen_id)
                    continue

                if screen_id in CONFIG_KEYS:
                    report("設定欄位 screenWallpapers.%s 應位於最外層, 已移除", screen_id)
                    continue

                normalized = self._normalize_media_path(media_path)

                if normalized is not None:
                    normalized_wallpapers[screen_id] = normalized

                else:
                    report("設定欄位 screenWallpapers.%s 媒體路徑無效, 已移除", screen_id)

        else:
            report("設定欄位 screenWallpapers 必須是物件, 使用空物件")

        config["screenWallpapers"] = normalized_wallpapers


        screen_follows = config.get("screenFollows", {})
        follows = {}

        if isinstance(screen_follows, dict):
            for follower, source in screen_follows.items():
                if isinstance(follower, str) and follower in CONFIG_KEYS:
                    report("設定欄位 screenFollows.%s 應位於最外層, 已移除", follower)
                else:
                    follows[follower] = source

        else:
            report("設定欄位 screenFollows 必須是物件, 使用空物件")

        config["screenFollows"] = FollowGraph.normalize(follows)
        for follower in follows:
            if follower not in config["screenFollows"]:
                report("設定欄位 screenFollows.%s 無效或形成循環, 已移除", follower)


        return config, issues


    def remember_recent(self, path: Path) -> None:
        """
        保存最近紀錄
        """

        media_path = self._relative_media_path(path)
        config = self.load()
        recent = config["recentVideo"]
        config["recentVideo"] = [media_path] + [
            item for item in recent if Path(item) != Path(media_path)
        ][:19]

        self.save(config)


    def remember_wallpaper(self, screen_id: str, path: Path) -> None:
        """
        保存單一螢幕的選擇
        """

        if not valid_screen_id(screen_id):
            raise ValueError("無效的螢幕識別")

        media_path = self._relative_media_path(path)
        config = self.load()
        config["screenWallpapers"][screen_id] = media_path
        self.save(config)


    def initialize_if_missing(self) -> None:
        """
        設定檔或欄位缺少時補齊預設值
        """

        try:
            config = self._read_config()

        except FileNotFoundError:
            config = {}

        except Exception:
            logger.exception("初始化 config 時讀取設定發生錯誤")
            return


        if not isinstance(config, dict):
            logger.warning("設定檔最外層必須是物件, 不寫回預設設定")
            return


        config, _ = self.validate(config)
        self.save(config)


    def load(self) -> dict:
        """
        讀取並驗證設定, 不自動寫回修正內容
        """

        try:
            config = self._read_config()

        except FileNotFoundError:
            config = {}

        except Exception:

            logger.exception("讀取config時發生錯誤")

            config = {}

        config, _ = self.validate(config)
        return config


    def save(self, config) -> bool:

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
            return True

        except Exception:
            logger.exception("儲存 config 時發生錯誤")

            try:
                if temp_path.exists():
                    temp_path.unlink()

            except Exception:
                pass

            return False


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
