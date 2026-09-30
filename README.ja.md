# BB 動画壁紙

**[繁體中文](README.md) | [简体中文](README.cn.md) | 日本語 | [English](README.en.md)**

壁紙として動画を再生できる、軽量な壁紙管理ソフトウェアです。  
壁紙を1つのフォルダに保存し、いつでも簡単に切り替えることができます。  

- 軽量（約200MB）
- 各種の自動一時停止条件を選択可能（ウィンドウの最大化、画面オフ、バッテリー駆動時）
- Windows 起動時の自動起動に対応

### スクリーンショット

![スクリーンショット](./assets/screenshot.png)

# ダウンロード

### Releases から最新版をダウンロード

- [bb-video-wallpaper-setup.exe](https://github.com/BeefBB/bb-video-wallpaper/releases)

# 使い方

1. 「動画フォルダーを開く」
2. フォルダに動画を入れる
3. 「動画を選択」

# 自分でビルドする場合

## 必要なもの

- [VLC](https://www.videolan.org/)

### `VLC` を適切な場所に配置

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

## 実行

```bash
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

```bash
pyinstaller --noconfirm --onedir --noconsole --uac-admin --icon=icon/bb-video-wallpaper.ico --name="BB Video Wallpaper" --add-data "icon;icon" bb-video-wallpaper.py
```

ビルド後のファイルは `./dist` に生成されます。  
その後、`lang/`、`VLC/`、`libvlc.dll` を `.exe` と同じ場所に配置してください。  
以下のような構成になります。  

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

`bb-video-wallpaper-setup.exe` には、**BB 動画壁紙**での動画再生に必要なコンポーネントのみを残した、削減版の **VLC 3.0.24** が含まれています。  
VLC はオープンソースソフトウェアです。関連する著作権およびライセンス情報については、`インストール先/VLC/` にある VLC のライセンス文書をご確認ください。  

# ライセンス

BB 動画壁紙は MIT License を採用しています。  
ソフトウェアに含まれる VLC 3.0.24 の関連コンポーネントは、VLC に適用されるライセンス条件に従って提供されます。  
