# Release package assembly for MaaRM Windows.
# Output: <OutRoot>/MaaRacingMaster-<Version>-win-x64.zip + .sha256
# Structure reproduces the validated local package:
#   <name>/{ MaaRacingMaster.Shell.exe + WinUI/.NET dlls, pyproject.toml,
#          maaracing_master/, assets/ (含 config/), apps/MaaRacingMaster.Shell/frontend/,
#          runtime/python/{python.exe, python311._pth, packages/, vcruntime140*.dll} }
# Usage: powershell -File assemble.ps1 -Version 1.0.0 -Configuration Release
#   -Configuration          Release (default, all SAFE pruning on) | Experimental (no pruning)
#   -DisableReleaseOptimizations   force off all production pruning (debug/reference, same as Experimental)
#   -HostPython             host interpreter, MUST be 3.11 (cp311) to match embedded
#   -SourceRuntimeDir       reuse an existing verified runtime (skip download+pip) for local re-verify
#   -KeepGoing              continue on error (local debug); default fail-fast
#   -SevenZ                 additionally build .7z primary artifact (solid LZMA2 256M, per C1-C bench) + .7z.sha256; zip always built as fallback
#   -SevenZPath             7za.exe path (default: scripts\release\tools\7za.exe -> build\7z\extra\x64\7za.exe; missing => zip-only fallback, non-fatal)
# Internal experiment switches below (Remove*) are driven by -Configuration; ordinary release
# does not need to pass them. See scripts/release/runtime-pruning-policy.md for the canonical whitelist.

param(
    [Parameter(Mandatory=$true)][string]$Version,
    [ValidateSet('Release','Experimental')][string]$Configuration = 'Release',
    [switch]$DisableReleaseOptimizations,   # force off all production pruning (debug/reference)
    [string]$RepoRoot = '',
    [string]$OutRoot = '',
    [string]$EmbedPythonUrl = 'https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip',
    [string]$LockFile = '',
    [string]$HostPython = 'python',
    [string]$SourceRuntimeDir = '',
    [string]$RuntimeCacheDir = '',
    [string]$VcVarsAll = '',   # native Launcher 编译用 MSVC vcvarsall.bat 路径（默认自动探测）
    [switch]$SkipPublish,
    [switch]$KeepGoing,
    [switch]$SevenZ,        # 额外产出 .7z 主推档（solid LZMA2 256M, 参数同 C1-C 基准）+ .7z.sha256；zip 保底始终产出
    [string]$SevenZPath = '',  # 7za.exe 路径；默认优先 scripts\release\tools\7za.exe，次选本地缓存 build\7z\extra\x64\7za.exe；均无则降级仅出 zip（不阻断）
    [switch]$RemoveWinAppSdkML,  # EXP-1: remove WinAppSDK AI/ML dead chain (43.6MB, MaaRM zero usage)
    [switch]$RemoveWidgets,       # EXP-2: remove WinAppSDK Widgets dead chain (2.5MB, MaaRM zero usage)
    [switch]$RemovePythonOrtCapi, # EXP-3: remove Python ORT capi\onnxruntime.dll (20.1MB, pyd self-contained)
    [switch]$RemovePilAvif,       # EXP-4A: remove Pillow _avif native ext (7.5MB, lazy-loaded AVIF only)
    [switch]$RemoveCrashDiagnostics, # EXP-4B: remove createdump/mscordaccore/DiaSymReader (4.7MB, on-demand diag; KEEP mscordbi)
    [switch]$RemoveNumpyDev,       # EXP-5A: remove numpy dev/test/build dirs (f2py/distutils/testing/tests/doc/_pyinstaller/ctypeslib, 2.55MB); KEEP _pytesttester/typing/_typing; no numpy source fork
    [switch]$RemovePythonTypingStubs, # EXP-5B-1: remove ALL runtime\python\packages\**\*.pyi (typing-only static stubs, 1.96MB/267 files); KEEP numpy.typing (still its .py), numpy._typing (runtime-loaded, its .pyi removed too but .py stays); verified no .py does open()/resources/pkgutil/metadata reads of .pyi
    [switch]$RemoveDistInfoInstallMetadata, # EXP-5C-1: remove INSTALLER/WHEEL/REQUESTED from every *.dist-info (only ~2.8KB total; install/dev-stage only, no runtime reader). METADATA/RECORD/entry_points.txt/top_level.txt untouched for 5C-1.
    [switch]$RemovePythonConsoleDev, # EXP-5E-1: remove packages\bin\*.exe console wrappers (~0.83MB/8 files: f2py numpy-config isympy normalizer idna onnxruntime_test rapidocr tqdm). All are pip console_scripts launchers (105.8KB each) for dev/test/CLI entry points. Verified: MaaRM sidecar & third-party runtime make ZERO subprocess/Popen calls to any of them (sidecar only runs git/taskkill); f2py.exe is an orphan (its numpy.f2py.f2py2e source was removed in EXP-5A). No native runtime/DLL embedded. Deleting loses only console CLI access, not library import. Default OFF; restore = reassemble w/o switch.
    [switch]$RemoveSympy,    # EXP-6: remove sympy (25.37MB) from packages. Verified via full-chain runtime trace (sidecar+Racing+Treasure+all third-party, 360 modules) that sympy is NEVER loaded on any MaaRM run path; onnxruntime DML inference does NOT load sympy either. Its only dependency source is onnxruntime_directml METADATA Requires-Dist:sympy, used only by offline tools (onnxruntime\tools\symbolic_shape_infer.py, transformers\shape_infer_helper.py) — NOT the inference/DML runtime path. MaaRM has zero sympy import. Deletion loses only onnxruntime offline symbolic shape-infer/quantization tooling. mpmath kept (separate EXP-7). Default OFF; restore = reassemble w/o switch.
    [switch]$RemoveMaaAgentBinary, # EXP-7: remove MaaAgentBinary (12.53MB) — Android/ADB agent binaries (23 adb/minicap + 56 minicap.so). Referenced only by maa\controller.py L802 AdbController path ("../MaaAgentBinary"); MaaRM uses Win32Controller (Win32 screenshot+gamepad), never ADB. Verified no runtime import loads MaaAgentBinary. Deleting breaks only future Android/ADB control. Default OFF; restore = reassemble w/o switch.
    [switch]$RemoveOrtOffline,  # EXP-8: remove onnxruntime offline toolchain deps — google\protobuf (1.57MB) + flatbuffers (0.08MB). Both are pure-python dependencies of onnxruntime\quantization + tools (cold paths), never loaded by runtime inference; runtime closure probe (onnxruntime + RapidOCR 构造+推理) confirms both NOT in sys.modules. dist-info 保留（与 RECORD 不删先例一致）。Cost: onnxruntime quantization / offline shape-infer / ort_format_model unusable (inference path unaffected). Default OFF; restore = reassemble w/o switch.
    [switch]$RemoveCvFfmpeg # EXP-9: remove opencv_videoio_ffmpeg500_64.dll (29.45MB) from cv2. dumpbin /dependents on cv2.pyd shows NO static dependency (ffmpeg dll is a runtime LoadLibrary'd videoio backend). Stage-2 probe (rm dll) confirms import cv2 + imencode/imdecode + cvtColor/resize/matchTemplate/dnn.NMSBoxes + chinese-path(utf8_patch chain) all OK; videoio(VideoWriter) unavailable but MaaRM production has ZERO videoio usage; any video-backend failure warns to STDERR, stdout(JSONL) stays clean. Cost: OpenCV video file read/write playback disabled. Default OFF; restore = reassemble w/o switch.
)

$ErrorActionPreference = 'Stop'

if (-not $RepoRoot) { $RepoRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent }
if (-not $OutRoot)  { $OutRoot  = $PSScriptRoot }
if (-not $LockFile) { $LockFile = Join-Path $PSScriptRoot 'requirements-runtime-lock.txt' }

$RepoRoot  = (Resolve-Path $RepoRoot).Path
if (-not (Test-Path $OutRoot)) { New-Item -ItemType Directory -Force -Path $OutRoot | Out-Null }
$OutRoot   = (Resolve-Path $OutRoot).Path
if (-not $RuntimeCacheDir) { $RuntimeCacheDir = Join-Path $RepoRoot 'build\runtime-cache' }
# 7za.exe 路径解析（-SevenZ 时用）：优先入库的 scripts\release\tools\7za.exe，次选本地缓存 build\7z\extra\x64\7za.exe；
# 均未找到保持 ''，§7.5 自动走"降级仅出 zip"分支（不阻断）。
if (-not $SevenZPath) {
    $builtin7z = Join-Path $RepoRoot 'scripts\release\tools\7za.exe'
    $cache7z   = Join-Path $RepoRoot 'build\7z\extra\x64\7za.exe'
    if (Test-Path $builtin7z)     { $SevenZPath = $builtin7z }
    elseif (Test-Path $cache7z)   { $SevenZPath = $cache7z }
}
# native Launcher 编译需 MSVC；未显式指定时自动探测常见 VS 安装路径
if (-not $VcVarsAll) {
    $candidates = @(
        'C:\Program Files\Microsoft Visual Studio\2022\Enterprise',
        'C:\Program Files\Microsoft Visual Studio\2022\Professional',
        'C:\Program Files\Microsoft Visual Studio\2022\Community',
        'C:\Program Files\Microsoft Visual Studio\2022\BuildTools',
        'C:\Program Files (x86)\Microsoft Visual Studio\2019\Enterprise',
        'C:\Program Files (x86)\Microsoft Visual Studio\2019\Professional',
        'C:\Program Files\Microsoft Visual Studio\2019\Community',
        'C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools',
        'D:\Microsoft Visual Studio\2019\Community',
        'D:\Microsoft Visual Studio\2022\Community'
    )
    $VcVarsAll = $candidates |
        ForEach-Object { $p = Join-Path $_ 'VC\Auxiliary\Build\vcvarsall.bat'; if (Test-Path $p) { $p } } |
        Select-Object -First 1
}
# ---------- 0.1 Configuration -> pruning switches ----------
# Production = all independently-validated SAFE removals ON (default).
# Experimental / -DisableReleaseOptimizations = no pruning (full runtime reference build).
# The 10 Remove* switches are internal; ordinary release never passes them directly.
if ($DisableReleaseOptimizations -or $Configuration -eq 'Experimental') {
    foreach ($sw in @('RemoveWinAppSdkML','RemoveWidgets','RemovePythonOrtCapi','RemovePilAvif',
                      'RemoveCrashDiagnostics','RemoveNumpyDev','RemovePythonTypingStubs',
                      'RemovePythonConsoleDev','RemoveSympy','RemoveMaaAgentBinary',
                      'RemoveOrtOffline','RemoveCvFfmpeg')) {
        if (-not (Get-Variable -Name $sw -ErrorAction SilentlyContinue)) { New-Variable -Name $sw -Value $false }
        else { Set-Variable -Name $sw -Value $false -Force }
    }
    Write-Host "[assemble] Configuration=$Configuration; production pruning DISABLED (full runtime)"
} else {
    foreach ($sw in @('RemoveWinAppSdkML','RemoveWidgets','RemovePythonOrtCapi','RemovePilAvif',
                      'RemoveCrashDiagnostics','RemoveNumpyDev','RemovePythonTypingStubs',
                      'RemovePythonConsoleDev','RemoveSympy','RemoveMaaAgentBinary',
                      'RemoveOrtOffline','RemoveCvFfmpeg')) {
        if (-not (Get-Variable -Name $sw -ErrorAction SilentlyContinue)) { New-Variable -Name $sw -Value $true }
        else { Set-Variable -Name $sw -Value $true -Force }
    }
    # 明确不纳入正式裁剪的项保持 OFF，仅保留在 Experimental 作参考
    Set-Variable -Name RemoveDistInfoInstallMetadata -Value $false -Force
    Write-Host "[assemble] Configuration=Release; production pruning ON (12 SAFE removals)"
}

$Name      = 'MaaRacingMaster-' + $Version + '-win-x64'
$StageRoot = Join-Path $OutRoot $Name

Write-Host "[assemble] Version=$Version stage=$StageRoot"

$errors = New-Object System.Collections.Generic.List[string]
$Fail = { param($m) Write-Host "FATAL: $m" -ForegroundColor Red; exit 1 }

# ---------- Production pruning Guard ----------
# 原则：宁可发布失败，也不要"悄悄少删一部分让体积膨胀"。
# 每个大项删除前先检查源文件存在；若应存在却缺失（依赖结构变化导致白名单失效）→ 报 error 阻止 release（除非 -KeepGoing）。
$Guard = {
    param([string]$label, [string]$path, [switch]$Require)
    if (Test-Path $path) {
        # 该删除的项还存在，正常
        return $true
    }
    if ($Require) {
        # 应存在（依赖敏感项），缺失说明结构变化 —— 严格模式返回 false，调用方报错
        return $false
    }
    # 不强制：允许缺失（版本号/细节变化），静默跳过
    return $null
}
# 敏感项清单：缺失即应版失败（这些是依赖版本敏感的删除项）
$PruneExpects = @(
    @{Label='WinAppSDK-AI/ML';                  Path='app\onnxruntime.dll'},
    @{Label='Python-ORT-capiDLL';               Path='runtime\python\packages\onnxruntime\capi\onnxruntime.dll'},
    @{Label='PIL-_avif';                        Path='runtime\python\packages\PIL\_avif.cp311-win_amd64.pyd'},
    @{Label='SymPy';                            Path='runtime\python\packages\sympy\__init__.py'},
    @{Label='MaaAgentBinary';                   Path='runtime\python\packages\MaaAgentBinary\README.md'}
)

# ---------- 缓存源指纹（防跨分支 / 改代码后误用过期缓存） ----------
$pubFinger = 'publish/' + ((Get-ChildItem (Join-Path $RepoRoot 'apps\MaaRacingMaster.Shell') -Recurse -File |
    Where-Object { $_.Extension -in '.cs','.xaml','.csproj','.manifest' -and $_.FullName -notmatch '\\(bin|obj)\\' } |
    Sort-Object FullName |
    ForEach-Object { (Get-FileHash $_.FullName -Algorithm SHA256).Hash }) -join ';')
$lockHash = (Get-FileHash $LockFile -Algorithm SHA256).Hash
$pyMinor  = (& $HostPython -c "import sys;print(sys.version_info[0],'.',sys.version_info[1])" 2>$null) -join ''
$rtFinger = 'runtime/' + $lockHash + '/' + $pyMinor
# 读取/写入缓存目录内的指纹标记
$CacheFPWrite = { param($dir,$finger) Set-Content -Path (Join-Path $dir '.cache-fingerprint') -Value $finger -Encoding ascii }
$CacheFPMatch = { param($dir,$finger) $f = Join-Path $dir '.cache-fingerprint'; if (Test-Path $f) { return ((Get-Content $f -Raw).Trim() -eq $finger) }; return $false }

# ---------- 0. stage ----------
if (Test-Path $StageRoot) { Remove-Item $StageRoot -Recurse -Force }
New-Item -ItemType Directory -Path $StageRoot | Out-Null

# ---------- 1. runtime ----------
$rtDir = Join-Path $StageRoot 'runtime\python'
# 复用已验证 runtime：显式 -SourceRuntimeDir 优先；其次仅当 build/runtime-cache
# 指纹（lock 文件 + host python）匹配时才复用，否则全量下载 + 组装后写回缓存。
$rtSource = $null
if ($SourceRuntimeDir) {
    $rtSource = (Resolve-Path $SourceRuntimeDir).Path
} elseif ((Test-Path $RuntimeCacheDir) -and (& $CacheFPMatch $RuntimeCacheDir $rtFinger)) {
    $rtSource = (Resolve-Path $RuntimeCacheDir).Path
}
if ($rtSource) {
    New-Item -ItemType Directory -Path $rtDir | Out-Null
    Copy-Item (Join-Path $rtSource '*') $rtDir -Recurse -Force
    Write-Host "[assemble] 复用已缓存 runtime: $rtSource (跳过下载+pip)"
} else {
    New-Item -ItemType Directory -Path $rtDir | Out-Null
    $embedZip = Join-Path $env:TEMP 'python-embed-3119.zip'
    if (-not (Test-Path $embedZip)) {
        # 加超时，避免网络卡死时无限挂起（正常下载 10MB 内）
        Invoke-WebRequest -Uri $EmbedPythonUrl -OutFile $embedZip -TimeoutSec 120
    }
    Set-Location $rtDir
    tar -xf $embedZip
    Set-Location $OutRoot

    $packages = Join-Path $rtDir 'packages'
    New-Item -ItemType Directory -Path $packages | Out-Null
    # 去掉 -q：逐包实时输出便于定位卡点；--progress-bar off 避免刷屏；保留超时/重试防无限挂
    # --find-links 优先用 scripts/release/wheels 内预构建 wheel（vgamepad 的 sdist 在 CI 卡 Preparing metadata，改走本地 wheel）
    & $HostPython -m pip install --disable-pip-version-check --no-warn-script-location --progress-bar off --timeout 60 --retries 2 --find-links (Join-Path $RepoRoot 'scripts\release\wheels') --target $packages -r $LockFile
    if ($LASTEXITCODE -ne 0) { $errors.Add('pip install runtime deps failed (host python must be 3.11/cp311)') }

    foreach ($dll in @('vcruntime140.dll', 'vcruntime140_1.dll')) {
        $sys = Join-Path $env:WinDir ('System32\' + $dll)
        if (Test-Path $sys) { Copy-Item $sys (Join-Path $rtDir $dll) -Force }
    }
}

# 统一清理 pip 生成的 __pycache__（历史 release 口径：runtime 不带字节码）。
# 新版 pip 在 --target 下会为 host(3.11) 预编译 pyc（~75MB）；用户首次运行会自动重建，
# 打包携带只会虚增体积并破坏 size gate 基线可比性。无条件执行（含复用 SourceRuntimeDir 的分支）。
$pycAll = Get-ChildItem $rtDir -Recurse -Directory -Filter '__pycache__' -EA SilentlyContinue
if ($pycAll) {
    $n = $pycAll.Count
    $pycAll | ForEach-Object { Remove-Item $_.FullName -Recurse -Force -EA SilentlyContinue }
    Write-Host "[assemble] 清理 runtime __pycache__: $n 个目录"
}

$pthContent = "python311.zip`n.`npackages`n..\..`n# repo root on sys.path; sidecar started with -m`nimport site"
Set-Content -Path (Join-Path $rtDir 'python311._pth') -Value $pthContent -Encoding ascii

# ---------- 2. dotnet publish ----------
# 统一输出到 build/publish-cache 并记录 C#/XAML 源指纹；
# -SkipPublish 时仅当指纹一致才复用（改过源码会自动重编，杜绝跨分支误用旧产物）。
# PoC 布局：GUI publish 整目录进入 StageRoot\app\（实现目录），根目录只保留 Launcher 与资源。
$publishDir = Join-Path $RepoRoot 'build\publish-cache'
$csproj = Join-Path $RepoRoot 'apps\MaaRacingMaster.Shell\MaaRacingMaster.Shell.csproj'
if ($SkipPublish -and (& $CacheFPMatch $publishDir $pubFinger)) {
    Write-Host '[assemble] 复用已编译 GUI（-SkipPublish，指纹一致）'
} else {
    if (Test-Path $publishDir) { Remove-Item $publishDir -Recurse -Force }
    New-Item -ItemType Directory -Path $publishDir | Out-Null
    & dotnet publish $csproj -c Release -r win-x64 --self-contained true -p:Version=$Version -o $publishDir | Out-Null
    if ($LASTEXITCODE -ne 0) { $errors.Add('dotnet publish failed') }
    & $CacheFPWrite $publishDir $pubFinger
}

# 复制 GUI 产物到 StageRoot\app\
$appDir = Join-Path $StageRoot 'app'
Get-ChildItem $publishDir -Recurse -File | ForEach-Object {
    $rel = $_.FullName.Substring($publishDir.Length).TrimStart('\')
    $dest = Join-Path $appDir $rel
    New-Item -ItemType Directory -Path (Split-Path $dest) -Force | Out-Null
    Copy-Item $_.FullName $dest -Force
}

# ---------- 2.5 publish 产物瘦身（作用于 StageRoot\app\）----------
# 1) *.exe.WebView2\EBWebView 是 WebView2 运行时生成的用户缓存
#    （Cookies/History/GPUCache/CodeCache 等），仅本机运行残留，纯冗余且带隐私，
#    用户机器首次运行会自动重建，删除无副作用。
# 2) 语言包目录仅保留 zh-CN：WindowsAppSDK 每个 self-contained publish 都会把
#    全量多语言 .mui 复制进来（几十个目录占用大量小文件），本项目只面向中文。
$cleanDir = Join-Path $StageRoot 'app'
$webview2Dirs = Get-ChildItem $cleanDir -Directory -Filter '*.exe.WebView2' -Recurse -ErrorAction SilentlyContinue
foreach ($d in $webview2Dirs) {
    Remove-Item $d.FullName -Recurse -Force
    Write-Host "[assemble] 清理 WebView2 用户缓存: $($d.Name)"
}
$langDirs = Get-ChildItem $cleanDir -Directory | Where-Object {
    $_.Name -ne 'zh-CN' -and (Get-ChildItem $_.FullName -Filter '*.mui' -ErrorAction SilentlyContinue | Select-Object -First 1)
}
foreach ($d in $langDirs) {
    Remove-Item $d.FullName -Recurse -Force
    Write-Host "[assemble] 清理语言包: $($d.Name)"
}

# ---------- 2.55 实验1：删除 WindowsAppSDK AI/ML 死链（-RemoveWinAppSdkML 开启时） ----------
# 白名单依据 deps.json 中 Microsoft.WindowsAppSDK.AI/1.8.79 与 Microsoft.WindowsAppSDK.ML/1.8.2197
# 子包布局（runtimes-framework\win-x64\native\ + lib\ + metadata\）逐文件映射到 app\ 目录得到；
# MaaRM C# 源码 / MaaRacingMaster.Shell.dll IL / dumpbin 静态引用三重证据均证明整条链零使用
# （推理在 Python sidecar 与 MaaFramework 内完成）。开关默认关闭；一键恢复 = 去掉开关重新 assemble。
if ($RemoveWinAppSdkML) {
    $mlWhitelist = @(
        # --- Microsoft.WindowsAppSDK.ML/1.8.2197 runtimes-framework\win-x64\native\ ---
        'onnxruntime.dll', 'DirectML.dll', 'onnxruntime_providers_shared.dll',
        # --- Microsoft.WindowsAppSDK.ML/1.8.2197 lib\net8.0-windows...\ ---
        'Microsoft.ML.OnnxRuntime.dll',
        'Microsoft.Windows.AI.MachineLearning.dll',
        'Microsoft.Windows.AI.MachineLearning.Projection.dll',
        'Microsoft.Windows.AI.MachineLearning.winmd',
        'Microsoft.Windows.AI.Imaging.dll','Microsoft.Windows.AI.Imaging.Projection.dll','Microsoft.Windows.AI.Imaging.winmd',
        'Microsoft.Windows.AI.Text.dll','Microsoft.Windows.AI.Text.Projection.dll','Microsoft.Windows.AI.Text.winmd',
        'Microsoft.Windows.AI.AICapabilities.dll',
        'Microsoft.Windows.AI.ContentSafety.dll','Microsoft.Windows.AI.ContentSafety.Projection.dll','Microsoft.Windows.AI.ContentSafety.winmd',
        'Microsoft.Windows.AI.Foundation.Projection.dll','Microsoft.Windows.AI.Foundation.winmd','Microsoft.Windows.AI.FoundationInternal.winmd',
        'Microsoft.Windows.AI.GenerativeInternal.winmd','Microsoft.Windows.AI.ContentModerationInternal.winmd',
        'Microsoft.Windows.AI.Projection.dll','Microsoft.Windows.AI.winmd',
        'Microsoft.Windows.Vision.dll', 'Microsoft.Windows.Vision.winmd','Microsoft.Windows.VisionInternal.winmd','Microsoft.Windows.Internal.Vision.winmd',
        'Microsoft.Windows.SemanticSearch.winmd','Microsoft.Windows.PrivateCommon.winmd',
        'Microsoft.Graphics.Imaging.dll','Microsoft.Graphics.Imaging.Projection.dll','Microsoft.Graphics.Imaging.winmd',
        'Microsoft.Graphics.Internal.Imaging.winmd','Microsoft.Graphics.ImagingInternal.winmd','Microsoft.Graphics.ImagingInternal.ImageObjectRemover.winmd',
        'Microsoft.Windows.Workloads.dll','Microsoft.Windows.Workloads.winmd','Microsoft.Windows.Workloads.Resources.dll','Microsoft.Windows.Workloads.Resources_ec.dll',
        'Microsoft.Windows.Private.Workloads.SessionManager.winmd',
        'SessionHandleIPCProxyStub.dll',
        'NpuDetect\NPUDetect.dll',
        'workloads.json','workloads.365.json','workloads.j32.json','workloads.lnl.json','workloads.qnn.json','workloads.stx.json'
    )
    # Skip gracefully if a whitelisted path does not exist in this output
    foreach ($rel in $mlWhitelist) {
        $target = Join-Path $appDir $rel
        if (Test-Path $target) {
            Remove-Item $target -Recurse -Force
            Write-Host "[assemble] EXP-1 remove AI/ML dead chain: $rel"
        }
    }
}

# ---------- 2.56 EXP-2: remove WindowsAppSDK Widgets dead chain (only with -RemoveWidgets) ----------
# Whitelist = all 3 files shipped by Microsoft.WindowsAppSDK.Widgets/1.8.251231004 into app\:
#   runtimes-framework\win-x64\native\Microsoft.Windows.Widgets.dll
#   lib\net6.0-windows10.0.17763.0\Microsoft.Windows.Widgets.Projection.dll
#   metadata\Microsoft.Windows.Widgets.winmd
# MaaRM C# source / XAML / MaaRacingMaster.Shell.dll use none of WidgetManager/FeedManager/WidgetProvider
# (zero reference verified). MaaRacingMaster.Shell.exe's embedded activation manifest still declares these
# WinRT classes, but that manifest is only a per-request lookup table (same proven-safe pattern as
# EXP-1 AI/ML): MaaRM never requests a Windows.Widgets.* type, so the vanished DLL is never resolved.
# Default OFF; restore = reassemble w/o switch.
if ($RemoveWidgets) {
    $widgetsWhitelist = @(
        'Microsoft.Windows.Widgets.dll',           # native, 2.293 MB
        'Microsoft.Windows.Widgets.Projection.dll',# managed projection, 0.160 MB
        'Microsoft.Windows.Widgets.winmd'          # winmd metadata, 0.035 MB
    )
    foreach ($rel in $widgetsWhitelist) {
        $target = Join-Path $appDir $rel
        if (Test-Path $target) {
            Remove-Item $target -Recurse -Force
            Write-Host "[assemble] EXP-2 remove Widgets dead chain: $rel"
        }
    }
}

# ---------- 2.57 EXP-3: remove Python ORT capi\onnxruntime.dll (only with -RemovePythonOrtCapi) ----------
# Target: packages\onnxruntime\capi\onnxruntime.dll (20.1MB).
# Evidence: onnxruntime_pybind11_state.pyd static import table has NO onnxruntime.dll dep;
# pyd bundles the full ORT engine. capi\onnxruntime.dll never appears in the process module
# list across import + DmlExecutionProvider + real inference + RapidOCR (verified twice).
# onnxruntime\__init__.py CDLL() calls are CUDA/cuDNN-only (nvidia DLLs), not onnxruntime.dll.
# Everything else in capi\ (pyd/DirectML.dll/providers_shared.dll/__init__.py) is preserved.
# Default OFF; restore = reassemble w/o switch.
if ($RemovePythonOrtCapi) {
    $capiortdll = Join-Path $rtDir 'packages\onnxruntime\capi\onnxruntime.dll'
    if (Test-Path $capiortdll) {
        Remove-Item $capiortdll -Recurse -Force
        Write-Host "[assemble] EXP-3 remove Python ORT capi\onnxruntime.dll"
    }
}

# ---------- 2.58 EXP-4A: remove Pillow _avif native ext (only with -RemovePilAvif) ----------
# Target: packages\PIL\_avif.cp311-win_amd64.pyd (7.5MB).
# Evidence: PIL.Image.open lazy-loads AvifImagePlugin only for ".avif"/".avifs" extensions;
# import PIL.Image does NOT load _avif (importtime + sys.modules verified). Project has zero
# Pillow usage and only png/jpg assets; rapidocr uses Pillow for OCR inputs (frames/memory, no
# avif path in current release behavior). PNG/JPEG handled by Pillow built-in codecs, unaffected.
# Everything else in PIL\ preserved. Default OFF; restore = reassemble w/o switch.
if ($RemovePilAvif) {
    $avifPyd = Join-Path $rtDir 'packages\PIL\_avif.cp311-win_amd64.pyd'
    if (Test-Path $avifPyd) {
        Remove-Item $avifPyd -Recurse -Force
        Write-Host "[assemble] EXP-4A remove Pillow _avif native ext"
    }
}

# ---------- 2.59 EXP-4B: remove .NET crash/diagnostics on-demand components (only with -RemoveCrashDiagnostics) ---
# Removes: createdump.exe, mscordaccore.dll, mscordaccore_amd64_*.dll, Microsoft.DiaSymReader.Native.amd64.dll
# (all from runtimepack.Microsoft.NETCore.App.Runtime.win-x64/8.0.30 native section; self-contained copy into app\).
# These are on-demand diagnostics: a normal .NET process loads only coreclr/clrjit/hostfxr/hostpolicy; mscordaccore(DAC),
# mscordbi(DBI), createdump(exe), DiaSymReader(symbol) stay unloaded even on a thrown exception (verified via a temp
# no-elevation net8 self-contained probe). Removing them loses crash-dump / SOS / debugger-symbol capability but not
# normal operation. mscordbi.dll is KEPT. Default OFF; restore = reassemble w/o switch.
if ($RemoveCrashDiagnostics) {
    foreach ($rel in @('createdump.exe','mscordaccore.dll','Microsoft.DiaSymReader.Native.amd64.dll')) {
        $t = Join-Path $appDir $rel
        if (Test-Path $t) { Remove-Item $t -Force; Write-Host "[assemble] EXP-4B remove crash/diag: $rel" }
    }
    Get-ChildItem $appDir -File -Filter 'mscordaccore_amd64_*.dll' -EA SilentlyContinue | ForEach-Object {
        Remove-Item $_.FullName -Force
        Write-Host "[assemble] EXP-4B remove crash/diag: $($_.Name)"
    }
}

# ---------- 2.60 EXP-5A: remove numpy dev/test/build-only dirs (only with -RemoveNumpyDev) ----------
# Whitelist (strict; total 2.55MB / 366 files at exp4b), all proven lazy/never-loaded on a normal
# import numpy / cv2 / onnxruntime (numpy.__getattr__ lazy submodules or pure testing/docs dirs).
#   f2py, distutils  -> Fortran/C build/compile tools; numpy.DEPRECATED lazy, no runtime import chain.
#   testing, tests   -> pytest harness; numpy.testing only via np.testing (lazy).
#   doc              -> package docs.
#   _pyinstaller     -> PyInstaller hook helpers.
#   ctypeslib        -> numpy.ctypeslib, lazy submodule, not used by MaaRM/RapidOCR/ORT.
# STRICT EXCLUSION (never removed): numpy\_pytesttester.py (top-level hard import in numpy/__init__.py,
#   `from numpy._pytesttester import PytestTester`; only 6KB, do NOT patch numpy source),
#   numpy\typing + numpy\_typing (reserved for EXP-5B .pyi study), numpy\core (compat shim),
#   numpy\_core\lib\random\linalg\fft\polynomial\matrixlib (runtime-required).
# No numpy source is forked/patched. Default OFF; restore = reassemble w/o switch.
if ($RemoveNumpyDev) {
    $numpyDevDirs = @('f2py','distutils','testing','tests','doc','_pyinstaller','ctypeslib')
    foreach ($d in $numpyDevDirs) {
        $t = Join-Path $rtDir "packages\numpy\$d"
        if (Test-Path $t) {
            $sz = (Get-ChildItem $t -Recurse -File -EA SilentlyContinue | Measure-Object Length -Sum).Sum
            Remove-Item $t -Recurse -Force
            Write-Host ("[assemble] EXP-5A remove numpy\{0}  ({1:N2} MB)" -f $d, ($sz/1MB))
        }
    }
    # guard: _pytesttester.py hard-imported by numpy __init__; must remain
    $guard = Join-Path $rtDir 'packages\numpy\_pytesttester.py'
    if (-not (Test-Path $guard)) {
        & $Fail 'EXP-5A guard breached: numpy\_pytesttester.py must be kept (top-level import). Aborting.'
    }
}

# ---------- 2.61 EXP-5B-1: remove all .pyi typing stubs (only with -RemovePythonTypingStubs) ----------
# Removes runtime\python\packages\**\*.pyi (267 files / 1.96MB at exp5a). All proven static typing-only:
#  - numpy.typing (110 .pyi) is a lazy deprecation shim; `import numpy` does NOT load it (verified sys.modules).
#  - numpy._typing IS runtime-loaded, but its _ufunc/_nbit_base resolve to their .py (verified __file__ = .py),
#    so the sibling _ufunc.pyi/_nbit_base.pyi are pure stubs with the .py preserved.
#  - Byte-scan: only 4 True literal ".pyi" hits in .py; three are comments/docstrings; the real path-readers
#    (numpy\typing\tests\test_isfile.py / test_typing.py) live in numpy.typing.tests, never imported at runtime;
#    no open()/importlib.resources/pkgutil/importlib.metadata read of .pyi anywhere on the normal load path.
# .py / .pyc / .pyd / .dist-info are NOT touched. numpy.typing & numpy._typing dirs retained (only .pyi inside removed).
# Verify this .pyi-specific removal is safe at runtime. Default OFF; restore = reassemble w/o switch.
if ($RemovePythonTypingStubs) {
    $pyis = Get-ChildItem (Join-Path $rtDir 'packages') -Recurse -File -Filter '*.pyi' -EA SilentlyContinue
    $n = $pyis.Count; $sz = ($pyis | Measure-Object Length -Sum).Sum
    foreach ($p in $pyis) { Remove-Item $p.FullName -Force }
    Write-Host ("[assemble] EXP-5B-1 remove {0} .pyi typing stubs  ({1:N2} MB)" -f $n, ($sz/1MB))
}

# ---------- 2.62 EXP-5C-1: remove INSTALLER/WHEEL/REQUESTED from dist-info (only with -RemoveDistInfoInstallMetadata) --
# Install/dev-stage only files; no runtime reader found in MaaRM sidecar (zero importlib.metadata/pkg_resources) nor on
# the normal import path of numpy/cv2/onnxruntime/rapidocr/maafw (onnxruntime's metadata calls are lazy function-internal,
# and its packaged dist-info is onnxruntime_directml-*.dist-info so importlib.metadata.version('onnxruntime') throws
# PackageNotFoundError EVEN BEFORE any removal — proving it is not load-path-critical). METADATA / RECORD / entry_points.txt /
# top_level.txt are NOT touched in this experiment (reserved 5C-2/5C-4). Default OFF; restore = reassemble w/o switch.
if ($RemoveDistInfoInstallMetadata) {
    $cnt = 0; $sz = 0L
    $pkgs = Join-Path $rtDir 'packages'
    foreach ($di in Get-ChildItem $pkgs -Directory -Filter '*.dist-info' -EA SilentlyContinue) {
        foreach ($fn in @('INSTALLER','WHEEL','REQUESTED')) {
            $p = Join-Path $di.FullName $fn
            if (Test-Path $p) { $sz += (Get-Item $p).Length; Remove-Item $p -Force; $cnt++ }
        }
    }
    Write-Host ("[assemble] EXP-5C-1 remove {0} install-meta files  ({1:N2} KB)" -f $cnt, ($sz/1KB))
}

# ---------- 2.63 EXP-5E-1: remove packages\bin\*.exe console wrappers (only with -RemovePythonConsoleDev) ----------
# 8 dev/test/CLI console launchers (~0.83MB), all pure pip console_scripts wrappers (105.8KB each, distinct hashes =
# distinct embedded entry points). No native runtime/DLL. Zero subprocess/Popen runtime call found from MaaRM sidecar
# (only git/taskkill) or from third-party runtime load path (numpy/conftest.py is pytest-only, sympy/autowrap.py is lazy
# Fortran codegen, tqdm/std.py match is the class name not a launch). f2py.exe is an orphan (numpy.f2py.f2py2e removed in
# EXP-5A). Deletion only removes console CLI access; library imports (RapidOCR class, tqdm, charset_normalizer) unaffected.
# Default OFF; restore = reassemble w/o switch.
if ($RemovePythonConsoleDev) {
    $binDir = Join-Path $rtDir 'packages\bin'
    if (Test-Path $binDir) {
        $exes = Get-ChildItem $binDir -File -Filter '*.exe' -EA SilentlyContinue
        $sz = ($exes | Measure-Object Length -Sum).Sum; $n = $exes.Count
        foreach ($e in $exes) { Remove-Item $e.FullName -Force }
        Write-Host("[assemble] EXP-5E-1 remove {0} console exe  ({1:N2} KB)" -f $n, ($sz / 1KB))
    } else {
        Write-Host '[assemble] EXP-5E-1: no packages\bin dir to clean'
    }
}

# ---------- 2.64 EXP-6: remove sympy (only with -RemoveSympy) ----------
# 25.37MB. Verified never loaded on any MaaRM run path (full-chain trace = 360 modules) nor after onnxruntime DML
# inference. Its only dependency is onnxruntime's offline symbolic shape-infer/transformers tooling, not the runtime
# inference/DML path. MaaRM has zero sympy import. mpmath is KEPT (separate EXP-7). Default OFF; restore = reassemble.
if ($RemoveSympy) {
    $t = Join-Path $rtDir 'packages\sympy'
    if (Test-Path $t) {
        $sz = (Get-ChildItem $t -Recurse -File -EA SilentlyContinue | Measure-Object Length -Sum).Sum
        Remove-Item $t -Recurse -Force
        # also drop sympy dist-info to keep metadata consistent with an absent package
        $di = Join-Path $rtDir 'packages\sympy-1.14.0.dist-info'
        if (Test-Path $di) { Remove-Item $di -Recurse -Force }
        Write-Host ("[assemble] EXP-6 remove sympy  ({0:N2} MB)" -f ($sz / 1MB))
    } else {
        Write-Host "[assemble] EXP-6: sympy absent, skip"
    }
}

# ---------- 2.65 EXP-7: remove MaaAgentBinary (only with -RemoveMaaAgentBinary) ----------
# Android/ADB agent binaries (12.53MB). Referenced by maa\controller.py AdbController only; MaaRM uses Win32Controller.
# Verified no runtime import loads it. Also drop its dist-info. Default OFF; restore = reassemble w/o switch.
if ($RemoveMaaAgentBinary) {
    $t = Join-Path $rtDir 'packages\MaaAgentBinary'
    if (Test-Path $t) {
        $sz = (Get-ChildItem $t -Recurse -File -EA SilentlyContinue | Measure-Object Length -Sum).Sum
        Remove-Item $t -Recurse -Force
        $di = Join-Path $rtDir 'packages\MaaAgentBinary-1.0.1.dist-info'
        if (Test-Path $di) { Remove-Item $di -Recurse -Force }
        Write-Host ("[assemble] EXP-7 remove MaaAgentBinary  ({0:N2} MB)" -f ($sz / 1MB))
    } else {
        Write-Host '[assemble] EXP-7: MaaAgentBinary absent, skip'
    }
}

# ---------- 2.66 EXP-8: remove ORT offline toolchain deps (only with -RemoveOrtOffline) ----------
# google\protobuf (1.57MB) + flatbuffers (0.08MB) — pure-python deps of onnxruntime\quantization + tools (cold paths).
# Runtime closure probe (import onnxruntime + RapidOCR 构造+推理, providers=[Dml,CPU]) confirms BOTH NOT loaded into
# sys.modules; C#/python runtime inference never touches them. onnxruntime's protobuf/flatbuffers are C++-embedded.
# dist-info 保留（最小侵入，与 policy "RECORD 不删" 先例一致）。Default OFF; restore = reassemble w/o switch.
if ($RemoveOrtOffline) {
    $targets = @(
        (Join-Path $rtDir 'packages\google\protobuf'),
        (Join-Path $rtDir 'packages\flatbuffers')
    )
    foreach ($t in $targets) {
        if (Test-Path $t) {
            $sz = (Get-ChildItem $t -Recurse -File -EA SilentlyContinue | Measure-Object Length -Sum).Sum
            Remove-Item $t -Recurse -Force
            Write-Host ("[assemble] EXP-8 remove {0}  ({1:N2} MB)" -f (Split-Path $t -Leaf), ($sz / 1MB))
        } else {
            Write-Host "[assemble] EXP-8: $(Split-Path $t -Leaf) absent, skip"
        }
    }
}

# ---------- 2.67 EXP-9: remove cv2 videoio ffmpeg backend (only with -RemoveCvFfmpeg) ----------
# opencv_videoio_ffmpeg500_64.dll (29.45MB) in packages\cv2. dumpbin /dependents on cv2.pyd shows NO static dependency
# (ffmpeg dll is a runtime LoadLibrary'd videoio backend). Stage-2 probe (rm dll on stage): import cv2 + imencode/imdecode
# + cvtColor/resize/matchTemplate/dnn.NMSBoxes + chinese-path(utf8_patch imencode->bytes->imdecode chain) all PASS;
# videoio(VideoWriter MJPG/XVID) unavailable but MaaRM production has ZERO highgui/videoio usage. Any video-backend failure
# warns to STDERR only; STDOUT(JSONL) stays clean. Default OFF; restore = reassemble w/o switch.
if ($RemoveCvFfmpeg) {
    $t = Join-Path $rtDir 'packages\cv2\opencv_videoio_ffmpeg500_64.dll'
    if (Test-Path $t) {
        $sz = (Get-Item $t).Length
        Remove-Item $t -Force
        Write-Host ("[assemble] EXP-9 remove opencv_videoio_ffmpeg500_64.dll  ({0:N2} MB)" -f ($sz / 1MB))
    } else {
        Write-Host '[assemble] EXP-9: ffmpeg dll absent, skip'
    }
}

# ---------- 2.6 native Launcher 编译 ----------
# 根目录唯一入口 MaaRacingMaster.exe（薄 Launcher，见 apps/MaaRacingMaster.Launcher/launcher.c）。
# 用 MSVC 编译为静态链接（/MT）GUI 子系统 exe，零外部 runtime 依赖；每次重新编译（体积小、快），不缓存。
# 现在通过 launcher.rc 一次性嵌入两类 Win32 资源：图标（resources/icon）与内嵌申请清单
# （launcher.manifest 的 requireAdministrator 提权）。作用：① 入口 exe 有图标；
# ② Launcher 启动即申请管理员权限，CreateProcessW 启动 MaaRacingMaster.Shell.exe 时子进程继承
# 同一 token，规避 ERROR_ELEVATION_REQUIRED (740)。
# 优先直接用 PATH 里的 cl/rc（CI 用 setup-msvc 已配置环境）；否则自动探测 vcvarsall.bat 初始化。
$launcherSrc = Join-Path $RepoRoot 'apps\MaaRacingMaster.Launcher\launcher.c'
$launcherOut = Join-Path $StageRoot 'MaaRacingMaster.exe'
$launcherObj = Join-Path $env:TEMP 'MaaRacingMaster.Launcher.obj'
# rc 内部的相对资源路径（..\..\assets\icon.ico、launcher.manifest）是按编译时的工作目录
# （而非 .rc 文件所在目录）解析的，因此编译前必须把 cwd 切到 apps\MaaRacingMaster.Launcher 并保持到结束。
# res 以相对名下发当前目录生成：rc.exe 对带引号的 /fo 及环境变量路径存在 RC1109 坑，
# 无引号相对名最稳；编译后立即删除该临时 res，避免污染源码目录。
$launcherDir = Join-Path $RepoRoot 'apps\MaaRacingMaster.Launcher'
$launcherRes = Join-Path $launcherDir 'MaaRacingMaster.Launcher.res'
Push-Location $launcherDir
try {
    $clInPath = (Get-Command cl.exe -ErrorAction SilentlyContinue)
    if ($clInPath -and (Test-Path -Path $launcherSrc)) {
        Write-Host "[assemble] 用 PATH 中的 cl.exe/rc.exe 编译 Launcher"
        & rc.exe /nologo /foMaaRacingMaster.Launcher.res launcher.rc
        if ($LASTEXITCODE -ne 0) { $errors.Add('Launcher 资源编译失败 (rc.exe 退出码 ' + $LASTEXITCODE + ')') }
        & cl.exe /nologo /utf-8 /O2 /MT /Fe:"$launcherOut" /Fo:"$launcherObj" launcher.c MaaRacingMaster.Launcher.res /link /SUBSYSTEM:WINDOWS user32.lib shell32.lib | Out-Null
        Remove-Item $launcherRes -Force -ErrorAction SilentlyContinue
        if ($LASTEXITCODE -ne 0) { $errors.Add('Launcher 编译失败 (cl.exe 退出码 ' + $LASTEXITCODE + ')') }
    } elseif (-not $VcVarsAll) {
        $errors.Add('未找到 MSVC (cl.exe 不在 PATH，也无 vcvarsall.bat)；无法编译 native Launcher。用 -VcVarsAll 指定路径，或 CI 先 setup-msvc')
    } elseif (-not (Test-Path -Path $launcherSrc)) {
        $errors.Add('missing launcher source: ' + $launcherSrc)
    } else {
        Write-Host "[assemble] 用 vcvarsall 编译 Launcher: $(Split-Path $launcherOut -Leaf)"
        # cmd /c 内 call vcvarsall 一次性生效（PowerShell 无法直接继承批处理环境）
        $cmdLine = "`"$VcVarsAll`" x64 >nul 2>&1 && rc.exe /nologo /foMaaRacingMaster.Launcher.res launcher.rc && cl.exe /nologo /utf-8 /O2 /MT /Fe:`"$launcherOut`" /Fo:`"$launcherObj`" launcher.c MaaRacingMaster.Launcher.res /link /SUBSYSTEM:WINDOWS user32.lib shell32.lib & del /q /s MaaRacingMaster.Launcher.res >nul 2>&1"
        cmd /c $cmdLine | Out-Null
        if ($LASTEXITCODE -ne 0) {
            $errors.Add('Launcher 编译失败 (cl.exe 退出码 ' + $LASTEXITCODE + ')')
        }
    }
} finally {
    Pop-Location
}
if (-not (Test-Path $launcherOut)) {
    $errors.Add('Launcher 编译后未生成: ' + $launcherOut)
} else {
    Write-Host "[assemble] Launcher OK: $launcherOut"
}
# Launcher 前置校验：app\MaaRacingMaster.Shell.exe 必须存在（Launcher 依赖它启动）
if (-not (Test-Path (Join-Path $appDir 'MaaRacingMaster.Shell.exe'))) {
    $errors.Add('app\MaaRacingMaster.Shell.exe not found — Launcher 无法启动 GUI')
}

# ---------- 3. whitelist ----------
# 模型已随插件自包含（maaracing_master/plugins/*/resources/，经下方 robocopy 整包带出），
# assets/ 仅保留应用级资产（图标/演示图/框架配置）。
foreach ($rel in @(
    'pyproject.toml', 'LICENSE', 'THIRD_PARTY_LICENSES.md',
    'assets\icon.ico',
    'assets\mra_icon.png', 'assets\config', 'apps\MaaRacingMaster.Shell\frontend')) {
    $src = Join-Path $RepoRoot $rel
    if (Test-Path $src) {
        $dest = Join-Path $StageRoot $rel
        New-Item -ItemType Directory -Path (Split-Path $dest) -Force | Out-Null
        Copy-Item $src $dest -Recurse -Force
    } else { $errors.Add('missing source path: ' + $rel) }
}
robocopy (Join-Path $RepoRoot 'maaracing_master') (Join-Path $StageRoot 'maaracing_master') /E /XD __pycache__ /XJ /NFL /NDL /NJH /NJS /NP | Out-Null

# ---------- 4. _version.py ----------
$verContent = @(
    '# file generated for release (no VCS)',
    'from __future__ import annotations',
    ('__version__ = version = "{0}"' -f $Version)
)
Set-Content -Path (Join-Path $StageRoot 'maaracing_master\_version.py') -Value $verContent -Encoding ascii

# ---------- 5. import self-check ----------
# vgamepad 单独容错：import 即建 ViGEmBus VBus()，无驱动环境（CI runner）会抛
# VIGEM_ERROR_BUS_NOT_FOUND。这证明 wheel 已正确装载，只是缺系统驱动，属于运行
# 环境问题而非打包问题（用户机器装了 ViGEmBus 驱动后即可正常使用），降级为 warning。
$py = Join-Path $rtDir 'python.exe'
$checkCode = @'
import sys
sys.path.insert(0, sys.argv[1])
for m in ('maa', 'onnxruntime', 'cv2', 'numpy', 'rapidocr', 'windows_capture'):
    __import__(m)
import maaracing_master
print(maaracing_master.__version__)
try:
    __import__('vgamepad')
except Exception as e:
    if 'VIGEM_ERROR_BUS_NOT_FOUND' in str(e):
        print('[warn] vgamepad installed but ViGEmBus driver missing; virtual gamepad unavailable (runtime env limit)')
    else:
        raise
'@
& $py -c $checkCode $StageRoot
if ($LASTEXITCODE -ne 0) { $errors.Add('import self-check failed') }

# 自检通过且本次为全新构建（未复用缓存/源码 runtime）→ 写回 build/runtime-cache（含指纹），供下次指纹匹配时复用
if ($LASTEXITCODE -eq 0 -and -not $rtSource) {
    New-Item -ItemType Directory -Path (Split-Path $RuntimeCacheDir -Parent) -Force | Out-Null
    if (Test-Path $RuntimeCacheDir) { Remove-Item $RuntimeCacheDir -Recurse -Force }
    Copy-Item $rtDir $RuntimeCacheDir -Recurse -Force
    & $CacheFPWrite $RuntimeCacheDir $rtFinger
    Write-Host "[assemble] runtime 已写入缓存: $RuntimeCacheDir"
}

# ---------- 5.5 Production pruning verification (Release only) ----------
# 1) Guard：敏感删除项在 Release 下必须已消失；若仍存在说明白名单失效 -> error。
# 2) 反向验证：确认"应被删"的不存在、"应保留"的仍存在，防误删。
if ($Configuration -eq 'Release' -and -not $DisableReleaseOptimizations) {
    # 应已被删除（缺失是正确结果）
    $AbsentChecks = @(
        'runtime\python\packages\sympy',
        'runtime\python\packages\MaaAgentBinary',
        'runtime\python\packages\onnxruntime\capi\onnxruntime.dll',
        'runtime\python\packages\PIL\_avif.cp311-win_amd64.pyd',
        'runtime\python\packages\numpy\f2py',
        'runtime\python\packages\numpy\distutils',
        'app\createdump.exe', 'app\mscordaccore.dll',
        'app\Microsoft.DiaSymReader.Native.amd64.dll',
        'app\Microsoft.Windows.Widgets.dll',
        'runtime\python\packages\google\protobuf',
        'runtime\python\packages\flatbuffers',
        'runtime\python\packages\cv2\opencv_videoio_ffmpeg500_64.dll'
    )
    foreach ($rel in $AbsentChecks) {
        $p = Join-Path $StageRoot $rel
        if (Test-Path $p) { $errors.Add("PRUNING-FAIL: '$rel' should be removed in Release but exists (whitelist stale?)") }
    }
    # 应保留（缺失是误删）
    $PresentChecks = @(
        'runtime\python\python.exe', 'runtime\python\pythonw.exe',
        'runtime\python\packages\maa',
        'runtime\python\packages\rapidocr\models',
        'MaaRacingMaster.exe'
    )
    foreach ($rel in $PresentChecks) {
        if (-not (Test-Path (Join-Path $StageRoot $rel))) {
            $errors.Add("PRUNING-REGRESSION: required '$rel' missing")
        }
    }
    # rapidocr 模型完整性守卫（B4′-0 教训）：仅查目录存在会把"空 models/"放行，
    # 而 rapidocr 3.9.2 构造即加载 det+cls+rec，任一缺失/空 → OCR 静默失效。
    # 用 Test-Path（非 -PathType）兼容目录亦可，三模型须存在且 > 0 字节。
    foreach ($onnx in @(
        'runtime\python\packages\rapidocr\models\PP-OCRv6_det_small.onnx',
        'runtime\python\packages\rapidocr\models\ch_ppocr_mobile_v2.0_cls_mobile.onnx',
        'runtime\python\packages\rapidocr\models\PP-OCRv6_rec_small.onnx')) {
        $mp = Join-Path $StageRoot $onnx
        if (-not (Test-Path $mp)) {
            $errors.Add("PRUNING-REGRESSION: required OCR model missing: $onnx")
        } elseif ((Get-Item $mp).Length -eq 0) {
            $errors.Add("PRUNING-REGRESSION: OCR model is 0 bytes (truncated): $onnx")
        }
    }
    # mscordbi.dll 必须在（app\，debugger attach 能力保留）
    if (-not (Test-Path (Join-Path $StageRoot 'app\mscordbi.dll'))) {
        $errors.Add("PRUNING-REGRESSION: app\mscordbi.dll missing (debugger attach must be kept)")
    }

    # ---------- 5.6 开发工具链不得入包（反向守卫）----------
    # 白名单只 robocopy maaracing_master/，tools\（NavKit 校准台：HTTP server + 前端
    # 构建产物，开发树 ~180MB）、tests\、docs\ 本就不该出现。此处反向断言：白名单一旦被
    # 放宽、或有人把开发工具挪进源码包，构建当场失败，而不是静默发给用户。
    # 注意区分：maaracing_master\core\navkit\ 是运行期数据面 loader + 决策引擎，必须随包。
    foreach ($rel in @('tools', 'tests', 'docs', '.github', 'scripts')) {
        if (Test-Path (Join-Path $StageRoot $rel)) {
            $errors.Add("DEV-TREE-LEAK: '$rel' must not be shipped")
        }
    }
    Get-ChildItem $StageRoot -Recurse -Directory -Filter 'navkit' -EA SilentlyContinue |
        Where-Object { $_.FullName -notmatch '\\core\\navkit$' } |
        ForEach-Object {
            $errors.Add('DEV-TREE-LEAK: 校准台目录入包 -> ' +
                $_.FullName.Substring($StageRoot.Length).TrimStart('\'))
        }
    $consoleMarkers = @('mpe.cmd', 'policy_server.py', 'check_truth.py')
    Get-ChildItem $StageRoot -Recurse -File -EA SilentlyContinue |
        Where-Object { $consoleMarkers -contains $_.Name } |
        ForEach-Object {
            $errors.Add('DEV-TREE-LEAK: 校准台脚本入包 -> ' +
                $_.FullName.Substring($StageRoot.Length).TrimStart('\'))
        }
}

# ---------- 6. errors ----------
if ($errors.Count -gt 0) {
    Write-Host ("[assemble] {0} error(s):" -f $errors.Count) -ForegroundColor Red
    foreach ($e in $errors) { Write-Host ('  - ' + $e) -ForegroundColor Red }
    if (-not $KeepGoing) { exit 1 }
}

# ---------- 7. zip + sha256 ----------
$zip = Join-Path $OutRoot ($Name + '.zip')
if (Test-Path $zip) { Remove-Item $zip -Force }
# 只用 $Name 目录的内容作 zip 顶层（而非再包一层 $Name 目录），
# 避免用户"解压到文件名文件夹"时目录多做一层嵌套。解压后 MaaRacingMaster.exe 直接在解压根。
& tar -a -cf $zip -C (Join-Path $OutRoot $Name) .
if ($LASTEXITCODE -ne 0) { exit 1 }
$hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
$shaLine = $hash + '  ' + (Split-Path $zip -Leaf)
Set-Content -Path ($zip + '.sha256') -Value $shaLine -Encoding ascii
Write-Host ("[assemble] done: {0}  {1}" -f $zip, $hash) -ForegroundColor Green

# ---------- 7.5 7z 主推产物（-SevenZ 时；参数与 C1-C solid256M 基准一致） ----------
# 7z 为极限压缩主推档；zip 保底必需（§7 恒产出）。顶层结构与 zip 一致（仅打包 $StageRoot 内容）。
$sevenzFile = Join-Path $OutRoot ($Name + '.7z')
if ($SevenZ) {
    if (Test-Path $SevenZPath) {
        if (Test-Path $sevenzFile) { Remove-Item $sevenzFile -Force }
        if (Test-Path ($sevenzFile + '.sha256')) { Remove-Item ($sevenzFile + '.sha256') -Force }
        # 打包 $StageRoot 内容为顶层：Set-Location 到 stage 后用 7za 打包 "."。
        # 不把 $StageRoot\* 通配符传给原生 exe（PowerShell 会对 * 做通配扩展成多参数 → 触发
        # "Cannot convert String to SwitchParameter"）。2>&1|Out-Null 隔离 7za stdout/stderr，
        # 避免在 $ErrorActionPreference='Stop' 下原生命令的输出被当成 ErrorRecord 中断。
        # §8 全程用绝对路径，不依赖 cwd，此处 Set-Location 安全。
        $prev = Get-Location
        Set-Location $StageRoot
        try { & $SevenZPath 'a' '-t7z' '-mx=9' '-m0=LZMA2:d=256m' '-ms=on' $sevenzFile '.' 2>&1 | Out-Null }
        finally { Set-Location $prev }
        if ($LASTEXITCODE -ne 0) { $errors.Add("7z 压缩失败 (7za exit $LASTEXITCODE)") } else {
            $hash7 = (Get-FileHash $sevenzFile -Algorithm SHA256).Hash.ToLower()
            Set-Content -Path ($sevenzFile + '.sha256') -Value ($hash7 + '  ' + (Split-Path $sevenzFile -Leaf)) -Encoding ascii
            Write-Host ("[assemble] 7z: {0}  {1}" -f $sevenzFile, $hash7) -ForegroundColor Green
        }
    } else {
        Write-Host "[assemble] WARN: -SevenZ 已指定但找不到 7za.exe -> 降级仅出 zip（不阻断）" -ForegroundColor Yellow
    }
}

# ---------- 8. Release Size Gate + report (Release only) ----------
# 与已验收 baseline (0.20.0: total 468.86 / zip 198.79) 对比，涨超 5MB 判 SIZE REGRESSION。
if ($Configuration -eq 'Release' -and -not $DisableReleaseOptimizations) {
    $g_runtime = (Get-ChildItem (Join-Path $StageRoot 'runtime') -Recurse -File -EA SilentlyContinue | Measure-Object Length -Sum).Sum
    $g_app = (Get-ChildItem (Join-Path $StageRoot 'app') -Recurse -File -EA SilentlyContinue | Measure-Object Length -Sum).Sum
    $g_sidecar = (Get-ChildItem (Join-Path $StageRoot 'maaracing_master') -Recurse -File -EA SilentlyContinue | Measure-Object Length -Sum).Sum
    $g_other = (Get-ChildItem $StageRoot -File -EA SilentlyContinue | Measure-Object Length -Sum).Sum +
               (Get-ChildItem $StageRoot -Directory | Where-Object { $_.Name -notin @('runtime','app','maaracing_master') } |
                ForEach-Object { (Get-ChildItem $_.FullName -Recurse -File -EA SilentlyContinue | Measure-Object Length -Sum).Sum } | Measure -Sum).Sum
    $g_total = $g_runtime + $g_app + $g_sidecar + $g_other
    $g_zip = (Get-Item $zip).Length
    # 7z 主推档体积（存在才记录；zip 仍为 baseline 主判据，7z 仅记录建新基线，不纳入本次 regression）
    if (Test-Path $sevenzFile) { $g_7z = (Get-Item $sevenzFile).Length } else { $g_7z = $null }
    # 7z 基线冷启动：本期（首个产出 7z 的版本）baseline_7z_mb = null，故 delta_7z_mb 恒 null，
    # 只记录实测 sevenz 值；下轮把本期实测值回填为 baseline 后，delta 才开始参与观察（仍不进 regression）。
    $baseline7z = $null
    $d7z = if ($baseline7z -ne $null -and $g_7z) { (($g_7z - $baseline7z) / 1MB) } else { $null }
    $dTotal = ($g_total - 468.86MB) / 1MB
    $dZip = ($g_zip - 198.79MB) / 1MB
    $sizes = [ordered]@{
        runtime = [math]::Round($g_runtime/1MB,2); app = [math]::Round($g_app/1MB,2)
        sidecar = [math]::Round($g_sidecar/1MB,2); other = [math]::Round($g_other/1MB,2)
        total_unpacked = [math]::Round($g_total/1MB,2); zip = [math]::Round($g_zip/1MB,2)
    }
    if ($g_7z) { $sizes['sevenz'] = [math]::Round($g_7z/1MB,2) }
    # 仅"正向变大"判 SIZE REGRESSION：delta<0 是新增裁剪的收益，不是回归。
    # 旧写法 abs()>5 会把 exp8/exp9 这类缩小 30+MB 的合法收益误判为 regression，属 bug。
    $regression = ($dTotal -gt 5) -or ($dZip -gt 5)
    $gitShort = (git rev-parse --short HEAD 2>$null) -join ''
    $reportData = [ordered]@{
        release_version = $Version
        configuration = $Configuration
        git_commit = $gitShort
        sizes = $sizes
        baseline_total_mb = 468.86; baseline_zip_mb = 198.79
        baseline_7z_mb = $baseline7z
        delta_7z_mb = if ($d7z -ne $null) { [math]::Round($d7z,2) } else { $null }
        delta_total_mb = [math]::Round($dTotal,2); delta_zip_mb = [math]::Round($dZip,2)
        size_regression = $regression
        generated = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ssZ')
    }
    $ReportPath = Join-Path $OutRoot 'release-size-report.md'
    $ReportJson = Join-Path $OutRoot 'release-size-report.json'
    Set-Content -Path $ReportJson -Value ($reportData | ConvertTo-Json -Depth 4) -Encoding utf8
    $md = @"
# Release Size Report

- version: $Version
- generated: $($reportData.generated)
- configuration: Release (all SAFE pruning on)
- git: $($reportData.git_commit)

## 1. 最终体积
| part | MB |
|---|---|
| runtime | $($sizes.runtime) |
| app | $($sizes.app) |
| sidecar | $($sizes.sidecar) |
| other | $($sizes.other) |
| total unpacked | $($sizes.total_unpacked) |
| zip | $($sizes.zip) |
| 7z | $(if($sizes.Contains('sevenz')){$sizes.sevenz}else{'-'}) |

## 2. vs baseline (0.20.0)
| | baseline | release | delta |
|---|---|---|---|
| total | 468.86 | $($sizes.total_unpacked) | $([math]::Round($dTotal,2)) |
| zip | 198.79 | $($sizes.zip) | $([math]::Round($dZip,2)) |
| 7z（新基线，仅记录） | - | $(if($sizes.Contains('sevenz')){$sizes.sevenz}else{'-'}) | $(if($g_7z){[math]::Round($d7z,2)}else{'-'}) |

## 3. Regression
$(if($regression){'SIZE REGRESSION (total/zip 较 baseline 涨超 5MB)'}else{'no regression'})
"@
    Set-Content -Path $ReportPath -Value $md -Encoding utf8
    $gateLine = "[assemble] Size gate: total=$($sizes.total_unpacked)MB zip=$($sizes.zip)MB (baseline 468.86/198.79)"
    if ($g_7z) { $gateLine += " 7z=$($sizes.sevenz)MB" }
    Write-Host $gateLine
}