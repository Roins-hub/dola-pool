$ErrorActionPreference = "Continue"
Set-Location 'F:\ai\视频逆向\dola-pool'
$out = Join-Path (Get-Location) 'server.log'
$err = Join-Path (Get-Location) 'server.err.log'
Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*Python312*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
if (Test-Path $out) { Remove-Item $out -Force -ErrorAction SilentlyContinue }
if (Test-Path $err) { Remove-Item $err -Force -ErrorAction SilentlyContinue }
Start-Process -FilePath 'python.exe' -ArgumentList '-m','uvicorn','server:app','--host','127.0.0.1','--port','8000' -WorkingDirectory 'F:\ai\视频逆向\dola-pool' -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden
Start-Sleep -Seconds 10
Write-Output "=== OUT ==="
Get-Content $out -Tail 25 -ErrorAction SilentlyContinue
Write-Output "=== ERR ==="
Get-Content $err -Tail 25 -ErrorAction SilentlyContinue
