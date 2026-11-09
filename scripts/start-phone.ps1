param(
  [switch]$Stop,
  [string]$Address = ''
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repo
$run = Join-Path $repo '.local-run'
$launcher = Join-Path $run 'start.ps1'
$registry = Join-Path $run 'lan-site.json'
$ruleName = 'Stopkadr_LAN_5174'

if (-not (Test-Path -LiteralPath $launcher)) {
  throw 'Локальная установка не найдена. Этот скрипт рассчитан на уже настроенный компьютер.'
}

function Stop-LanSite {
  if (-not (Test-Path -LiteralPath $registry)) { return }
  $entry = Get-Content -Raw -LiteralPath $registry | ConvertFrom-Json
  $process = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
  if ($process) {
    if ($process.Path -ne $entry.path -or $process.StartTime.ToUniversalTime().Ticks.ToString() -ne $entry.started) {
      throw 'Не удалось подтвердить процесс сайта для телефона. Он не остановлен.'
    }
    Stop-Process -InputObject $process
  }
  Remove-Item -LiteralPath $registry
}

function Test-Url([string]$url) {
  try {
    $response = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 4
    return $response.StatusCode -eq 200
  } catch { return $false }
}

if ($Stop) {
  Stop-LanSite
  & $launcher -Stop
  'Проект и доступ с телефона остановлены.'
  return
}

if (-not $Address) {
  $socket = [System.Net.Sockets.UdpClient]::new()
  try {
    # Connect selects the route without sending a packet.
    $socket.Connect('1.1.1.1', 53)
    $Address = $socket.Client.LocalEndPoint.Address.ToString()
  } finally { $socket.Dispose() }
}
$ip = $null
if (-not [System.Net.IPAddress]::TryParse($Address, [ref]$ip) -or
    $ip.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
    -not ($Address -match '^10\.' -or $Address -match '^192\.168\.' -or $Address -match '^172\.(1[6-9]|2[0-9]|3[01])\.')) {
  throw 'Не удалось определить локальный IPv4-адрес. Подключите компьютер к домашней сети Wi-Fi.'
}

if (-not (Test-Url 'http://127.0.0.1:5173/v1/plans')) {
  if (Test-Path -LiteralPath (Join-Path $run 'services.json')) {
    throw 'Локальный сервер не отвечает, хотя службы отмечены как запущенные. Сначала проверьте их состояние.'
  }
  & $launcher
  if (-not (Test-Url 'http://127.0.0.1:5173/v1/plans')) { throw 'Сайт на компьютере не запустился.' }
}

$rule = (& netsh advfirewall firewall show rule "name=$ruleName" 2>&1 | Out-String)
$escapedAddress = [regex]::Escape($Address)
$ruleReady = $LASTEXITCODE -eq 0 -and $rule -match '(?im)^\s*Enabled:\s*Yes\s*$' -and
  $rule -match '(?im)^\s*Direction:\s*In\s*$' -and $rule -match '(?im)^\s*Action:\s*Allow\s*$' -and
  $rule -match "(?im)^\s*LocalIP:\s*$escapedAddress(/32)?\s*$" -and
  $rule -match '(?im)^\s*RemoteIP:\s*LocalSubnet\s*$' -and $rule -match '(?im)^\s*LocalPort:\s*5174\s*$' -and
  $rule -match '(?im)^\s*Profiles:\s*(Private,Public|Public,Private)\s*$'
if (-not $ruleReady) {
  if ($LASTEXITCODE -eq 0) {
    $arguments = @('advfirewall','firewall','set','rule',"name=$ruleName",'new','enable=yes','dir=in','action=allow',
      'protocol=TCP',"localip=$Address",'localport=5174','remoteip=localsubnet','profile=private,public')
  } else {
    $arguments = @('advfirewall','firewall','add','rule',"name=$ruleName",'dir=in','action=allow','protocol=TCP',
      "localip=$Address",'localport=5174','remoteip=localsubnet','profile=private,public')
  }
  'Windows может запросить права администратора для доступа из локальной сети.'
  $firewall = Start-Process -FilePath 'netsh.exe' -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -PassThru -Wait
  if ($firewall.ExitCode -ne 0) { throw 'Не удалось настроить правило брандмауэра.' }
}

$alreadyRunning = $false
if (Test-Path -LiteralPath $registry) {
  $entry = Get-Content -Raw -LiteralPath $registry | ConvertFrom-Json
  $process = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
  if ($process -and $process.Path -eq $entry.path -and $process.StartTime.ToUniversalTime().Ticks.ToString() -eq $entry.started -and
      $entry.address -eq $Address -and (Test-Url "http://${Address}:5174/v1/plans")) {
    $alreadyRunning = $true
  } else { Stop-LanSite }
}
if (-not $alreadyRunning) {
  $env:SITE_HOST = $Address
  $env:SITE_PORT = '5174'
  $env:API_ORIGIN = 'http://127.0.0.1:8000'
  $node = (Get-Command node -ErrorAction Stop).Source
  $savedGemini = $env:GEMINI_API_KEY
  $savedTelegram = $env:TELEGRAM_BOT_TOKEN
  try {
    Remove-Item Env:GEMINI_API_KEY, Env:TELEGRAM_BOT_TOKEN -ErrorAction SilentlyContinue
    $process = Start-Process -FilePath $node -ArgumentList @('site/serve.js') -WorkingDirectory $repo -WindowStyle Hidden `
      -RedirectStandardOutput (Join-Path $run 'lan-site.out.log') -RedirectStandardError (Join-Path $run 'lan-site.err.log') -PassThru
  } finally {
    $env:GEMINI_API_KEY = $savedGemini
    $env:TELEGRAM_BOT_TOKEN = $savedTelegram
  }
  [pscustomobject]@{ pid=$process.Id; path=(Get-Item -LiteralPath $node).FullName; started=$process.StartTime.ToUniversalTime().Ticks.ToString(); address=$Address; port=5174 } |
    ConvertTo-Json | Set-Content -Encoding utf8 -LiteralPath $registry
  $ready = $false
  for ($attempt = 0; $attempt -lt 15; $attempt++) {
    if (Test-Url "http://${Address}:5174/v1/plans") { $ready = $true; break }
    Start-Sleep -Milliseconds 300
  }
  if (-not $ready) { throw 'Сайт для телефона не запустился. Проверьте .local-run/lan-site.err.log.' }
}

"Адрес для телефона: http://${Address}:5174"
'Телефон и компьютер должны находиться в одной доверенной сети Wi-Fi. Соединение HTTP не шифруется.'
