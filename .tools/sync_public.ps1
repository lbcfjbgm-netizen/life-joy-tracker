# 把工作副本的代码改动同步到公开仓库（yamtrack-public），然后可选推送 GitHub
#
# 为什么需要它：
#   工作副本 yamtrack\ 和公开仓库 yamtrack-public\ 是两个独立 git 仓库，
#   在工作副本里改的代码不会自动出现在公开仓库里。要推 GitHub 必须手动同步。
#
# 为什么不能整目录镜像：
#   工作副本有 2.4G（含 .venv / 数据库 / Steam 登录态 / 抓取的原始 JSON），
#   公开仓库只有 62M。整目录镜像会把隐私数据和 2G 垃圾一起推上去。
#   所以这里只同步「两个仓库都在跟踪的文件」，并按白名单排除危险目录。
#
# 换行符说明：
#   公开仓库有 .gitattributes（*.ps1 强制 CRLF，*.py 交给 git）。
#   两边 diff 报「文件不同」大多数时候只是换行符差异，不代表内容有差。
#   本脚本用 git 自己判断差异，不自己比字节。
#
# 用法（在 PowerShell 里）：
#   # 只看要同步哪些文件，不动手
#   pwsh -File "<工作副本>\.tools\sync_public.ps1" -WhatIf
#
#   # 同步 + 提交（不推送）
#   pwsh -File "<工作副本>\.tools\sync_public.ps1"
#
#   # 同步 + 提交 + 推送 GitHub
#   pwsh -File "<工作副本>\.tools\sync_public.ps1" -Push

[CmdletBinding()]
param(
    [string]$Src = '',
    [string]$Dst = '',
    [switch]$Push,
    [switch]$WhatIf,
    [string]$Message = ''
)

$ErrorActionPreference = 'Stop'

# 默认路径：本脚本在 <工作副本>\.tools\ 下，所以工作副本 = $PSScriptRoot 的上一级，
# 公开仓库又是工作副本的上一级里的 yamtrack-public。
if ([string]::IsNullOrWhiteSpace($Src)) { $Src = Split-Path -Parent $PSScriptRoot }
if ([string]::IsNullOrWhiteSpace($Src)) {
    # 极少数情况（例如把脚本内容当命令直接粘进终端）$PSScriptRoot 是空的，
    # 这时退回当前目录，绝不写死任何本机绝对路径。
    $Src = (Get-Location).Path
    Write-Host "提示：定位不到脚本所在目录，已退回使用当前目录：$Src" -ForegroundColor Yellow
}
if ([string]::IsNullOrWhiteSpace($Dst)) { $Dst = Join-Path (Split-Path -Parent $Src) 'yamtrack-public' }

# ---------------------------------------------------------------- 安全检查
# 这些前缀下的东西绝对不能同步过去：隐私、体积、本机运行产物
$DenyPrefixes = @(
    '.git/', '.venv/', '.tools/steam-profile/', '.tools/raw/', '.tools/logs/',
    '.tools/backups/', '.tools/p3a-chrome/', '.tools/proxy_pool/', '.tools/ui-probe/',
    '.tools/subraw/', '.tools-archive/', '.i18nwork/', 'src/db/', 'src/staticfiles/',
    'src/tmp', 'tmp/', '.pytest_cache/', '.ruff_cache/', '.agents/', '.claude/', '.codex/',
    '_tmp/', 'site/'
)
# 这些后缀/文件名绝对不能同步
$DenyFiles = @('.env', '.env.bak', 'db.sqlite3', 'db.sqlite3-shm', 'db.sqlite3-wal',
               'launch.json', 'AGENTS.md', 'CLAUDE.md', 'requests.http', 'TODO.txt')
$DenyExt = @('.pyc', '.log', '.pid', '.sqlite3', '.db')

function Test-Denied {
    param([string]$RelPath)
    $p = $RelPath -replace '\\', '/'
    foreach ($pre in $DenyPrefixes) { if ($p.StartsWith($pre)) { return $true } }
    $leaf = Split-Path -Leaf $p
    if ($DenyFiles -contains $leaf) { return $true }
    if ($leaf -like '.env.bak*') { return $true }
    foreach ($ext in $DenyExt) { if ($leaf.EndsWith($ext)) { return $true } }
    if ($leaf -like '_*' -and $p.StartsWith('.tools/')) { return $true }
    if ($leaf -like 'tmp*' -and $p.StartsWith('src/')) { return $true }
    return $false
}

Write-Host ''
Write-Host '==================================================' -ForegroundColor White
Write-Host '  同步 工作副本 → 公开仓库' -ForegroundColor White
Write-Host '==================================================' -ForegroundColor White
Write-Host "  来源：$Src"
Write-Host "  目标：$Dst"

foreach ($d in @($Src, $Dst)) {
    if (-not (Test-Path -LiteralPath $d)) { Write-Host "  ❌ 目录不存在：$d" -ForegroundColor Red; exit 1 }
    if (-not (Test-Path -LiteralPath (Join-Path $d '.git'))) { Write-Host "  ❌ 不是 git 仓库：$d" -ForegroundColor Red; exit 1 }
}

# ---------------------------------------------------------------- 1. 差量计算
Write-Host ''
Write-Host '=== 1/4 计算差异（只比两边都跟踪的文件）===' -ForegroundColor Cyan

$srcFiles = & git -C $Src ls-files
$dstFiles = & git -C $Dst ls-files
if ($LASTEXITCODE -ne 0) { Write-Host '  ❌ git ls-files 失败' -ForegroundColor Red; exit 1 }

$dstSet = @{}
foreach ($f in $dstFiles) { $dstSet[$f -replace '\\', '/'] = $true }

$toCopy = New-Object System.Collections.Generic.List[string]
$missingInDst = New-Object System.Collections.Generic.List[string]
$skipped = New-Object System.Collections.Generic.List[string]

foreach ($f in $srcFiles) {
    $rel = $f -replace '\\', '/'
    if (Test-Denied $rel) { $skipped.Add($rel); continue }
    $srcFull = Join-Path $Src ($rel -replace '/', '\')
    if (-not (Test-Path -LiteralPath $srcFull)) { continue }

    if (-not $dstSet.ContainsKey($rel)) {
        # 公开仓库还没跟踪这个文件。三种情况：
        #   ① .github/ 下 —— 故意删掉的（原版 Yamtrack 的 Actions），不同步
        #   ② 工作副本里「源码/工具」类新文件 —— 要同步，否则新增的 .py 永远推不上去
        #   ③ 本机运行产物（JSON/日志/进度文件）—— 不同步
        if ($rel -match '^\.github/') {
            $missingInDst.Add($rel)
        } elseif ($rel -match '^\.tools/[^/]+\.(py|ps1|md)$' -or
                  $rel -match '^src/.*\.(py|html|css|js)$' -or
                  $rel -match '^deploy/.*\.(ps1|md|bat|cmd|vbs)$' -or
                  $rel -match '^scripts/.*\.py$') {
            $toCopy.Add($rel)
        } else {
            $missingInDst.Add($rel)
        }
        continue
    }
    $dstFull = Join-Path $Dst ($rel -replace '/', '\')
    if (-not (Test-Path -LiteralPath $dstFull)) { $toCopy.Add($rel); continue }

    # 用 git 判断内容差异（忽略换行符），比字节靠谱
    & git -C $Src diff --no-index --quiet --ignore-cr-at-eol -- $srcFull $dstFull *> $null
    if ($LASTEXITCODE -ne 0) { $toCopy.Add($rel) }
}

Write-Host ("  内容有变化：{0} 个" -f $toCopy.Count) -ForegroundColor Green
Write-Host ("  安全排除：  {0} 个" -f $skipped.Count)
Write-Host ("  公开仓库未跟踪（不同步）：{0} 个" -f $missingInDst.Count)

if ($toCopy.Count -gt 0) {
    Write-Host ''
    Write-Host '  要同步的文件：' -ForegroundColor Yellow
    foreach ($f in $toCopy) { Write-Host "    $f" }
}
if ($missingInDst.Count -gt 0 -and $missingInDst.Count -le 25) {
    Write-Host ''
    Write-Host '  这些在工作副本里有、公开仓库不跟踪（正常，多半是 .github 或本机产物）：' -ForegroundColor DarkGray
    foreach ($f in $missingInDst) { Write-Host "    $f" -ForegroundColor DarkGray }
}
if ($skipped.Count -gt 0 -and $skipped.Count -le 25) {
    Write-Host ''
    Write-Host '  被安全检查拦下（隐私/体积/运行产物）：' -ForegroundColor DarkGray
    foreach ($f in $skipped) { Write-Host "    $f" -ForegroundColor DarkGray }
}

if ($toCopy.Count -eq 0) {
    Write-Host ''
    Write-Host '  两边内容已经一致，没有要同步的。' -ForegroundColor Green
    if (-not $Push) { Write-Host '完成。'; exit 0 }
}

if ($WhatIf) {
    Write-Host ''
    Write-Host '  -WhatIf 模式：只报告，没有改动任何文件。' -ForegroundColor Yellow
    exit 0
}

# ---------------------------------------------------------------- 2. 复制
Write-Host ''
Write-Host '=== 2/4 复制文件 ===' -ForegroundColor Cyan
$copied = 0
foreach ($rel in $toCopy) {
    $srcFull = Join-Path $Src ($rel -replace '/', '\')
    $dstFull = Join-Path $Dst ($rel -replace '/', '\')
    $dstDir = Split-Path -Parent $dstFull
    if (-not (Test-Path -LiteralPath $dstDir)) { New-Item -ItemType Directory -Path $dstDir -Force | Out-Null }
    Copy-Item -LiteralPath $srcFull -Destination $dstFull -Force
    $copied++
}
Write-Host ("  已复制 {0} 个文件" -f $copied) -ForegroundColor Green

# ---------------------------------------------------------------- 3. 提交
Write-Host ''
Write-Host '=== 3/4 在公开仓库提交 ===' -ForegroundColor Cyan
& git -C $Dst add -A
if ($LASTEXITCODE -ne 0) { Write-Host '  ❌ git add 失败' -ForegroundColor Red; exit 1 }

$status = & git -C $Dst status --porcelain
if (-not $status) {
    Write-Host '  没有需要提交的改动。' -ForegroundColor Green
} else {
    if ([string]::IsNullOrWhiteSpace($Message)) {
        $Message = '同步工作副本改动：界面状态记忆 + 部署体检提示修正'
    }
    & git -C $Dst commit -m $Message
    if ($LASTEXITCODE -ne 0) { Write-Host '  ❌ git commit 失败' -ForegroundColor Red; exit 1 }
    Write-Host '  已提交' -ForegroundColor Green
}

# ---------------------------------------------------------------- 4. 推送
Write-Host ''
Write-Host '=== 4/4 推送 GitHub ===' -ForegroundColor Cyan
if (-not $Push) {
    Write-Host '  没带 -Push，只提交不推送。要推就加 -Push 再跑一次。' -ForegroundColor Yellow
    Write-Host ''
    Write-Host '完成。'
    exit 0
}

# 推送前先确保分支有 upstream，否则 git push 会直接报
# "fatal: The current branch main has no upstream branch"（不是 token 问题，
# 别被前面那句提示误导）。
$branch = (& git -C $Dst rev-parse --abbrev-ref HEAD).Trim()
& git -C $Dst rev-parse --abbrev-ref "$branch@{upstream}" *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  分支 $branch 还没设上游，先设成 origin/$branch" -ForegroundColor Yellow
    & git -C $Dst push --set-upstream origin $branch
} else {
    & git -C $Dst push
}

if ($LASTEXITCODE -ne 0) {
    Write-Host ''
    Write-Host '  ❌ 推送失败。' -ForegroundColor Red
    Write-Host '  常见原因：' -ForegroundColor Yellow
    Write-Host '    1. GitHub token 过期（当时生成的只有 7 天有效期）' -ForegroundColor Yellow
    Write-Host '       去 https://github.com/settings/tokens 重新生成' -ForegroundColor Yellow
    Write-Host '    2. 没设上游分支 → 本脚本会自动补 --set-upstream，若仍失败看上面报错原文' -ForegroundColor Yellow
    exit 1
}
Write-Host '  ✅ 已推送到 GitHub' -ForegroundColor Green

Write-Host ''
Write-Host '完成。'
