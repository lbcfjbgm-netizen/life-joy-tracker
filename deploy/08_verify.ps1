#Requires -Version 5.1
<#
  08_verify.ps1 —— 部署后验证（只读）

  干什么：
    1) 抓 8 个页面，报 HTTP 状态码 + 字节数 + 耗时
    2) 从抓回来的 HTML 里抽 4 个数据基线数字（游戏总数、已完成数、进行中数、统计页总数）
    3) 单独量 3 个页面耗时（首屏 / 列表 / 统计），跑两遍取第二遍，避开冷启动
    4) 出一张表，最后汇总「成功 N / 失败 M」

  副作用：
    无。只发 HTTP GET，只读 SQLite（mode=ro）。不写文件、不改数据、不重启站点。

  前置：
    站点得已经起来（双击 start-win.vbs，或 schtasks /run /tn "SteamTracker Start"）。
    没起来的话本脚本会把每条都标 FAIL 并提示你去启动，不会自己拉起来。

  用法：
    powershell ...\deploy\08_verify.ps1
    powershell ...\deploy\08_verify.ps1 -BaseUrl "http://127.0.0.1:8001"
#>
[CmdletBinding()]
param(
    [string]$Root = '',   # 空 = 自动定位成「本脚本所在 deploy 目录的上一级」
    [string]$BaseUrl = 'http://127.0.0.1:8001',
    [int]$TimeoutSec = 45,
    [switch]$DryRun,
    [switch]$Apply,
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

if ($Help) { Get-Help $PSCommandPath -Detailed; return }

Write-Host ''
Write-Host '==================================================' -ForegroundColor White
Write-Host '  部署验证 08_verify.ps1  （只读，不改任何东西）' -ForegroundColor White
Write-Host "  目标站点：$BaseUrl" -ForegroundColor White
Write-Host '==================================================' -ForegroundColor White

$RootPath = $null
if (Test-Path -LiteralPath $Root) { $RootPath = (Resolve-Path -LiteralPath $Root).Path }
$BaseUrl = $BaseUrl.TrimEnd('/')

# ---------------------------------------------------------------- 0. 站点活着吗
Write-Step '第 0 步 / 站点在不在'
$alive = $false
try {
    $probe = Invoke-WebRequest -Uri "$BaseUrl/" -UseBasicParsing -TimeoutSec 15 -MaximumRedirection 3
    if ($probe.StatusCode -eq 200) { $alive = $true; Write-Pass "站点有响应（HTTP 200，$($probe.RawContentLength) 字节）" }
    else { Write-Warn "站点有响应但状态码是 $($probe.StatusCode)" '可能被重定向到登录页，属于正常（用 YAMTRACK_AUTO_LOGIN_USERNAME 免登录时会跳）。' }
} catch {
    Write-Fail "连不上 $BaseUrl/" '站点没起来。启动方式只有一条：schtasks /run /tn "SteamTracker Start"（或双击 start-win.vbs）。绝对不要用 Start-Process 直接起 start_silent.py，会被 DSH 的作业对象连坐杀掉。'
}
if (-not $alive) {
    Write-Warn '站点没起来，后面的检查会全 FAIL，但本脚本照样跑完给你看，不会中途退出' ''
}

# ---------------------------------------------------------------- 1. 8 个页面
Write-Step '第 1 步 / 抓 8 个页面'
$pages = @(
    @{ Path = '/';                          Desc = '首页（游戏库）' },
    @{ Path = '/admin/game';                Desc = '游戏列表（全部）' },
    # 注意：这里只能用 ?layout=grid&density=N。绝不许用 ?status= / ?sort= / ?layout=table
    # —— 那三个参数会被视图写回用户偏好，验证一遍就把用户的界面搞乱了。
    @{ Path = '/admin/game?layout=grid&density=8'; Desc = '游戏列表（密度 8）' },
    @{ Path = '/statistics';                Desc = '统计页' },
    @{ Path = '/journal';                   Desc = '日志/时间线' },
    @{ Path = '/calendar';                  Desc = '日历' },
    @{ Path = '/lists';                     Desc = '自定义清单' },
    @{ Path = '/settings/preferences';      Desc = '设置-偏好' }
)

$results = @()
foreach ($p in $pages) {
    $url = "$BaseUrl$($p.Path)"
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $code = $null; $len = 0; $body = ''
    try {
        $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec $TimeoutSec -MaximumRedirection 3
        $code = [int]$resp.StatusCode
        $len = $resp.RawContentLength
        $body = $resp.Content
    } catch {
        $ex = $_.Exception
        if ($ex.Response -and $ex.Response.StatusCode) { $code = [int]$ex.Response.StatusCode }
        else { $code = -1 }
        $len = 0
    }
    $sw.Stop()
    $results += [pscustomobject]@{
        Path = $p.Path; Desc = $p.Desc; Code = $code; Bytes = $len
        Ms = [int]$sw.ElapsedMilliseconds; Body = $body
    }
}
Write-Host ''
Write-Host ("  {0,-38} {1,-6} {2,10} {3,8}  {4}" -f '路径', '状态', '字节', '耗时', '说明') -ForegroundColor White
Write-Host ('  ' + ('-' * 88)) -ForegroundColor DarkGray
foreach ($r in $results) {
    $codeText = if ($r.Code -eq -1) { 'ERR' } else { "$($r.Code)" }
    $color = if ($r.Code -eq 200) { 'Green' } elseif ($r.Code -eq -1 -or $r.Code -ge 500) { 'Red' } else { 'Yellow' }
    Write-Host ("  {0,-38} {1,-6} {2,10} {3,8}  {4}" -f $r.Path, $codeText, $r.Bytes, "$($r.Ms) ms", $r.Desc) -ForegroundColor $color
    if ($r.Code -eq 200) { Write-Pass "$($r.Path) → 200" }
    elseif ($r.Code -eq -1) { Write-Fail "$($r.Path) → 连不上/超时" '站点挂了或这个路由非常慢。先看 .tools\logs\site.err 最后几十行。' }
    elseif ($r.Code -ge 500) { Write-Fail "$($r.Path) → HTTP $($r.Code)（服务器内部错误）" '这是真 bug。看 .tools\logs\site.err 里对应的 Traceback。' }
    else { Write-Warn "$($r.Path) → HTTP $($r.Code)" '3xx 通常是跳登录页，属正常；4xx 说明路由或权限有问题。' }
}

# ---------------------------------------------------------------- 2. 数据基线
Write-Step '第 2 步 / 4 个数据基线数字'
function Get-BaselineFromDb {
    param([string]$DbPath, [string]$PyExe)
    if (-not (Test-Path -LiteralPath $DbPath)) { return $null }
    if (-not (Test-Path -LiteralPath $PyExe)) { return $null }
    $code = @'
import sqlite3, sys
db = sys.argv[1]
try:
    con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    cur = con.cursor()
    out = {}
    out["games"] = cur.execute("select count(*) from app_game").fetchone()[0]
    out["items"] = cur.execute("select count(*) from app_item").fetchone()[0]
    for status in ("Completed", "In progress", "Planning", "Paused", "Dropped"):
        out[status] = cur.execute("select count(*) from app_game where status=?", (status,)).fetchone()[0]
    out["events"] = cur.execute("select count(*) from events_event").fetchone()[0] if cur.execute("select count(*) from sqlite_master where type='table' and name='events_event'").fetchone()[0] else 0
    out["ach"] = cur.execute("select count(*) from app_game where achievements_total > 0").fetchone()[0]
    for k, v in out.items():
        print("%s=%s" % (k, v))
    con.close()
except Exception as exc:
    print("ERR=%s" % exc)
'@
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("verify_" + [guid]::NewGuid().ToString('N') + ".py")
    try {
        Set-Content -LiteralPath $tmp -Value $code -Encoding UTF8
        $raw = (& $PyExe $tmp $DbPath 2>&1 | Out-String)
        $map = @{}
        foreach ($line in ($raw -split "`r?`n")) {
            if ($line -match '^([^=]+)=(.+)$') { $map[$Matches[1]] = $Matches[2] }
        }
        return $map
    } catch {
        return $null
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

$dbPath = if ($RootPath) { Join-Path $RootPath 'src\db\db.sqlite3' } else { $null }
$pyExe = if ($RootPath) { Join-Path $RootPath '.venv\Scripts\python.exe' } else { $null }
$dbStats = if ($dbPath) { Get-BaselineFromDb -DbPath $dbPath -PyExe $pyExe } else { $null }

if ($dbStats -and $dbStats.ContainsKey('games')) {
    Write-Pass "直接读数据库（只读模式，mode=ro）：$dbPath"
    $b1 = $dbStats['games']; $b2 = $dbStats['items']; $b3 = $dbStats['Completed']; $b4 = $dbStats['In progress']
    Write-Info "① 游戏总数（app_game）      = $b1"
    Write-Info "② 条目总数（app_item）      = $b2"
    Write-Info "③ 已完成（Completed）       = $b3"
    Write-Info "④ 进行中（In progress）     = $b4"
    if ($dbStats.ContainsKey('ach'))   { Write-Info "   有成就数据的游戏         = $($dbStats['ach'])" }
    if ($dbStats.ContainsKey('events')){ Write-Info "   事件数（events_event）    = $($dbStats['events'])" }
    foreach ($k in @('Planning', 'Paused', 'Dropped')) {
        if ($dbStats.ContainsKey($k)) { Write-Info "   状态 $k = $($dbStats[$k])" }
    }
} elseif ($dbStats -and $dbStats.ContainsKey('ERR')) {
    Write-Fail "读数据库失败：$($dbStats['ERR'])" '可能被独占锁住。不影响页面验证，跳过数字比对。'
} else {
    Write-Warn '读不到数据库基线（路径不对或 python 不可用）' '只做页面验证。'
}

# 再从首页 HTML 里抽一组数字，和数据库对照（能发现「页面没刷新」这类问题）
Write-Step '第 3 步 / 页面上的数字 vs 数据库（交叉核对）'
$homeBody = ($results | Where-Object { $_.Path -eq '/' } | Select-Object -First 1).Body
if ($homeBody -and $homeBody.Length -gt 1000) {
    # 首页会渲染形如 "2,783" 的计数；抽所有千分位/纯数字里最大的几个做参考
    $nums = [regex]::Matches($homeBody, '(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d{3,6})(?![\d.,])') |
        ForEach-Object { $_.Groups[1].Value -replace ',', '' } |
        ForEach-Object { [int]$_ } |
        Sort-Object -Unique
    if ($nums.Count -gt 0) {
        Write-Pass "首页 HTML 里抽到 $($nums.Count) 个不同的数字（前 8 大）"
        Write-Info (($nums | Sort-Object -Descending | Select-Object -First 8) -join ' / ')
        if ($dbStats -and $dbStats.ContainsKey('games')) {
            $g = [int]$dbStats['games']
            if ($nums -contains $g) { Write-Pass "数据库的游戏总数 $g 在首页 HTML 里出现了（说明页面数据是新的）" }
            else { Write-Warn "数据库的游戏总数 $g 没在首页 HTML 里出现" '可能是首页做了分页/格式化，也可能模板缓存在输出旧数据。改过 .html/.py 的话先重启站点（坑表第 4 条）。' }
        }
    } else {
        Write-Info '首页 HTML 里没抽到显眼的数字（页面上数字可能都带图表/单位），跳过交叉核对。'
    }
} else {
    Write-Warn '没拿到首页 HTML，跳过交叉核对' '站点没起来或首页报错。'
}

# ---------------------------------------------------------------- 4. 耗时
Write-Step '第 4 步 / 3 个页面耗时（跑两遍取第二遍）'
$timingTargets = @(
    @{ Path = '/';               Desc = '首屏/游戏库' },
    @{ Path = '/admin/game';     Desc = '游戏列表' },
    @{ Path = '/statistics';     Desc = '统计页' }
)
$timingRows = @()
foreach ($t in $timingTargets) {
    $url = "$BaseUrl$($t.Path)"
    $best = $null; $second = $null
    foreach ($round in 1..2) {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        try { $null = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec $TimeoutSec -MaximumRedirection 3 } catch { }
        $sw.Stop()
        $ms = [int]$sw.ElapsedMilliseconds
        if ($round -eq 1) { $second = $ms } else { $best = $ms }
    }
    $timingRows += [pscustomobject]@{ Path = $t.Path; Desc = $t.Desc; First = $second; Second = $best }
}
Write-Host ''
Write-Host ("  {0,-20} {1,10} {2,10}  {3}" -f '路径', '第一遍', '第二遍', '说明') -ForegroundColor White
Write-Host ('  ' + ('-' * 70)) -ForegroundColor DarkGray
foreach ($r in $timingRows) {
    $color = if ($r.Second -le 800) { 'Green' } elseif ($r.Second -le 2500) { 'Yellow' } else { 'Red' }
    Write-Host ("  {0,-20} {1,8} ms {2,8} ms  {3}" -f $r.Path, $r.First, $r.Second, $r.Desc) -ForegroundColor $color
    if ($r.Second -le 800) { Write-Pass "$($r.Path) 第二遍 $($r.Second) ms（正常）" }
    elseif ($r.Second -le 2500) { Write-Warn "$($r.Second) ms 偏慢" '如果不是首次访问，看是不是计划任务优先级没设成 4（坑表第 8 条），或者图片没预热（跑 06）。' }
    else { Write-Fail "$($r.Second) ms 很慢" '先跑 06_warm_images.ps1 -Apply 预热图片；再看计划任务 Priority 是不是 4。' }
}

# ---------------------------------------------------------------- 5. 红线复检
Write-Step '第 5 步 / 两条红线复检（顺便看一眼，不改）'
if ($RootPath) {
    $envFile = Join-Path $RootPath '.env'
    if (Test-Path -LiteralPath $envFile) {
        $dbHostLine = @(Get-Content -LiteralPath $envFile -Encoding UTF8 | Where-Object { $_ -match '^\s*DB_HOST\s*=' })
        if ($dbHostLine.Count -gt 0) { Write-Fail '.env 里有 DB_HOST' '马上删掉，否则随时会切去连 Postgres。' }
        else { Write-Pass '.env 里没有 DB_HOST（走 SQLite）' }
        $debugVal = $null
        foreach ($line in (Get-Content -LiteralPath $envFile -Encoding UTF8)) {
            if ($line -match '^\s*DEBUG\s*=\s*(.*)$') { $debugVal = $Matches[1].Trim(); break }
        }
        if ($debugVal -match '^(?i)(true|1|yes|on)$') { Write-Pass "DEBUG = $debugVal（正确）" }
        elseif ($debugVal) { Write-Fail "DEBUG = $debugVal" '必须是 True，否则静态文件全 404、页面变纯文字。' }
        else { Write-Warn '没读到 DEBUG' 'settings.py 默认 False，务必在 .env 里显式写 DEBUG=True。' }
    } else {
        Write-Warn '找不到 .env' '跑 04_setup_env.ps1 检查。'
    }
    # 页面里有没有内联样式（判断 CSS 到底加没加载）
    if ($homeBody -and $homeBody.Length -gt 1000) {
        if ($homeBody -match 'main\.css') { Write-Pass '首页 HTML 引用了 main.css' }
        else { Write-Warn '首页 HTML 里没看到 main.css' '可能站点换了引用方式；如果页面也确实是纯文字，去看坑表第 1、3 条。' }
    }
}

# ---------------------------------------------------------------- 汇总
Write-Host ''
Write-Host '==================================================' -ForegroundColor White
Write-Host ("  汇总：成功 $script:OkCount / 失败 $script:FailCount / 警告 $script:WarnCount") -ForegroundColor White
Write-Host '==================================================' -ForegroundColor White
if ($script:FailCount -eq 0) {
    Write-Host '  全绿，站点是好的。' -ForegroundColor Green
} else {
    Write-Host '  有失败项，按上面每条的「怎么办」处理。' -ForegroundColor Red
}
Write-Host '  提醒：本脚本只发 GET 请求 + 只读数据库，没有改动任何东西。' -ForegroundColor DarkGray
exit $(if ($script:FailCount -gt 0) { 1 } else { 0 })