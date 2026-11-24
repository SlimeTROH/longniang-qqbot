# QQ bot launcher. The configured OneBot service runs independently.
[CmdletBinding()]
param(
    [switch]$Status, [switch]$Stop, [switch]$StopAll, [switch]$Restart,
    [switch]$NoBot, [switch]$WithWeb, [switch]$WebOnly, [switch]$NoPause, [switch]$Check
)
$ErrorActionPreference = 'Stop'
$BOT_DIR = $PSScriptRoot
$ROOT = Split-Path -Parent $BOT_DIR
$BOT_PY = Join-Path $ROOT 'venv\Scripts\python.exe'
$PROXY_DIR = Join-Path $ROOT 'gemini-proxy'
$PROXY_EXE = Join-Path $PROXY_DIR 'cli-proxy-api.exe'
$PROXY_CFG = Join-Path $PROXY_DIR 'config.yaml'
$QQCTL = Join-Path $BOT_DIR 'qqctl.py'
$BOT_SCRIPT = Join-Path $BOT_DIR 'bot.py'
$WEB_SCRIPT = Join-Path $BOT_DIR 'erp_server.py'
$PROXY_PORT = 0
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

function Test-Port([int]$Port) {
    $client = New-Object Net.Sockets.TcpClient
    try { return ($client.ConnectAsync('127.0.0.1', $Port).Wait(350) -and $client.Connected) }
    catch { return $false }
    finally { $client.Dispose() }
}

function Wait-Port([int]$Port, [int]$Seconds) {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    while ($watch.Elapsed.TotalSeconds -lt $Seconds) {
        if (Test-Port $Port) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Get-PortOwner([int]$Port) {
    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($connection) { return [int]$connection.OwningProcess }
    return $null
}

function Test-OwnedApplication($Process, [string]$Script) {
    if (-not $Process -or $Process.Name -notmatch '^python(w)?\.exe$') { return $false }
    $command = [string]$Process.CommandLine
    if ($command -match [regex]::Escape($Script)) { return $true }
    $name = [regex]::Escape([IO.Path]::GetFileName($Script))
    if ($command -notmatch ('\b' + $name + '(?:$|[\s"''])')) { return $false }
    if ($Process.ExecutablePath -ieq $BOT_PY) { return $true }
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($Process.ParentProcessId)" -ErrorAction SilentlyContinue
    return ($parent -and $parent.ExecutablePath -ieq $BOT_PY)
}

function Get-OwnedApplications([string]$Script) {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { Test-OwnedApplication $_ $Script }
}

function Stop-OwnedApplication([string]$Script, [int]$Port) {
    $ownerId = Get-PortOwner $Port
    if ($ownerId) {
        $ownerProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerId"
        if (-not (Test-OwnedApplication $ownerProcess $Script)) {
            throw "端口 $Port 被其他程序占用，已保留该程序"
        }
    }
    foreach ($process in @(Get-OwnedApplications $Script | Sort-Object ProcessId -Descending)) {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($ownerId) {
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            if (-not (Get-PortOwner $Port)) { break }
            Start-Sleep -Milliseconds 150
        }
        if (Get-PortOwner $Port) { throw "端口 $Port 未释放" }
    }
}

function Show-OneBotStatus {
    try {
        $raw = & $BOT_PY -B $QQCTL status
        if ($LASTEXITCODE -ne 0) { throw 'OneBot 状态查询失败' }
        $health = $raw | ConvertFrom-Json -ErrorAction Stop
        if ($health.error -and $health.connected) {
            Write-Warning 'OneBot 已连接，状态查询未成功。'
        } elseif ($health.online -and $health.account_matches -eq $false) {
            Write-Warning 'OneBot 已上线，登录账号与 QQBOT_ACCOUNT 不符。'
        } elseif ($health.online) {
            Write-Host '  OneBot 账号在线。'
        } elseif ($health.connected) {
            Write-Host '  OneBot 已连接，QQ 账号离线。'
        } else {
            Write-Host '  等待 OneBot 连接。请在 OneBot 服务端启动并登录机器人账号。' -ForegroundColor Yellow
        }
    } catch {
        Write-Warning '无法读取 OneBot 连接状态。'
    }
}

function Show-Status {
    Write-Host "`nQQ 机器人服务状态" -ForegroundColor Cyan
    foreach ($row in @(
        @{Name='本机代理';Port=$PROXY_PORT},
        @{Name='Gemini 中转';Port=8317}, @{Name='机器人本地控制';Port=8091}, @{Name='网页服务';Port=8090}
    )) {
        $running = Test-Port $row.Port
        $label = if ($running) {'运行中'} else {'未监听'}
        Write-Host ('  {0,-19} :{1,-5} {2}' -f $row.Name,$row.Port,$label)
    }
    Show-OneBotStatus
}

function Start-Web {
    if (Test-Port 8090) {
        $webOwner = Get-CimInstance Win32_Process -Filter "ProcessId=$(Get-PortOwner 8090)"
        if (-not (Test-OwnedApplication $webOwner $WEB_SCRIPT)) { throw '端口 8090 被其他程序占用' }
        Write-Host '网页服务已在运行'; return
    }
    if (@(Get-OwnedApplications $WEB_SCRIPT).Count) { Write-Host '网页服务正在启动'; return }
    Start-Process -FilePath $BOT_PY -ArgumentList @('-B', ('"' + $WEB_SCRIPT + '"')) `
        -WorkingDirectory $BOT_DIR -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $BOT_DIR 'logs\web-launch.out.log') `
        -RedirectStandardError (Join-Path $BOT_DIR 'logs\web-launch.err.log') | Out-Null
    if (-not (Wait-Port 8090 15)) { throw '网页服务未就绪，请检查 logs\web-launch.err.log' }
}

function Stop-Services {
    Stop-OwnedApplication $BOT_SCRIPT 8091
    Get-CimInstance Win32_Process -Filter "Name='cli-proxy-api.exe'" |
        Where-Object { $_.ExecutablePath -ieq $PROXY_EXE } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    if ($StopAll) {
        Stop-OwnedApplication $WEB_SCRIPT 8090
    }
    Write-Host '已停止所选服务。OneBot 和本机代理由各自的程序管理。'
}

function Start-Services {
    if (-not (Test-Path -LiteralPath $BOT_SCRIPT -PathType Leaf)) { throw '缺少 bot.py' }
    if (-not (Test-Port $PROXY_PORT)) { throw "请先连接本机代理（端口 $PROXY_PORT）" }
    if (-not (Test-Port 8317) -and (Test-Path -LiteralPath $PROXY_EXE -PathType Leaf) -and (Test-Path -LiteralPath $PROXY_CFG -PathType Leaf)) {
        $proxyProcesses = @(Get-CimInstance Win32_Process -Filter "Name='cli-proxy-api.exe'" | Where-Object { $_.ExecutablePath -ieq $PROXY_EXE })
        if (-not $proxyProcesses.Count) {
            Start-Process -FilePath $PROXY_EXE -ArgumentList @('--config',('"' + $PROXY_CFG + '"')) `
                -WorkingDirectory $PROXY_DIR -WindowStyle Hidden `
                -RedirectStandardOutput (Join-Path $PROXY_DIR 'cli-proxy-api.out.log') `
                -RedirectStandardError (Join-Path $PROXY_DIR 'cli-proxy-api.err.log') | Out-Null
        }
        if (-not (Wait-Port 8317 20)) { Write-Warning '中转未就绪，主对话仍可使用 Vertex 直连' }
    }
    if ($WithWeb) { Start-Web }
    if ($NoBot) { Show-Status; return }
    $existingBots = @(Get-OwnedApplications $BOT_SCRIPT)
    if ((Test-Port 8091) -and -not $existingBots.Count) { throw '端口 8091 被其他程序占用' }
    if ($existingBots.Count) {
        Write-Host '机器人已在运行，未重复启动。需要重启请加 -Restart。'
        Show-Status
        return
    }
    Show-OneBotStatus
    Push-Location $BOT_DIR
    try {
        Write-Host '机器人正在运行，关闭本窗口可停止机器人。' -ForegroundColor Green
        & $BOT_PY -B $BOT_SCRIPT
        if ($LASTEXITCODE -ne 0) { throw "机器人退出，错误码 $LASTEXITCODE；请检查 logs\bot.log" }
    } finally { Pop-Location }
}

$result = 0
$mutex = $null
$ownsMutex = $false
try {
    if (-not (Test-Path -LiteralPath $BOT_PY)) { throw '找不到项目 Python 环境' }
    $network = & $BOT_PY -B (Join-Path $BOT_DIR 'network_config.py') | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0 -or $network.host -notin @('127.0.0.1','localhost')) { throw '请检查 QQBOT_PROXY 本机代理配置' }
    $PROXY_PORT = [int]$network.port
    if ($Check) {
        $missing = @($BOT_SCRIPT, $WEB_SCRIPT, $QQCTL) |
            Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) }
        @{ok=($missing.Count -eq 0);missing=@($missing);onebot_mode='external'} | ConvertTo-Json -Compress
        if ($missing.Count) { $result = 1 }
    } elseif ($Status) { Show-Status }
    elseif ($Stop -or $StopAll) { Stop-Services }
    elseif ($WebOnly) { New-Item -ItemType Directory -Force -Path (Join-Path $BOT_DIR 'logs') | Out-Null; Start-Web }
    else {
        if ($Restart) { Stop-OwnedApplication $BOT_SCRIPT 8091 }
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $tag = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($BOT_DIR.ToLowerInvariant()))).Replace('-','').Substring(0,16) }
        finally { $sha.Dispose() }
        $mutex = New-Object Threading.Mutex($false, ('Local\QQBotLaunch_' + $tag))
        try { $ownsMutex = $mutex.WaitOne(3000) } catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
        if ($ownsMutex) { New-Item -ItemType Directory -Force -Path (Join-Path $BOT_DIR 'logs') | Out-Null; Start-Services }
        else { Write-Host '启动器已在运行，显示当前状态。'; Show-Status }
    }
} catch { Write-Host ('[错误] ' + $_.Exception.Message) -ForegroundColor Red; $result = 1 }
finally {
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
}
if (-not $NoPause -and -not $Check -and -not $Status) { Read-Host '按回车关闭窗口' | Out-Null }
exit $result
