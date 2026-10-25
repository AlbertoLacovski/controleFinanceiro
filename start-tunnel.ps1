$ErrorActionPreference = 'Stop'
$ProjectPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimePath = Join-Path $ProjectPath 'runtime'
New-Item -ItemType Directory -Force -Path $RuntimePath | Out-Null
$UrlPath = Join-Path $RuntimePath 'cloudflare-url.txt'
$OutPath = Join-Path $RuntimePath 'cloudflared.out.log'
$ErrPath = Join-Path $RuntimePath 'cloudflared.err.log'
$PidPath = Join-Path $RuntimePath 'cloudflared.pid'
$Health = curl.exe --silent --show-error --output NUL --write-out '%{http_code}' 'http://127.0.0.1:8001/health'
if ($Health -ne '200') {
    throw 'O painel não está ativo. Em outro PowerShell, execute .\start.ps1 -Public.'
}

$Cloudflared = (Get-Command cloudflared -ErrorAction Stop).Source
$Existing = Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" |
    Where-Object { $_.CommandLine -like '*tunnel*--url*127.0.0.1:8001*' } |
    Select-Object -First 1
if ($Existing) {
    if (Test-Path -LiteralPath $UrlPath) {
        $SavedUrl = (Get-Content -Raw -LiteralPath $UrlPath).Trim()
        if ($SavedUrl -match '^https://[a-z0-9-]+\.trycloudflare\.com/?$') {
            Write-Host "Túnel já ativo: $SavedUrl" -ForegroundColor Green
            return
        }
    }
    throw 'Já existe um cloudflared apontando para o painel. Encerre o processo antigo antes de criar outro túnel.'
}

Remove-Item -LiteralPath $UrlPath,$OutPath,$ErrPath,$PidPath -Force -ErrorAction SilentlyContinue
$Process = Start-Process -FilePath $Cloudflared `
    -ArgumentList @('tunnel','--url','http://127.0.0.1:8001','--no-autoupdate') `
    -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $OutPath -RedirectStandardError $ErrPath
[System.IO.File]::WriteAllText($PidPath,[string]$Process.Id,[System.Text.Encoding]::ASCII)

$TunnelUrl = ''
for ($Attempt = 0; $Attempt -lt 45 -and -not $TunnelUrl; $Attempt++) {
    Start-Sleep -Seconds 1
    foreach ($LogPath in @($OutPath,$ErrPath)) {
        if (Test-Path -LiteralPath $LogPath) {
            $LogText = Get-Content -Raw -LiteralPath $LogPath -ErrorAction SilentlyContinue
            if ($LogText -match 'https://[a-z0-9-]+\.trycloudflare\.com') {
                $TunnelUrl = $Matches[0]
                break
            }
        }
    }
    if ($Process.HasExited) { break }
}
if (-not $TunnelUrl) {
    if (-not $Process.HasExited) { Stop-Process -Id $Process.Id -Force }
    throw 'O Cloudflare Tunnel não retornou uma URL. Confira runtime\cloudflared.err.log.'
}

[System.IO.File]::WriteAllText($UrlPath,$TunnelUrl,[System.Text.Encoding]::UTF8)
Write-Host "Painel acessível em: $TunnelUrl" -ForegroundColor Green
Write-Host "URL guardada em: $UrlPath"
