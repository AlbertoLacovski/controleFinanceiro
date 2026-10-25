param([switch]$Public)

$ErrorActionPreference = 'Stop'
$ProjectPath = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectPath
$RuntimePath = Join-Path $ProjectPath 'runtime'
$VenvPython = Join-Path $ProjectPath '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $VenvPython)) {
    py -3 -m venv (Join-Path $ProjectPath '.venv')
}
& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $ProjectPath 'requirements.txt')
$env:FINANCE_DATA_DIR = $RuntimePath
$env:APP_HOST = '127.0.0.1'
$env:APP_PORT = '8001'
$env:APP_ENV = 'production'
$env:COOKIE_SECURE = if ($Public) { 'true' } else { 'false' }
if ($Public) {
    Write-Host 'Fluxo pronto para acesso por HTTPS via Cloudflare Tunnel em http://127.0.0.1:8001.' -ForegroundColor Green
} else {
    Write-Host 'Fluxo disponível em http://127.0.0.1:8001. Deixe esta janela aberta enquanto usar o painel.' -ForegroundColor Green
}
& $VenvPython (Join-Path $ProjectPath 'app.py')
