# 專案腳本說明

這份文件介紹專案內各個腳本的用途, 執行方式及使用順序. 以下指令以 Windows PowerShell 為例, 工作目錄為專案根目錄

## 快速查找

想做的事                       | 使用的腳本                                                         | 結果
---                            | ---                                                                | ---
從原始碼啟動桌布程式           | [run.bat](./run.bat)                                               | 開啟桌布與系統匣選單
修改桌布, 選單或匯入介面       | [bb-video-wallpaper.py](./bb-video-wallpaper.py)                   | 主程式的實作
修改媒體匯入                   | [media_import.py](./media_import.py)                               | 去重, 複製, 容量檢查與取消
修改 Windows 整合              | [windows_integration.py](./windows_integration.py)                 | 桌面掛載, 視窗偵測, 電源通知與開機排程
修改設定驗證與語言載入         | [config.py](./config.py)                                           | 設定模組
修改圖片顯示                   | [bb_image_display.py](./bb_image_display.py)                       | Qt 圖片輸出與子視窗管理
修改縮圖產生與快取             | [thumbnails.py](./thumbnails.py)                                   | 縮圖模組
從完整 VLC 製作精簡版          | [create-vlc-lite.ps1](./create-vlc-lite.ps1)                       | 產生專案根目錄下的 `VLC-lite/`
將 Python 程式打包成執行檔     | [build.bat](./build.bat)                                           | 產生 `dist/BB Video Wallpaper/`
將打包結果製作成安裝程式       | [BB Video Wallpaper.iss](./installer/BB%20Video%20Wallpaper.iss)   | 在 `installer/` 產生安裝檔
檢查設定檔處理                 | [test_config.py](./tests/test_config.py)                           | 執行設定檔相關測試
檢查媒體匯入                   | [test_media_import.py](./tests/test_media_import.py)               | 檢查容量, 去重, 命名, 進度與取消
檢查匯入選單                   | [test_import_menu.py](./tests/test_import_menu.py)                 | 檢查選單排列, 進度訊號, 取消與結束狀態
檢查播放與暫停狀態             | [test_playback_state.py](./tests/test_playback_state.py)           | 檢查圖片與影片切換, 載入時序及桌布重掛
檢查 Windows 整合              | [test_windows_integration.py](./tests/test_windows_integration.py) | 模擬 Windows API, 檢查桌面掛載與電源及排程介面
檢查圖片原生視窗               | [test_image_display.py](./tests/test_image_display.py)             | 檢查圖片讀取, 顯示, 視窗重綁與釋放
檢查縮圖快取                   | [test_thumbnails.py](./tests/test_thumbnails.py)                   | 執行快取相關測試
檢查實際影片縮圖比例           | [test_video_thumbnails.py](./tests/test_video_thumbnails.py)       | 使用 VLC-lite 解碼合成影片並檢查結果

## 建議使用順序

首次準備專案時:

1. 安裝 Python, 將完整的 Windows VLC 放入專案根目錄的 `VLC/`
2. 執行 [create-vlc-lite.ps1](./create-vlc-lite.ps1), 產生精簡版 VLC
3. 執行 [run.bat](./run.bat), 建立 Python 環境並啟動程式
4. 開發或修改功能後, 執行測試
5. 要發佈時, 執行 [build.bat](./build.bat)
6. 使用 Inno Setup 編譯 [BB Video Wallpaper.iss](./installer/BB%20Video%20Wallpaper.iss)

平常修改 Python 程式後, 通常只需要重新啟動程式與執行測試. 更換 VLC 版本或調整保留的模組時, 才需要重新產生精簡版; 要更新發佈檔時, 則重新打包並製作安裝檔

## 啟動程式

### run.bat

[run.bat](./run.bat) 是從原始碼啟動程式的入口, 可以直接雙擊, 或在 PowerShell 執行:

```powershell
.\run.bat
```

它依序處理下列工作:

1. 檢查管理員權限, 必要時顯示提權提示並重新啟動自身
2. 將工作目錄切換到腳本所在的專案根目錄
3. 如果 `.venv/` 不存在, 透過 PATH 中的 `python` 建立虛擬環境, 並安裝 [requirements.txt](./requirements.txt) 中的套件
4. 使用虛擬環境內的 Python 執行 [bb-video-wallpaper.py](./bb-video-wallpaper.py)
5. 程式退出後保留命令視窗, 方便查看輸出

**環境建立只在 `.venv/` 不存在時執行. ** 已有虛擬環境時, 更新套件需求後需自行執行:

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
```

Python 與 VLC 的架構需要相容, 例如搭配使用 64 位元版本. 只有安裝 Python 套件仍不足以播放影片, 還需要 VLC 的 DLL 與外掛

### bb-video-wallpaper.py

[bb-video-wallpaper.py](./bb-video-wallpaper.py) 是主程式, 包含:

- Windows 桌布視窗, VLC 影片播放器與 Qt 圖片輸出整合
- 系統匣選單, 語言與最近使用紀錄
- 媒體匯入介面, 背景執行緒與懸停預覽介面
- 自動暫停, 螢幕電源事件與開機啟動選項
- 路徑, 日誌與 VLC 初始化

另有五個由主程式使用的模組:

- [config.py](./config.py): `ConfigStore` 負責設定初始化, 驗證, 讀寫與語言檔載入, 所需路徑由主程式傳入
- [media_import.py](./media_import.py): 負責內容去重, 分塊複製, 容量檢查, 命名衝突與取消清理; 匯入目錄及翻譯函式由主程式傳入, 透過回呼回報進度與查詢取消狀態
- [windows_integration.py](./windows_integration.py): 負責單一實例 Mutex, 桌面就緒檢查, 桌布掛載, 虛擬桌面與最大化視窗偵測, 電源通知及開機排程; 建立排程時由主程式傳入執行檔路徑
- [bb_image_display.py](./bb_image_display.py): 共用主程式的 `QApplication`, 以 `Instance` 接收桌布 HWND 並顯示圖片; 主程式管理桌布掛載與媒體切換, 模組管理圖片及內部子視窗
- [thumbnails.py](./thumbnails.py): 負責支援的媒體格式, 縮圖產生, 快取驗證與背景排隊; 主程式將媒體與快取目錄傳給 `ThumbnailThread`

五個模組都不反向匯入主程式, 匯入時不會建立使用者資料夾或設定日誌. Windows 模組只在呼叫相關函式時建立 Mutex, 掛載桌面或管理排程. 影片縮圖只在實際使用時匯入 VLC, 因此主程式仍先完成 DLL 載入設定; 媒體匯入, Windows 整合, 圖片顯示與圖片縮圖測試不需初始化 VLC

虛擬環境準備完成後, 也可以先以系統管理員身分開啟 PowerShell, 切換到專案根目錄後直接執行:

```powershell
.\.venv\Scripts\python.exe .\bb-video-wallpaper.py
```

主程式會優先載入同層的 `VLC-lite/`, 找不到時才使用 `VLC/`. 兩個資料夾同時存在時, 更換完整 VLC 並不會切換目前使用的精簡版

`--task` 是開機排程使用的內部參數, 一般手動啟動不需要加上

程式資料存放在 `%LOCALAPPDATA%\BB\BB Video Wallpaper\`:

路徑          | 用途
---           | ---
`config.json` | 語言, 自動暫停選項與最近使用紀錄
`video/`      | 媒體資料夾, 匯入的檔案放在其下的 `_imports/` 子目錄
`thumbnails/` | 縮圖快取
`log/log.log` | 日誌, 達到上限後輪替

## 準備精簡版 VLC

### create-vlc-lite.ps1

[create-vlc-lite.ps1](./create-vlc-lite.ps1) 從專案根目錄的完整 `VLC/` 複製需要的檔案, 產生 `VLC-lite/`

來源目錄至少應包含完整 VLC 發行包中的 DLL, 授權文件, `vlc-cache-gen.exe` 與 `plugins/`, 例如:

```text
專案根目錄/
├─ VLC/
│  ├─ libvlc.dll
│  ├─ libvlccore.dll
│  ├─ vlc-cache-gen.exe
│  ├─ COPYING.txt
│  └─ plugins/
└─ create-vlc-lite.ps1
```

執行方式:

```powershell
.\create-vlc-lite.ps1
```

如果 PowerShell 的執行原則阻擋這個腳本, 可以只對這次執行指定:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\create-vlc-lite.ps1
```

腳本會先刪除既有的 `VLC-lite/`, 再建立新的輸出. 手動放進輸出目錄的檔案也會被移除; 需要長期保留的模組應加入腳本的複製規則

目前保留的外掛類別為 `access`, `codec`, `d3d11`, `d3d9`, `demux`, `packetizer`, `video_chroma` 與 `video_output`, 另外從 `video_filter` 保留 `libtransform_plugin.dll`, 供記憶體影片輸出處理旋轉. 複製完成後會重新產生外掛快取, 並顯示精簡前後的容量

調整保留清單後, 需重新執行腳本, 並用新產生的精簡版驗證播放與縮圖. 現有影片測試涵蓋一般比例, 非正方形像素, 以及 90/180/270 度旋轉後的尺寸與實際畫面方向

## 打包與製作安裝程式

### build.bat

[build.bat](./build.bat) 使用 PyInstaller 產生可執行的程式目錄

```powershell
.\build.bat
```

執行前, 先準備好 `VLC-lite/`. 腳本本身不會執行 VLC 精簡流程

它會:

1. 在 `.venv/` 不存在時建立環境並安裝套件
2. 以 `--onedir`, `--noconsole`, `--uac-admin` 打包主程式, 並加入圖示及載入圖片; 直接匯入的 [config.py](./config.py), [media_import.py](./media_import.py), [windows_integration.py](./windows_integration.py), [bb_image_display.py](./bb_image_display.py) 與 [thumbnails.py](./thumbnails.py) 會一併納入
3. 將 `VLC-lite/` 複製至輸出目錄, 發佈時改名為 `VLC/`, 另外在執行檔旁複製一份 `libvlc.dll`
4. 複製語言檔案
5. 清除根目錄下的 `build/` 與產生的 `BB Video Wallpaper.spec`

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

發佈時需要整個輸出目錄, 單獨複製 EXE 會缺少執行所需的檔案. 要製作安裝程式時, 接著執行下一步

### installer/BB Video Wallpaper.iss

[BB Video Wallpaper.iss](./installer/BB%20Video%20Wallpaper.iss) 是 Inno Setup 的安裝程式定義檔

使用方式:

1. 先完成 [build.bat](./build.bat) 的打包
2. 在 Inno Setup Compiler 開啟這份定義檔
3. 發佈新版本時, 確認檔案開頭的 `MyAppVersion` 已更新
4. 執行 Compile

如果 `ISCC.exe` 已在 PATH 中, 也可使用:

```powershell
ISCC.exe '.\installer\BB Video Wallpaper.iss'
```

編譯器會將 `dist/BB Video Wallpaper/` 的內容製作成安裝檔, 輸出至 `installer/`, 名稱為 `bb-video-wallpaper-版本號-setup.exe`, 版本號中的句點會改成連字號

這份定義檔也設定安裝位置, 開始功能表捷徑, 選用的桌面捷徑及解除安裝資訊. 它不會重新打包 Python 程式; 編譯前需確認輸入目錄已更新. 目前安裝完成後自動啟動程式的區段是註解狀態

解除安裝時會移除 `BBVideoWallpaper` 開機排程, 使用者資料仍保留在 `%LOCALAPPDATA%\BB\BB Video Wallpaper\`

同一份安裝檔支援繁體中文, 簡體中文, 英文與日文, 使用 Inno Setup 編譯器隨附的語言檔. 首次安裝依 Windows 顯示語言預選, 不支援時使用英文; 升級時預選上次安裝使用的語言, 並提供語言選擇視窗. 開始功能表與桌面捷徑名稱會隨安裝語言切換, 升級時清除本程式已知的舊語言捷徑

主程式保留既有的語言設定. 未指定語言時, 優先使用安裝語言; 安裝語言無法讀取或不受支援時使用系統語言, 系統語言也不受支援時則使用繁體中文

## 執行測試

測試使用 Python 內建的 `unittest`. 在虛擬環境與套件準備完成後, 執行全部測試:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

也可以單獨執行一份測試:

```powershell
.\.venv\Scripts\python.exe .\tests\test_config.py
.\.venv\Scripts\python.exe .\tests\test_media_import.py
.\.venv\Scripts\python.exe .\tests\test_import_menu.py
.\.venv\Scripts\python.exe .\tests\test_playback_state.py
.\.venv\Scripts\python.exe .\tests\test_windows_integration.py
.\.venv\Scripts\python.exe .\tests\test_image_display.py
.\.venv\Scripts\python.exe .\tests\test_thumbnails.py
.\.venv\Scripts\python.exe .\tests\test_video_thumbnails.py
```

測試腳本                                                           | 檢查內容                                                               | 額外需求
---                                                                | ---                                                                    | ---
[test_config.py](./tests/test_config.py)                           | 缺檔初始化, 預設設定, 無效欄位, 路徑與紀錄驗證, 語言載入, 寫入失敗處理 |
[test_media_import.py](./tests/test_media_import.py)               | 同名匯入, 長檔名, 內容去重, 磁碟容量, 進度與取消後清理                 |
[test_import_menu.py](./tests/test_import_menu.py)                 | 匯入前後選單排列, 主執行緒進度更新, 取消, 錯誤與退出處理               | PySide6
[test_playback_state.py](./tests/test_playback_state.py)           | 圖片與影片切換, 讀取失敗, 載入期間暫停, Qt 執行緒及重掛後狀態          | PySide6
[test_windows_integration.py](./tests/test_windows_integration.py) | 桌面掛載分支, 虛擬桌面, 電源解析, 64 位元 Handle 與排程路徑      |
[test_image_display.py](./tests/test_image_display.py)             | 圖片讀取失敗保留畫面, 原生子視窗, 隱藏後再顯示, 重綁與釋放             | PySide6
[test_thumbnails.py](./tests/test_thumbnails.py)                   | 長檔名, 獨立快取, 舊快取替換, 清理範圍與儲存失敗處理                   | PySide6
[test_video_thumbnails.py](./tests/test_video_thumbnails.py)       | 影片縮圖比例, 旋轉方向, 多影像軌, 取消及錯誤處理                       | 專案內的 VLC-lite, FFmpeg

測試會在暫存目錄操作, 不會啟動桌布或改寫使用者設定. Windows 整合測試使用模擬 API, 不建立實際排程或改變桌面掛載. 圖片顯示測試建立隱藏的 Qt 目標視窗, 驗證實際 Windows 子視窗生命週期. 影片測試會呼叫 FFmpeg 產生短片, 再用 VLC-lite 擷取縮圖; FFmpeg 是測試工具, 主程式的影片播放與影片縮圖仍使用 VLC

影片測試在缺少 Windows, VLC-lite 或 FFmpeg 時會顯示 `skipped`, 代表該組測試沒有執行. 檢查結果時也要留意跳過的項目

## 常見情況

情況                                     | 檢查方式
---                                      | ---
修改了套件需求, 但啟動或打包仍使用舊環境 | 手動執行虛擬環境的 `python -m pip install -r requirements.txt`
更新了完整 VLC, 執行結果卻沒變           | 主程式優先使用 `VLC-lite/`; 重新產生精簡版後再測試
改了 VLC 精簡規則, 安裝版本卻沒更新      | 依序重新產生 VLC-lite, 執行打包, 再編譯安裝檔
打包完成但缺少語言或 VLC 檔案            | 檢查輸出目錄的實際內容與批次檔的複製輸出
想知道執行時的錯誤原因                   | 查看使用者資料目錄下的 `log/log.log`
影片測試顯示 `skipped`                   | 檢查 Windows 環境, `VLC-lite/libvlc.dll` 及 PATH 中的 `ffmpeg`
