# BB Video Wallpaper

**[繁體中文](README.md) | English**

Play videos as your desktop wallpaper while keeping it lightweight and simple to manage.

Store your wallpapers in a single folder, making it easy to switch between them at any time.

- Lightweight (~200 MB)
- Various automatic pause options (when a window is maximized, the screen is turned off, or the PC is running on battery)
- Optional launch on system startup

### Screenshot

![Screenshot](./assets/screenshot.png)

# Download

### Download the latest version from Releases

- [bb-video-wallpaper-setup.exe](https://github.com/BeefBB/bb-video-wallpaper/releases)

# Usage

1. Open the video folder
2. Put your videos into the folder
3. Select a video

# Want to Build It Yourself?

## Requirements

- [VLC](https://www.videolan.org/)

### Put `VLC` in the appropriate location

```text
bb-video-wallpaper/
 ├─ icon/
 │   └─ bb-video-wallpaper.ico
 ├─ lang/
 │   └─ ...
 ├─ VLC/
 │   ├─ plugins/
 │   ├─ libvlc.dll
 │   ├─ libvlccore.dll
 │   └─ ...
 ├─ bb-video-wallpaper.py
 ├─ libvlc.dll
 └─ requirements.txt
```

## Run

```bash
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

```bash
pyinstaller --noconfirm --onedir --noconsole --uac-admin --icon=icon/bb-video-wallpaper.ico --name="BB Video Wallpaper" --add-data "icon;icon" bb-video-wallpaper.py
```

After building, the output will be in `./dist`.

Then place `lang/`, `VLC/`, and `libvlc.dll` next to the `.exe`.

The final folder structure should look like this:

```text
Folder/
 ├─ _internal/
 │   └─ ...
 ├─ lang/
 │   └─ ...
 ├─ VLC/
 │   ├─ plugins/
 │   ├─ libvlc.dll
 │   ├─ libvlccore.dll
 │   └─ ...
 ├─ BB Video Wallpaper.exe
 └─ libvlc.dll
```

# VLC

`bb-video-wallpaper-setup.exe` includes a reduced version of **VLC 3.0.23**, containing only the components required by BB Video Wallpaper for video playback.

VLC is open-source software. For copyright and license information, please refer to the VLC license documents located in `Installation Directory/VLC/`.

# Copyright

BB Video Wallpaper is licensed under the MIT License.

The included VLC 3.0.23 components are provided under the applicable VLC license terms.
