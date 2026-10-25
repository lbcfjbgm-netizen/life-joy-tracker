#Requires -Version 5.1
<#
  07_tasks.ps1 —— 注册 Windows 计划任务（默认只打印，不注册）

  本机实际的计划任务（2026-10-25 用 schtasks /query 逐个核对过）：
    1. SteamTracker Start        按需启动整个站点。没有任何触发器，靠手动或被看门狗调用
    2. SteamTracker Watchdog     每 5 分钟探一次 8001，连续几次探不通就 schtasks /run 启动
    3. SteamTracker TokenRefresh 每天 03:50 刷新 Steam 家庭共享令牌写进 .env
    4. SteamTracker RecentPlay   每天 04:00 把最近玩过的游戏标成「进行中」

  第 5 个「每天 10:00 自动导入 Steam」不在计划任务里，它是站内 Celery beat 的周期任务
  （存在 django_celery_beat_periodictask 表里），靠 worker + beat 两个进程跑。
  所以这里不需要注册，也别去注册。

  为什么必须走计划任务启动：
    直接用 Start-Process / Popen 起 start_silent.py，会被 DSH 的 Windows 作业对象
    （JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE）连坐杀掉，日志干干净净查不出原因。
    计划任务不在任何 Job 里，起的进程才是真独立的。

  为什么必须设 Priority=4：
    不设的话 Windows 会把进程优先级降到 BelowNormal，页面实测慢 24%~35%。

  默认行为（不加 -Apply）：
    只打印「将要注册的 4 个任务 + 完整命令 + XML 内容」，一个都不注册。
  真注册：
    -Apply
  额外：
    -Run        注册完立刻触发一次 SteamTracker Start（等价 schtasks /run，需配合 -Apply）
    -Disable    把 4 个任务全部禁用（含 Watchdog，临时停机时用）

  用法：
    powershell ...\deploy\07_tasks.ps1
    powershell ...\deploy\07_tasks.ps1 -Apply
    powershell ...\deploy\07_tasks.ps1 -Apply -Run
#>
[CmdletBinding()]
param(
    [string]$Root = '',   # 空 = 自动定位成「本脚本所在 deploy 目录的上一级」
    [switch]$DryRun,
    [switch]$Apply,
    [switch]$Run,
    [switch]$Disable,
    [switch]$Help
)

# $Root 默认自动定位：本脚本在 <项目根>\deploy\ 下，所以项目根就是脚本目录的上一级。
# 注意：不能把这段写进 param 的默认值里 —— 脚本带 [CmdletBinding()] 时，
# param 默认值求值阶段 $PSScriptRoot 还是空的（PowerShell 5.1 / 7 都一样），
# 会在「项目根目录不存在：」那里误报。必须放在 param 块之后。
if ([string]::IsNullOrWhiteSpace($Root)) { $Root = Split-Path -Parent $PSScriptRoot }
if ([string]::IsNullOrWhiteSpace($Root)) {
    Write-Host '无法自动定位项目根目录（$PSScriptRoot 为空）。请手工指定：-Root "项目根路径"' -ForegroundColor Red
    exit 1
}

$script:OkCount = 0
$script:FailCount = 0
$script:WarnCount = 0

function Write-Step { param([string]$Text) Write-Host ''; Write-Host "── $Text ──────────────────────────────" -ForegroundColor Cyan }
function Write-Pass { param([string]$Text) $script:OkCount++; Write-Host "  [OK]   $Text" -ForegroundColor Green }
function Write-Warn { param([string]$Text, [string]$Fix) $script:WarnCount++; Write-Host "  [WARN] $Text" -ForegroundColor Yellow; if ($Fix) { Write-Host "         怎么办: $Fix" -ForegroundColor Yellow } }
function Write-Fail { param([string]$Text, [string]$Fix) $script:FailCount++; Write-Host "  [FAIL] $Text" -ForegroundColor Red; if ($Fix) { Write-Host "         怎么办: $Fix" -ForegroundColor Red } }
function Write-Info { param([string]$Text) Write-Host "         $Text" -ForegroundColor DarkGray }
function Write-Plan { param([string]$Text) Write-Host "  [计划] $Text" -ForegroundColor Magenta }

if ($Help) { Get-Help $PSCommandPath -Detailed; return }

$doApply = ($Apply -and -not $DryRun)

Write-Host ''
Write-Host '==================================================' -ForegroundColor White
Write-Host '  计划任务 07_tasks.ps1' -ForegroundColor White
Write-Host ("  模式：" + $(if ($doApply) { '会真的注册/修改（-Apply）' } else { '只打印，不动系统（默认）' })) -ForegroundColor White
Write-Host '==================================================' -ForegroundColor White

if (-not (Test-Path -LiteralPath $Root)) { Write-Host "项目根目录不存在：$Root" -ForegroundColor Red; exit 1 }
$RootPath = (Resolve-Path -LiteralPath $Root).Path
$py = Join-Path $RootPath '.venv\Scripts\python.exe'
$pyw = Join-Path $RootPath '.venv\Scripts\pythonw.exe'
$startSilent = Join-Path $RootPath 'start_silent.py'
$watchdog = Join-Path $RootPath '.tools\watchdog.py'
$tokenRefresh = Join-Path $RootPath '.tools\refresh-steam-token.py'
$recentPlay = Join-Path $RootPath 'scripts\mark_recent_play.py'
$tmpDir = Join-Path $RootPath 'deploy\_tmp'

# ---------------------------------------------------------------- 1. 前置
Write-Step '第 1 步 / 前置条件'
$missingExe = @()
foreach ($p in @($py, $pyw, $startSilent, $watchdog, $tokenRefresh, $recentPlay)) {
    if (Test-Path -LiteralPath $p) { Write-Pass "存在：$p" }
    else { Write-Fail "不存在：$p" '注册之前必须存在，否则任务到点会静默失败（计划任务不会告诉你文件没了）。'; $missingExe += $p }
}
if ($missingExe.Count -gt 0) {
    Write-Warn '有文件缺失，后面照样把命令打印出来给你看，但别急着 -Apply' '先把文件补齐。'
}

# ---------------------------------------------------------------- 2. 当前状态
Write-Step '第 2 步 / 当前已经注册了哪些（只读 schtasks /query）'
$taskNames = @('SteamTracker Start', 'SteamTracker Watchdog', 'SteamTracker TokenRefresh', 'SteamTracker RecentPlay')
$existing = @{}
foreach ($t in $taskNames) {
    $q = schtasks /query /tn "$t" /fo LIST 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0) {
        $existing[$t] = $true
        Write-Pass "已存在：$t"
        # 顺手看优先级：这是坑表第 8 条
        $xml = schtasks /query /tn "$t" /xml 2>&1 | Out-String
        $pm = [regex]::Match($xml, '<Priority>(\d+)</Priority>')
        if ($pm.Success) {
            $prio = [int]$pm.Groups[1].Value
            if ($prio -eq 4) { Write-Info "优先级 <Priority>4</Priority>（Normal，正确）" }
            else { Write-Warn "优先级是 $prio，不是 4" '会被降到 BelowNormal，页面慢 24%~35%。重新注册一次（带 -Apply）就会写成 4。' }
        } else {
            Write-Warn '任务 XML 里没有 <Priority>，会被当成默认（BelowNormal）' '需要重新注册并写死 4。'
        }
    } else {
        $existing[$t] = $false
        Write-Warn "没有：$t" '本脚本 -Apply 会创建它。'
    }
}

# ---------------------------------------------------------------- 3. 任务定义
Write-Step '第 3 步 / 将要注册的任务定义'

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
Write-Info "登录身份：$currentUser（LogonType=InteractiveToken，不存密码）"

function New-TaskXml {
    param(
        [string]$Description,
        [string]$Command,
        [string]$Arguments,
        [string]$WorkingDirectory,
        [string]$TriggerXml,
        [int]$ExecutionMinutes = 0
    )
    $execLimit = if ($ExecutionMinutes -gt 0) { "    <ExecutionTimeLimit>PT$($ExecutionMinutes)M</ExecutionTimeLimit>`n" } else { '' }
    return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>$Description</Description>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>$currentUser</UserId>
      <LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
$execLimit    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Priority>4</Priority>
    <IdleSettings>
      <Duration>PT10M</Duration>
      <WaitTimeout>PT1H</WaitTimeout>
      <StopOnIdleEnd>true</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
  </Settings>
  <Triggers>
$TriggerXml
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>$Command</Command>
      <Arguments>$Arguments</Arguments>
      <WorkingDirectory>$WorkingDirectory</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
}

$triggerNone = '    <!-- 没有触发器：只在手动或被看门狗调用时运行 -->'
$trigger5Min = @"
    <TimeTrigger>
      <StartBoundary>2026-10-21T00:00:00+08:00</StartBoundary>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>true</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
"@
$trigger0350 = @"
    <CalendarTrigger>
      <StartBoundary>2026-10-21T03:50:00+08:00</StartBoundary>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
"@
$trigger0400 = @"
    <CalendarTrigger>
      <StartBoundary>2026-10-21T04:00:00+08:00</StartBoundary>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
"@

$tasks = @(
    @{
        Name  = 'SteamTracker Start'
        Desc  = 'Start the local Steam tracker stack on demand (runs outside any DSH job)'
        Cmd   = $pyw
        Args  = '"' + $startSilent + '"'
        Work  = $RootPath
        Trig  = $triggerNone
        Limit = 0
        When  = '没有触发器（手动 / 被看门狗调用）'
    },
    @{
        Name  = 'SteamTracker Watchdog'
        Desc  = 'Restart the local Steam tracker site when port 8001 stops answering'
        Cmd   = $pyw
        Args  = '"' + $watchdog + '"'
        Work  = $RootPath
        Trig  = $trigger5Min
        Limit = 10
        When  = '每 5 分钟'
    },
    @{
        Name  = 'SteamTracker TokenRefresh'
        Desc  = 'Refresh the Steam family token from the dedicated Chrome profile, daily 03:50'
        Cmd   = $pyw
        Args  = '"' + $tokenRefresh + '" --task'
        Work  = $RootPath
        Trig  = $trigger0350
        Limit = 15
        When  = '每天 03:50'
    },
    @{
        Name  = 'SteamTracker RecentPlay'
        Desc  = 'Steam recently played -> In progress, daily 04:00'
        Cmd   = $py
        Args  = '"' + $recentPlay + '"'
        Work  = $RootPath
        Trig  = $trigger0400
        Limit = 30
        When  = '每天 04:00'
    }
)

foreach ($t in $tasks) {
    Write-Host ''
    Write-Host "  ▸ $($t.Name)" -ForegroundColor White
    Write-Info "作用：$($t.Desc)"
    Write-Info "触发：$($t.When)"
    Write-Info "命令：$($t.Cmd)"
    Write-Info "参数：$($t.Args)"
    Write-Info "起始目录：$($t.Work)"
    Write-Info '优先级：<Priority>4</Priority>（Normal，必须写，否则页面慢 24%~35%）'
}

Write-Step '第 4 步 / 要执行的具体命令（可直接复制）'
foreach ($t in $tasks) {
    $xmlPath = Join-Path $tmpDir ("task_" + ($t.Name -replace ' ', '_') + ".xml")
    Write-Plan ("schtasks /create /tn `"" + $t.Name + "`" /xml `"" + $xmlPath + "`" /f")
}
Write-Host ''
Write-Info '完整手动流程（不想用本脚本时照这个做）：'
Write-Info '1) 把下面每个任务的 XML 存成 deploy\_tmp\task_xxx.xml（UTF-16 编码）'
Write-Info '2) schtasks /create /tn "任务名" /xml "路径" /f'
Write-Info '3) schtasks /query /tn "任务名" /xml   ← 确认里面有 <Priority>4</Priority>'

# 展开第一个任务的 XML 给用户看（只打印，不写盘）
Write-Step '第 5 步 / XML 长什么样（示例：SteamTracker Start）'
$sampleXml = New-TaskXml -Description $tasks[0].Desc -Command $tasks[0].Cmd -Arguments $tasks[0].Args `
    -WorkingDirectory $tasks[0].Work -TriggerXml $tasks[0].Trig -ExecutionMinutes 0
foreach ($line in ($sampleXml -split "`r?`n")) { Write-Host "         $line" -ForegroundColor DarkGray }

# ---------------------------------------------------------------- 6. 注册
Write-Step '第 6 步 / 注册'
if ($Disable) {
    if (-not $doApply) {
        Write-Info '你要求禁用，但当前是只打印模式。真禁用请加 -Apply。'
        foreach ($t in $tasks) { Write-Plan "schtasks /change /tn `"$($t.Name)`" /disable" }
    } else {
        foreach ($t in $tasks) {
            $r = schtasks /change /tn "$($t.Name)" /disable 2>&1 | Out-String
            if ($LASTEXITCODE -eq 0) { Write-Pass "已禁用：$($t.Name)" }
            else { Write-Fail "禁用失败：$($t.Name) → $($r.Trim())" '任务可能不存在，先 -Apply 注册一遍。' }
        }
        Write-Info '全部禁用后站点不会再被自动拉起。要恢复：schtasks /change /tn "任务名" /enable'
    }
} elseif (-not $doApply) {
    Write-Info '只打印模式，什么都没注册。真注册请重跑并加 -Apply：'
    Write-Info ('powershell -NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Apply')
    Write-Info '注册需要「以管理员身份」运行 PowerShell 吗？—— 注册当前用户自己的任务不需要管理员，直接跑就行。'
} else {
    if (-not (Test-Path -LiteralPath $tmpDir)) {
        try { New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null; Write-Pass "创建临时目录：$tmpDir" }
        catch { Write-Fail "建临时目录失败：$($_.Exception.Message)" 'deploy 目录可能只读。' }
    }
    foreach ($t in $tasks) {
        $xmlPath = Join-Path $tmpDir ("task_" + ($t.Name -replace ' ', '_') + ".xml")
        $xmlText = New-TaskXml -Description $t.Desc -Command $t.Cmd -Arguments $t.Args `
            -WorkingDirectory $t.Work -TriggerXml $t.Trig -ExecutionMinutes $t.Limit
        try {
            # schtasks 要 UTF-16（带 BOM）的 XML
            [System.IO.File]::WriteAllText($xmlPath, $xmlText, [System.Text.Encoding]::Unicode)
            Write-Pass "已生成 XML：$xmlPath"
        } catch {
            Write-Fail "写 XML 失败：$($_.Exception.Message)" '跳过这个任务，其它继续。'
            continue
        }
        $createOut = schtasks /create /tn "$($t.Name)" /xml "$xmlPath" /f 2>&1 | Out-String
        if ($LASTEXITCODE -eq 0) {
            Write-Pass "已注册：$($t.Name)"
        } else {
            $hint = '常见原因：已存在同名任务且被别的用户所有 / 命令行里的路径带空格没加引号 / 任务计划服务没开。'
            Write-Fail "注册失败：$($t.Name) → $($createOut.Trim())" $hint
        }
    }
    # 立刻回读验证优先级
    Write-Info '回读验证（确认 Priority=4）：'
    foreach ($t in $tasks) {
        $xmlNow = schtasks /query /tn "$($t.Name)" /xml 2>&1 | Out-String
        $pm = [regex]::Match($xmlNow, '<Priority>(\d+)</Priority>')
        if ($pm.Success -and [int]$pm.Groups[1].Value -eq 4) { Write-Pass "$($t.Name) ：Priority=4" }
        elseif ($pm.Success) { Write-Fail "$($t.Name) ：Priority=$($pm.Groups[1].Value)，不是 4" '页面会变慢，重跑 -Apply。' }
        else { Write-Warn "$($t.Name) ：回读不到 XML" '任务可能没建成，用 schtasks /query /tn 手工看一眼。' }
    }
}

# ---------------------------------------------------------------- 7. 站内 Celery beat
Write-Step '第 7 步 / 第 5 个「任务」不用注册'
Write-Info '「每天 10:00 自动导入 Steam」是站内 Celery beat 的周期任务，存在数据库表里：'
Write-Info '  django_celery_beat_periodictask'
Write-Info '它靠 celery worker + celery beat 两个进程跑，由 start_silent.py 一起拉起。'
Write-Info '不要为它注册计划任务，否则会一天导两次。'
Write-Info '要改时间：网站「设置 → 导入数据」，或直接改那张表。'

# ---------------------------------------------------------------- 8. 立刻启动
Write-Step '第 8 步 / 立刻启动站点（可选）'
if (-not $Run) {
    Write-Info '没要求立刻启动。手动启动就一条命令（不要用 Start-Process！会被连坐杀掉）：'
    Write-Info 'schtasks /run /tn "SteamTracker Start"'
} elseif (-not $doApply) {
    Write-Plan 'schtasks /run /tn "SteamTracker Start"'
    Write-Info '（需要 -Apply 才会真执行）'
} else {
    $runOut = schtasks /run /tn "SteamTracker Start" 2>&1 | Out-String
    if ($LASTEXITCODE -eq 0) {
        Write-Pass '已触发启动，等 15 秒后访问 http://127.0.0.1:8001'
        Write-Info '（启动是异步的，立刻访问可能连不上，稍等再试）'
    } else {
        Write-Fail "触发失败：$($runOut.Trim())" '任务可能没注册成功；先看上一步的输出。'
    }
}

# ---------------------------------------------------------------- 汇总
Write-Host ''
Write-Host '==================================================' -ForegroundColor White
Write-Host ("  汇总：成功 $script:OkCount / 失败 $script:FailCount / 警告 $script:WarnCount") -ForegroundColor White
Write-Host '==================================================' -ForegroundColor White
Write-Host '  记住：绝对不要用 Start-Process / Popen 直接起 start_silent.py，' -ForegroundColor DarkGray
Write-Host '        一律走 schtasks /run /tn "SteamTracker Start"。' -ForegroundColor DarkGray
if (-not $doApply) { Write-Host '  （提醒：这次只打印命令，系统计划任务一个字都没改）' -ForegroundColor DarkGray }
if ($doApply -and (Test-Path -LiteralPath $tmpDir)) {
    Write-Info "生成的 XML 留在 $tmpDir 里，方便你核对或手工重放；不需要了可以整个删掉。"
}
exit $(if ($script:FailCount -gt 0) { 1 } else { 0 })