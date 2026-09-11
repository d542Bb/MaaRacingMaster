"""attribution：文件 → package 归因。

依据：
  - Python site-packages（runtime/python/packages）：目录名 package ，dist-info 提供 version
  - native 文件 (.pyd/.dll/.exe) 依据其所在 package 目录归因
  - app/ 目录：归因到 "app"(.NET/WinUI) 或具体包
  - assets / sidecar 单独归因
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import config


class Attriber:
    def __init__(self, exp_root: Path):
        self.exp_root = exp_root
        self.packages_dir = exp_root / "runtime" / "python" / "packages"
        self._pkg_meta: dict[str, str] = {}     # pkg -> version

    def scan_pkg_meta(self) -> None:
        """从 *.dist-info 采集 package 版本。"""
        for di in self.packages_dir.glob("*.dist-info"):
            name = di.name.replace(".dist-info", "")
            version = _extract_version(di)
            pkg = _dist_to_pkg(name)
            if pkg:
                self._pkg_meta[pkg] = self._pkg_meta.get(pkg) or version

    def attribute(self, rel: str) -> str:
        """返回 rel 的归因 package 名。"""
        relp = rel.replace("\\", "/")
        # site-packages 内
        if relp.startswith("runtime/python/packages/"):
            tail = relp[len("runtime/python/packages/"):]
            return tail.split("/")[0]   # 顶层目录（package 或 *.dist-info）
        if relp.startswith("app"):
            return "app(.NET/WinUI)"
        if relp.startswith("assets"):
            return "assets"
        if relp.startswith("maaracing_master"):
            return "mra-sidecar"
        if relp.startswith("runtime"):
            return "runtime-shell"
        return "other"

    def version(self, pkg: str) -> str:
        return self._pkg_meta.get(pkg, "")


def _dist_to_pkg(dist_name: str) -> str:
    """PyPI 规范化：下划线/点转连字符，与 package 目录不一定一致。
    这里只做拆版本：`numpy-2.4.6` -> `numpy`。"""
    if dist_name.count("-") >= 1:
        return dist_name.split("-")[0]
    return dist_name


def _extract_version(di: Path) -> str:
    meta = di / "METADATA"
    if meta.exists():
        try:
            for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.lower().startswith("version:"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    # 兜底从目录名取
    name = di.name.replace(".dist-info", "")
    parts = name.split("-")[1:]
    return ".".join(parts) if parts else ""