"""V0.2-alpha：app/ 归因增强（增量于 V0.1，不改动既有 Layer）。

解答：app/154.58MB 里哪些是真正的 WinUI/.NET runtime closure，哪些只是附带组件。
手段：
  1. deps.json -> dll -> NuGet package -> version -> 父依赖（权威归因）
  2. managed assembly closure：依赖 deps.json 的 runtimeTargets 分发清单（官方 publish 源）
  3. WinRT projection 标记（Microsoft.Windows.SDK.NET / Microsoft.InteractiveExperiences 等）
  4. XAML 标记（Microsoft.ui.xaml / Microsoft.WinUI / .xaml/.pri/.xbf）
  5. 每 dll 归入 app 子分类（.NET CORE REQUIRED / WINUI-XAML / WINRT-PROJECTION / WEBVIEW2 / WINAPPSDK-CORE / STATIC-ONLY / RUNTIME-LOADED / UNKNOWN）
"""
from __future__ import annotations

import json
from pathlib import Path
from collections import defaultdict

APP_CLASS = {
    "NETCORE_REQUIRED": "NETCORE-REQUIRED",
    "WINUI_XAML": "WINUI-XAML",
    "WINRT_PROJECTION": "WINRT-PROJECTION",
    "WEBVIEW2": "WEBVIEW2",
    "WINAPPSDK_CORE": "WINAPPSDK-CORE",
    "RUNTIME_LOADED": "RUNTIME-LOADED",
    "STATIC_ONLY": "STATIC-ONLY",
    "UNKNOWN": "UNKNOWN",
}

# XAML / WinUI 核心 dll 清单
XAML_ROOT = {
    "Microsoft.ui.xaml.dll", "Microsoft.WinUI.dll", "Microsoft.UI.Xaml.Controls.dll",
    "Microsoft.UI.Xaml.Internal.dll", "Microsoft.UI.Composition.dll",
}
XAML_DEPENDENCY_HINTS = ("Xaml", "WinUI")
XAML_RESOURCE_HINTS = ("resources.19h1", "Resources.dll", "DWriteCore", "dwmcorei",
                        "DwmSceneI", "dcompi", "wuceffectsi", "CoreMessagingXP", ".pri",
                        ".winmd", "Xaml.winmd", "Microsoft.DirectManipulation")
WINRT_PROJECTION_HINTS = (
    "Microsoft.Windows.SDK.NET.dll", "Microsoft.InteractiveExperiences.Projection.dll",
    "Microsoft.Security.Authentication", "Microsoft.Graphics", "Microsoft.Foundation",
)
# .NET native 运行层（非 BCL managed，但属 runtime closure）
NETCORE_NATIVE_HINTS = ("clrgc", "marshal", "msquic", "hostfxr", "hostpolicy",
                        "clrjit", "coreclr.dll")
WEBVIEW2_HINT = "WebView2"


class AppAuditV02:
    def __init__(self, app_dir: Path):
        self.app_dir = app_dir
        self.deps: dict = {}
        self.dll_meta: dict[str, dict] = {}      # dll basename -> {package, version, parent}
        self.package_dlls: dict[str, list[str]] = {}
        # managed refs: dll -> set(dll), 从 deps.targets 的同 package runtimeTargets 推导
        self.managed_edges: dict[str, set[str]] = {}

    def load_deps(self) -> None:
        deps_file = self.app_dir / "MaaRacingMaster.Shell.deps.json"
        if not deps_file.exists():
            return
        self.deps = json.loads(deps_file.read_text(encoding="utf-8"))
        tgt = self.deps["targets"][".NETCoreApp,Version=v8.0/win-x64"]
        self._build_package_dlls(tgt)

    def _build_package_dlls(self, tgt: dict) -> None:
        """从 deps.targets 收集每个 package 分发的 runtime 文件，映射 dll->package。"""
        for pkg_key, entries in tgt.items():
            pkg_name, _, ver = pkg_key.rpartition("/")
            runtime = entries.get("runtime", {})
            runtimeTargets = entries.get("runtimeTargets", {})
            for relpath in list(runtime.keys()) + list(runtimeTargets.keys()):
                if relpath.endswith(".dll"):
                    # 取顶层非目录段拼出的 dll 名（可能含 win-x64 子路径）
                    name = relpath.split("/")[-1]
                    self.dll_meta.setdefault(name, {"package": pkg_name, "version": ver, "parent": pkg_name})
                    self.package_dlls.setdefault(pkg_name, []).append(name)

    # ---- 每个 app dll 的分类 ----
    def classify_dll(self, name: str, size: int) -> dict:
        meta = self.dll_meta.get(name, {"package": "", "version": "", "parent": ""})
        pkg = meta["package"]
        low = name.lower()
        xaml_root = name in XAML_ROOT or any(h in name for h in XAML_DEPENDENCY_HINTS)
        xaml_res = any(h in name for h in XAML_RESOURCE_HINTS)
        netcore_native = any(h in low for h in NETCORE_NATIVE_HINTS)
        winrt_proj = any(h in name for h in WINRT_PROJECTION_HINTS)
        webview = WEBVIEW2_HINT in name or WEBVIEW2_HINT in pkg
        winappsdk = "WindowsAppSDK" in pkg or "Microsoft.WindowsAppRuntime" in name
        netcore = name.startswith("System.") or name in (
            "coreclr.dll", "clrjit.dll", "hostfxr.dll", "hostpolicy.dll",
            "System.Private.CoreLib.dll",
        )
        if xaml_root or xaml_res:
            cls = APP_CLASS["WINUI_XAML"]
            tag = "XAML-ROOT" if xaml_root else "XAML-RESOURCE"
        elif winrt_proj:
            cls = APP_CLASS["WINRT_PROJECTION"]
            tag = "WINRT-PROJECTION"
        elif webview:
            cls = APP_CLASS["WEBVIEW2"]
            tag = "WEBVIEW2"
        elif netcore or netcore_native:
            cls = APP_CLASS["NETCORE_REQUIRED"]
            tag = "NETCORE"
        elif winappsdk:
            cls = APP_CLASS["WINAPPSDK_CORE"]
            tag = "WINAPPSDK"
        elif meta.get("package"):
            cls = APP_CLASS["WINAPPSDK_CORE"]
            tag = "PACKAGED"
        else:
            cls = APP_CLASS["UNKNOWN"]
            tag = "UNKNOWN"
        return {"name": name, "size": size, "pkg": pkg, "version": meta["version"],
                "parent": meta["parent"], "class": cls, "tag": tag}

    # ---- WinRT projection 报告（SDK.NET 同族） ----
    def sdknet_report(self, size: int) -> dict:
        sys_dll = "System.Private.CoreLib.dll"
        return {
            "file": "Microsoft.Windows.SDK.NET.dll",
            "size_mb": size / (1024 * 1024),
            "package": self.dll_meta.get("Microsoft.Windows.SDK.NET.dll", {}).get("package", ""),
            "version": self.dll_meta.get("Microsoft.Windows.SDK.NET.dll", {}).get("version", ""),
            "referenced_by": self.sdknet_ref_by(),
            "note": "WinRT projection assembly; deps.json places it under Microsoft.WindowsAppSDK package (local)",
        }

    def sdknet_ref_by(self) -> list[str]:
        return [n for n, m in self.dll_meta.items() if "WindowsAppSDK" in m.get("package", "")]

    def aggregate(self) -> dict:
        classes = defaultdict(lambda: {"mb": 0.0, "files": 0})
        top20 = defaultdict(list)
        for n in sorted(self.dll_meta.keys()):
            pass
        return {"classes": dict(classes), "top20": dict(top20)}