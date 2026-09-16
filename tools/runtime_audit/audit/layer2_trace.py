"""Layer 2：Python 运行时 trace 采集主进程。

用 exp runtime python 执行 layer2_sidecar.py，获取加载的 module_files 集合，
并辅助 Layer1 判定 Python 文件的 runtime_loaded（YES/NO）。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path



def run_runtime_trace(exp_root: Path, out_json: Path) -> set[Path]:
    """调用发行版 runtime python 执行 sidecar tracer，返回<发行根相对路径>集合(带斜杠)。"""
    py = exp_root / "runtime" / "python" / "python.exe"
    if not py.exists():
        raise FileNotFoundError(f"runtime python not found: {py}")
    sidecar = Path(__file__).resolve().parent / "layer2_sidecar.py"
    cmd = [str(py), str(sidecar), str(exp_root), str(out_json)]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    data = json.loads(out_json.read_text(encoding="utf-8"))
    return set(data.get("module_files", []))


def load_previous(out_json: Path) -> set[Path]:
    if out_json.exists():
        data = json.loads(out_json.read_text(encoding="utf-8"))
        return set(data.get("module_files", []))
    return set()


def mark_py_runtime(files: dict, reload_files: set):
    """对 audit.model.FileRecord 列表，把属于 reload 集合的 .py 标为 RUNTIME-LOADED=YES；
    其余 Python 源文件标 NO（在静态可达前提下即为 STATIC-ONLY）。reload_files 为 posix 相对路径。"""
    reload_set = {Path(r).as_posix() for r in reload_files}
    for rec in files.values():
        if str(rec.path).endswith(".py") or str(rec.path).endswith(".pyd") or str(rec.path).endswith(".so"):
            rec.runtime_loaded = "YES" if Path(rec.path).as_posix() in reload_set else "NO"