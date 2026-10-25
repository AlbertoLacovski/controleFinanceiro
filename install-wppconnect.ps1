$ErrorActionPreference = 'Stop'
$ProjectPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$WppPath = Join-Path $ProjectPath 'runtime\wppconnect'
$NpmCache = Join-Path $ProjectPath 'runtime\npm-cache'
New-Item -ItemType Directory -Force -Path $WppPath, $NpmCache | Out-Null
$env:PUPPETEER_SKIP_DOWNLOAD = 'true'
$env:PUPPETEER_CACHE_DIR = Join-Path $ProjectPath 'runtime\wpp-browser-cache'
npm install --prefix $WppPath --cache $NpmCache --no-audit --no-fund '@wppconnect/server@2.10.27' 'node@22.22.2' '@babel/runtime@^7.29.2' 'prom-client@^14.2.0' 'socket.io-client@4.8.3'
if ($LASTEXITCODE -ne 0) {
    throw 'Não foi possível instalar o WPPConnect. Confira a conexão com a internet e tente novamente.'
}
