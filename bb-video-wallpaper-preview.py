import json
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QFont, QCursor

from config import SUPPORTED_LANGUAGES


MenuItem = str | tuple[str, bool] | None


def add_previews(
    menu: QMenu, code: str, text: dict[str, str], chinese_text: dict[str, str], multiple: bool,
) -> None:

    prefix = "多螢幕-" if multiple else "單螢幕-"
    screens = tuple(f'{text["monitor"]} {number}' for number in range(1, 4))
    preview_titles = chinese_text | {
        screen: f'{chinese_text["monitor"]} {number}' for number, screen in enumerate(screens, 1)
    }
    submenus = {
        "select_video", "options", "advanced", "font_size", "preview_size", "auto_pause", "language",
        "hardware_decoding", "reset_defaults", "scaling_mode", "background_color", "follow_monitor",
        *screens,
    }

    def add_preview(name: str, *items: MenuItem) -> None:

        title = preview_titles.get(name, name).partition(" (")[0]
        preview = menu.addMenu(prefix + title)

        for item in items:
            if item is None:
                preview.addSeparator()
            elif isinstance(item, tuple):
                label, checked = item
                action = preview.addAction(text.get(label, label))
                action.setCheckable(True)
                action.setChecked(checked)
            elif item in submenus:
                preview.addMenu(text.get(item, item))
            else:
                preview.addAction(text.get(item, item))

    if multiple:
        main_items = ("all_play", "all_pause", "all_stop", None, *screens)
    else:
        main_items = (("play", True), "pause", "stop", None, "select_video")

    add_preview(
        "主選單", *main_items, "add_media", "open_video_folder", None,
        "options", None, "advanced", None, "exit",
    )

    add_preview(
        "options", "font_size", "preview_size", None, "auto_pause", None, "language", None, ("startup", False),
    )

    add_preview("advanced", "open_config_file", "open_log_folder", "hardware_decoding", "reset_defaults")
    add_preview(
        "hardware_decoding", ("hardware_decoding_auto", True), ("hardware_decoding_d3d11", False),
        ("hardware_decoding_dxva2", False), ("hardware_decoding_disabled", False),
    )
    add_preview("reset_defaults", "confirm_reset_defaults")

    files = (
        "scaling_mode", None, ("Ocean.mp4", True), ("City Lights.mp4", False),
        ("Forest Walk.mp4", False), "clear_recent_videos", None,

        ("City Lights.mp4", False), ("Clouds.jpg", False), ("Forest Walk.mp4", False),
        ("Mountain Lake.mp4", False), ("Ocean.mp4", True), ("Rainy Night.mp4", False),
        ("Sunset Drive.mp4", False),
    )
    add_preview("select_video", *files)

    if multiple:
        for screen in screens:
            add_preview(screen, ("play", True), "pause", "stop", None, "follow_monitor", *files)

        # 以最後一台螢幕為例, 跟隨清單排除自己
        add_preview("follow_monitor", *((screen, screen == screens[0]) for screen in screens[:-1]))

    add_preview(
        "scaling_mode", ("scaling_fill", False), ("scaling_fit", True),
        ("scaling_stretch", False), None, "background_color",
    )

    colors = ((key, False) for key in text if key.startswith("color_"))
    add_preview("background_color", ("background_color_auto", True), *colors)

    sizes = ("size_largest", "size_large", "size_normal", "size_small", "size_smallest")
    add_preview("font_size", *((key, key == "size_small") for key in sizes))
    add_preview("preview_size", *((key, key == "size_normal") for key in sizes), ("preview_disabled", False))

    add_preview(
        "auto_pause", ("auto_pause_fullscreen", True), ("auto_pause_screen_off", True),
        ("auto_pause_battery", False),
    )

    add_preview("language", *((name, code == language) for name, language in SUPPORTED_LANGUAGES.items()))


def main() -> int:

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    font = QFont()
    font.setPointSize(11)
    app.setFont(font)

    base_dir = Path(__file__).resolve().parent
    tray = QSystemTrayIcon()
    tray.setIcon(QIcon(str(base_dir / "icon" / "bb-video-wallpaper.ico")))
    tray.setToolTip("BB 影片桌布")

    menu = QMenu()
    menu.aboutToShow.connect(
        lambda: menu.activateWindow() if menu.isVisible() else None,
        Qt.ConnectionType.QueuedConnection
    )

    language_names = {
        "zh-TW": "繁體中文",
        "zh-CN": "簡體中文",
        "ja-JP": "日文",
        "ko-KR": "韓文",
        "en-US": "英文",
    }
    texts = {}
    for code in SUPPORTED_LANGUAGES.values():
        with (base_dir / "lang" / f"{code}.json").open(encoding="utf-8") as language_file:
            texts[code] = json.load(language_file)

    for code, text in texts.items():
        language_menu = menu.addMenu(language_names[code])
        add_previews(language_menu, code, text, texts["zh-TW"], multiple=False)
        add_previews(language_menu, code, text, texts["zh-TW"], multiple=True)

    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: menu.popup(QCursor.pos())
        if reason == QSystemTrayIcon.ActivationReason.Trigger
        else None
    )

    tray.show()

    return app.exec()


if __name__ == "__main__":

    sys.exit(main())
