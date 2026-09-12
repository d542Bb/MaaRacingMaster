# ============================================================================
# 源码直跑启动器（Debug Shell）：让屏幕上跑的窗口就是当前仓库的 Python 源码。
#
#   用法  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dev\run.ps1
#         （日常用仓库根 dev-shell.cmd 双击即可）
#   参数  -Rebuild        强制重新编译 .NET Shell（默认只在 C#/前端更新时才编）
#         -StopExisting   结束正在运行的 MaaRM 实例（含打包版），再起源码版
#         -CheckDeps      额外做一次 .venv 真导入检查（import maa/cv2/numpy）
#         -NoLaunch       只编译与自检，不启动窗口
#   编码  本文件含中文，必须保持 UTF-8 **with BOM**：Windows PowerShell 5.1 无 BOM
#         时按本地代码页误读，中文提示变乱码、引号配对被吞成语法错误。
#
# 为什么需要它：Shell.exe 的仓库根解析是「--app-root > MaaRM_APP_ROOT > 从 exe
# 基目录向上找 pyproject.toml」（MainWindow.xaml.cs:ResolveRepoRoot），而正式包把
# MaaRM_APP_ROOT 指到安装根、并把源码复制进包里，于是打包目录里的 exe 一旦双击，
# 跑的是包里那份快照，仓库改动完全不生效。本脚本只启动 bin\ 下的 Shell.exe，
# 让上面那条向上探测落到仓库根，Python 侧走 .venv -m maaracing_master.core.sidecar。
# ============================================================================

param(
    [switch]$Rebuild,
    [switch]$StopExisting,
    [switch]$CheckDeps,
    [switch]$NoLaunch
)

$ErrorActionPreference = 'Stop'

# 仓库根一律由脚本自身位置推导，绝不写死本机路径
$RepoRoot = (Resolve-Path (Join-Path (Split-Path $PSScriptRoot -Parent) '..')).Path
$ShellProj = Join-Path $RepoRoot 'apps\MaaRacingMaster.Shell\MaaRacingMaster.Shell.csproj'
$VenvPy = Join-Path $RepoRoot '.venv\Scripts\python.exe'

function Write-Step($tag, $msg) { Write-Host ("[{0}] {1}" -f $tag, $msg) }
function Fail($msg) {
    Write-Host "[FAIL] $msg" -ForegroundColor Red
    exit 1
}

Write-Host ''
Write-Host '================ MaaRM 源码直跑（Debug Shell） ================'

# ---------------- 1. 预检：跑源码需要的三件套 ----------------
if (-not (Test-Path (Join-Path $RepoRoot 'pyproject.toml'))) {
    Fail "仓库根 marker 缺失：$RepoRoot（Shell.exe 靠 pyproject.toml 向上探测仓库根）"
}
if (-not (Test-Path $ShellProj)) { Fail "找不到 C# 工程：$ShellProj" }
if (-not (Test-Path $VenvPy)) {
    Fail @'
缺 .venv —— 按 README「2. 安装 Python 环境」建一次即可：
    py -3.11 -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt
'@
}
$dotnet = Get-Command dotnet -ErrorAction SilentlyContinue
if (-not $dotnet) { Fail 'PATH 上没有 dotnet —— 安装 .NET SDK（README 环境要求）后重试。' }

$commit = ''
try { $commit = (& git -C $RepoRoot rev-parse --short HEAD 2>$null) } catch { }
$dirty = ''
try {
    if ((& git -C $RepoRoot status --porcelain 2>$null)) { $dirty = ' (工作区有未提交改动)' }
} catch { }
Write-Step 'repo' "$RepoRoot @ ${commit}$dirty"
Write-Step 'python' "$VenvPy  （sidecar: -u -m maaracing_master.core.sidecar）"

# 环境隔离：MaaRM_APP_ROOT 是 Launcher 递给 Shell 的「字条」，优先级高于「从 exe
# 向上找 pyproject.toml」。终端里残留这张字条会把源码版静默领去别处，与双击错打包
# exe 属同一类事故；启动前打印原值并清除，让源码模式只能靠仓库门牌定位。
if ($env:MaaRM_APP_ROOT) {
    Write-Step 'env' "清除残留 MaaRM_APP_ROOT = $($env:MaaRM_APP_ROOT)（它会盖掉仓库根自动探测）"
    Remove-Item -Path Env:MaaRM_APP_ROOT -ErrorAction SilentlyContinue
}

if ($CheckDeps) {
    & $VenvPy -c 'import maa, cv2, numpy' 2>&1 | ForEach-Object { Write-Host "  $_" }
    if ($LASTEXITCODE -ne 0) { Fail '.venv 依赖不完整（import maa/cv2/numpy 失败）：pip install -r requirements.txt' }
    Write-Step 'deps' 'import maa / cv2 / numpy OK'
}

# ---------------- 2. 增量编译 .NET Shell ----------------
# 只有 C#/XAML/工程文件/前端资源比现有 exe 新才编，纯 Python 改动秒级直达。
$BinExe = Get-ChildItem (Join-Path $RepoRoot 'apps\MaaRacingMaster.Shell\bin') -Recurse -Filter 'MaaRacingMaster.Shell.exe' -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match '\\Debug\\' } | Sort-Object LastWriteTime -Descending | Select-Object -First 1

$NeedBuild = $true
if (-not $Rebuild -and $BinExe) {
    $ExeTime = $BinExe.LastWriteTime
    $Sources = @(Get-ChildItem (Join-Path $RepoRoot 'apps\MaaRacingMaster.Shell') -Recurse -File -Include '*.cs', '*.xaml', '*.csproj', '*.manifest' |
        Where-Object { $_.FullName -notmatch '\\(bin|obj)\\' })
    $Sources += @(Get-ChildItem (Join-Path $RepoRoot 'apps\MaaRacingMaster.Shell\frontend') -Recurse -File -ErrorAction SilentlyContinue)
    $Newer = $Sources | Where-Object { $_.LastWriteTime -gt $ExeTime }
    if ($Newer) {
        Write-Step 'build' "C#/前端有 $($Newer.Count) 个文件比 exe 新，重新编译（先列出最关键的）："
        $Newer | Sort-Object LastWriteTime -Descending | Select-Object -First 5 | ForEach-Object {
            Write-Host ('        ' + $_.FullName.Substring($RepoRoot.Length + 1))
        }
    }
    else {
        $NeedBuild = $false
        Write-Step 'build' "跳过（exe 比全部 C#/前端资源新：$($BinExe.LastWriteTime.ToString('MM-dd HH:mm'))）"
    }
}
if ($NeedBuild) {
    Write-Step 'build' 'dotnet build -c Debug（首次约需拉取 NuGet，稍候）'
    & dotnet build $ShellProj -c Debug --nologo -v minimal
    if ($LASTEXITCODE -ne 0) { Fail 'dotnet build 失败 —— 上面是 MSBuild 原文，按它修。' }
    $BinExe = Get-ChildItem (Join-Path $RepoRoot 'apps\MaaRacingMaster.Shell\bin') -Recurse -Filter 'MaaRacingMaster.Shell.exe' -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '\\Debug\\' } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $BinExe) { Fail '编译成功但 bin\ 下找不到 MaaRacingMaster.Shell.exe' }
}

# ---------------- 3. 单例防呆：旧实例会让新实例静默退出 ----------------
# Shell 与 Launcher 共用同一注册表单例锁（App.xaml.cs 的 Local\MaaRacingMaster 节），
# 打包版在跑时再起源码版，只会看到"点了没反应"。
$Existing = @(Get-CimInstance Win32_Process -Filter "Name='MaaRacingMaster.exe' OR Name='MaaRacingMaster.Shell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -or $_.ExecutablePath })
if ($Existing.Count -gt 0) {
    Write-Host '[warn] 已有 MaaRM 实例在运行：' -ForegroundColor Yellow
    $Existing | ForEach-Object { Write-Host ("        {0}  pid={1}" -f $_.ExecutablePath, $_.ProcessId) }
    if (-not $StopExisting) {
        Write-Host '        互斥锁会让源码版启动后立即退出。先关掉它，或加 -StopExisting 让本脚本代劳。' -ForegroundColor Yellow
        Fail '存在冲突实例，未启动。'
    }
    foreach ($p in $Existing) {
        Write-Step 'kill' "pid=$($p.ProcessId)  $($p.ExecutablePath)"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 800
}

if ($NoLaunch) { Write-Step 'done' '-NoLaunch：已编译未启动。'; exit 0 }

# ---------------- 4. 启动 ----------------
# 先记下已存在的后端 pid（-StopExisting 后可能仍有残留），自证时只认新起的那个。
$PreLaunch = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'maaracing_master' } | ForEach-Object { $_.ProcessId })

# exe 的 app.manifest 带 requireAdministrator：这一步由系统弹 UAC，子进程继承 token。
Write-Step 'launch' $BinExe.FullName
try { Start-Process -FilePath $BinExe.FullName -WorkingDirectory $RepoRoot -ErrorAction Stop }
catch { Fail "启动失败：$($_.Exception.Message)（UAC 弹窗被取消？）" }

# ---------------- 5. 自证：本次后端到底由哪个 python 拉起 ----------------
# 打包版 sidecar = 包内 runtime\python\python.exe，源码版 = 仓库 .venv\Scripts\python.exe。
# 这条判据不依赖日志措辞，直接查进程，是唯一不会骗人的口径。
$Backend = $null
$VenvDir = (Split-Path $VenvPy -Parent) + '\'
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 500
    $Backend = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'maaracing_master' -and $PreLaunch -notcontains $_.ProcessId } |
        Select-Object -First 1
    if ($Backend) { break }
}

$appData = Join-Path $env:APPDATA 'MaaRacingMaster'
Write-Host ''
if (-not $Backend) {
    Write-Host '[verify] 20s 内未见本次的 sidecar 进程 —— 若 UAC 被取消或后端启动失败，' -ForegroundColor Yellow
    Write-Host '         窗口会显示 backend unavailable；重跑本脚本并留意弹窗。' -ForegroundColor Yellow
}
elseif ($Backend.ExecutablePath -and $Backend.ExecutablePath.StartsWith($VenvDir, [StringComparison]::OrdinalIgnoreCase)) {
    Write-Host '[verify] 源码模式 ✓' -ForegroundColor Green
    Write-Host "         后端 pid=$($Backend.ProcessId) 加载自 $($Backend.ExecutablePath)"
    Write-Host '         改 Python / pipeline JSON / policy 存盘后重开模块（或重跑本脚本）即生效。'
}
else {
    Write-Host '[verify] 注意：后端不是本仓库 .venv 拉起的 ——' -ForegroundColor Red
    Write-Host "         pid=$($Backend.ProcessId)  $($Backend.ExecutablePath)" -ForegroundColor Red
    Write-Host '         这次跑的仍是那个目录里的代码快照，仓库改动不会生效。' -ForegroundColor Red
}
Write-Host ''
Write-Host "  日志    $appData\logs\MaaRM_*.log"
Write-Host "  鉴宝存图 $appData\debug\treasure\<会话>\raw\"
Write-Host "  框架日志 $appData\framework\maafw.log"
Write-Host ''
