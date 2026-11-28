import json
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QFont, QCursor, QPainter, QPixmap

from config import SUPPORTED_LANGUAGES


MenuItem = str | tuple[str, bool] | None

BACKGROUND_COLORS = {
    "color_black": "#000000",
    "color_PMS419_moonless_night": "#212322",
    "color_PMS425_sesame": "#54575a",
    "color_PMS423_metal": "#8a8d8e",
    "color_PMS427_concrete": "#d0d3d5",
    "color_PMS663_cloud": "#e6e1e7",
    "color_white": "#ffffff",
    "color_PMS7533_cocoa": "#483729",
    "color_PMS7623_brick": "#8f292b",
    "color_PMS176_cherry_blossom": "#ffb3bb",
    "color_PMS715_orange": "#f98e2c",
    "color_PMS2015_wood_plank": "#facfa9",
    "color_PMS7401_sand": "#f7e1a5",
    "color_PMS129_sunflower": "#f6cf3f",
    "color_PMS386_lemon": "#eaeb6c",
    "color_PMS2268_leaf": "#75d87e",
    "color_PMS5565_sage": "#7d9c91",
    "color_PMS2244_forest": "#0e7f6f",
    "color_PMS278_sky": "#8ab8e9",
    "color_PMS655_deep_sea": "#002554",
    "color_PMS7445_taro": "#a8a2c3",
    "color_PMS2077_grape": "#6f40a2",
}


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

    def add_preview(
        name: str, *items: MenuItem, title_suffix: str = "", following: bool = False, disabled: tuple[str, ...] = (),
    ) -> None:

        title = preview_titles.get(name, name).partition(" (")[0]
        preview = menu.addMenu(prefix + title + title_suffix)

        for item in items:
            if item is None:
                preview.addSeparator()
                continue

            if isinstance(item, tuple):
                label, checked = item
                action = preview.addAction(text.get(label, label))

                background = BACKGROUND_COLORS.get(label)
                if background is not None:

                    swatch = QPixmap(16, 16)
                    swatch.fill(QColor("#808080"))
                    painter = QPainter(swatch)
                    painter.fillRect(1, 1, 14, 14, QColor(background))
                    painter.end()

                    action.setIcon(QIcon(swatch))

                action.setCheckable(True)
                action.setChecked(checked)
            else:
                label = item
                if label in submenus:
                    action = preview.addMenu(text.get(label, label)).menuAction()
                    if label == "follow_monitor":
                        action.setCheckable(True)
                        action.setChecked(following)
                else:
                    action = preview.addAction(text.get(label, label))

            action.setEnabled(label not in disabled)

    if multiple:
        main_items = ("all_play", "all_pause", "all_stop", None, *screens)
    else:
        main_items = (("play", True), ("pause", False), ("stop", False), None, "select_video")

    add_preview(
        "主選單", *main_items, "add_media", "open_video_folder", None,
        "options", None, "advanced", None, "exit",
    )

    add_preview(
        "options", "font_size", "preview_size", None, "auto_pause", None, "language", None, ("startup", False),
    )

    add_preview(
        "advanced", "open_config_file", "open_log_folder", "clear_image_cache", "hardware_decoding", "reset_defaults",
    )
    add_preview(
        "hardware_decoding", ("hardware_decoding_auto", True), ("hardware_decoding_d3d11", False),
        ("hardware_decoding_dxva2", False), ("hardware_decoding_disabled", False),
    )
    add_preview("reset_defaults", "confirm_reset_defaults")

    recent = ("Clouds.jpg", "Ocean.mp4", "City Lights.mp4", "Forest Walk.mp4")
    files = (
        "City Lights.mp4", "Clouds.jpg", "Forest Walk.mp4", "Mountain Lake.mp4",
        "Ocean.mp4", "Rainy Night.mp4", "Sunset Drive.mp4",
    )

    def media_items(selected: str | None) -> tuple[MenuItem, ...]:

        return (
            "scaling_mode", "background_color", None,
            *((file, file == selected) for file in recent), "clear_recent_videos", None,
            *((file, file == selected) for file in files),
        )

    if multiple:
        add_preview(
            screens[0], ("play", True), ("pause", False), ("stop", False),
            None, "follow_monitor", *media_items("Ocean.mp4"),
        )
        add_preview(
            screens[1], ("play", True), ("pause", False), ("stop", False),
            None, "follow_monitor", *media_items("Clouds.jpg"), disabled=("pause",),
        )
        add_preview(
            screens[2], ("play", False), ("pause", False), ("stop", False),
            None, "follow_monitor", *media_items(None), following=True,
        )

        # 以最後一台螢幕為例, 跟隨清單排除自己
        add_preview("follow_monitor", *((screen, screen == screens[0]) for screen in screens[:-1]))
    else:
        add_preview("select_video", *media_items("Ocean.mp4"))

    add_preview(
        "scaling_mode", ("scaling_fill", False), ("scaling_fit", True), ("scaling_stretch", False),
    )

    backgrounds = (("background_color_auto", True), ("background_blur", False))
    colors = tuple((key, False) for key in BACKGROUND_COLORS)
    add_preview("background_color", *backgrounds, None, *colors, title_suffix=" (影片)")
    add_preview(
        "background_color", *backgrounds, ("background_ray1", False), ("background_ray2", False), None, *colors,
        title_suffix=" (圖片)",
    )

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
