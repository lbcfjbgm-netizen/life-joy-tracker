#Requires -Version 5.1
<#
  02_install_deps.ps1 —— 装 Python 依赖 + 检查 Tailwind CLI

  干什么：
    先自动找依赖清单（本项目实际叫 requirements-win.txt，不是 requirements.txt），
    再把 .venv 里已装的包和清单对比，只列出缺的；确认没问题才真的 pip install。

  默认行为（不传 -Apply）：
    只体检，不安装。会告诉你「清单在哪 / 一共几个包 / 缺几个 / 缺哪些」。

  真要装：
    -Apply

  可选项：
    -InstallTailwind   发现全局 tailwindcss 缺失时，顺手 npm install -g tailwindcss

  用法：
    # 在项目根目录（有 deploy\ 和 src\ 的那一层）执行：
    powershell -NoProfile -ExecutionPolicy Bypass -File ".\deploy\02_install_deps.ps1"
    # 在项目根目录（有 deploy\ 和 src\ 的那一层）执行：
    powershell -NoProfile -ExecutionPolicy Bypass -File ".\deploy\02_install_deps.ps1" -Apply
#>
[CmdletBinding()]
param(
    [string]$Root = '',   # 空 = 自动定位成「本脚本所在 deploy 目录的上一级」
    [switch]$DryRun,
    [switch]$Apply,
    [switch]$InstallTailwind,
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

$doApply = ($Apply -and -not $DryRun)

Write-Host ''
Write-Host '==================================================' -ForegroundColor White
Write-Host '  安装依赖 02_install_deps.ps1' -ForegroundColor White
Write-Host ("  模式：" + $(if ($doApply) { '真的安装（-Apply）' } else { '只体检，不安装（默认）' })) -ForegroundColor White
Write-Host '==================================================' -ForegroundColor White
if (-not $doApply) { Write-Host '  想真装就加 -Apply；加之前建议先看下面的体检结果。' -ForegroundColor DarkGray }

if (-not (Test-Path -LiteralPath $Root)) {
    Write-Host "项目根目录不存在：$Root" -ForegroundColor Red
    exit 1
}
$RootPath = (Resolve-Path -LiteralPath $Root).Path

# ---------------------------------------------------------------- 1. 找清单
Write-Step '第 1 步 / 找依赖清单'
$candidates = @(
    'requirements.txt',
    'requirements-win.txt',
    'requirements-dev.txt',
    'src\requirements.txt'
)
$reqFile = $null
foreach ($c in $candidates) {
    $full = Join-Path $RootPath $c
    if (Test-Path -LiteralPath $full) { $reqFile = $full; break }
}
if (-not $reqFile) {
    Write-Fail "项目里找不到任何 requirements*.txt（找过：$($candidates -join '、')）" '依赖清单是装的唯一依据，先确认项目是不是拷全了。'
    Write-Host ''
    Write-Host "  汇总：成功 $script:OkCount / 失败 $script:FailCount / 警告 $script:WarnCount" -ForegroundColor Red
    exit 1
}
Write-Pass "依赖清单：$reqFile"
if ((Split-Path -Leaf $reqFile) -ne 'requirements.txt') {
    Write-Info "注意：本机实际文件名是 $(Split-Path -Leaf $reqFile)，不是常见的 requirements.txt。下面都用这个真实文件。"
}

# ---------------------------------------------------------------- 2. 解析清单
Write-Step '第 2 步 / 解析清单内容'
$rawLines = @(Get-Content -LiteralPath $reqFile -Encoding UTF8)
$reqs = @()
foreach ($line in $rawLines) {
    $t = $line.Trim()
    if ($t -eq '' -or $t.StartsWith('#')) { continue }
    if ($t.StartsWith('-')) { continue }   # -r / -e 之类，跳过
    # 包名规范化：取开头 [A-Za-z0-9._-]，去掉 extras 和版本约束
    if ($t -match '^\s*([A-Za-z0-9][A-Za-z0-9._-]*)') {
        $name = $Matches[1].ToLower()
        $name = $name -replace '[._]', '-'
        $reqs += [pscustomobject]@{ Name = $name; Raw = $t }
    }
}
if ($reqs.Count -eq 0) {
    Write-Fail '清单解析出 0 个包（文件是不是空的？）' '打开清单看一眼，正常应该有二十来个包名。'
} else {
    Write-Pass "解析到 $($reqs.Count) 个包"
}

# ---------------------------------------------------------------- 3. 解释器
Write-Step '第 3 步 / 确认用的是项目自己的 .venv'
$py = Join-Path $RootPath '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) {
    Write-Fail "找不到 $py" '没有虚拟环境，先建 .venv 再装依赖（见 README 前置要求）。'
    Write-Host ''
    Write-Host "  汇总：成功 $script:OkCount / 失败 $script:FailCount / 警告 $script:WarnCount" -ForegroundColor Red
    exit 1
}
$pyVer = ''
try { $pyVer = (& $py --version 2>&1 | Out-String).Trim() } catch { $pyVer = '' }
if ($pyVer -match '3\.12\.') {
    Write-Pass "解释器：$py（$pyVer）"
} elseif ($pyVer) {
    Write-Warn "解释器版本是 $pyVer，项目要求 3.12" '不是 3.12 时装依赖可能失败，建议重建 .venv。'
} else {
    Write-Fail "python.exe 跑不起来" '先手动执行 "' + $py + '" --version 看真实报错。'
}

$prefix = ''
try { $prefix = (& $py -c "import sys;print(sys.prefix)" 2>&1 | Out-String).Trim() } catch { $prefix = '' }
if ($prefix -and (Test-Path -LiteralPath (Join-Path $prefix '.venv'))) {
    Write-Pass "确认是虚拟环境：$prefix"
} elseif ($prefix -match '\.venv') {
    Write-Pass "确认是虚拟环境：$prefix"
} else {
    Write-Warn "解释器的 sys.prefix 看起来不像 .venv（$prefix）" '正常应该是 <项目根>\.venv。如果是系统 Python，装包会污染全局。'
}

# ---------------------------------------------------------------- 4. 对比已装
Write-Step '第 4 步 / 对比已装包（只读 pip list，不联网）'
$installed = @{}
try {
    $json = (& $py -m pip list --format=json --disable-pip-version-check 2>&1 | Out-String)
    $parsed = $json | ConvertFrom-Json
    foreach ($p in $parsed) {
        $n = $p.name.ToLower() -replace '[._]', '-'
        $installed[$n] = $p.version
    }
    Write-Pass "已装包数量：$($installed.Count)"
} catch {
    Write-Fail "读不到已装包列表（$($_.Exception.Message)）" '先手动跑 "' + $py + '" -m pip list 看能不能出结果；pip 坏了就用 "python -m ensurepip" 修复。'
}
if ($installed.Count -eq 0) {
    Write-Fail '已装包列表是空的' '这种 .venv 基本是废的，建议删掉重建。'
}

$missing = @()
foreach ($r in $reqs) {
    if (-not $installed.ContainsKey($r.Name)) { $missing += $r }
}
if ($missing.Count -eq 0) {
    Write-Pass '清单里的包全都装了，跳过安装（幂等）'
} else {
    Write-Warn "缺 $($missing.Count) 个包" '下面会列出来；确认后加 -Apply 真装。'
    foreach ($m in $missing) { Write-Info "缺：$($m.Name)    （清单里写作 $($m.Raw)）" }
}

# 版本差异只提示，不拦。~=X.Y.Z 的语义是「>=X.Y.Z 且仍在 X.Y 这一支」，
# 所以只比较「前两段」，一致就算满足，不纠结第三段。
function Get-MajorMinor {
    param([string]$Version)
    if (-not $Version) { return '' }
    $parts = $Version.Trim() -split '[.\-+]'
    if ($parts.Count -ge 2) { return "$($parts[0]).$($parts[1])" }
    return $parts[0]
}
$versionHints = @()
foreach ($r in $reqs) {
    if (-not $installed.ContainsKey($r.Name)) { continue }
    if ($r.Raw -match '~=\s*([0-9][0-9A-Za-z.\-]*)') {
        $want = $Matches[1]
        $have = $installed[$r.Name]
        if (-not $want -or -not $have) { continue }
        if ((Get-MajorMinor $want) -ne (Get-MajorMinor $have)) {
            $versionHints += "$($r.Name)：装的 $have，清单写 $want"
        }
    }
}
if ($versionHints.Count -gt 0) {
    Write-Warn "有 $($versionHints.Count) 个包版本和清单写的不一致（多数不影响运行）" '要严格对齐就手动 pip install 指定版本；别为了对齐去动别的包。'
    foreach ($v in $versionHints) { Write-Info $v }
}

# ---------------------------------------------------------------- 5. 关键包抽查
Write-Step '第 5 步 / 关键包抽查'
$keyPkgs = @('django', 'celery', 'django-celery-beat', 'django-debug-toolbar', 'python-decouple', 'pillow', 'redis')
foreach ($k in $keyPkgs) {
    if ($installed.ContainsKey($k)) { Write-Pass "$k = $($installed[$k])" }
    else { Write-Fail "关键包缺失：$k" '缺它站点起不来，务必装上（-Apply 会自动包含）。' }
}

# ---------------------------------------------------------------- 6. 安装
Write-Step '第 6 步 / 执行安装'
if (-not $doApply) {
    Write-Info '这是体检模式，什么都没装。真要装请重跑并加 -Apply：'
    Write-Info ('powershell -NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath + '" -Apply')
} elseif ($missing.Count -eq 0) {
    Write-Pass '没有要装的，直接跳过（重复执行安全）'
} else {
    Write-Info "开始安装：$py -m pip install -r `"$reqFile`""
    Write-Info '（联网慢的话先设代理：$env:HTTPS_PROXY="http://127.0.0.1:7897"）'
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & $py -m pip install -r $reqFile --disable-pip-version-check
    $code = $LASTEXITCODE
    $sw.Stop()
    if ($code -eq 0) {
        Write-Pass ("pip install 完成，用了 {0:N0} 秒" -f $sw.Elapsed.TotalSeconds)
    } else {
        Write-Fail "pip install 失败（退出码 $code）" '常见原因：1) 网连不上 → 先设 HTTPS_PROXY 再试；2) 某个包要编译 → 装对应 VS Build Tools；3) Python 版本不是 3.12。'
    }
}

# ---------------------------------------------------------------- 7. Tailwind
Write-Step '第 7 步 / Tailwind CLI 检查'
$twModule = Join-Path $env:APPDATA 'npm\node_modules\tailwindcss'
$twCmd = Join-Path $env:APPDATA 'npm\tailwindcss.cmd'
$npxCmd = Get-Command npx -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $twModule) {
    Write-Pass "tailwindcss 模块在：$twModule"
    if (Test-Path -LiteralPath $twCmd) { Write-Pass "命令行入口在：$twCmd" }
    else { Write-Warn '有模块但没有 tailwindcss.cmd' 'npm install -g tailwindcss 重装一次即可。' }
} else {
    Write-Warn "没装全局 tailwindcss（找不到 $twModule）" '执行 npm install -g tailwindcss。不加 -InstallTailwind 的话本脚本不会替你装。'
}
if (-not $npxCmd) { Write-Warn '找不到 npx' '装 Node.js LTS 后重开窗口。' }
$twVer = ''
if ($npxCmd -and (Test-Path -LiteralPath $twModule)) {
    try { $twVer = (& npx --no-install tailwindcss --help 2>&1 | Select-Object -First 1 | Out-String).Trim() } catch { $twVer = '' }
    if ($twVer) { Write-Info "tailwind 自报版本：$twVer" }
}

if ($InstallTailwind) {
    if (Test-Path -LiteralPath $twModule) {
        Write-Pass '已经装了，不再重复装（幂等）'
    } elseif (-not $doApply) {
        Write-Info '你要求装 tailwind，但当前是体检模式；加 -Apply 才会真装。'
    } else {
        Write-Info '开始装全局 tailwindcss（本机 npm 已配 npmmirror 镜像，通常几秒）'
        & npm install -g tailwindcss
        if ($LASTEXITCODE -eq 0) { Write-Pass 'tailwindcss 安装完成' }
        else { Write-Fail "npm install -g tailwindcss 失败（退出码 $LASTEXITCODE）" '先确认 node/npm 能用：npm --version。网络问题可先设代理。' }
    }
}

# ---------------------------------------------------------------- 汇总
Write-Host ''
Write-Host '==================================================' -ForegroundColor White
Write-Host ("  汇总：成功 $script:OkCount / 失败 $script:FailCount / 警告 $script:WarnCount") -ForegroundColor White
Write-Host '==================================================' -ForegroundColor White
if ($script:FailCount -eq 0) {
    Write-Host '  依赖这块没问题，接着跑 03_setup_db.ps1。' -ForegroundColor Green
} else {
    Write-Host '  有失败项，先照上面的「怎么办」处理，再重跑本脚本。' -ForegroundColor Red
}
if (-not $doApply) { Write-Host '  （提醒：这次是体检模式，什么都没装）' -ForegroundColor DarkGray }
exit $(if ($script:FailCount -gt 0) { 1 } else { 0 })