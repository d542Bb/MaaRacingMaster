# P2b 真机冒烟启动：NAVKIT_SOURCE=v4 注入后启动 mra_shell GUI
# 用法：在仓库根目录执行  .\tools\experiments\v4-p2b-smoke\run_v4_smoke.ps1
$ErrorActionPreference = "Stop"
Set-Location "$PSScriptRoot\..\..\.."

$env:NAVKIT_SOURCE = "v4"
Write-Host "[v4-smoke] NAVKIT_SOURCE = $env:NAVKIT_SOURCE"

$exe = Get-ChildItem "apps\mra_shell\bin" -Recurse -Filter "mra_shell.exe" |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $exe) {
    Write-Host "[v4-smoke] 未找到 mra_shell.exe——请先编译 GUI（README 4. 编译 GUI 并启动）" -ForegroundColor Red
    exit 1
}
Write-Host "[v4-smoke] 启动 $($exe.FullName)"
Write-Host ""
Write-Host "真机冒烟步骤："
Write-Host "  1. GUI 里照常启动鉴宝模块（手柄连接游戏机、点开始）"
Write-Host "  2. 日志里找 [v4] 行：帧注入控制器 / 手柄租约已绑定 / 常驻图已加载"
Write-Host "  3. 跑 2-3 个完整会话后正常停止模块"
Write-Host "  4. 退出后把 debug 目录最新的 trace 会话名告诉我（或直接说跑完了）"
Write-Host ""
& $exe.FullName
