#Requires -Version 5.1
<#
  01_check_env.ps1 —— 环境体检（纯只读）

  干什么：
    把跑这个站点需要的东西一次性全查一遍：Python 版本、Node、Chrome、磁盘、8001 端口、
    .venv、关键目录、关键文件。不达标的那一项会明确告诉你「哪里不对 + 怎么补」。

  副作用：
    无。只读目录、只读文件、只读注册表式查询和端口查询。不安装、不改文件、不重启站点。

  用法（在任意目录下都能跑）：
    # 在项目根目录（有 deploy\ 和 src\ 的那一层）执行：
    powershell -NoProfile -ExecutionPolicy Bypass -File ".\deploy\01_check_env.ps1"

  可选参数：
    -Root "D:\别的路径\yamtrack"   换项目根目录。默认不用给：自动取本脚本所在目录（deploy）
                                    的上一级，也就是项目根。
    -Help                          只打印这段说明
#>
[CmdletBinding()]
param(
    [string]$Root = '',   # 空 = 自动定位成「本脚本所在 deploy 目录的上一级」
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

# ---------------------------------------------------------------- 公共报错小工具
$script:OkCount = 0
$script:FailCount = 0
$script:WarnCount = 0

function Write-Step {
    param([string]$Text)
    Write-Host ''
    Write-Host "── $Text ──────────────────────────────" -ForegroundColor Cyan
}
function Write-Pass {
    param([string]$Text)
    $script:OkCount++
    Write-Host "  [OK]   $Text" -ForegroundColor Green
}
function Write-Warn {
    param([string]$Text, [string]$Fix)
    $script:WarnCount++
    Write-Host "  [WARN] $Text" -ForegroundColor Yellow
    if ($Fix) { Write-Host "         怎么办: $Fix" -ForegroundColor Yellow }
}
function Write-Fail {
    param([string]$Text, [string]$Fix)
    $script:FailCount++
    Write-Host "  [FAIL] $Text" -ForegroundColor Red
    if ($Fix) { Write-Host "         怎么办: $Fix" -ForegroundColor Red }
}

if ($Help) {
    Get-Help $PSCommandPath -Detailed
    return
}

Write-Host ''
Write-Host '==================================================' -ForegroundColor White
Write-Host '  环境体检 01_check_env.ps1  （只读，不改任何东西）' -ForegroundColor White
Write-Host '==================================================' -ForegroundColor White
if ($Apply) {
    Write-Host '提示：本脚本是只读体检，-Apply 对它没有意义，已按只读方式执行。' -ForegroundColor DarkGray
}

# ------------------------------------------------------------------ 1. 项目根目录
Write-Step '第 1 步 / 项目根目录'
$RootPath = $null
if (-not (Test-Path -LiteralPath $Root)) {
    Write-Fail "项目根目录不存在：$Root" '确认路径拼写；如果项目搬到别处了，用 -Root "新路径" 指过去。'
} else {
    $RootPath = (Resolve-Path -LiteralPath $Root).Path
    if ($RootPath -ne $Root.TrimEnd('\')) {
        Write-Warn "根目录解析后是 $RootPath（和参数不完全一致，一般没事）"
    }
    Write-Pass "项目根目录：$RootPath"
}

if (-not $RootPath) {
    Write-Host ''
    Write-Host '根目录都没有，后面没法查了。合计：成功 0 / 失败 1 / 警告 0' -ForegroundColor Red
    exit 1
}

# ------------------------------------------------------------------ 2. 关键目录
Write-Step '第 2 步 / 关键目录是否齐全'
# 字段说明：
#   Rel  / Desc  目录相对路径 / 人话说明
#   Auto         该目录是「跑部署脚本时自动生成」的产物，新 clone 下来本来就该没有。
#                打了 Auto = $true 的目录缺失时只报 [WARN]（不计入失败数、不影响退出码）。
#                GenBy 说明是哪个脚本生成的，直接用在该条提示里。
#                → 以后新增「会自动生成」的目录，只要在清单里加一行 Auto 即可，不用改下面的循环。
$needDirs = @(
    @{ Rel = 'src';              Desc = 'Django 源码目录（manage.py 在这层）' },
    @{ Rel = 'src\config';       Desc = '配置目录（settings.py）' },
    @{ Rel = 'src\db';           Desc = '数据库目录（db.sqlite3 放这）'; Auto = $true; GenBy = '03_setup_db.ps1（或站点首次启动时 settings.py 自动建）' },
    @{ Rel = 'src\static';       Desc = '静态源文件（tailwind 输出到这）' },
    @{ Rel = 'src\staticfiles';  Desc = 'collectstatic 的产物目录';      Auto = $true; GenBy = '05_static.ps1' },
    @{ Rel = 'src\templates';    Desc = '模板目录' },
    @{ Rel = '.venv';            Desc = 'Python 虚拟环境' },
    @{ Rel = '.tools';           Desc = '本地工具/缓存/日志目录' },
    @{ Rel = '.tools\logs';      Desc = '运行日志目录' },
    @{ Rel = '.tools\redis';     Desc = '便携 Redis 目录' }
)
foreach ($d in $needDirs) {
    $full = Join-Path $RootPath $d.Rel
    if (Test-Path -LiteralPath $full) {
        Write-Pass "$($d.Rel)  →  $($d.Desc)"
    } elseif ($d.Auto) {
        $by = if ($d.GenBy) { $d.GenBy } else { '后续部署脚本' }
        $txt = "缺少目录 $($d.Rel)（$($d.Desc)） —— 新部署时正常缺失，不用手动创建"
        $fix = "跑完 $by 就会自动出现；只有站点已经在跑、这里还长期是空的，才需要去查。"
        Write-Warn $txt $fix
    } else {
        Write-Fail "缺少目录 $($d.Rel)（$($d.Desc)）" "先确认项目是不是拷全了；$($d.Rel) 属于项目自带目录，不该手动重建。"
    }
}

# ------------------------------------------------------------------ 3. Python
Write-Step '第 3 步 / Python（要求 3.12）'
$py = Join-Path $RootPath '.venv\Scripts\python.exe'
$pyw = Join-Path $RootPath '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $py)) {
    Write-Fail "虚拟环境里的 python 不存在：$py" '重建虚拟环境：在项目根跑 "C:\Program Files\Python312\python.exe" -m venv .venv（需先装 Python 3.12）。'
} else {
    $ver = ''
    try { $ver = (& $py -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>&1 | Out-String).Trim() } catch { $ver = '' }
    if ($ver -eq '3.12') {
        Write-Pass "python.exe 版本 $ver（正确）"
    } elseif ($ver -match '^3\.') {
        Write-Warn "python.exe 版本是 $ver，要求 3.12" '版本不一致时部分依赖装不上。建议用 Python 3.12 重建 .venv。'
    } else {
        Write-Fail "python.exe 跑不起来或读不到版本（原始输出：$ver）" '在项目根执行 .venv\Scripts\python.exe --version 看真实报错。'
    }
    if (Test-Path -LiteralPath $pyw) {
        Write-Pass 'pythonw.exe 存在（计划任务靠它静默启动）'
    } else {
        Write-Warn 'pythonw.exe 不存在' '计划任务和 start-win.vbs 都依赖 pythonw.exe，缺了会静默失败。建议重建 .venv。'
    }
}

# ------------------------------------------------------------------ 4. Node / npx
Write-Step '第 4 步 / Node 和 npx（tailwind 要用）'
$node = Get-Command node -ErrorAction SilentlyContinue
if ($node) {
    $nodeVer = ''
    try { $nodeVer = (& node --version 2>&1 | Out-String).Trim() } catch { $nodeVer = '' }
    Write-Pass "node：$($node.Source)  版本 $nodeVer"
} else {
    Write-Fail '找不到 node 命令' '装 Node.js LTS（https://nodejs.org），装完重开一个窗口再跑本脚本。'
}
$npx = Get-Command npx -ErrorAction SilentlyContinue
if ($npx) {
    Write-Pass "npx：$($npx.Source)"
} else {
    Write-Warn '找不到 npx' 'npx 随 Node 一起装，通常和 node 同时缺失；补装 Node 即可。'
}

$twGlobal = Join-Path $env:APPDATA 'npm\node_modules\tailwindcss'
if (Test-Path -LiteralPath $twGlobal) {
    Write-Pass "tailwindcss 全局模块存在：$twGlobal"
} else {
    Write-Warn "没找到全局 tailwindcss：$twGlobal" '执行 npm install -g tailwindcss；装完由 05_static.ps1 负责编译。'
}

# ------------------------------------------------------------------ 5. Chrome
Write-Step '第 5 步 / Chrome（续 Steam 令牌要用）'
$chromeCandidates = @(
    'C:\Program Files\Google\Chrome\Application\chrome.exe',
    'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
)
$chromeFound = $null
foreach ($c in $chromeCandidates) {
    if ($c -and (Test-Path -LiteralPath $c)) { $chromeFound = $c; break }
}
if ($chromeFound) {
    Write-Pass "Chrome：$chromeFound"
} else {
    Write-Warn '常见路径下没找到 chrome.exe' '只有「自动续 Steam 令牌」需要 Chrome。不装也能跑站点，令牌失效后手动更新 .env 即可。'
}

# ------------------------------------------------------------------ 6. 磁盘
Write-Step '第 6 步 / 磁盘剩余空间（建议 2GB 以上）'
try {
    $driveLetter = (Split-Path -Qualifier $RootPath).TrimEnd(':')
    $freeGB = [math]::Round((Get-PSDrive -Name $driveLetter).Free / 1GB, 1)
    if ($freeGB -ge 2) {
        Write-Pass "$($driveLetter): 剩余 $freeGB GB"
    } else {
        Write-Fail "$($driveLetter): 只剩 $freeGB GB" '清出至少 2GB（图片缓存 + 数据库备份要占地方）。'
    }
} catch {
    Write-Warn "读不到磁盘剩余空间：$($_.Exception.Message)" '可以忽略；自己看「此电脑」确认 C 盘还有空间即可。'
}

# ------------------------------------------------------------------ 7. 端口
Write-Step '第 7 步 / 8001 端口（站点地址 http://127.0.0.1:8001）'
$portBusy = $false
$portOwner = ''
try {
    $conns = @(Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction Stop)
    if ($conns.Count -gt 0) {
        $portBusy = $true
        $procId = ($conns | Select-Object -First 1).OwningProcess
        try { $portOwner = (Get-Process -Id $procId -ErrorAction Stop).ProcessName } catch { $portOwner = "PID $procId" }
    }
} catch {
    # 老系统没有 Get-NetTCPConnection，退回 netstat
    $ns = (netstat -ano | Select-String ':8001\s' | Select-String 'LISTENING')
    if ($ns) { $portBusy = $true; $portOwner = 'netstat 显示有进程在听' }
}
if ($portBusy) {
    Write-Warn "8001 已经被占用（$portOwner）" '如果你本来就开着站点，这是正常现象；本站点重复启动是安全的。要是准备换端口跑新实例，先双击 stop-win.vbs。'
} else {
    Write-Pass '8001 端口空闲（站点当前没在跑）'
}
$redisBusy = @(Get-NetTCPConnection -LocalPort 6379 -State Listen -ErrorAction SilentlyContinue).Count
if ($redisBusy -gt 0) { Write-Pass 'Redis 6379 在监听（Celery 依赖它）' }
else { Write-Warn 'Redis 6379 没在监听' '正常：start_silent.py 会自己拉起 Redis。只有定时导入不生效时才需要关注。' }

# ------------------------------------------------------------------ 8. 关键文件
Write-Step '第 8 步 / 关键文件'
$needFiles = @(
    @{ Rel = 'start_silent.py';            Desc = '静默启动器（Redis + Django + Celery）' },
    @{ Rel = 'stop_silent.py';             Desc = '静默停止器' },
    @{ Rel = 'start-win.vbs';              Desc = '双击启动（纯 ASCII）' },
    @{ Rel = 'stop-win.vbs';               Desc = '双击停止（纯 ASCII）' },
    @{ Rel = 'src\manage.py';              Desc = 'Django 管理入口' },
    @{ Rel = 'src\config\settings.py';     Desc = '配置（DEBUG / DB_HOST 都在这）' },
    @{ Rel = 'src\static\css\input.css';   Desc = 'tailwind 输入文件' },
    @{ Rel = 'src\static\css\main.css';    Desc = 'tailwind 产物' },
    @{ Rel = '.tools\redis\redis-server.exe'; Desc = '便携 Redis' }
)
foreach ($f in $needFiles) {
    $full = Join-Path $RootPath $f.Rel
    if (Test-Path -LiteralPath $full) {
        Write-Pass "$($f.Rel)  →  $($f.Desc)"
    } else {
        Write-Fail "缺少文件 $($f.Rel)（$($f.Desc)）" '从备份/仓库把文件找回来，别自己新建占位文件。'
    }
}

Write-Step '第 9 步 / .env 和数据库'
$envFile = Join-Path $RootPath '.env'
if (Test-Path -LiteralPath $envFile) {
    $envSize = (Get-Item -LiteralPath $envFile).Length
    Write-Pass ".env 存在（$envSize 字节；本脚本不打印里面的任何值）"
} else {
    Write-Fail '缺少 .env' '跑 04_setup_env.ps1 看缺哪些键，再按提示补。'
}
$dbFile = Join-Path $RootPath 'src\db\db.sqlite3'
if (Test-Path -LiteralPath $dbFile) {
    $dbMB = [math]::Round((Get-Item -LiteralPath $dbFile).Length / 1MB, 1)
    Write-Pass "数据库存在：src\db\db.sqlite3（$dbMB MB）"
} else {
    Write-Warn '数据库不存在（新机器第一次部署就会出现）' '正常：03_setup_db.ps1 -Apply 会建库并跑 migrate。注意会得到一个空站点。'
}

# ------------------------------------------------------------------ 10. 依赖清单
Write-Step '第 10 步 / 依赖清单文件'
$reqCandidates = @('requirements.txt', 'requirements-win.txt', 'src\requirements.txt')
$reqFound = $null
foreach ($rc in $reqCandidates) {
    $full = Join-Path $RootPath $rc
    if (Test-Path -LiteralPath $full) { $reqFound = $full; break }
}
if ($reqFound) {
    $pkgCount = @(Get-Content -LiteralPath $reqFound -Encoding UTF8 |
        Where-Object { $_.Trim() -ne '' -and -not $_.Trim().StartsWith('#') }).Count
    Write-Pass "依赖清单：$reqFound（$pkgCount 个包）"
} else {
    Write-Fail '找不到 requirements.txt / requirements-win.txt' '这是装依赖的唯一依据，缺了 02_install_deps.ps1 没法干活。'
}

# ------------------------------------------------------------------ 汇总
Write-Host ''
Write-Host '==================================================' -ForegroundColor White
Write-Host ("  汇总：成功 $script:OkCount / 失败 $script:FailCount / 警告 $script:WarnCount") -ForegroundColor White
Write-Host '==================================================' -ForegroundColor White
if ($script:FailCount -eq 0 -and $script:WarnCount -eq 0) {
    Write-Host '  环境没问题，接着跑 02_install_deps.ps1。' -ForegroundColor Green
} elseif ($script:FailCount -eq 0) {
    Write-Host '  没有致命问题，警告项自己看一眼要不要处理。可以接着跑 02_install_deps.ps1。' -ForegroundColor Yellow
} else {
    Write-Host '  有硬性缺项，先把上面 [FAIL] 的几条解决掉，再往下走。' -ForegroundColor Red
    Write-Host '  提示：本脚本只读，不会替你修任何东西。' -ForegroundColor Red
}
exit $(if ($script:FailCount -gt 0) { 1 } else { 0 })