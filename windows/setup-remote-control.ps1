<#
setup-remote-control.ps1  (Windows)
让这台常开的 Windows 主机可以从 claude.ai/code（网页）和 Claude 手机 App 远程操控。

做三件事：
  1. 体检：claude CLI、版本、登录、Remote Control 可用性、目录信任、首次确认
  2. 在 settings.json 打开 remoteControlAtStartup —— 本机手动开的 claude 会话也自动出现在网页端
  3. 注册一个任务计划程序任务，登录后在隐藏窗口里常驻运行 `claude remote-control`（退出后 30 秒内自动拉起）

第一次用之前，必须在项目目录里手动运行一次  claude remote-control  完成 y/n 确认（脚本会检测并提示）。
官方文档没有明确写 Remote Control 支持原生 Windows，所以这次手动运行同时也是在验证它能用。

用法（在一个你用 claude 打开过的项目目录里，用 PowerShell 执行；不需要管理员）：
  powershell -ExecutionPolicy Bypass -File setup-remote-control.ps1                 # 安装并启动
  powershell -ExecutionPolicy Bypass -File setup-remote-control.ps1 -Status         # 看运行状态和最近日志
  powershell -ExecutionPolicy Bypass -File setup-remote-control.ps1 -Uninstall      # 卸载任务
  powershell -ExecutionPolicy Bypass -File setup-remote-control.ps1 -DryRun         # 只打印将要做的事，不改任何东西

可选参数：
  -Dir <路径>              用哪个目录当会话的工作目录（默认当前目录）
  -Name <名字>             网页端看到的会话名（默认计算机名）
  -PermissionMode <模式>   会话的权限模式（default acceptEdits plan auto dontAsk bypassPermissions manual），默认不传
  -SkipTrustCheck          跳过"目录信任 / 首次确认"的检测
#>
[CmdletBinding()]
param(
  [string]$Dir = (Get-Location).Path,
  [string]$Name = "",
  [ValidateSet("", "default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions", "manual")]
  [string]$PermissionMode = "",
  [switch]$SkipTrustCheck,
  [switch]$DryRun,
  [switch]$Status,
  [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$TaskName  = "Claude Remote Control"
$BaseDir   = Join-Path $env:LOCALAPPDATA "claude-remote-control"
$RunScript = Join-Path $BaseDir "run.ps1"
$LogFile   = Join-Path $BaseDir "claude-remote-control.log"
if ($env:CLAUDE_CONFIG_DIR) {
  $ConfDir      = $env:CLAUDE_CONFIG_DIR
  $GlobalConfig = Join-Path $env:CLAUDE_CONFIG_DIR ".claude.json"
} else {
  $ConfDir      = Join-Path $env:USERPROFILE ".claude"
  $GlobalConfig = Join-Path $env:USERPROFILE ".claude.json"
}
$Settings   = Join-Path $ConfDir "settings.json"
$MinVersion = [Version]"2.1.200"
$Utf8NoBom  = New-Object System.Text.UTF8Encoding($false)   # settings.json 不能带 BOM
$Utf8Bom    = New-Object System.Text.UTF8Encoding($true)    # .ps1 要带 BOM，PowerShell 5.1 才按 UTF-8 读

function Log($m)  { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  √ $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  ! $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "  × $m" -ForegroundColor Red; exit 1 }

if ($Name -match "[`"'`r`n]") { Die "-Name 里不能有引号或换行" }

# 在 $ErrorActionPreference = "Stop" 下，PowerShell 5.1 会把原生程序写到 stderr 的内容当成终止错误；
# 所以调用 claude / git 时临时放宽，并把输出统一转成字符串数组
function Invoke-Native {
  param([string]$Exe, [string[]]$ArgList, [switch]$DiscardStderr)
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    if ($DiscardStderr) { $out = & $Exe @ArgList 2>$null } else { $out = & $Exe @ArgList 2>&1 }
    return @($out | ForEach-Object { "$_" })
  } catch {
    return @()
  } finally {
    $ErrorActionPreference = $prev
  }
}

function Find-Claude {
  # 官方安装器装的那份最稳定，优先
  $native = Join-Path $env:USERPROFILE ".local\bin\claude.exe"
  if (Test-Path -LiteralPath $native) { return $native }
  $cmd = Get-Command claude -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  Die "找不到 claude 命令。先安装：irm https://claude.ai/install.ps1 | iex"
}

function Get-ClaudeVersion($claude) {
  $out = (Invoke-Native $claude @("--version") -DiscardStderr | Select-Object -First 1)
  if (-not $out) { return $null }
  $first = ($out -split "\s+")[0]
  $m = [regex]::Match($first, "^\d+\.\d+\.\d+")
  if ($m.Success) { return [Version]$m.Value }
  return $null
}

function Test-Login($claude) {
  try {
    $raw = (Invoke-Native $claude @("auth", "status", "--json") -DiscardStderr) -join "`n"
    if (-not $raw.Trim()) { return $null }
    return ($raw | ConvertFrom-Json)
  } catch { return $null }
}

function Normalize-Path($p) {
  try { return ([IO.Path]::GetFullPath($p)).TrimEnd("\").ToLowerInvariant() } catch { return $p.ToLowerInvariant() }
}

# 读 ~/.claude.json：目录（或它所在仓库根目录 / 任一上级目录）是否已信任、首次 y/n 确认是否已做过
function Inspect-GlobalConfig($dir) {
  if (-not (Test-Path -LiteralPath $GlobalConfig)) { return $null }
  try { $cfg = Get-Content -LiteralPath $GlobalConfig -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return $null }
  $trusted = @{}
  if ($cfg.projects) {
    foreach ($p in $cfg.projects.PSObject.Properties) {
      if ($p.Value -and $p.Value.hasTrustDialogAccepted -eq $true) { $trusted[(Normalize-Path $p.Name)] = $true }
    }
  }
  $homeDir = Normalize-Path $env:USERPROFILE
  # CLI 从当前目录往上找已信任的目录，到 git 仓库根目录为止；不在仓库里才会一直找到盘符根
  $top = (Invoke-Native "git" @("-C", $dir, "rev-parse", "--show-toplevel") -DiscardStderr | Select-Object -First 1)
  $stop = if ($top) { Normalize-Path $top } else { $null }
  $isTrusted = $false
  $cur = Normalize-Path $dir
  while ($true) {
    $isRoot = ($cur -match "^[a-z]:$")
    if ($trusted.ContainsKey($cur) -and $cur -ne $homeDir -and -not $isRoot) { $isTrusted = $true; break }
    if ($stop -and $cur -eq $stop) { break }
    $parent = Split-Path -Path $cur -Parent
    if (-not $parent -or $parent -eq $cur) { break }
    $cur = Normalize-Path $parent
  }
  return @{ trusted = $isTrusted; consent = ($cfg.remoteDialogSeen -eq $true) }
}

function Get-SettingsEnvVars {
  if (-not (Test-Path -LiteralPath $Settings)) { return @() }
  try { $cfg = Get-Content -LiteralPath $Settings -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return @() }
  if (-not $cfg.env) { return @() }
  $bad = @("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
           "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_OAUTH_TOKEN", "DISABLE_TELEMETRY",
           "DO_NOT_TRACK", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_GROWTHBOOK")
  return @($cfg.env.PSObject.Properties.Name | Where-Object { $bad -contains $_ })
}

# 只找服务模式的 claude 进程（命令行里是独立的 remote-control 参数），不碰你手动开的 --remote-control 交互会话
function Get-ServerProcesses {
  try {
    Get-CimInstance Win32_Process -Filter "Name = 'claude.exe' OR Name = 'node.exe'" -ErrorAction SilentlyContinue |
      Where-Object { $_.CommandLine -and $_.CommandLine -match "(^|\s)remote-control(\s|$)" -and $_.CommandLine -notmatch "--remote-control" }
  } catch { @() }
}

function Stop-ServerProcesses {
  foreach ($p in @(Get-ServerProcesses)) {
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
  }
}

function Preflight {
  Log "体检"
  if ([Environment]::OSVersion.Platform -ne "Win32NT") {
    if ($DryRun) { Warn "当前不是 Windows，只做 dry-run 演示" } else { Die "这个脚本只能在 Windows 上运行。macOS 请用 mac/，Linux 请用 linux/" }
  }

  $script:Claude = Find-Claude
  Ok "claude: $Claude"

  $ver = Get-ClaudeVersion $Claude
  if (-not $ver) { Die "无法读取 claude 版本（claude --version 没有输出）" }
  if ($ver -lt $MinVersion) { Die "claude 版本 $ver 太旧，建议 ≥ $MinVersion。运行: claude update" }
  Ok "版本: $ver"

  $auth = Test-Login $Claude
  if (-not $auth -or $auth.loggedIn -ne $true) { Die "claude 还没登录 claude.ai 账号。先运行: claude auth login" }
  if ($auth.apiProvider -and $auth.apiProvider -ne "firstParty") { Die "当前走的不是 Anthropic 官方 API（Bedrock / Vertex / 网关），Remote Control 不可用" }
  Ok "已登录 claude.ai"

  $vars = @("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_OAUTH_TOKEN", "DISABLE_TELEMETRY", "DO_NOT_TRACK",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_GROWTHBOOK")
  foreach ($v in $vars) {
    foreach ($scope in @("User", "Machine")) {
      if ([Environment]::GetEnvironmentVariable($v, $scope)) {
        Warn "系统环境变量（$scope）里设置了 $v，任务计划程序启动的服务会继承它，很可能让 Remote Control 不可用。在 系统属性 → 环境变量 里删掉再试"
      }
    }
  }
  $bad = @(Get-SettingsEnvVars)
  if ($bad.Count -gt 0) { Warn "$Settings 的 env 块里有: $($bad -join ' ') —— 可能让 Remote Control 不可用；下一步的探测会给出结论" }

  # 用 claude 自己判断 Remote Control 可不可用：不可用时 --help 会直接报错而不是打印帮助
  $probe = (Invoke-Native $Claude @("remote-control", "--help")) -join "`n"
  if (-not $probe.Trim() -or $probe -match "(?im)^\s*(error|×|✗)") {
    if ($DryRun) { Warn "Remote Control 可用性探测未通过（dry-run 继续）: $(($probe -split "`n")[0])" }
    else { Die "Remote Control 在这台机器上不可用，claude 的原话：`n$probe" }
  } else { Ok "Remote Control 可用" }

  if (-not (Test-Path -LiteralPath $Dir -PathType Container)) { Die "目录不存在: $Dir" }
  $script:Dir = (Resolve-Path -LiteralPath $Dir).Path.TrimEnd("\")
  $homeDir = $env:USERPROFILE.TrimEnd("\")
  if ($Dir -ieq $homeDir -or $Dir -match "^[A-Za-z]:$") { Die "不能用用户目录或盘符根目录当工作目录（Claude 从不信任它们）。cd 到一个项目目录再运行，或加 -Dir <路径>" }

  if ($SkipTrustCheck) {
    Warn "已跳过目录信任 / 首次确认检测"
  } else {
    $info = Inspect-GlobalConfig $Dir
    if ($null -eq $info) {
      Warn "读不到 $GlobalConfig，无法判断目录信任 / 首次确认状态，继续"
    } elseif (-not $info.trusted -or -not $info.consent) {
      $t = if ($info.trusted) { "yes" } else { "no" }
      $c = if ($info.consent) { "yes" } else { "no" }
      Die @"
还差一步手动确认（目录信任: $t，Remote Control 首次确认: $c）。请先在 PowerShell 里运行一次：

    cd "$Dir"; claude remote-control

  · 出现  Enable Remote Control? (y/n)  时输入 y
  · 如果弹出目录信任提示，选 Yes
  · 看到会话链接后按 Ctrl+C 退出

然后重新运行本脚本。这一步不能省：后台任务没有键盘，卡在这个提问上就会一直重启。
（确定都做过、只是检测不准：加 -SkipTrustCheck）
"@
    }
  }
  Ok "工作目录: $Dir"
}

function Enable-StartupSetting {
  Log "打开 remoteControlAtStartup（本机手动开的 claude 会话也会自动出现在网页端）"
  if ($DryRun) { Write-Host "   [dry-run] 写入 ${Settings}: `"remoteControlAtStartup`": true"; return }
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Settings) | Out-Null
  $obj = [PSCustomObject]@{}
  if (Test-Path -LiteralPath $Settings) {
    $raw = Get-Content -LiteralPath $Settings -Raw -Encoding UTF8
    if ($raw -and $raw.Trim()) {
      try { $obj = $raw | ConvertFrom-Json } catch { Warn "$Settings 不是合法 JSON，未改动；请手动加入 `"remoteControlAtStartup`": true"; return }
      if ($obj -isnot [PSCustomObject]) { Warn "$Settings 顶层不是对象，未改动"; return }
    }
  }
  if ($obj.remoteControlAtStartup -eq $true) { Ok "已经是 true，无需改动"; return }
  if (Test-Path -LiteralPath $Settings) { Copy-Item -LiteralPath $Settings -Destination "$Settings.bak-$(Get-Date -Format yyyyMMddHHmmss)" }
  $obj | Add-Member -NotePropertyName remoteControlAtStartup -NotePropertyValue $true -Force
  $json = ConvertTo-Json -InputObject $obj -Depth 64
  [IO.File]::WriteAllText($Settings, $json + "`n", $Utf8NoBom)
  Ok "已写入 $Settings"
}

function Build-RunScript {
  $sessionName = if ($Name) { $Name } else { $env:COMPUTERNAME }
  $extra = if ($PermissionMode) { " --permission-mode $PermissionMode" } else { "" }
  # 不把 claude 的输出重定向到文件：保留控制台，避免它因为没有终端而拒绝启动。会话链接去 claude.ai/code 侧边栏里按名字找
  return @"
# 由 setup-remote-control.ps1 生成，任务计划程序「$TaskName」在登录时运行它
`$ErrorActionPreference = 'Continue'
Set-Location -LiteralPath '$Dir'
`$log = '$LogFile'
while (`$true) {
  Add-Content -LiteralPath `$log -Value "[`$(Get-Date -Format s)] 启动 claude remote-control（$sessionName）"
  & '$Claude' remote-control --name '$sessionName'$extra
  Add-Content -LiteralPath `$log -Value "[`$(Get-Date -Format s)] 退出，代码 `$LASTEXITCODE，30 秒后重启"
  Start-Sleep -Seconds 30
}
"@
}

function Install-Task {
  Log "注册任务计划程序任务: $TaskName"
  $run = Build-RunScript
  if ($DryRun) {
    ($run -split "`n") | ForEach-Object { Write-Host "   | $_" }
    Write-Host "   [dry-run] Register-ScheduledTask '$TaskName'（登录时启动，隐藏窗口，无时间限制）"
    return
  }
  New-Item -ItemType Directory -Force -Path $BaseDir | Out-Null
  $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($existing) {
    Log "已有旧的任务，先停掉"
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Stop-ServerProcesses
    Start-Sleep -Seconds 2
  }
  if (Test-Path -LiteralPath $LogFile) { Move-Item -LiteralPath $LogFile -Destination "$LogFile.1" -Force }
  [IO.File]::WriteAllText($RunScript, $run, $Utf8Bom)

  $user = "$env:USERDOMAIN\$env:USERNAME"
  $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$RunScript`""
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
  $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
              -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
  $settings.ExecutionTimeLimit = "PT0S"   # 不限时长，否则默认 3 天后被杀
  $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
  Start-ScheduledTask -TaskName $TaskName
  Ok "任务已注册并启动（登录后自启，退出后 30 秒内自动拉起）"
}

function Show-Result {
  Log "等待服务启动（最多 30 秒）"
  $running = $false
  for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 2
    if (@(Get-ServerProcesses).Count -gt 0) { $running = $true; break }
  }
  if ($running) { Ok "claude remote-control 正在运行" }
  else { Warn "还没看到 claude remote-control 进程。过一会儿用 -Status 再看；如果它在反复退出，先在 PowerShell 里手动运行  claude remote-control  看它报什么错" }
  $sessionName = if ($Name) { $Name } else { $env:COMPUTERNAME }
  Write-Host @"

接下来：
  1. 在你的电脑上用登录了这台主机账号的浏览器打开 https://claude.ai/code ，侧边栏里找名为「$sessionName」的会话（带 Remote Control 标记）
  2. 手机 Claude App 里同样能看到并操作它
  3. 别让这台电脑休眠：以管理员身份运行  powercfg /change standby-timeout-ac 0
  4. 这是登录后自启，不是开机自启：重启后要有人登录一次。要无人值守可开自动登录（netplwiz）

查看状态:  powershell -ExecutionPolicy Bypass -File $PSCommandPath -Status
卸载:      powershell -ExecutionPolicy Bypass -File $PSCommandPath -Uninstall
"@
}

function Show-Status {
  $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($task) {
    $info = $task | Get-ScheduledTaskInfo
    Write-Host "任务: $TaskName  状态: $($task.State)  上次运行: $($info.LastRunTime)  上次结果: $($info.LastTaskResult)"
  } else {
    Write-Host "任务未注册（没安装，或已卸载）"
  }
  $procs = @(Get-ServerProcesses)
  if ($procs.Count -gt 0) { Write-Host "claude remote-control 进程: $($procs.ProcessId -join ', ')" } else { Write-Host "没有在运行的 claude remote-control 进程" }
  if (Test-Path -LiteralPath $LogFile) {
    Write-Host "--- 最近日志: $LogFile ---"
    Get-Content -LiteralPath $LogFile -Tail 20
    Write-Host "（这个日志只记录启动/退出。要看 claude 自己的输出或报错，在 PowerShell 里手动运行  claude remote-control）"
  } else {
    Write-Host "(暂无日志)"
  }
}

function Uninstall-Task {
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  }
  Stop-ServerProcesses
  if (Test-Path -LiteralPath $RunScript) { Remove-Item -LiteralPath $RunScript -Force }
  Ok "已卸载任务（$Settings 里的 remoteControlAtStartup 保留，不需要的话手动改成 false）"
}

if ($Status) { Show-Status; exit 0 }
if ($Uninstall) { Uninstall-Task; exit 0 }

Preflight
Enable-StartupSetting
Install-Task
if (-not $DryRun) { Show-Result }
