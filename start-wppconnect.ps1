$ErrorActionPreference = 'Stop'
$ProjectPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$WppPath = Join-Path $ProjectPath 'runtime\wppconnect'
$NodeExe = Join-Path $WppPath 'node_modules\node\bin\node.exe'
$SecretPath = Join-Path $WppPath '.secret-key'
$BrowserCandidates = @(
    'C:\Program Files\Google\Chrome\Application\chrome.exe',
    'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    'C:\Program Files\Microsoft\Edge\Application\msedge.exe'
)
$BrowserExe = $BrowserCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not (Test-Path -LiteralPath $NodeExe)) {
    & (Join-Path $ProjectPath 'install-wppconnect.ps1')
}
if (-not $BrowserExe) {
    throw 'Instale ou localize Google Chrome ou Microsoft Edge para iniciar o WPPConnect.'
}
if (-not (Test-Path -LiteralPath $SecretPath)) {
    $PythonPath = Join-Path $ProjectPath '.venv\Scripts\python.exe'
    $GeneratedSecret = & $PythonPath -c 'import secrets; print(secrets.token_urlsafe(48))'
    [System.IO.File]::WriteAllText($SecretPath, $GeneratedSecret, [System.Text.UTF8Encoding]::new($false))
}

$env:SECRET_KEY = (Get-Content -Raw -Encoding UTF8 -LiteralPath $SecretPath).Trim()
$env:HOST = 'http://127.0.0.1'
$env:PORT = '21465'
$env:MANAGER_ENABLED = 'false'
$env:TOKEN_STORE_TYPE = 'file'
$env:CUSTOM_USER_DATA_DIR = [System.IO.Path]::GetFullPath((Join-Path $ProjectPath 'runtime\wpp-user-data')) + '\'
$env:PUPPETEER_EXECUTABLE_PATH = $BrowserExe
$env:PUPPETEER_SKIP_DOWNLOAD = 'true'
$env:FINANCE_DATA_DIR = Join-Path $ProjectPath 'runtime'
$PythonPath = Join-Path $ProjectPath '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw 'Inicie primeiro o painel com start.ps1 para preparar o Python.'
}
& $PythonPath -c "import os,app; app.set_secret('whatsapp_secret',os.environ['SECRET_KEY']); app.set_setting('whatsapp_base_url','http://127.0.0.1:21465'); app.set_setting('whatsapp_session','financas')"
if ($LASTEXITCODE -ne 0) {
    throw 'Não foi possível salvar a configuração local do WPPConnect no painel.'
}

Push-Location -LiteralPath $WppPath
try {
    Write-Host "WPPConnect local em http://127.0.0.1:21465 usando $BrowserExe" -ForegroundColor Green
    & $NodeExe '.\loopback-launcher.cjs'
} finally {
    Pop-Location
}
