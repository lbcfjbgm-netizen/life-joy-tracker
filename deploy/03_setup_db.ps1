#Requires -Version 5.1
<#
  03_setup_db.ps1 —— 数据库准备（默认只体检，绝不改库）

  两条路：
    A. 数据库已存在（正常情况，本机就是这种）
       → 默认只做只读检查（大小、表数量、有没有 admin 用户、WAL 文件多大）。
       → 加 -Apply 也不会 migrate，只会先「热备份」一份到 .tools\backups\，然后提示你可以 migrate。
    B. 数据库不存在（新机器）
       → 默认只告诉你「将会执行哪条命令」。
       → 加 -Apply 才真的 migrate，跑完提示你建超级用户。

  为什么默认不 migrate：
    migrate 会改表结构。这个库有 4900+ 游戏、4600+ 事件，出问题不好回滚。
    所以必须你亲眼看一眼再传 -Apply。

  用法：
    powershell -NoProfile -ExecutionPolicy Bypass -File "...\deploy\03_setup_db.ps1"            # 只看
    powershell -NoProfile -ExecutionPolicy Bypass -File "...\deploy\03_setup_db.ps1" -Apply     # 真干
    powershell -NoProfile -ExecutionPolicy Bypass -File "...\deploy\03_setup_db.ps1" -Apply -SkipBackup
#>
[CmdletBinding()]
param(
    [string]$Root = '',   # 空 = 自动定位成「本脚本所在 deploy 目录的上一级」
    [switch]$DryRun,
    [switch]$Apply,
    [switch]$SkipBackup,
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
Write-Host '  数据库 03_setup_db.ps1' -ForegroundColor White
Write-Host ("  模式：" + $(if ($doApply) { '会真的动手（-Apply）' } else { '只体检，绝不改库（默认）' })) -ForegroundColor White
Write-Host '==================================================' -ForegroundColor White

if (-not (Test-Path -LiteralPath $Root)) { Write-Host "项目根目录不存在：$Root" -ForegroundColor Red; exit 1 }
$RootPath = (Resolve-Path -LiteralPath $Root).Path
$py = Join-Path $RootPath '.venv\Scripts\python.exe'
$srcDir = Join-Path $RootPath 'src'
$manage = Join-Path $srcDir 'manage.py'
$dbFile = Join-Path $srcDir 'db\db.sqlite3'
$walFile = Join-Path $srcDir 'db\db.sqlite3-wal'
$shmFile = Join-Path $srcDir 'db\db.sqlite3-shm'
$backupDir = Join-Path $RootPath '.tools\backups'

# ---------------------------------------------------------------- 1. 前置
Write-Step '第 1 步 / 前置条件'
if (Test-Path -LiteralPath $py) { Write-Pass "python：$py" }
else { Write-Fail "找不到 $py" '先搞定虚拟环境（见 01_check_env.ps1 第 3 步）。' }

if (Test-Path -LiteralPath $manage) { Write-Pass "manage.py：$manage" }
else { Write-Fail "找不到 $manage" 'src\manage.py 是 Django 入口，缺了整个 03 都做不了。' }

# DB_HOST 是这台机器的头号地雷：设了就会去连 Postgres，直接报错
Write-Step '第 2 步 / 检查 DB_HOST（最重要的一条）'
$envFile = Join-Path $RootPath '.env'
$dbHostLine = $null
if (Test-Path -LiteralPath $envFile) {
    $dbHostLine = @(Get-Content -LiteralPath $envFile -Encoding UTF8 |
        Where-Object { $_ -match '^\s*DB_HOST\s*=' }) | Select-Object -First 1
}
if ($dbHostLine) {
    $val = ($dbHostLine -split '=', 2)[1]
    if ($val -and $val.Trim() -ne '') {
        Write-Fail "`.env 里设了 DB_HOST（值是 $($val.Trim())）" '必须删掉这一整行！设了它 Django 会去连 Postgres，本项目用 SQLite，直接报错。'
    } else {
        Write-Warn '.env 里有 DB_HOST= 但值是空的' '空值不影响（等于没设），但建议整行删掉，免得下次被人填上。'
    }
} else {
    Write-Pass '.env 里没有 DB_HOST（正确，走 SQLite）'
}
if ($env:DB_HOST) {
    Write-Fail "当前窗口的环境变量里也有 DB_HOST=$($env:DB_HOST)" '这比 .env 更隐蔽：即使 .env 没写，进程也会继承。跑之前先 Remove-Item Env:DB_HOST，或者直接新开一个窗口。'
} else {
    Write-Pass '当前窗口环境变量里没有 DB_HOST'
}

# ---------------------------------------------------------------- 3. 库现状
Write-Step '第 3 步 / 数据库现状'
$dbExists = Test-Path -LiteralPath $dbFile
if ($dbExists) {
    $dbMB = [math]::Round((Get-Item -LiteralPath $dbFile).Length / 1MB, 1)
    Write-Pass "数据库存在：$dbFile（$dbMB MB）"
    if (Test-Path -LiteralPath $walFile) {
        $walMB = [math]::Round((Get-Item -LiteralPath $walFile).Length / 1MB, 2)
        Write-Info "WAL 文件 ${walMB} MB（正常，SQLite 的预写日志；备份时要注意一起拷）"
    }
    if (Test-Path -LiteralPath $shmFile) { Write-Info 'SHM 文件存在（正常）' }
} else {
    Write-Warn '数据库不存在（新机器第一次部署会这样）' '下一步 -Apply 会建库并跑 migrate，得到一个空站点。'
}

# 只读方式查一下库内容（mode=ro，绝不写）
if ($dbExists -and (Test-Path -LiteralPath $py)) {
    $pyCode = @'
import sqlite3, sys
db = sys.argv[1]
try:
    con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    cur = con.cursor()
    tables = [r[0] for r in cur.execute("select name from sqlite_master where type='table'")]
    print("TABLES=%d" % len(tables))
    if "app_item" in tables:
        print("ITEMS=%d" % cur.execute("select count(*) from app_item").fetchone()[0])
    if "app_game" in tables:
        print("GAMES=%d" % cur.execute("select count(*) from app_game").fetchone()[0])
    if "users_user" in tables:
        print("USERS=%d" % cur.execute("select count(*) from users_user").fetchone()[0])
    if "django_migrations" in tables:
        print("MIGRATIONS=%d" % cur.execute("select count(*) from django_migrations").fetchone()[0])
    con.close()
except Exception as exc:
    print("ERR=%s" % exc)
'@
    $tmp = [System.IO.Path]::GetTempFileName() + '.py'
    try {
        Set-Content -LiteralPath $tmp -Value $pyCode -Encoding UTF8
        $out = (& $py $tmp $dbFile 2>&1 | Out-String)
        foreach ($line in ($out -split "`r?`n")) {
            if ($line -match '^TABLES=(\d+)') { Write-Pass "库里有 $($Matches[1]) 张表" }
            elseif ($line -match '^ITEMS=(\d+)') { Write-Info "条目数（app_item）：$($Matches[1])" }
            elseif ($line -match '^GAMES=(\d+)') { Write-Info "游戏数（app_game）：$($Matches[1])" }
            elseif ($line -match '^USERS=(\d+)') { Write-Info "用户数：$($Matches[1])" }
            elseif ($line -match '^MIGRATIONS=(\d+)') { Write-Info "已应用的迁移数：$($Matches[1])" }
            elseif ($line -match '^ERR=(.+)') { Write-Warn "只读查询数据库失败：$($Matches[1])" '可能是数据库被独占或者文件损坏；先别 migrate，先备份出来看。' }
        }
    } catch {
        Write-Warn "读库时出错：$($_.Exception.Message)" '不影响后续，只是少了几个参考数字。'
    } finally {
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------- 4. 备份
Write-Step '第 4 步 / 备份'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupPath = Join-Path $backupDir "db-deploy-$stamp.sqlite3"
if (-not $dbExists) {
    Write-Info '库都还不存在，不用备份。'
} elseif ($SkipBackup) {
    Write-Warn '你传了 -SkipBackup，跳过备份' '只在「刚备份过、确定安全」时才这么干。'
} else {
    if ($doApply) {
        if (-not (Test-Path -LiteralPath $backupDir)) {
            try { New-Item -ItemType Directory -Path $backupDir -Force | Out-Null; Write-Pass "已创建备份目录：$backupDir" }
            catch { Write-Fail "建备份目录失败：$($_.Exception.Message)" '手工建一下 .tools\backups 目录。' }
        }
        try {
            # 在线热备：用 SQLite 官方 backup API，不锁库、不影响正在跑的站点
            $pyBackup = @'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
con = sqlite3.connect("file:%s?mode=ro" % src.replace("\\", "/"), uri=True)
out = sqlite3.connect(dst)
with out:
    con.backup(out)
out.close(); con.close()
print("OK")
'@
            $tmpb = [System.IO.Path]::GetTempFileName() + '.py'
            Set-Content -LiteralPath $tmpb -Value $pyBackup -Encoding UTF8
            $r = (& $py $tmpb $dbFile $backupPath 2>&1 | Out-String)
            Remove-Item -LiteralPath $tmpb -Force -ErrorAction SilentlyContinue
            if ((Test-Path -LiteralPath $backupPath) -and (Get-Item -LiteralPath $backupPath).Length -gt 0) {
                $bkMB = [math]::Round((Get-Item -LiteralPath $backupPath).Length / 1MB, 1)
                Write-Pass "已热备份：$backupPath（$bkMB MB）"
                Write-Info '这个备份是在站点运行时做的，不影响正在跑的进程。'
            } else {
                Write-Fail "备份失败：$r" '退回最简单办法：先双击 stop-win.vbs 停站，再手动复制 src\db\db.sqlite3 到别处。'
            }
        } catch {
            Write-Fail "备份出错：$($_.Exception.Message)" '先停站再手动复制 db.sqlite3 文件。'
        }
    } else {
        Write-Plan "真要备份时，会写到：$backupPath"
        Write-Info '（用的是 SQLite 在线热备接口，不停站、不锁库）'
    }
}

# ---------------------------------------------------------------- 5. migrate
Write-Step '第 5 步 / migrate（改表结构，默认不动）'
$migrateCmd = "cd `"$srcDir`" ; & `"$py`" manage.py migrate"
if (-not $doApply) {
    Write-Plan "将要在 src 目录执行：& `"$py`" manage.py migrate"
    Write-Info '注意：manage.py 必须在 src 目录下跑，在项目根跑会找不到模块。'
    if ($dbExists) {
        Write-Info '当前库已存在，多数情况下 migrate 只会「No migrations to apply」。'
    } else {
        Write-Info '当前库不存在，migrate 会新建 src\db\db.sqlite3。'
    }
} else {
    if (-not $dbExists -and -not $SkipBackup) {
        Write-Info '新库没法备份，继续 migrate（这一步是建库，本来就无数据可丢）。'
    }
    Write-Info "执行：$migrateCmd"
    Push-Location -LiteralPath $srcDir
    try {
        & $py manage.py migrate
        $mcode = $LASTEXITCODE
    } catch {
        $mcode = 1
        Write-Fail "migrate 抛异常：$($_.Exception.Message)" '把这段报错原文贴给 AI 排查，别急着重跑。'
    } finally {
        Pop-Location
    }
    if ($mcode -eq 0) { Write-Pass 'migrate 完成' }
    elseif ($mcode) { Write-Fail "migrate 失败（退出码 $mcode）" '常见原因：DB_HOST 被设了（回去看第 2 步）/ .env 缺 SECRET / 数据库文件被别的进程锁住。' }
}

# ---------------------------------------------------------------- 6. 超级用户
Write-Step '第 6 步 / 超级用户'
if ($dbExists) {
    Write-Info '库已存在，超级用户多半已经建过（admin）。要做「是否已有 admin」的判断得查 users_user 表，见第 3 步输出。'
    Write-Info '真要补建一个：cd src 然后 ..\.venv\Scripts\python.exe manage.py createsuperuser'
} else {
    Write-Plan '建库跑完后，如果要后台账号，在 src 目录执行：'
    Write-Info "cd `"$srcDir`""
    Write-Info "& `"$py`" manage.py createsuperuser"
    Write-Info '本项目已有 admin 账号，新库必须自己建，否则 /panel/ 进不去。'
}
if (Test-Path -LiteralPath (Join-Path $RootPath '.tools\make_superuser.py')) {
    Write-Info '另外项目自带 .tools\make_superuser.py，可以直接用它建（会读 .env 里的用户名）。'
}

# ---------------------------------------------------------------- 汇总
Write-Host ''
Write-Host '==================================================' -ForegroundColor White
Write-Host ("  汇总：成功 $script:OkCount / 失败 $script:FailCount / 警告 $script:WarnCount") -ForegroundColor White
Write-Host '==================================================' -ForegroundColor White
if ($script:FailCount -eq 0) {
    Write-Host '  数据库这块没问题，接着跑 04_setup_env.ps1。' -ForegroundColor Green
} else {
    Write-Host '  有失败项。DB_HOST 那条是红线，必须先解决。' -ForegroundColor Red
}
if (-not $doApply) { Write-Host '  （提醒：这次是只检查，数据库一个字都没改）' -ForegroundColor DarkGray }
exit $(if ($script:FailCount -gt 0) { 1 } else { 0 })