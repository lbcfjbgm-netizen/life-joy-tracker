#Requires -Version 5.1
<#
  05_static.ps1 —— 静态文件（collectstatic + Tailwind 编译）

  干什么（严格按这个顺序，顺序错了会白干）：
    1) 设 $env:NODE_PATH = %APPDATA%\npm\node_modules
       —— 不设会报 "Can't resolve 'tailwindcss'"（CLI 是全局装的，npx 找不到）
    2) cd src 后 npx tailwindcss -i ./static/css/input.css -o ./static/css/main.css
    3) 把 src\static\css\main.css 复制到 src\staticfiles\css\main.css
       —— 这一步绝对不能省，否则 staticfiles 那份不更新，浏览器用旧缓存
    4) 可选：python manage.py collectstatic --noinput

  默认行为（不传 -Apply）：
    只体检：tailwind 在不在、NODE_PATH 能不能解析到、两个 main.css 的时间差、staticfiles 有多少文件。
    一个字节都不写。

  真跑：
    -Apply                 编译 + 复制（会动 src\static\css\main.css 和 src\staticfiles\css\main.css）
    -Apply -Collect        再加上 collectstatic

  用法：
    powershell ...\deploy\05_static.ps1
    powershell ...\deploy\05_static.ps1 -Apply
    powershell ...\deploy\05_static.ps1 -Apply -Collect
#>
[CmdletBinding()]
param(
    [string]$Root = '',   # 空 = 自动定位成「本脚本所在 deploy 目录的上一级」
    [switch]$DryRun,
    [switch]$Apply,
    [switch]$Collect,
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
Write-Host '  静态文件 05_static.ps1' -ForegroundColor White
Write-Host ("  模式：" + $(if ($doApply) { '会真的编译（-Apply）' } else { '只体检，不写任何文件（默认）' })) -ForegroundColor White
Write-Host '==================================================' -ForegroundColor White

if (-not (Test-Path -LiteralPath $Root)) { Write-Host "项目根目录不存在：$Root" -ForegroundColor Red; exit 1 }
$RootPath = (Resolve-Path -LiteralPath $Root).Path
$py = Join-Path $RootPath '.venv\Scripts\python.exe'
$srcDir = Join-Path $RootPath 'src'
$inputCss = Join-Path $srcDir 'static\css\input.css'
$outCss = Join-Path $srcDir 'static\css\main.css'
$dstCss = Join-Path $srcDir 'staticfiles\css\main.css'
$staticFilesDir = Join-Path $srcDir 'staticfiles'
$nodeModules = Join-Path $env:APPDATA 'npm\node_modules'

# ---------------------------------------------------------------- 1. 输入文件
Write-Step '第 1 步 / 输入文件'
if (Test-Path -LiteralPath $inputCss) { Write-Pass "input.css：$inputCss" }
else { Write-Fail "找不到 $inputCss" 'tailwind 的入口文件，缺了没法编译。从仓库/备份找回。' }
if (Test-Path -LiteralPath $outCss) {
    $o = Get-Item -LiteralPath $outCss
    Write-Pass "产物 main.css：$([math]::Round($o.Length/1KB,1)) KB，改动时间 $($o.LastWriteTime)"
} else { Write-Warn "还没有产物 $outCss" '第一次编译会生成它。' }
if (Test-Path -LiteralPath $dstCss) {
    $dstat = Get-Item -LiteralPath $dstCss
    Write-Pass "staticfiles 副本：$([math]::Round($dstat.Length/1KB,1)) KB，改动时间 $($dstat.LastWriteTime)"
    if ((Test-Path -LiteralPath $outCss)) {
        $hashA = (Get-FileHash -LiteralPath $outCss -Algorithm SHA256).Hash
        $hashB = (Get-FileHash -LiteralPath $dstCss -Algorithm SHA256).Hash
        if ($hashA -eq $hashB) { Write-Pass '两份 main.css 内容一致（当前是同步的）' }
        else { Write-Warn '两份 main.css 内容不一致' '说明编译完忘了 Copy-Item。跑一次 -Apply 就能同步（第 5 步会自动复制）。' }
    }
} else { Write-Warn "还没有 staticfiles 副本 $dstCss" '跑一次 -Apply 会自动生成。' }

# ---------------------------------------------------------------- 2. NODE_PATH
Write-Step '第 2 步 / NODE_PATH（关键，不设必失败）'
Write-Info "将要设置：`$env:NODE_PATH = `"$nodeModules`""
$twModule = Join-Path $nodeModules 'tailwindcss'
if (Test-Path -LiteralPath $twModule) {
    Write-Pass "tailwindcss 模块存在：$twModule"
} else {
    Write-Fail "NODE_PATH 指向的位置没有 tailwindcss：$twModule" '执行 npm install -g tailwindcss。装完这里就会出现 tailwindcss 目录。'
}
$npxCmd = Get-Command npx -ErrorAction SilentlyContinue
if ($npxCmd) { Write-Pass "npx：$($npxCmd.Source)" }
else { Write-Fail '找不到 npx' '装 Node.js LTS，装完重开窗口。' }

# 真正把 NODE_PATH 设进当前进程（只影响本脚本这一进程，退出即失效，不改系统环境变量）
$originalNodePath = $env:NODE_PATH
$env:NODE_PATH = $nodeModules
Write-Pass "本进程 NODE_PATH 已设为 $env:NODE_PATH（只影响本次运行，不改系统变量）"

# 抽一次「能不能解析到 tailwindcss」做只读验证：列包目录 + 读 package.json
$twVersion = ''
$twPkg = Join-Path $twModule 'package.json'
if (Test-Path -LiteralPath $twPkg) {
    try {
        $twVersion = (Get-Content -LiteralPath $twPkg -Raw -Encoding UTF8 | ConvertFrom-Json).version
        Write-Pass "tailwindcss 版本：$twVersion"
        if ($twVersion -match '^3\.') {
            Write-Warn '版本是 3.x，但 input.css 用的是 v4 语法（@import "tailwindcss"）' '应该是 v4（本机实测 4.3.3）。用 v3 编译会直接报错，别降级。'
        }
    } catch {
        Write-Warn "读 tailwindcss 版本失败：$($_.Exception.Message)" '不影响编译，跳过。'
    }
}

# ---------------------------------------------------------------- 3. 计划
Write-Step '第 3 步 / 将要执行的命令（逐条照抄即可）'
Write-Plan "`$env:NODE_PATH = `"$nodeModules`""
Write-Plan "cd `"$srcDir`""
Write-Plan 'npx tailwindcss -i ./static/css/input.css -o ./static/css/main.css'
Write-Plan "Copy-Item `"$outCss`" `"$dstCss`" -Force"
if ($Collect) { Write-Plan "cd `"$srcDir`" ; & `"$py`" manage.py collectstatic --noinput" }
else { Write-Info '（collectstatic 没开。要跑就加 -Collect；本机平时不需要，staticfiles 靠 tailwind 的 Copy-Item 就够）' }
Write-Info '注意：这些命令必须在 src 目录下跑，在项目根跑 tailwind 会找不到 ./static/css/input.css。'

# ---------------------------------------------------------------- 4. 编译
Write-Step '第 4 步 / 编译 Tailwind'
if (-not $doApply) {
    Write-Info '体检模式，没有编译。真要编译请重跑并加 -Apply。'
    if (Test-Path -LiteralPath $outCss) {
        $cur = Get-Item -LiteralPath $outCss
        Write-Info "当前 main.css 是 $($cur.LastWriteTime) 的版本，可以先用着。"
    }
} else {
    if (-not (Test-Path -LiteralPath $inputCss)) {
        Write-Fail '输入文件不存在，跳过编译' '先解决第 1 步。'
    } elseif (-not (Test-Path -LiteralPath $twModule)) {
        Write-Fail 'tailwindcss 没装，跳过编译' 'npm install -g tailwindcss'
    } elseif (-not $npxCmd) {
        Write-Fail 'npx 不可用，跳过编译' '装 Node.js。'
    } else {
        $beforeSize = if (Test-Path -LiteralPath $outCss) { (Get-Item -LiteralPath $outCss).Length } else { 0 }
        Write-Info "执行：cd `"$srcDir`" ; npx tailwindcss -i ./static/css/input.css -o ./static/css/main.css"
        $sw = [Diagnostics.Stopwatch]::StartNew()
        Push-Location -LiteralPath $srcDir
        try {
            $twOut = (& npx tailwindcss -i './static/css/input.css' -o './static/css/main.css' 2>&1 | Out-String)
            $twCode = $LASTEXITCODE
        } catch {
            $twCode = 1
            $twOut = $_.Exception.Message
        } finally {
            Pop-Location
            $sw.Stop()
        }
        Write-Output ($twOut.Trim() -split "`r?`n" | Select-Object -First 6 | ForEach-Object { "         $_" })
        if ($twCode -eq 0) {
            $afterSize = if (Test-Path -LiteralPath $outCss) { (Get-Item -LiteralPath $outCss).Length } else { 0 }
            Write-Pass ("tailwind 编译完成，用时 {0:N1} 秒，main.css {1} KB → {2} KB" -f $sw.Elapsed.TotalSeconds, [math]::Round($beforeSize/1KB,1), [math]::Round($afterSize/1KB,1))
        } else {
            Write-Fail "tailwind 编译失败（退出码 $twCode）" '看上面几行报错：如果报「Can''t resolve ''tailwindcss''」就是 NODE_PATH 没设对；如果报 @import 语法错，就是 tailwind 版本太老（需要 4.x）。'
        }
    }
}

# ---------------------------------------------------------------- 5. 复制
Write-Step '第 5 步 / 同步到 staticfiles（绝对不能省）'
if (-not $doApply) {
    Write-Info "体检模式，没有复制。真跑时会：Copy-Item `"$outCss`" `"$dstCss`" -Force"
} else {
    if (-not (Test-Path -LiteralPath $outCss)) {
        Write-Fail '没有 main.css 可复制' '上一步编译失败了，先解决它。'
    } else {
        if (-not (Test-Path -LiteralPath (Split-Path -Parent $dstCss))) {
            try { New-Item -ItemType Directory -Path (Split-Path -Parent $dstCss) -Force | Out-Null }
            catch { Write-Fail "建 staticfiles\css 目录失败：$($_.Exception.Message)" '手工建一下这个目录。' }
        }
        try {
            Copy-Item -LiteralPath $outCss -Destination $dstCss -Force
            $h1 = (Get-FileHash -LiteralPath $outCss -Algorithm SHA256).Hash
            $h2 = (Get-FileHash -LiteralPath $dstCss -Algorithm SHA256).Hash
            if ($h1 -eq $h2) { Write-Pass '已复制，两份 main.css 哈希一致' }
            else { Write-Fail '复制后哈希还不一致' '可能被别的进程占着，稍后重试。' }
        } catch {
            Write-Fail "复制失败：$($_.Exception.Message)" '检查 src\staticfiles\css 是否只读，或文件被占用。'
        }
    }
}

# ---------------------------------------------------------------- 6. collectstatic
Write-Step '第 6 步 / collectstatic（可选）'
if (-not $Collect) {
    Write-Info '没要求跑 collectstatic，跳过。要跑就加 -Collect。'
} else {
    if (-not $doApply) {
        Write-Plan "cd `"$srcDir`" ; & `"$py`" manage.py collectstatic --noinput"
    } elseif (-not (Test-Path -LiteralPath $py)) {
        Write-Fail "找不到 $py，跳过 collectstatic" '先修好虚拟环境。'
    } else {
        Push-Location -LiteralPath $srcDir
        try {
            & $py manage.py collectstatic --noinput
            $cCode = $LASTEXITCODE
        } catch {
            $cCode = 1
            Write-Fail "collectstatic 抛异常：$($_.Exception.Message)" '把报错贴出来排查。'
        } finally {
            Pop-Location
        }
        if ($cCode -eq 0) { Write-Pass 'collectstatic 完成' }
        elseif ($cCode) { Write-Fail "collectstatic 失败（退出码 $cCode）" '本项目 DEBUG=True 时静态文件由 Django 直接服务，collectstatic 不是必须的；失败也可以先用站点。' }
    }
}

# ---------------------------------------------------------------- 7. 结果
Write-Step '第 7 步 / 现在静态文件的状态'
if (Test-Path -LiteralPath $staticFilesDir) {
    $sfCount = @(Get-ChildItem -LiteralPath $staticFilesDir -Recurse -File -ErrorAction SilentlyContinue).Count
    Write-Pass "src\staticfiles 下有 $sfCount 个文件"
} else {
    Write-Warn 'src\staticfiles 目录不存在' '首次部署正常；跑一次 -Apply 会生成。'
}
Write-Info '提醒：静态文件改完不用重启站点（每请求读盘）。但改 .html / .py 必须重启，见 README 坑表第 4 条。'

# 还原 NODE_PATH，避免污染同一窗口后续命令
if ($null -eq $originalNodePath) { Remove-Item Env:NODE_PATH -ErrorAction SilentlyContinue }
else { $env:NODE_PATH = $originalNodePath }

# ---------------------------------------------------------------- 汇总
Write-Host ''
Write-Host '==================================================' -ForegroundColor White
Write-Host ("  汇总：成功 $script:OkCount / 失败 $script:FailCount / 警告 $script:WarnCount") -ForegroundColor White
Write-Host '==================================================' -ForegroundColor White
if ($script:FailCount -eq 0) {
    Write-Host '  静态文件这块没问题，五步必跑流程到这里就结束了。' -ForegroundColor Green
    Write-Host '  下一步：双击 start-win.vbs 起站点，或跑 08_verify.ps1 验一遍。' -ForegroundColor Green
} else {
    Write-Host '  有失败项，照上面「怎么办」处理。' -ForegroundColor Red
}
if (-not $doApply) { Write-Host '  （提醒：这次是体检模式，没写任何文件）' -ForegroundColor DarkGray }
exit $(if ($script:FailCount -gt 0) { 1 } else { 0 })