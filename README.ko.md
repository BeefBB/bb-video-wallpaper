# BB 동영상 배경화면

**[繁體中文](README.md) | [简体中文](README.cn.md) | [日本語](README.ja.md) | 한국어 | [English](README.en.md)**

동영상을 바탕 화면 배경으로 재생할 수 있는 가벼운 배경화면 관리 프로그램입니다.  
배경화면을 한 폴더에 보관하여 언제든지 간편하게 전환할 수 있습니다.  

- 가벼운 용량 (약 200 MB)
- 최근 사용한 배경화면 기록
- 자동 일시 중지 조건 선택 (창 최대화, 화면 꺼짐, 배터리 사용)
- Windows 시작 시 자동 실행 옵션

### 스크린샷

![스크린샷](./assets/screenshot.png)

# 다운로드

### Releases에서 최신 버전 다운로드

- [bb-video-wallpaper-setup.exe](https://github.com/BeefBB/bb-video-wallpaper/releases)

# 사용 방법

1. 시스템 트레이 아이콘을 마우스 오른쪽 버튼으로 클릭하고 "동영상 추가"를 선택합니다
2. 동영상이나 이미지를 선택합니다
3. 이후에는 "동영상 선택"에서 배경화면을 전환할 수 있습니다

"동영상 폴더 열기"로 동영상이나 이미지를 직접 추가한 다음, "동영상 선택"에서 선택할 수도 있습니다.  

> "최근 기록 지우기"는 현재 배경화면을 유지하면서 최근 사용 기록을 지웁니다. 파일은 삭제하지 않습니다.  

## 로그

문제가 발생하면 `%LOCALAPPDATA%\BB\BB Video Wallpaper\log\log.log`를 확인하세요.  
로그에는 작업 내역, 경고 및 오류가 기록되며, 처리된 예외에는 전체 스택 추적이 포함됩니다.  

# 직접 빌드하려면?

## 요구 사항

- [VLC](https://www.videolan.org/)

### `VLC`를 적절한 위치에 배치

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
 ├─ bb_image_display.py
 ├─ config.py
 ├─ media_import.py
 ├─ media_preview.py
 ├─ playback.py
 ├─ screen_follow.py
 ├─ windows_integration.py
 ├─ thumbnails.py
 ├─ libvlc.dll
 └─ requirements.txt
```

## 실행

```bash
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

```bash
pyinstaller --noconfirm --onedir --noconsole --uac-admin --icon=icon/bb-video-wallpaper.ico --name="BB Video Wallpaper" --add-data "icon;icon" bb-video-wallpaper.py
```

빌드 결과는 `./dist`에 생성됩니다.  
그런 다음 `lang/`, `VLC/`, `libvlc.dll`을 `.exe` 옆에 배치하세요.  
최종 폴더 구조는 다음과 같습니다:  

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

## 기본값

### config.json

이름 | 항목 | 기본값
--- | --- | ---
언어 | "language" | 필드가 없으면: 설치 언어 > 시스템 언어 > 중국어 번체; 필드가 있지만 유효하지 않으면: 중국어 번체
창 최대화 시 자동 일시 중지 | "autoPauseIfFullscreen" | true
화면 꺼짐 시 자동 일시 중지 | "autoPauseIfScreenOff" | true
배터리 사용 시 자동 일시 중지 | "autoPauseIfOnBattery" | false
글꼴 크기 | "fontSize" | 11 pt, 6-48
최근 기록 표시 개수 | "recentDisplayCount" | 6개, 0-20
미리보기 크기 | "previewSize" | 3 (보통), 1-5는 100-500 px에 대응
최근 사용 기록 | "recentVideo" | [], 0-20개
모니터별 배경화면 | "screenWallpapers" | {}
모니터 따라가기 관계 | "screenFollows" | {}

언어 코드는 `zh-TW`, `zh-CN`, `ja-JP`, `ko-KR`, `en-US` 중 하나와 정확히 일치해야 합니다. 예를 들어 시스템 언어가 `en-GB`여도 `en-US`로 자동 매핑되지 않습니다.

### 내부 상수

이름 | 항목 | 기본값
--- | --- | ---
미디어 폴더 | MEDIA_DIR | `%LOCALAPPDATA%\BB\BB Video Wallpaper\video\`
단일 파일 가져오기 폴더 | SINGLE_IMPORT_DIR | 미디어 폴더 안의 `_imports/`
썸네일 폴더 | THUMBNAIL_DIR | `%LOCALAPPDATA%\BB\BB Video Wallpaper\thumbnails\`
로그 파일 |  | `%LOCALAPPDATA%\BB\BB Video Wallpaper\log\log.log`
VLC 배경화면 재생 옵션 |  | 오디오 비활성화, 동영상 제목 숨김, 반복 옵션 `--input-repeat=65535`
배경화면 크기 조정 방식 |  | 이미지와 동영상 모두 종횡비를 유지하면서 가운데를 기준으로 잘라 화면을 채움
시작 시 배경화면 선택 순서 |  | 해당 모니터에 저장된 배경화면 > 최근 기록의 첫 항목 > 미디어 목록의 첫 항목, 앞의 두 후보는 파일이 존재해야 함
시작 시 최대화 감지 지연 시간 | fullscreen_detection_timer | 10 s
자동 일시 중지 확인 간격 | check_auto_pause_timer | 0.5 s
배경화면 재연결 기본 지연 시간 |  | 0.2 s
바탕 화면이 준비되지 않았을 때 재연결 대기 시간 |  | 2 s
배경화면 재연결 실패 후 재시도 간격 |  | 2 s
배경화면 재연결 실패 후 최대 재시도 횟수 |  | 재시도 2회, 최초 시도를 포함해 총 3회
VLC 중지 시 깨우기 간격 |  | 0.05 s
시작 시 바탕 화면 준비 상태 확인 간격 |  | 1 s
알림 표시 시간 |  | 3 s
로그 수준 |  | INFO
로그 파일당 최대 크기 |  | 2 MiB
이전 로그 보관 개수 |  | 3개
따라가는 모니터의 정기 동기화 간격 | SYNC_INTERVAL_MS | 60 s
따라가는 모니터의 동기화 오차 허용값 | SYNC_TOLERANCE_MS | 0.5 s
시작 시 모니터 따라가기 관계 복원 전 대기 시간 | RESTORE_DELAY_MS | 3 s, 저장된 따라가기 관계가 있을 때만 대기
첫 미리보기 표시 전 대기 시간 | preview_timer | 0.5 s
항목을 벗어난 뒤 미리보기 모드를 종료하기까지의 시간 | reset_timer | 1 s
미리보기 이미지의 가장 긴 변 |  | `previewSize * 100` px, 좌우 각각 4 px의 안쪽 여백 제외, 화면 공간이 부족하면 축소
미리보기 배경색 |  | #202020
미리보기 안쪽 여백 |  | 4 px
미리보기와 메뉴 사이 간격 |  | 8 px, 왼쪽에 우선 표시하고 공간이 부족하면 오른쪽에 표시
로딩 이미지를 불러오지 못했을 때 대체 이미지 |  | 너비 `previewSize * 100` px, 높이는 16:9 비율로 반올림, 짙은 회색 (`Qt.GlobalColor.darkGray`)
썸네일 너비 |  | `previewSize * 100` px, 높이는 미디어의 종횡비에 맞춰 조정; 필요한 크기만 생성하고 각각 캐시
썸네일 형식 및 품질 |  | JPEG 85
썸네일용 VLC 옵션 |  | 하드웨어 디코딩 (`--avcodec-hw=none`), 오디오, 동영상 제목 및 OSD 비활성화, `--quiet` 활성화
동영상 정보 분석 제한 시간 |  | 5 s
동영상 프레임 캡처 제한 시간 |  | 10 s
분석 및 캡처 대기 중 확인 간격 |  | 0.05 s
동영상 썸네일에 사용할 프레임 |  | 처음 디코딩된 프레임
지원하는 동영상 확장자 | SUPPORTED_VIDEO_EXTENSIONS | .mp4, .mkv, .webm, .mov, .avi
지원하는 이미지 확장자 | SUPPORTED_IMAGE_EXTENSIONS | .jpg, .jpeg, .png, .bmp, .webp
가져오기 시 추가로 확보할 여유 공간 | IMPORT_RESERVE_BYTES | 200 MiB
가져오기 읽기 블록 크기 |  | SHA-256 계산과 파일 복사 모두 1 MiB
최대화 감지 경계 허용값 | FULLSCREEN_MARGIN | 4 px, 모니터 작업 영역을 기준으로 판단
최대화 감지에서 제외할 창 제목 | WINDOW_TITLE_BLACKLIST | `""`, `default ime`, `msctfime ui`, `windows 輸入體驗`, `搜尋`, `search`, `開始`, `start`, `工作檢視`, `task view`
최대화 감지에서 제외할 창 클래스 | SHELL_CLASSES | `Progman`, `WorkerW`, `Shell_TrayWnd`, `Shell_SecondaryTrayWnd`
바탕 화면 연결 메시지당 제한 시간 |  | 1 s
Windows 시작 시 자동 실행 |  | 기존 작업이 없으면 false, 실제 상태는 Windows 예약 작업의 존재 여부에 따라 결정, 활성화하면 로그온 시 실행

# VLC

`bb-video-wallpaper-setup.exe`에는 BB 동영상 배경화면의 동영상 재생에 필요한 구성 요소만 남긴 **VLC 3.0.24** 축소 버전이 포함되어 있습니다.  
VLC는 오픈 소스 소프트웨어입니다. 저작권 및 라이선스 정보는 `설치 폴더/VLC/`에 있는 VLC 라이선스 문서를 참조하세요.  

# 라이선스

BB 동영상 배경화면은 MIT License로 배포됩니다.  
포함된 VLC 3.0.24 구성 요소에는 해당 VLC 라이선스 조건이 적용됩니다.  
