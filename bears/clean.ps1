# .venv 삭제: 다음 .\run.ps1 실행 때 다시 만들어진다. (.chrome-profile 은 로그인 세션이라 남겨둔다)

# 스크립트가 있는 폴더 기준으로 동작
Set-Location $PSScriptRoot

# .venv 폴더 통째로 삭제 (없어도 에러 없이 넘어감)
Remove-Item .venv -Recurse -Force -ErrorAction SilentlyContinue
Write-Host ".venv deleted"
