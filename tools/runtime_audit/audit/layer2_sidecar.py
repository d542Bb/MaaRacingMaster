"""Layer 2 的 sidecar：在发行版 runtime python 下执行，输出 Python 运行时轨迹。

用法（由审计主进程调用）：
    <exp-root>/runtime/python/python.exe run_trace.py <exp-root> <out-json>

覆盖：
  - baseline：只 import 交互期（import 前 module 集合，由主进程用纯解释器快照对比）
  - 目标 import 序列：sidecar+Treasure+YOLO/DML+RapidOCR+MaaFramework
  - 输出 { "modules": [绝对路径, ...], "module_keys": [...], "dynamic_hint": [...] }
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    exp_root = Path(sys.argv[1])
    out_json = Path(sys.argv[2])
    # 注入 sidecar 顶层与 sidecar 包目录，使发行里的 maaracing_master 可被 import
    sys.path.insert(0, str(exp_root))
    sys.path.insert(0, str(exp_root / "maaracing_master" / "core"))

    loaded: set[Path] = set()

    def _run():
        # 关键：先 import 交互期内核，再去收集，确保 sidecar 完整初始化
        import maaracing_master  # noqa
        import maaracing_master.core.sidecar  # noqa
        import maaracing_master.core.controller  # noqa
        import maaracing_master.plugins.treasure.manifest  # noqa
        import maaracing_master.plugins.treasure.module  # noqa
        import maaracing_master.plugins.treasure.ocr  # noqa
        import numpy  # noqa
        import cv2  # noqa
        import onnxruntime  # noqa
        import rapidocr  # noqa
        import maa  # noqa

    _run()

    module_keys = set()
    for m in sys.modules:
        module_keys.add(m)
        # 记录模块真实文件位置（第三方包在 packages 下、sidecar 在发行内）
        mod = sys.modules[m]
        file = getattr(mod, "__file__", None)
        if file and isinstance(file, str):
            try:
                loaded.add(Path(file).resolve())
            except OSError:
                pass

    # 输出相对项目根/发行根的模块文件集合（统一 posix 斜杠，便于主进程对比）
    rel_files = []
    for p in loaded:
        try:
            rel_files.append(p.relative_to(exp_root).as_posix())
        except ValueError:
            rel_files.append(str(p))
    # 若模块未被识别（无 __file__，如 builtin 扩展）通过 sys.builtin_module_names 补充，但不用于 pyd 文件

    result = {
        "module_keys": sorted(module_keys),
        "module_files": sorted(rel_files),
    }
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())