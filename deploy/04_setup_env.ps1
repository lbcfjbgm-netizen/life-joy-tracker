#Requires -Version 5.1
<#
  04_setup_env.ps1 —— 配置对比（默认只读，不写任何东西）

  干什么：
    1) 读 .env 里的「键名」（只统计键名，绝不打印值）；
    2) 拿它跟基线键表 / .env.example（如果哪天有了）对比，列出缺哪些；
    3) 逐条说清每个键该怎么填、填错会怎样；
    4) 重点盯两条红线：DEBUG 必须是 True、DB_HOST 必须不存在。

  安全说明：
    本脚本从设计上就不输出任何值，只输出键名、长度、是否为空。
    .env 里是真实密钥（Steam 令牌、SECRET、三方 token），泄露等于把账号送人。

  模板：
    如果项目里没有 .env.example，默认只在屏幕上打印一份「只有键名和占位符」的模板。
    想把模板存成文件，传 -WriteTemplate：会写到 deploy\.env.template（只写 deploy 里，不碰项目根）。

  用法：
    powershell ...\deploy\04_setup_env.ps1
    powershell ...\deploy\04_setup_env.ps1 -WriteTemplate
#>
[CmdletBinding()]
param(
    [string]$Root = '',   # 空 = 自动定位成「本脚本所在 deploy 目录的上一级」
    [switch]$DryRun,
    [switch]$Apply,
    [switch]$WriteTemplate,
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
Write-Host '  配置 04_setup_env.ps1  （只读键名，不打印值）' -ForegroundColor White
Write-Host '==================================================' -ForegroundColor White

if (-not (Test-Path -LiteralPath $Root)) { Write-Host "项目根目录不存在：$Root" -ForegroundColor Red; exit 1 }
$RootPath = (Resolve-Path -LiteralPath $Root).Path
$envFile = Join-Path $RootPath '.env'
$exampleFile = Join-Path $RootPath '.env.example'

# 每个键：说明 + 是不是必须 + 该填什么
$keySpec = @(
    @{ Key = 'SECRET';                       Req = $true;  Note = 'Django 的 SECRET_KEY，随机长字符串。缺了会退回一个写死的默认值，本地自用勉强能跑，但别这么干。' },
    @{ Key = 'DEBUG';                        Req = $true;  Note = '必须是 True。改成 False 后静态文件全 404，页面变纯文字。要藏 debug_toolbar 用 INTERNAL_IPS=[]，不要动 DEBUG。' },
    @{ Key = 'ALLOWED_HOSTS';                Req = $false; Note = '本地自用填 * 即可。' },
    @{ Key = 'TZ';                           Req = $false; Note = '时区，本机是 Asia/Hong_Kong 或 Asia/Shanghai，影响「今天/本周」的统计口径。' },
    @{ Key = 'REDIS_URL';                    Req = $true;  Note = 'redis://127.0.0.1:6379/0，Celery 和缓存都靠它。' },
    @{ Key = 'STEAM_API_KEY';                Req = $true;  Note = 'Steam 网页版申请的 Web API Key（32 位）。导入游戏库、成就都要它。' },
    @{ Key = 'STEAM_ID';                     Req = $true;  Note = '你的 Steam 64 位 ID（7656 开头）。' },
    @{ Key = 'STEAM_ACCESS_TOKEN';           Req = $false; Note = '家庭共享令牌，很长（500+ 字符）。由计划任务每天自动续；没有也能跑，只是看不到家庭共享库。' },
    @{ Key = 'YAMTRACK_AUTO_LOGIN_USERNAME'; Req = $false; Note = '本地免登录用，填 admin 这类已存在的用户名。' },
    @{ Key = 'NOTION_TOKEN';                 Req = $false; Note = 'Notion 导入用，没有就留空。' },
    @{ Key = 'WEREAD_API_KEY';               Req = $false; Note = '微信读书导入用，没有就留空。' },
    @{ Key = 'DOUBAN_PROXY';                 Req = $false; Note = '豆瓣导入走的本机代理。start_silent.py 会兜底注入 http://127.0.0.1:7899。' }
)

# ---------------------------------------------------------------- 1. .env 在不在
Write-Step '第 1 步 / .env 是否存在'
if (-not (Test-Path -LiteralPath $envFile)) {
    Write-Fail '缺少 .env' '照下面「第 6 步」的模板新建一个，存成 UTF-8（无 BOM 也行），放在项目根。'
    $pairs = @()
} else {
    $envSize = (Get-Item -LiteralPath $envFile).Length
    Write-Pass ".env 存在（$envSize 字节）"
    Write-Info '本脚本只读键名，绝不打印任何值。'
}

# ---------------------------------------------------------------- 2. 解析键名
Write-Step '第 2 步 / 解析 .env 里的键'
$pairs = @()          # @{ Key=...; Len=...; Empty=... }
$dupKeys = @()
$rawLines = @()
if (Test-Path -LiteralPath $envFile) {
    $rawLines = @(Get-Content -LiteralPath $envFile -Encoding UTF8)
    $seen = @{}
    foreach ($line in $rawLines) {
        if ($line -match '^\s*#' -or $line.Trim() -eq '') { continue }
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
            $k = $Matches[1]
            $v = $Matches[2]
            $pairs += [pscustomobject]@{ Key = $k; Len = $v.Length; Empty = ($v.Trim() -eq '') }
            if ($seen.ContainsKey($k)) { $dupKeys += $k }
            $seen[$k] = $true
        }
    }
}
if ($pairs.Count -gt 0) {
    Write-Pass "解析到 $($pairs.Count) 个键"
    foreach ($p in $pairs) {
        $state = if ($p.Empty) { '空值' } else { "长度 $($p.Len)" }
        Write-Info ("{0,-28} {1}" -f $p.Key, $state)
    }
} elseif (Test-Path -LiteralPath $envFile) {
    Write-Warn '.env 里一个键都没解析出来' '文件可能是空的，或格式不对（必须是 KEY=VALUE 一行一个）。'
}
if ($dupKeys.Count -gt 0) {
    Write-Warn "有重复的键：$($dupKeys -join '、')" '重复时后面的值会覆盖前面的，容易踩坑，建议留一份。'
}

$haveKeys = @{}
foreach ($p in $pairs) { $haveKeys[$p.Key.ToUpper()] = $p }

# ---------------------------------------------------------------- 3. .env.example 对比
Write-Step '第 3 步 / 和 .env.example 对比'
if (Test-Path -LiteralPath $exampleFile) {
    Write-Pass "发现 .env.example：$exampleFile"
    $exKeys = @()
    foreach ($line in (Get-Content -LiteralPath $exampleFile -Encoding UTF8)) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') { $exKeys += $Matches[1] }
    }
    $missingInEnv = @($exKeys | Where-Object { -not $haveKeys.ContainsKey($_.ToUpper()) })
    if ($missingInEnv.Count -eq 0) {
        Write-Pass '.env.example 里列的键，.env 全都有'
    } else {
        Write-Fail "`.env 缺少 .env.example 里列的 $($missingInEnv.Count) 个键：$($missingInEnv -join '、')" '照第 6 步模板逐条补上；不想用的键也必须留一行（值可以空）。'
    }
} else {
    Write-Warn '项目里没有 .env.example（本机实际情况就是这样）' '不算错。本脚本会按「已知键基线」+「现有 .env 的键名」来对比，不靠这个文件。'
}

# ---------------------------------------------------------------- 4. 基线对比
Write-Step '第 4 步 / 关键键基线检查'
foreach ($spec in $keySpec) {
    $k = $spec.Key.ToUpper()
    if ($haveKeys.ContainsKey($k)) {
        $v = $haveKeys[$k]
        if ($v.Empty -and $spec.Req) {
            Write-Fail "$($spec.Key) 存在但是空值" $spec.Note
        } else {
            Write-Pass "$($spec.Key) 已配置（$(if ($v.Empty) { '空值' } else { "长度 $($v.Len)" })）"
        }
    } else {
        if ($spec.Req) { Write-Fail "缺少必需键 $($spec.Key)" $spec.Note }
        else { Write-Warn "缺少可选键 $($spec.Key)" $spec.Note }
    }
}

# ---------------------------------------------------------------- 5. 两条红线
Write-Step '第 5 步 / 两条红线（踩了站点就打不开）'

# 红线一：DEBUG 必须 True
if ($haveKeys.ContainsKey('DEBUG')) {
    $d = $haveKeys['DEBUG']
    $raw = $null
    foreach ($line in $rawLines) { if ($line -match '^\s*DEBUG\s*=\s*(.*)$') { $raw = $Matches[1].Trim(); break } }
    if ($raw -match '^(?i)(true|1|yes|on)$') {
        Write-Pass 'DEBUG = True（正确，千万别改）'
    } elseif ($raw -match '^(?i)(false|0|no|off)$') {
        Write-Fail "DEBUG = $raw ，这会把站点搞成纯文字" '改成 DEBUG=True。关掉 DEBUG 后 Django 不再代管静态文件，CSS/JS 全 404，页面裸奔。想藏 debug_toolbar 就保持 INTERNAL_IPS=[]（settings.py 里已经是空的）。'
    } else {
        Write-Warn "DEBUG 的值不是标准 true/false（$raw）" '统一写成 True。'
    }
} else {
    Write-Fail '没有 DEBUG 这个键' 'settings.py 里 DEBUG 默认是 False！不显式写 DEBUG=True，站点必变纯文字。'
}

# 红线二：DB_HOST 绝对不能有
if ($haveKeys.ContainsKey('DB_HOST')) {
    Write-Fail '.env 里出现了 DB_HOST' '整行删掉！settings.py 只要读到 DB_HOST 有值就切去连 Postgres，本项目是 SQLite，立刻报错。'
} else {
    Write-Pass '没有 DB_HOST（正确，走 SQLite）'
}
if ($env:DB_HOST) {
    Write-Fail "当前窗口环境变量里有 DB_HOST=$($env:DB_HOST)" '这个比 .env 更阴：进程会继承。先 Remove-Item Env:DB_HOST，或新开窗口再跑。'
}

# 其他容易手滑的
if ($haveKeys.ContainsKey('DB_NAME') -or $haveKeys.ContainsKey('DB_USER') -or $haveKeys.ContainsKey('DB_PASSWORD')) {
    Write-Warn '出现了 DB_NAME / DB_USER / DB_PASSWORD 之类' '这些是 Postgres 的键。本项目用 SQLite，留着没用还容易误导，建议删掉。'
}
if ($haveKeys.ContainsKey('BASE_URL') -and -not $haveKeys['BASE_URL'].Empty) {
    Write-Warn 'BASE_URL 有值' '它会把静态文件路径前缀改掉，本地 http://127.0.0.1:8001 会 404。本地跑请留空或删掉。'
}

# ---------------------------------------------------------------- 6. 脱敏模板
Write-Step '第 6 步 / 脱敏模板（只有键名和占位符，绝不含真实值）'
$templateLines = @()
$templateLines += '# ============================================================'
$templateLines += '# Steam 记录站 .env 模板（脱敏，只有键名和占位符）'
$templateLines += '# 用法：复制成项目根的 .env，再把 <...> 换成真实值'
$templateLines += '# 红线：DEBUG 必须 True；不要加 DB_HOST'
$templateLines += '# ============================================================'
$templateLines += ''
foreach ($spec in $keySpec) {
    $templateLines += "# $($spec.Note)"
    switch ($spec.Key) {
        'SECRET'                       { $templateLines += 'SECRET=<随机长字符串，别用示例值>' }
        'DEBUG'                        { $templateLines += 'DEBUG=True' }
        'ALLOWED_HOSTS'                { $templateLines += 'ALLOWED_HOSTS=*' }
        'TZ'                           { $templateLines += 'TZ=Asia/Hong_Kong' }
        'REDIS_URL'                    { $templateLines += 'REDIS_URL=redis://127.0.0.1:6379/0' }
        'STEAM_API_KEY'                { $templateLines += 'STEAM_API_KEY=<32 位 Steam Web API Key>' }
        'STEAM_ID'                     { $templateLines += 'STEAM_ID=<7656 开头的 17 位 SteamID64>' }
        'STEAM_ACCESS_TOKEN'           { $templateLines += 'STEAM_ACCESS_TOKEN=<家庭共享令牌，由计划任务自动续，可留空>' }
        'YAMTRACK_AUTO_LOGIN_USERNAME' { $templateLines += 'YAMTRACK_AUTO_LOGIN_USERNAME=<用户名，如 admin>' }
        'NOTION_TOKEN'                 { $templateLines += 'NOTION_TOKEN=<可留空>' }
        'WEREAD_API_KEY'               { $templateLines += 'WEREAD_API_KEY=<可留空>' }
        'DOUBAN_PROXY'                 { $templateLines += 'DOUBAN_PROXY=<可留空，默认 http://127.0.0.1:7899>' }
        default                        { $templateLines += "$($spec.Key)=<按上面的说明填>" }
    }
    $templateLines += ''
}

# 现有 .env 里有、基线上没有的键，也列出来（只键名）
$extra = @($pairs | Where-Object { $k = $_.Key.ToUpper(); -not ($keySpec | Where-Object { $_.Key.ToUpper() -eq $k }) })
if ($extra.Count -gt 0) {
    $templateLines += '# ---- 现有 .env 里额外的键（基线模板没覆盖，按需保留）----'
    foreach ($e in $extra) { $templateLines += "$($e.Key)=<现有 .env 里有这个键，值请自己保留>" }
    $templateLines += ''
}

if (Test-Path -LiteralPath $exampleFile) {
    Write-Info '.env.example 已存在，模板仅作参考，不会覆盖它。'
}
Write-Host ''
foreach ($l in $templateLines) { Write-Host "  $l" -ForegroundColor DarkGray }

if ($WriteTemplate) {
    $outFile = Join-Path $RootPath 'deploy\.env.template'
    try {
        # UTF-8 无 BOM 写入（.env 解析器对 BOM 敏感）
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllLines($outFile, $templateLines, $utf8NoBom)
        Write-Pass "模板已写到：$outFile"
        Write-Info '只写 deploy 目录里，项目根的 .env 一个字都没动。'
    } catch {
        Write-Fail "写模板失败：$($_.Exception.Message)" '可以手动复制上面屏幕上的内容。'
    }
} else {
    Write-Info '想存成文件就加 -WriteTemplate（只会写到 deploy\.env.template，不碰项目根）。'
}

# ---------------------------------------------------------------- 汇总
Write-Host ''
Write-Host '==================================================' -ForegroundColor White
Write-Host ("  汇总：成功 $script:OkCount / 失败 $script:FailCount / 警告 $script:WarnCount") -ForegroundColor White
Write-Host '==================================================' -ForegroundColor White
if ($script:FailCount -eq 0) {
    Write-Host '  配置没问题，接着跑 05_static.ps1。' -ForegroundColor Green
} else {
    Write-Host '  有失败项，改完 .env 再重跑本脚本。改完 .env 记得重启站点才生效。' -ForegroundColor Red
}
Write-Host '  提醒：本脚本从不写 .env，也不打印任何真实值。' -ForegroundColor DarkGray
exit $(if ($script:FailCount -gt 0) { 1 } else { 0 })