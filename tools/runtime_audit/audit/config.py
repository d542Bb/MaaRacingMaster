"""审计配置：路径、入口、排除规则、oracle 定义。"""
from __future__ import annotations

from pathlib import Path

# 项目根（由上层的 pyproject/调用方注入，默认自动探测）
PROJECT_ROOT = Path(__file__).resolve().parents[3]   # tools/runtime_audit/audit -> 项目根
DEFAULT_EXP_ROOT = PROJECT_ROOT / "build" / "exp6" / "MaaRacingAssistant-0.19.0-win-x64"

# 审计根目录名
REL_RUNTIME = "runtime"
REL_APP = "app"

# Python 包的 site-packages 位置（本发行是 runtime\python\packages）
PY_PACKAGES_REL = "runtime/python/packages"

# 各层入口：sidecar 顶层包 + 插件
SIDECAR_TOP = "maaracing_master"
SIDECAR_MAIN = "maaracing_master.__main__"
PLUGINS = ("treasure",)

# native 扩展后缀
NATIVE_SUFFIXES = {".exe", ".dll", ".pyd", ".so"}
# 审计静态 import 时跳过的第三方压缩包/数据目录（保留 .py / .pyd / .so）
PY_TOP_LEVEL_DIRS_TO_SKIP_STATIC = set()   # sympy 等整包目录不发散，但顶层 package 本身仍归因

# Layer2 runtime trace 使用的无 GUI 触发序列（在 exp runtime python 中执行）
RUNTIME_TRACE_IMPORTS = [
    "maaracing_master",
    "maaracing_master.core.sidecar",
    "maaracing_master.core.controller",
    "maaracing_master.plugins.treasure.manifest",
    "maaracing_master.plugins.treasure.module",
    "maaracing_master.plugins.treasure.ocr",
    "numpy",
    "cv2",
    "onnxruntime",
    "rapidocr",
    "maa",
]

# Layer3 PE 解析：跳过系统目录（不追 Windows 系统 DLL）
PE_SKIP_BASENAMES = {
    "KERNEL32.dll", "USER32.dll", "GDI32.dll", "ADVAPI32.dll", "SHELL32.dll",
    "ole32.dll", "OLEAUT32.dll", "WS2_32.dll", "msvcp140.dll", "vcruntime140.dll",
    "vcruntime140_1.dll", "ucrtbase.dll", "ntdll.dll", "combase.dll", "windowscodecs.dll",
    "api-ms-win-*", "ext-ms-win-*", "MSVCP140.dll", "concrt140.dll", "sechost.dll",
    "BCrypt.dll", "WINMM.dll", "IMM32.dll", "userenv.dll", "SHCORE.dll", "dwmapi.dll",
    "IPHLPAPI.dll", "powrprof.dll", "cfgmgr32.dll", "propsys.dll", "uxtheme.dll",
    "msdmo.dll", "avrt.dll", "shlwapi.dll", "NETAPI32.dll", "VERSION.dll",
    "MSIMG32.dll", "wtsapi32.dll", "dxgi.dll", "d3d11.dll",
}


def is_py_file(p: Path) -> bool:
    return p.suffix.lower() == ".py"


def is_native_file(p: Path) -> bool:
    return p.suffix.lower() in NATIVE_SUFFIXES