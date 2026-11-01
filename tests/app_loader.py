"""
以模組方式載入含連字號的主程式, 每次使用獨立的模組狀態
"""

import importlib.util
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]


def load_app_module() -> ModuleType:

    spec = importlib.util.spec_from_file_location("bb_video_wallpaper", ROOT / "bb-video-wallpaper.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
