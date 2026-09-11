"""classify：综合各层证据，为每个文件给出最终分类 + 删除 confidence。

判定规则（V0.1）：
  1) runtime_loaded == YES
        -> 分类：native 且被引用/必需 = REQUIRED；.py/.pyd 进过 sys.modules = RUNTIME-LOADED
        -> confidence：HIGH（已被运行证实加载）
  2) Python 文件、静态可达、但 runtime 未加载 = STATIC-ONLY
        -> confidence：不确定；由 dynamic_hint 升降（有懒加载/反射/importlib -> LOW；纯普通 import 链死枝 -> MEDIUM）
  3) 前缀命中 oracle-REMOVE（dev/test 边界）-> DEV-ONLY（f2py/distutils/bin/*.exe）
  4) 无静态引用 + 无运行时证据 + 无 oracle-KEEP -> UNUSED-CANDIDATE
  5) 无法静态判定（app/*.dll 含 WinRT/XAML 激活、assets 等）-> UNKNOWN（confidence UNKNOWN/LOW）
"""
from __future__ import annotations

from pathlib import Path

from .model import FileClass, Confidence
from . import config
from . import oracle

# 已证实 KEEP 的顶层包：其下 native 即使 baseline 未捕获 sys.modules 也视为必需。
KEEP_TOP_PKGS = {"cv2", "numpy", "onnxruntime", "maa", "rapidocr", "PIL", "windows_capture",
                 "vgamepad", "shapely", "yaml", "pyclipper", "google", "charset_normalizer",
                 "idna", "urllib3", "requests"}


class Classifier:
    def __init__(self, exp_root: Path, py_graph, pe_graph):
        self.exp_root = exp_root
        self.py_graph = py_graph
        self.pe_graph = pe_graph
        # 构建 sidecar 顶层引用边集合（本包内所有 .py 的顶层 import 归并）
        self.py_static_pkgs: dict[str, set[str]] = {}   # rel_py -> set(top_pkg)

    def classify_file(self, rec) -> None:
        """就地填充 rec.file_class / rec.confidence / rec.reason / rec.dynamic_kinds。"""
        rel = Path(rec.path).as_posix()
        reason: list[str] = []
        cls: str = rec.file_class if rec.file_class != "UNKNOWN" else FileClass.UNKNOWN.value
        conf: str = Confidence.UNKNOWN.value

        # -- 1) runtime-loaded --
        if rec.runtime_loaded == "YES":
            if rel.endswith((".py", ".pyd", ".so")) or (rel.endswith((".dll",)) and rec.pkg in ("app(.NET/WinUI)",)):
                cls = FileClass.RUNTIME_LOADED.value
            else:
                cls = FileClass.REQUIRED.value
            conf = Confidence.HIGH.value
            reason.append("runtime-load observed")
            rec.file_class, rec.confidence = cls, conf
            return

        # -- 2) DEV-ONLY via oracle dev/test 子串 --
        if _is_dev_only(rel):
            cls = FileClass.DEV_ONLY.value
            conf = Confidence.MEDIUM.value
            reason.append("dev/test/build-only by heuristics (f2py/distutils/bin/sympy-tools)")
            rec.file_class, rec.confidence = cls, conf
            return

        # -- 3) app/*.dll (WinRT/XAML) -> UNKNOWN, 需要动态激活证据 --
        if rel.startswith("app/"):
            if rel.endswith(".dll"):
                cls = FileClass.UNKNOWN.value
                conf = Confidence.LOW.value
                reason.append("app/.NET/WinUI; static PE/import insufficient (WinRT/XAML reflection)")
            else:
                cls = FileClass.UNKNOWN.value
                conf = Confidence.UNKNOWN.value
                reason.append("app non-dll asset; play-safe unknown")
            rec.file_class, rec.confidence = cls, conf
            return

        # -- 4) Python static-only --
        if rel.endswith(".py"):
            # 属于某个被 import 的 top package？由 Layer1 判定 sidecar；第三方包可能只有少数 .py 直接可到
            top = _top_pkg_of(rel)
            if top and _oracle_keep_prefix(rel):
                cls = FileClass.REQUIRED.value
                conf = Confidence.MEDIUM.value
                reason.append("static-reachable & oracle-KEEP package; not runtime-traced in baseline")
            else:
                cls = FileClass.STATIC_ONLY.value
                conf = Confidence.MEDIUM.value
                reason.append("python static-only; lazy-load not ruled out")
            rec.file_class, rec.confidence = cls, conf
            return

        # -- 5) native, 非 app, 非 runtime ---
        if rel.endswith((".dll", ".pyd", ".so")) or rel.endswith(".exe"):
            refd = _is_native_referenced(rel, self.pe_graph.edges)
            top = _top_pkg_of(rel)
            is_stdlib_pyd = rel.startswith("runtime/python/") and not rel.startswith("runtime/python/packages/") and rel.endswith(".pyd")
            if rec.runtime_loaded == "YES":
                cls = FileClass.RUNTIME_LOADED.value
                conf = Confidence.HIGH.value
                reason.append("native actually loaded at runtime")
            elif is_stdlib_pyd or top in KEEP_TOP_PKGS or _oracle_keep_prefix(rel):
                cls = FileClass.REQUIRED.value
                conf = Confidence.MEDIUM.value
                reason.append(("stdlib interpreter-native" if is_stdlib_pyd else f"KEEP-package({top}) native"))
            elif refd:
                cls = FileClass.STATIC_ONLY.value
                conf = Confidence.MEDIUM.value
                reason.append("native static-referenced by PE imports; load not observed in baseline")
            else:
                cls = FileClass.UNUSED_CANDIDATE.value
                conf = Confidence.HIGH.value
                reason.append("native; no static PE dep & no runtime evidence")
            rec.file_class, rec.confidence = cls, conf
            return

        # -- 6) 其余数据/资源 —— 视 oracle 前缀 --
        if _oracle_keep_prefix(rel):
            cls = FileClass.REQUIRED.value
            conf = Confidence.MEDIUM.value
            reason.append("data/resource in oracle-KEEP package")
        else:
            cls = FileClass.UNKNOWN.value
            conf = Confidence.UNKNOWN.value
            reason.append("non-python/non-native data; evidence low")
        rec.file_class, rec.confidence = cls, conf

    def flag_dynamic(self, rec) -> None:
        """标记动态加载候选，供报告 highlit（不从分类层面直接 SAFE）。"""
        rel = Path(rec.path).as_posix()
        dyn = []
        # sidecar importlib.import_module 拼接
        for _mk, refs, dyn_seen in self.py_graph.iterate():
            for d in dyn_seen:
                if d and d.split(".")[0] in (top := _top_pkg_of(rel)) and top:
                    dyn.append("IMPORTLIB_DYNAMIC")
                    break
            if dyn:
                break
        if rel.startswith("app/") and rel.endswith((".dll", ".winmd")):
            dyn.append("WINRT_ACTIVATION")
        if "Xaml" in rel or "XAML" in rel:
            dyn.append("XAML_REFLECTION")
        if dyn:
            rec.dynamic_kinds = sorted(set(dyn))


def _is_dev_only(rel: str) -> bool:
    from . import oracle as _o
    for prefix, cls, _ in _o.KNOWN["REMOVE"]:
        if rel.startswith(prefix) and cls in ("DEV-ONLY", "UNUSED-CANDIDATE"):
            # f2py/distutils/bin => dev-only; sympy/avif/widgets/AppBinary => unused-candidate 不在此类
            if any(x in rel for x in ("/f2py", "/distutils", "/bin/", "/testing", "/tests", "/doc", "/_pyinstaller")):
                return True
    # 仅当不在 sympy/MaaAgentBinary/Widgets 等纯数据候选时才判 dev
    if "packages/bin/" in rel or rel.endswith("/bin") or "/packages/bin/" in rel:
        return True
    return False


def _top_pkg_of(rel: str) -> str:
    if "runtime/python/packages/" in rel:
        tail = rel.split("runtime/python/packages/")[1]
        return tail.split("/")[0]
    if rel.startswith("maaracing_master"):
        return "mra-sidecar"
    if rel.startswith("app"):
        return "app(.NET/WinUI)"
    return ""


def _oracle_keep_prefix(rel: str) -> bool:
    for prefix, _, _ in oracle.KNOWN["KEEP"]:
        if rel.startswith(prefix):
            return True
    return False


def _is_native_referenced(rel: str, pe_edges: dict[str, set[str]]) -> bool:
    """rel 是否是某 native 的 PE 依赖目标（相对名或文件名出现在某 import 表）。"""
    base = Path(rel).name
    for _src, deps in pe_edges.items():
        if base in deps or rel in deps:
            return True
    return False