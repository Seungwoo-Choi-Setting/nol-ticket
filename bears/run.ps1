# 실행 진입점: venv 준비 -> 디버그 포트로 Chrome 실행 -> watch_click.py 감시 시작
# 사용: .\run.ps1   (막히면: powershell -ExecutionPolicy Bypass -File .\run.ps1)

# 스크립트가 있는 폴더 기준으로 동작
Set-Location $PSScriptRoot

# .venv가 없으면 만들고 playwright 설치 (py 런처 사용: Store 가짜 python 회피)
if (-not (Test-Path .venv)) {
    py -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -q playwright
}

# 디버그 포트(9222)가 닫혀 있으면 일반 Chrome을 전용 프로필로 실행 (로그인은 최초 1회)
$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
if (-not (Test-NetConnection localhost -Port 9222 -InformationLevel Quiet -WarningAction SilentlyContinue)) {
    # --disable-*: 창이 다른 창 뒤에 가려져도 타이머/렌더링이 느려지지 않게 함
    Start-Process $chrome -ArgumentList "--remote-debugging-port=9222", "--user-data-dir=`"$PSScriptRoot\.chrome-profile`"", "--disable-background-timer-throttling", "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows", "https://nol.yanolja.com"
    Read-Host "Log in on the opened Chrome, then press Enter"
}

# 좌석 감시 시작 (venv의 python을 직접 호출해서 activate 불필요)
.\.venv\Scripts\python.exe watch_click.py
