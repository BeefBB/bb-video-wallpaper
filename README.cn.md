# BB 视频壁纸

**[繁體中文](README.md) | 简体中文 | [日本語](README.ja.md) | [English](README.en.md)**

可以将视频作为壁纸播放，同时也是一款非常轻量的壁纸管理软件。  
将壁纸保存在一个文件夹中，以便随时切换。  

- 轻量（约 200MB）
- 可选择各种自动暂停时机（窗口最大化、屏幕关闭、未接通电源）
- 可选择开机自动启动

### 截图

![截图](./assets/screenshot.png)

# 下载

### 到 Releases 下载最新版

- [bb-video-wallpaper-setup.exe](https://github.com/BeefBB/bb-video-wallpaper/releases)

# 使用

1. 「打开视频文件夹」
2. 将视频放入文件夹
3. 「选择视频」

# 想自己编译？

## 需求

- [VLC](https://www.videolan.org/)

### 将 `VLC` 放在适当的位置

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

## 运行

```bash
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

```bash
pyinstaller --noconfirm --onedir --noconsole --uac-admin --icon=icon/bb-video-wallpaper.ico --name="BB Video Wallpaper" --add-data "icon;icon" bb-video-wallpaper.py
```

打包后会生成在 `./dist`。  
然后将 `lang/`、`VLC/`、`libvlc.dll` 放在 `.exe` 旁边。  
目录结构如下：  

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

`bb-video-wallpaper-setup.exe` 内含经过精简的 **VLC 3.0.24**，仅保留 **BB 视频壁纸**播放视频所需的组件。  
VLC 是开源软件，相关版权和许可证信息请参阅 `安装目录/VLC/` 中的 VLC 许可证文件。  

# 版权

BB 视频壁纸采用 MIT License。  
软件内含的 VLC 3.0.24 相关组件根据 VLC 所适用的许可证条款提供。  
