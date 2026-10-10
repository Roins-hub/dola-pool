# dola-pool 本地一键启动（Windows）
# 用法：双击 start-local.bat，或在 PowerShell 里执行 .\start-local.ps1
# 首次运行会自动：创建虚拟环境 → 安装依赖 → 下载 Chromium → 生成 dola.env，然后启动服务。
# 所有依赖、缓存、浏览器都放在项目目录内（.venv / .browsers / .cache），不写系统盘。
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Info($m) { Write-Host "[-] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[X] $m" -ForegroundColor Red; Read-Host "按回车退出"; exit 1 }

# 缓存全部放到项目目录
$env:PIP_CACHE_DIR = Join-Path $Root ".cache\pip"
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $Root ".browsers"

# ---------- Python ----------
$py = $null
foreach ($cand in @("py -3", "python", "python3")) {
  $parts = $cand.Split(" ")
  try {
    $v = & $parts[0] $parts[1..($parts.Length-1)] -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0 -and $v) { $py = $parts; break }
  } catch {}
}
if (-not $py) { Fail "没找到 Python。请安装 Python 3.10+（https://www.python.org/downloads/ ，安装时勾选 Add to PATH）" }
$ver = & $py[0] $py[1..($py.Length-1)] -c "import sys;print(1 if sys.version_info>=(3,10) else 0)"
if ($ver -ne "1") { Fail "Python 版本太低，需要 3.10 以上" }
Ok ("Python " + (& $py[0] $py[1..($py.Length-1)] -V))

# ---------- Node.js（纯 API 出片签名需要）----------
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
  Warn "没找到 Node.js：Cookie 账号的验证和出片会失败。请安装 Node.js 18+（https://nodejs.org/）后重新运行。"
} else { Ok ("Node.js " + (node -v)) }

# ---------- 虚拟环境与依赖 ----------
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
  Info "创建虚拟环境 .venv ..."
  & $py[0] $py[1..($py.Length-1)] -m venv .venv
  if ($LASTEXITCODE -ne 0) { Fail "创建虚拟环境失败" }
}
$stamp = Join-Path $Root ".venv\.deps-ok"
$reqHash = (Get-FileHash requirements.txt).Hash + (Get-FileHash deploy-extra-requirements.txt).Hash
if (-not (Test-Path $stamp) -or (Get-Content $stamp -Raw).Trim() -ne $reqHash) {
  Info "安装 Python 依赖（首次约 2~5 分钟）..."
  & $venvPy -m pip install -q --upgrade pip
  & $venvPy -m pip install -q -r requirements.txt -r deploy-extra-requirements.txt
  if ($LASTEXITCODE -ne 0) { Fail "依赖安装失败，请检查网络（国内可先执行：set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple）" }
  Set-Content -Path $stamp -Value $reqHash -Encoding ascii
}
Ok "Python 依赖就绪"

if (-not (Test-Path (Join-Path $Root ".browsers"))) {
  Info "下载 Chromium 浏览器（约 150 MB）..."
  & $venvPy -m patchright install chromium
  if ($LASTEXITCODE -ne 0) { Warn "Chromium 下载失败：浏览器出片路径不可用（纯 API 出片不受影响），可稍后重跑本脚本" }
}

# ---------- 配置文件 ----------
$envFile = Join-Path $Root "dola.env"
if (-not (Test-Path $envFile)) {
  $key = "sk-dola-" + ([guid]::NewGuid().ToString("N"))
  $lines = Get-Content (Join-Path $Root "deploy\dola.env.example") -Encoding UTF8 | ForEach-Object {
    if ($_ -match '^DOLA_API_KEYS=') { "DOLA_API_KEYS=$key" }
    elseif ($_ -match '^PLAYWRIGHT_BROWSERS_PATH=') { "PLAYWRIGHT_BROWSERS_PATH=$($env:PLAYWRIGHT_BROWSERS_PATH)" }
    else { $_ }
  }
  [System.IO.File]::WriteAllLines($envFile, $lines, (New-Object System.Text.UTF8Encoding($false)))
  Ok "已生成配置 dola.env（API Key: $key）"
}

# 读取 dola.env 到当前进程环境变量
$cfg = @{}
foreach ($line in [System.IO.File]::ReadAllLines($envFile)) {
  $t = $line.Trim()
  if ($t -eq "" -or $t.StartsWith("#") -or -not $t.Contains("=")) { continue }
  $k, $v = $t.Split("=", 2)
  $cfg[$k.Trim()] = $v.Trim()
  [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), "Process")
}
if (-not $env:PLAYWRIGHT_BROWSERS_PATH) { $env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $Root ".browsers" }
$hostAddr = if ($cfg["DOLA_HOST"]) { $cfg["DOLA_HOST"] } else { "127.0.0.1" }
$port = if ($cfg["DOLA_PORT"]) { $cfg["DOLA_PORT"] } else { "8002" }

Write-Host ""
Write-Host "==============================================================" -ForegroundColor Green
Write-Host "  管理面板   http://127.0.0.1:$port/"
Write-Host "  默认账号   admin / admin123（登录后到「用户管理」改密码）"
Write-Host "  API Key    $($cfg['DOLA_API_KEYS'])"
Write-Host "  配置文件   $envFile"
Write-Host "  dola.com 只允许日本/韩国 IP：本机不在日韩时，请在面板「代理管理」配代理，"
Write-Host "  或在 dola.env 里设置 DOLA_PROXY（例如本机 Clash：http://127.0.0.1:7890）"
Write-Host "  按 Ctrl+C 停止服务"
Write-Host "==============================================================" -ForegroundColor Green
Write-Host ""

& (Join-Path $Root ".venv\Scripts\uvicorn.exe") server:app --host $hostAddr --port $port --workers 1
