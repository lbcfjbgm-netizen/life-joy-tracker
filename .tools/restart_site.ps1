# 重启 life joy 站点（唯一正确姿势）
#
# 为什么必须这样：
#   1. 模板被 Django 的 cached.Loader 缓存，站点又是 --noreload 启动的，
#      改了 .html 不重启，活站永远输出旧版。
#   2. 不能用 Start-Process 起站点 —— 会被 DSH 的作业对象连坐杀掉，
#      日志全干净查不出原因。
#   3. bash 里调 schtasks /run 会「假成功」（返回 0 但没真跑），必须用 pwsh。
#
# 用法（在 PowerShell 里）：
#   pwsh -File "C:\Users\USERNAME\Downloads\life joy\yamtrack\.tools\restart_site.ps1"

$ErrorActionPreference = 'Stop'
$Port = 8001
$TaskName = 'SteamTracker Start'
$Url = "http://127.0.0.1:$Port/"

Write-Host '=== 1/5 找占用 8001 的进程 ==='
$pids = @()
$lines = netstat -ano | Select-String ":$Port\s" | Select-String 'LISTENING'
foreach ($l in $lines) {
    $parts = ($l.ToString().Trim() -split '\s+')
    $candidate = $parts[-1]
    if ($candidate -match '^\d+$' -and $candidate -ne '0') { $pids += [int]$candidate }
}
$pids = $pids | Sort-Object -Unique

if ($pids.Count -eq 0) {
    Write-Host '  没找到监听进程（站点可能已经停了）'
} else {
    foreach ($p in $pids) {
        $proc = Get-Process -Id $p -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Host ("  杀 PID {0} ({1})" -f $p, $proc.ProcessName)
            Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
        }
    }
}

Write-Host '=== 2/5 等端口释放（最多 15 秒）==='
$freed = $false
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    $still = netstat -ano | Select-String ":$Port\s" | Select-String 'LISTENING'
    if (-not $still) { $freed = $true; break }
}
if ($freed) { Write-Host '  端口已释放' } else { Write-Host '  ⚠ 15 秒后端口仍被占用，可能还有子进程' }

Write-Host '=== 3/5 清掉停止标记（否则看门狗不来救）==='
$flag = Join-Path $PSScriptRoot 'stopped.flag'
if (Test-Path $flag) { Remove-Item $flag -Force; Write-Host '  已删 stopped.flag' } else { Write-Host '  本来就没有 stopped.flag' }

Write-Host '=== 4/5 通过计划任务启动（脱离 DSH 的作业对象）==='
# 必须走计划任务：不在任何 Job 里，救回来的进程才是真独立
schtasks /run /tn $TaskName | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ⚠ schtasks 返回 $LASTEXITCODE，看看计划任务还在不在："
    schtasks /query /tn $TaskName /fo LIST
    exit 1
}
Write-Host '  已投递启动请求'

Write-Host '=== 5/5 等站点起来并验证（最多 90 秒）==='
$up = $false
for ($i = 0; $i -lt 45; $i++) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-WebRequest -Uri $Url -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        if ($r.StatusCode -eq 200) {
            Write-Host ("  ✅ 站点已起，首页 200，{0} 字节" -f $r.RawContentLength)
            $up = $true
            break
        }
    } catch {
        # 还没起来，继续等
    }
}

if (-not $up) {
    Write-Host '  ❌ 90 秒内没起来，去看日志：'
    Write-Host ('     ' + (Join-Path $PSScriptRoot 'logs\last-start.log'))
    exit 1
}

Write-Host '=== 抽验几个关键页面 ==='
foreach ($path in @('/statistics', '/year-in-review', '/near-complete', '/admin/game')) {
    try {
        $r = Invoke-WebRequest -Uri ($Url.TrimEnd('/') + $path) -TimeoutSec 30 -UseBasicParsing -ErrorAction Stop
        Write-Host ("  {0,-18} {1}" -f $path, $r.StatusCode)
    } catch {
        $code = $null
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        Write-Host ("  {0,-18} {1}" -f $path, $(if ($code) { $code } else { 'ERR' }))
    }
}

Write-Host ''
Write-Host '完成。'
