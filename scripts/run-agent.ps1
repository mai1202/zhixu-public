# Pi 智能助手服务只监听回环地址；桥接凭证从既有 AUTH_HMAC_KEY 派生，不写入仓库。
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot 'backend/.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = Join-Path $repoRoot 'runtime/python/python.exe' }
if (-not (Test-Path -LiteralPath $python)) { throw '找不到 Python 运行时，请先运行安装脚本或使用发布包。' }
if (-not $env:AUTH_HMAC_KEY) { throw '缺少 AUTH_HMAC_KEY；不要使用临时新密钥连接既有数据库。' }
$env:PI_BRIDGE_TOKEN = (& $python -c "import hashlib,hmac,os; print(hmac.new(os.environ['AUTH_HMAC_KEY'].encode(),b'notification-pi-bridge-v1',hashlib.sha256).hexdigest())").Trim()
if ($LASTEXITCODE -ne 0 -or $env:PI_BRIDGE_TOKEN.Length -lt 32) { throw 'Pi 桥接凭证派生失败。' }
$env:PI_CALLBACK_URL = if ($env:PI_CALLBACK_URL) { $env:PI_CALLBACK_URL } else { 'http://127.0.0.1:8080' }
$env:PI_AGENT_PORT = if ($env:PI_AGENT_PORT) { $env:PI_AGENT_PORT } else { '5011' }
$node = if (Test-Path -LiteralPath (Join-Path $repoRoot 'runtime/node/node.exe')) { Join-Path $repoRoot 'runtime/node/node.exe' } else { (Get-Command node -ErrorAction Stop).Source }
Push-Location $repoRoot
try { & $node 'agent-service/src/server.mjs' } finally { Pop-Location; Remove-Item Env:PI_BRIDGE_TOKEN -ErrorAction SilentlyContinue }
