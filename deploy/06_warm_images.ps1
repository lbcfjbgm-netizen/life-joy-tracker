#Requires -Version 5.1
<#
  06_warm_images.ps1 —— 预热图片缓存（可选步骤）

  干什么：
    站点所有封面/大图都走本机缓存（.tools\raw\covers\），新机器上这个目录是空的
    （本机现在 5900 多张、约 490MB，不进版本库）。空目录时页面第一次访问每张图
    都要现下载，看起来就是「图片全灰块、一个接一个慢慢冒出来」。
    这个脚本调项目自带的 .tools\warm_images.py 提前把图抓下来存好。

  默认行为（不加 -Apply）：
    调 warm_images.py --dry-run，只打印「一共多少 URL / 已缓存多少 / 还差多少」。
    dry-run 分支在脚本里是 return 0，一个字节都不会下载。

  真跑：
    -Apply                   全部预热（约几百张，看网络可能十几分钟）
    -Apply -Only heroes      只热「大屏 hero 图」
    -Apply -Only covers      只热封面
    -Apply -Limit 50         先热 50 张试水（强烈建议先这么试）

  用法：
    powershell ...\deploy\06_warm_images.ps1
    powershell ...\deploy\06_warm_images.ps1 -Only heroes
    powershell ...\deploy\06_warm_images.ps1 -Apply -Only covers -Limit 50
#>
[CmdletBinding()]
param(
    [string]$Root = '',   # 空 = 自动定位成「本脚本所在 deploy 目录的上一级」
    [switch]$DryRun,
    [switch]$Apply,
    [ValidateSet('all', 'covers', 'heroes')]
    [string]$Only = 'all',
    [int]$Limit = 0,
    [switch]$ActiveOnly,
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
Write-Host '  预热图片 06_warm_images.ps1' -ForegroundColor White
Write-Host ("  模式：" + $(if ($doApply) { '真的下载（-Apply）' } else { '只报数量，不下载（默认）' })) -ForegroundColor White
Write-Host ("  范围：$Only" + $(if ($Limit -gt 0) { "，最多 $Limit 张" } else { '，全部' }) + $(if ($ActiveOnly) { '，只算默认列表里能看到的' } else { '' })) -ForegroundColor White
Write-Host '==================================================' -ForegroundColor White

if (-not (Test-Path -LiteralPath $Root)) { Write-Host "项目根目录不存在：$Root" -ForegroundColor Red; exit 1 }
$RootPath = (Resolve-Path -LiteralPath $Root).Path
$py = Join-Path $RootPath '.venv\Scripts\python.exe'
$srcDir = Join-Path $RootPath 'src'
$warmScript = Join-Path $RootPath '.tools\warm_images.py'
$coverDir = Join-Path $RootPath '.tools\raw\covers'
$dbFile = Join-Path $srcDir 'db\db.sqlite3'

# ---------------------------------------------------------------- 1. 前置
Write-Step '第 1 步 / 前置条件'
if (Test-Path -LiteralPath $py) { Write-Pass "python：$py" }
else { Write-Fail "找不到 $py" '先修虚拟环境；本脚本调不动 warm_images.py 了。' }
if (Test-Path -LiteralPath $warmScript) { Write-Pass "预热脚本：$warmScript" }
else { Write-Fail "找不到 $warmScript" '.tools\warm_images.py 是项目自带的，缺了没法预热。' }
if (Test-Path -LiteralPath $dbFile) { Write-Pass "数据库：$dbFile" }
else { Write-Warn '数据库不存在' '预热要靠数据库里的封面 URL，先跑 03_setup_db.ps1。' }

# ---------------------------------------------------------------- 2. 缓存目录现状
Write-Step '第 2 步 / 缓存目录现状（只读统计）'
if (Test-Path -LiteralPath $coverDir) {
    $files = @(Get-ChildItem -LiteralPath $coverDir -Recurse -File -ErrorAction SilentlyContinue)
    $sumBytes = 0
    foreach ($f in $files) { $sumBytes += $f.Length }
    $mb = [math]::Round($sumBytes / 1MB, 1)
    Write-Pass "缓存目录：$coverDir"
    Write-Pass "已有 $($files.Count) 个文件，约 $mb MB"
    if ($files.Count -eq 0) {
        Write-Warn '目录是空的（新机器第一次部署就是这样）' '跑 -Apply 预热。图片全灰块就是它造成的。'
    } elseif ($mb -lt 100) {
        Write-Warn "只有 $mb MB，可能没热完" '再跑一次 -Apply，已缓存的会自动跳过（断点续传）。'
    }
} else {
    Write-Warn "缓存目录不存在：$coverDir" '正常，新机器没有。跑 -Apply 会自动建。'
    Write-Info '这就是「图片全是灰块」的直接原因：缓存目录空，每张图都要现去 Steam 下载。'
}

# ---------------------------------------------------------------- 3. 域名白名单
Write-Step '第 3 步 / 封面域名白名单（本机只有两个能用）'
Write-Info '只用这两个：media.steampowered.com 和 shared.fastly.steamstatic.com'
Write-Info 'cloudflare / akamai 那几个在本机全超时，预热时会被自动过滤掉（输出里的 not_cacheable）。'
foreach ($host_ in @('media.steampowered.com', 'shared.fastly.steamstatic.com')) {
    $ok = $null
    try { $ok = Test-NetConnection -ComputerName $host_ -Port 443 -InformationLevel Quiet -WarningAction SilentlyContinue -ErrorAction Stop }
    catch { $ok = $null }
    if ($ok -eq $true) { Write-Pass "$host_ : 443 可连" }
    elseif ($ok -eq $false) { Write-Warn "$host_ : 443 连不上" '网络问题。预热会大量失败但不会搞坏东西；联网恢复后重跑即可（断点续传）。' }
    else { Write-Warn "$host_ : 网络连通性检测不可用" '跳过这项，不影响预热。' }
}

# ---------------------------------------------------------------- 4. 组装命令
Write-Step '第 4 步 / 将要执行的命令'
$argsList = @()
$argsList += '--only'; $argsList += $Only
if ($Limit -gt 0) { $argsList += '--limit'; $argsList += "$Limit" }
if ($ActiveOnly) { $argsList += '--active-only' }
$prettyArgs = ($argsList -join ' ')
$dryCmd = "cd `"$srcDir`" ; & `"$py`" `"$warmScript`" --dry-run $prettyArgs"
$realCmd = "cd `"$srcDir`" ; & `"$py`" `"$warmScript`" $prettyArgs"
Write-Plan "先探数量：$dryCmd"
if ($doApply) { Write-Plan "再真跑：$realCmd" } else { Write-Info "真跑命令（加 -Apply 才执行）：$realCmd" }

# ---------------------------------------------------------------- 5. 跑
function Invoke-Warm {
    param([string]$Arguments, [string]$Label)
    Write-Info "执行 $Label：& `"$py`" `"$warmScript`" $Arguments"
    $sw = [Diagnostics.Stopwatch]::StartNew()
    Push-Location -LiteralPath $srcDir
    try {
        $out = (& $py $warmScript @($Arguments -split ' ') 2>&1 | Out-String)
        $code = $LASTEXITCODE
    } catch {
        $out = $_.Exception.Message
        $code = 1
    } finally {
        Pop-Location
        $sw.Stop()
    }
    $lines = @($out.Trim() -split "`r?`n" | Where-Object { $_ -ne '' })
    foreach ($l in ($lines | Select-Object -Last 25)) { Write-Output "         $l" }
    return @{ Code = $code; Output = $out; Seconds = $sw.Elapsed.TotalSeconds }
}

Write-Step '第 5 步 / 探数量（无副作用）'
$probe = $null
if (Test-Path -LiteralPath $py -and Test-Path -LiteralPath $warmScript) {
    $probeArgs = '--dry-run ' + $prettyArgs
    $r0 = Invoke-Warm -Arguments $probeArgs -Label 'dry-run'
    if ($r0.Code -eq 0) {
        Write-Pass ("数量探测完成，用时 {0:N1} 秒" -f $r0.Seconds)
        $m = [regex]::Match($r0.Output, 'targets=(\d+) already_cached=(\d+) to_fetch=(\d+)')
        if ($m.Success) {
            Write-Info "总目标 $($m.Groups[1].Value) 张 / 已缓存 $($m.Groups[2].Value) 张 / 还差 $($m.Groups[3].Value) 张"
            if ([int]$m.Groups[3].Value -eq 0) { Write-Pass '全都已经缓存过了，不需要再跑（幂等）' }
        }
    } else {
        Write-Fail "数量探测失败（退出码 $($r0.Code)）" '看上一条输出的报错。常见：数据库被锁、.env 里 DB_HOST 被设了、依赖没装全。'
    }
} else {
    Write-Fail '前置条件不满足，跳过数量探测' '先解决第 1 步。'
}

Write-Step '第 6 步 / 真跑预热'
if (-not $doApply) {
    Write-Info '体检模式，没有下载。真要下载请重跑并加 -Apply。'
    Write-Info '建议先小批量试水，确认能下再全量：'
    Write-Info ('powershell -NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Apply -Limit 50')
} else {
    $r1 = Invoke-Warm -Arguments $prettyArgs -Label '实际下载'
    if ($r1.Code -eq 0) {
        Write-Pass ("预热完成，用时 {0:N0} 秒" -f $r1.Seconds)
        $m2 = [regex]::Match($r1.Output, 'done=(\d+).*?failed=(\d+)')
        if ($m2.Success) { Write-Info "成功 $($m2.Groups[1].Value) 张 / 失败 $($m2.Groups[2].Value) 张" }
        Write-Info '失败的通常是网络抖动，重跑会从断点继续，已缓存的自动跳过。'
    } else {
        Write-Fail "预热失败（退出码 $($r1.Code)）" '这不会搞坏站点，只是图片还得现下。联网恢复后重跑同一条命令即可。'
    }
}

# ---------------------------------------------------------------- 7. 结果
Write-Step '第 7 步 / 跑完之后再看一眼缓存目录'
if (Test-Path -LiteralPath $coverDir) {
    $files2 = @(Get-ChildItem -LiteralPath $coverDir -Recurse -File -ErrorAction SilentlyContinue)
    $sum2 = 0
    foreach ($f in $files2) { $sum2 += $f.Length }
    Write-Pass "现在有 $($files2.Count) 个文件，约 $([math]::Round($sum2/1MB,1)) MB"
} else {
    Write-Info '缓存目录还没建（只在真跑时才建）。'
}

# ---------------------------------------------------------------- 汇总
Write-Host ''
Write-Host '==================================================' -ForegroundColor White
Write-Host ("  汇总：成功 $script:OkCount / 失败 $script:FailCount / 警告 $script:WarnCount") -ForegroundColor White
Write-Host '==================================================' -ForegroundColor White
Write-Host '  这是可选步骤：跳过它站点照样能跑，只是第一屏图片慢一点。' -ForegroundColor DarkGray
if (-not $doApply) { Write-Host '  （提醒：这次只报了数量，一张图都没下载）' -ForegroundColor DarkGray }
exit $(if ($script:FailCount -gt 0) { 1 } else { 0 })