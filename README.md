# BB 影片桌布

**繁體中文 | [简体中文](README.cn.md) | [日本語](README.ja.md) | [English](README.en.md)**

能在桌布播放影片, 同時也是一個極輕量的桌布管理軟體  
將桌布保存在一個資料夾中, 以便隨時切換  

- 輕量 (~200MB)
- 紀錄最近使用的桌布
- 可選各種自動暫停時機 (視窗最大化, 螢幕關閉, 沒插電)
- 可選開機自動啟動

### 截圖

![截圖](./assets/screenshot.png)

# 下載

### 到 Releases 下載最新版

- [bb-video-wallpaper-setup.exe](https://github.com/BeefBB/bb-video-wallpaper/releases)

# 使用

1. 在系統匣圖示上按右鍵, 選擇 "新增影片"
2. 選取影片或圖片
3. 之後可從 "選擇影片" 切換桌布

也可以透過 "開啟影片資料夾" 手動放入影片或圖片, 再從 "選擇影片" 選取  

> "清除紀錄" 會清除最近使用紀錄並保留目前桌布, 不會刪除檔案  

## 日誌

遇到問題時, 可查看 `%LOCALAPPDATA%\BB\BB Video Wallpaper\log\log.log`,  
日誌記錄操作資訊, 警告與錯誤, 已捕捉的例外包含完整錯誤堆疊  

# 想自己編譯?

## 需求

- [VLC](https://www.videolan.org/)

### 把 `VLC` 放在適當位置

```
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
 ├─ config.py
 ├─ media_import.py
 ├─ media_preview.py
 ├─ windows_integration.py
 ├─ thumbnails.py
 ├─ libvlc.dll
 └─ requirements.txt
```

## 執行

```bash
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

```bash
pyinstaller --noconfirm --onedir --noconsole --uac-admin --icon=icon/bb-video-wallpaper.ico --name="BB Video Wallpaper" --add-data "icon;icon" bb-video-wallpaper.py
```

打包後會在 `./dist`,  
然後將 `lang/`, `VLC/`, `libvlc.dll` 放在 .exe 旁邊  
像是這樣:  

```
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

`bb-video-wallpaper-setup.exe` 內含經刪減的 **VLC 3.0.24**, 僅保留 BB 影片桌布 播放影片所需的元件  
VLC 為開源軟體, 相關版權與授權資訊請參閱 `安裝目錄/VLC/` 中的 VLC 授權文件  

# 版權

BB 影片桌布 採用 MIT License  
軟體內含的 VLC 3.0.24 相關元件依 VLC 所適用的授權條款提供  
