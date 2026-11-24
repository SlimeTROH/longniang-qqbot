[CmdletBinding()]
param(
    [ValidateSet('Menu','Status','Start','Restart','Stop','StopAll','Logs','Console','Login','WebUI','Web','Nb','Dev','Sync','AllowLan','InstallDsh','Check')]
    [string]$Action = 'Menu'
)
$ErrorActionPreference = 'Stop'
$BOT_DIR = Split-Path -Parent $PSScriptRoot
$ROOT = Split-Path -Parent $BOT_DIR
$PYTHON = Join-Path $ROOT 'venv\Scripts\python.exe'
$LAUNCH = Join-Path $BOT_DIR 'launch.ps1'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

function Invoke-Launch([string[]]$Options) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $LAUNCH -NoPause @Options
    if ($LASTEXITCODE -ne 0) { throw '启动器执行未成功，请查看上面的提示' }
}

function Invoke-Tool([string]$Selected) {
    switch ($Selected) {
        'Start' { Invoke-Launch -Options @(); break }
        'Status' { Invoke-Launch -Options @('-Status'); break }
        'Restart' { Invoke-Launch -Options @('-Restart'); break }
        'Stop' { Invoke-Launch -Options @('-Stop'); break }
        'StopAll' { Invoke-Launch -Options @('-StopAll'); break }
        'Logs' {
            $file = Join-Path $BOT_DIR 'logs\bot.log'
            if (-not (Test-Path -LiteralPath $file)) { throw '机器人日志尚未生成' }
            Write-Host '显示实时日志，按 Ctrl+C 结束。'
            Get-Content -LiteralPath $file -Encoding UTF8 -Tail 40 -Wait
            break
        }
        'Console' { Push-Location $BOT_DIR; try { & $PYTHON -B (Join-Path $BOT_DIR 'qqctl.py') } finally { Pop-Location }; break }
        'Login' {
            Invoke-Launch -Options @('-Status')
            Write-Host '请在 OneBot 服务端登录机器人账号。'
            break
        }
        'WebUI' {
            $url = 'http://127.0.0.1:6099/webui'
            try {
                Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop | Out-Null
            } catch {
                throw '本机 OneBot 管理页未启动，请先打开已安装的协议端。'
            }
            Start-Process -FilePath $url
            break
        }
        'Web' { Invoke-Launch -Options @('-WebOnly'); break }
        'Nb' {
            Push-Location $BOT_DIR
            $savedPath = $env:PATH
            try { $env:PATH = (Split-Path -Parent $PYTHON) + ';' + $env:PATH; & $env:ComSpec /k 'title NoneBot CLI' }
            finally { $env:PATH = $savedPath; Pop-Location }
            break
        }
        'Dev' {
            $existing = Get-NetTCPConnection -LocalPort 8091 -State Listen -ErrorAction SilentlyContinue
            if ($existing) { throw '请先停止正式机器人，再启动开发模式，避免重复实例' }
            Push-Location $BOT_DIR
            try { & $PYTHON -B (Join-Path $BOT_DIR 'dev.py') } finally { Pop-Location }
            break
        }
        'Sync' {
            $node = 'D:\node-v24.19.0-win-x64\node.exe'
            if (-not (Test-Path -LiteralPath $node)) { $node = 'node' }
            Write-Host '请先退出 DSH 桌面，再同步会话。'
            & $node (Join-Path $BOT_DIR 'dsh_import_remote.js') --apply
            if ($LASTEXITCODE -ne 0) { throw '同步失败，请检查上面的提示' }
            break
        }
        'AllowLan' {
            $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
            $principal = New-Object Security.Principal.WindowsPrincipal($identity)
            if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
                throw '请右键管理工具，以管理员身份运行后再选此项'
            }
            & netsh.exe advfirewall firewall delete rule 'name=NovaAI Web 8090' | Out-Null
            & netsh.exe advfirewall firewall add rule 'name=NovaAI Web 8090' 'dir=in' 'action=allow' 'protocol=TCP' 'localport=8090' 'profile=private,domain'
            if ($LASTEXITCODE -ne 0) { throw '防火墙规则设置失败' }
            Write-Host '已允许专用/域网络访问 8090；手机请使用本机的局域网 IP。'
            break
        }
        'InstallDsh' {
            $node = 'D:\node-v24.19.0-win-x64\node.exe'
            $cli = 'D:\DeepSeek-Harness\apps\cli\lib\bin.js'
            if (-not (Test-Path -LiteralPath $node) -or -not (Test-Path -LiteralPath $cli)) { throw '找不到 DSH 或 Node.js' }
            $savedHome = $env:DSH_HOME
            try {
                $env:DSH_HOME = Join-Path $ROOT 'dsh-acp-home'
                & $node $cli plugin --profile acp add ('file:' + ((Join-Path $ROOT 'dsh-qq-bridge') -replace '\\','/'))
                if ($LASTEXITCODE -ne 0) { throw '插件安装失败' }
            } finally { $env:DSH_HOME = $savedHome }
            break
        }
        'Check' {
            Invoke-Launch -Options @('-Check')
            $missing = @('qqctl.py','dev.py','erp_server.py','dsh_import_remote.js') |
                Where-Object { -not (Test-Path -LiteralPath (Join-Path $BOT_DIR $_)) }
            @{management_ok=($missing.Count -eq 0);missing=@($missing)} | ConvertTo-Json -Compress
            if ($missing.Count) { throw '管理工具的目标文件缺失' }
            break
        }
    }
}

if ($Action -ne 'Menu') {
    try { Invoke-Tool $Action; exit 0 }
    catch { Write-Host ('[错误] ' + $_.Exception.Message) -ForegroundColor Red; exit 1 }
}
$choices = @{ '1'='Start';'2'='Status';'3'='Restart';'4'='Stop';'5'='Logs';'6'='Console';
              '7'='Login';'8'='WebUI';'9'='Web';'N'='Nb';'D'='Dev';'S'='Sync';'L'='AllowLan';'I'='InstallDsh';'A'='StopAll' }
while ($true) {
    Write-Host "`nQQ 机器人管理工具" -ForegroundColor Cyan
    Write-Host '  1 启动机器人       2 查看状态          3 重启机器人'
    Write-Host '  4 停止机器人/中转  5 查看实时日志      6 命令行控制台'
    Write-Host '  7 QQ 登录状态      8 打开登录管理页    9 启动网页服务'
    Write-Host '  N 插件命令行       D 开发模式          S 同步远程会话'
    Write-Host '  L 允许手机访问     I 安装 DSH 插件     A 停止全部服务'
    Write-Host '  0 退出'
    $answer = (Read-Host '选择').Trim().ToUpperInvariant()
    if ($answer -eq '0') { break }
    if (-not $choices.ContainsKey($answer)) { Write-Host '没有该选项'; continue }
    try { Invoke-Tool $choices[$answer] }
    catch { Write-Host ('[错误] ' + $_.Exception.Message) -ForegroundColor Red }
}
