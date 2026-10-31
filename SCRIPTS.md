# 專案腳本說明

這份文件依準備環境, 日常開發, 測試與發佈的流程, 說明各腳本的用途及操作方式. 以下指令以 Windows PowerShell 為例, 工作目錄為專案根目錄

## 準備環境與啟動

需要 Windows, PATH 中可使用的 `python`, 以及完整的 Windows VLC 發行包. Python 與 VLC 的架構必須相容, 例如皆使用 64 位元版本; 只有 Python 套件仍不足以播放影片

### 產生精簡版 VLC

將完整 VLC 放入專案根目錄的 `VLC/`, 保留 DLL, 授權文件, `vlc-cache-gen.exe` 與 `plugins/` 等原始內容, 再執行 [create-vlc-lite.ps1](./create-vlc-lite.ps1):

```powershell
.\create-vlc-lite.ps1
```

腳本依複製清單保留播放所需的 DLL 與外掛, 包含處理影片旋轉的 `libtransform_plugin.dll`, 輸出至 `VLC-lite/`, 並重新產生外掛快取及顯示容量差異. 保留清單以腳本中的規則為準

**執行時會刪除既有的 `VLC-lite/`, 包括手動加入的檔案**. 需要長期保留的模組應加入複製規則. 更換 VLC 版本或調整規則後, 需重新產生精簡版並驗證播放與影片縮圖

若 PowerShell 執行原則阻擋腳本, 可只對這次執行指定:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\create-vlc-lite.ps1
```

### 啟動程式

雙擊 [run.bat](./run.bat), 或在 PowerShell 執行:

```powershell
.\run.bat
```

腳本會檢查管理員權限, 必要時提示提權, 再切換到專案根目錄. 首次執行會建立 `.venv/` 並安裝 [requirements.txt](./requirements.txt) 中的套件, 然後以虛擬環境啟動 [bb-video-wallpaper.py](./bb-video-wallpaper.py). 程式退出後會保留命令視窗, 方便查看輸出

[run.bat](./run.bat) 與 [build.bat](./build.bat) 都只在 `.venv/` 不存在時建立環境並安裝套件. 更新套件需求後, 已有的環境需手動更新:

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
```

環境準備完成後, 也可在以系統管理員身分開啟的 PowerShell 直接執行:

```powershell
.\.venv\Scripts\python.exe .\bb-video-wallpaper.py
```

主程式優先使用同層的 `VLC-lite/`, 找不到時才使用 `VLC/`. 兩者同時存在時, 更新完整 VLC 後仍需重新產生精簡版. `--task` 是開機排程使用的內部參數, 手動啟動不需要加上

### 執行時資料

使用者資料存放在 `%LOCALAPPDATA%\BB\BB Video Wallpaper\`:

路徑          | 用途
---           | ---
`config.json` | 語言, 自動暫停選項, 各螢幕桌布與共用最近使用紀錄
`video/`      | 媒體資料夾, 匯入檔案存放在其下的 `_imports/`
`thumbnails/` | 縮圖快取
`log/log.log` | 日誌, 達到上限後輪替

## 日常開發

依要修改的功能查找實作:

檔案                                               | 職責
---                                                | ---
[bb-video-wallpaper.py](./bb-video-wallpaper.py)   | 主程式組裝, VLC 初始化, 桌布生命週期, 系統匣與匯入介面, 自動暫停及桌面重建恢復
[config.py](./config.py)                           | 設定初始化, 驗證與讀寫, 語言檔載入
[media_import.py](./media_import.py)               | 媒體內容去重, 分塊複製, 容量檢查, 命名衝突與取消清理
[media_preview.py](./media_preview.py)             | 預覽選單, 懸停時序, 縮圖訊號與預覽視窗定位
[windows_integration.py](./windows_integration.py) | 單一實例, 螢幕識別, 桌面掛載與就緒檢查, 虛擬桌面及最大化視窗偵測, 電源通知與開機排程
[bb_image_display.py](./bb_image_display.py)       | Qt 圖片輸出, 原生子視窗管理與重綁
[thumbnails.py](./thumbnails.py)                   | 支援的媒體格式, 縮圖產生, 快取驗證與背景排隊

模組整合時需維持以下邊界:

- 模組不反向匯入主程式, 匯入時不建立使用者資料夾或設定日誌. 路徑, 翻譯函式與進度及取消回呼由呼叫端傳入; Windows 的 Mutex, 桌面掛載與排程操作也只在呼叫相關函式時執行
- Qt 元件共用主程式的 `QApplication`. 主程式負責桌布掛載, 媒體切換與退出時停止縮圖工作; 圖片模組管理內部子視窗, 預覽模組管理選單及縮圖顯示
- 主程式先完成 VLC DLL 載入設定. 影片縮圖只在實際使用時匯入 VLC, 其他模組測試可獨立執行

日常修改後, 重新啟動程式並執行相關測試即可. 更動 VLC 時需重建精簡版, 準備發佈時則接著打包與製作安裝檔

## 執行測試

測試使用 Python 內建的 `unittest`, 在 Windows 與專案套件準備完成後執行:

```powershell
.\test.bat
```

[test.bat](./test.bat) 會切換到專案根目錄, 必要時建立虛擬環境並安裝套件, 再執行全部測試. 結束後會保留視窗方便查看結果, 並回傳測試的結束碼

也可直接執行:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

只檢查一個範圍時, 可從下表選擇測試檔案單獨執行, 例如:

```powershell
.\.venv\Scripts\python.exe .\tests\test_config.py
```

### 測試範圍

測試腳本                                                           | 檢查重點
---                                                                | ---
[test_config.py](./tests/test_config.py)                           | 預設設定, 欄位與路徑驗證, 各螢幕桌布及紀錄, 語言載入與讀寫失敗處理
[test_media_import.py](./tests/test_media_import.py)               | 去重, 命名, 容量檢查, 進度與取消後清理
[test_import_menu.py](./tests/test_import_menu.py)                 | 匯入選單排列, 主執行緒進度更新, 取消及退出處理
[test_media_preview.py](./tests/test_media_preview.py)             | 懸停時序, 預覽模式, 過期縮圖, Qt 訊號與螢幕邊界
[test_playback_state.py](./tests/test_playback_state.py)           | 媒體切換與暫停, 載入時序, 桌布重掛排程, 重試及退出取消
[test_desktop_events.py](./tests/test_desktop_events.py)           | 隱藏頂層視窗接收桌面重建通知, 桌布容器銷毀後仍能接收通知
[test_video_recovery.py](./tests/test_video_recovery.py)           | 實際 VLC 影片輸出失效後的重建, 播放及暫停狀態與進度恢復
[test_windows_integration.py](./tests/test_windows_integration.py) | 模擬掛載, 螢幕識別, 電源及排程 API, 以隱藏視窗驗證備援容器
[test_image_display.py](./tests/test_image_display.py)             | 圖片讀取, 隱藏及顯示, 原生子視窗重綁與釋放
[test_thumbnails.py](./tests/test_thumbnails.py)                   | 快取命名, 獨立性, 舊快取替換, 清理範圍與儲存失敗處理
[test_video_thumbnails.py](./tests/test_video_thumbnails.py)       | 實際影片縮圖比例, 非正方形像素, 旋轉方向, 多影像軌, 取消及錯誤處理

### 測試環境與執行影響

一般測試使用暫存資料, 模擬 API 或隱藏的 Qt 元件, 不啟動桌布或改寫使用者設定. 預覽測試模擬游標與螢幕, 不移動實際滑鼠

原生視窗測試只操作測試建立的視窗及程序. 桌面通知只送給測試視窗; 影片重掛測試會結束自己的畫面外父視窗程序, 確認 VLC 能恢復輸出. 這些測試不廣播訊息, 不重啟 Explorer, 不建立實際開機排程或改變使用者的桌面掛載

影片重掛與影片縮圖測試另需專案內的 `VLC-lite/` 及 PATH 中的 `ffmpeg`. FFmpeg 僅用來產生測試短片, 程式的影片播放與縮圖仍使用 VLC. 缺少 Windows, VLC-lite 或 FFmpeg 時, 這兩組測試會顯示 `skipped`; 部分案例也可能因缺少指定編碼器而跳過, 檢查結果時需留意未執行的項目

## 打包與發佈

發佈順序為: 確認 VLC-lite 與測試結果, 使用 PyInstaller 打包, 再使用 Inno Setup 製作安裝檔

### 打包程式

先準備好 `VLC-lite/`, 再執行 [build.bat](./build.bat):

```powershell
.\build.bat
```

腳本不會執行 VLC 精簡流程. 它會依序:

1. 檢查 Python 虛擬環境, 必要時建立環境並安裝套件
2. 以 PyInstaller 的 `--onedir`, `--noconsole`, `--uac-admin` 打包主程式, 一併納入直接匯入的模組, 圖示及載入圖片
3. 複製 VLC-lite 至輸出目錄並改名為 `VLC/`, 另在執行檔旁複製一份 `libvlc.dll`, 再複製語言檔案
4. 清除根目錄下的 `build/` 與產生的 `BB Video Wallpaper.spec`

主要輸出結構:

```text
dist/BB Video Wallpaper/
├─ BB Video Wallpaper.exe
├─ libvlc.dll
├─ _internal/
│  └─ icon/
├─ lang/
└─ VLC/
   ├─ libvlc.dll
   ├─ libvlccore.dll
   └─ plugins/
```

發佈時需要整個輸出目錄, 單獨複製 EXE 會缺少執行所需的檔案

### 製作安裝檔

[BB Video Wallpaper.iss](./installer/BB%20Video%20Wallpaper.iss) 是 Inno Setup 定義檔, 以打包完成的 `dist/BB Video Wallpaper/` 為輸入:

1. 確認打包結果已更新
2. 發佈新版本時, 更新定義檔開頭的 `MyAppVersion`
3. 使用 Inno Setup Compiler 開啟定義檔並執行 Compile

若 `ISCC.exe` 已在 PATH 中, 也可執行:

```powershell
ISCC.exe '.\installer\BB Video Wallpaper.iss'
```

安裝檔輸出至 `installer/`, 名稱為 `bb-video-wallpaper-版本號-setup.exe`, 版本號中的句點改為連字號. 編譯安裝檔不會重新打包 Python 程式

安裝行為集中定義在同一份定義檔:

項目       | 行為
---        | ---
安裝語言   | 支援繁體中文, 簡體中文, 英文與日文, 使用編譯器隨附的語言檔. 首次依 Windows 顯示語言預選, 不支援時使用英文; 升級時預選上次語言, 並提供語言選擇視窗
捷徑       | 建立開始功能表捷徑, 桌面捷徑可選. 名稱隨安裝語言切換, 升級時清除本程式已知的舊語言捷徑
安裝後啟動 | 自動啟動區段目前為註解狀態
解除安裝   | 移除 `BBVideoWallpaper` 開機排程, 保留使用者資料

主程式的語言載入由 [config.py](./config.py) 處理: 保留既有語言設定; 未指定時依序使用安裝語言, 系統語言, 最後以繁體中文作為預設

## 常見情況

情況                                   | 檢查方式
---                                    | ---
套件需求已變更, 啟動或打包仍使用舊環境 | 依[啟動程式](#啟動程式)中的指令更新虛擬環境套件
更新完整 VLC 後, 執行結果沒有改變      | 確認主程式優先載入的 VLC-lite 是否已重新產生
修改 VLC 精簡規則後, 安裝版本沒有更新  | 依序重新產生 VLC-lite, 打包程式, 編譯安裝檔
打包完成但缺少語言或 VLC 檔案          | 檢查輸出目錄的實際內容與批次檔的複製輸出
需要查看執行錯誤                       | 查看使用者資料目錄下的 `log/log.log`
影片測試顯示 `skipped`                 | 依[測試環境與執行影響](#測試環境與執行影響)檢查必要工具與跳過原因
