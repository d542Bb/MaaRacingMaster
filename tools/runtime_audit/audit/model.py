"""运行时闭包审计数据模型 (V0.1)。

定义文件级记录、分类枚举、confidence 枚举与包级记录。
所有审计层共享这些结构，最终序列化为 JSON。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum


class FileClass(str, Enum):
    """最终文件分类。

    REQUIRED        - 正常运行时必需（runtime trace 明确加载，或 native 关键路径）
    RUNTIME-LOADED  - 运行 trace 实测加载（对 Python = 进入 sys.modules；对 native = 进 Module snapshot）
    STATIC-ONLY     - 静态依赖图可达，但运行 trace 未观测到加载（可能是懒加载/反射/投送，不等于可删）
    DEV-ONLY        - 明确属开发/测试/构建辅助工具（该域人工已证实不需要运行时）
    UNUSED-CANDIDATE- 无任何静态/动态证据指向使用，候选可删
    UNKNOWN         - 证据不足（尤其 WinRT/XAML/配置驱动等无法静态判定的）
    """

    REQUIRED = "REQUIRED"
    RUNTIME_LOADED = "RUNTIME-LOADED"
    STATIC_ONLY = "STATIC-ONLY"
    DEV_ONLY = "DEV-ONLY"
    UNUSED_CANDIDATE = "UNUSED-CANDIDATE"
    UNKNOWN = "UNKNOWN"


class Confidence(str, Enum):
    """删除信心级别。独立于文件分类：
    STATIC-ONLY/UNKNOWN 绝不等于 SAFE TO DELETE；
    UNUSED-CANDIDATE 也需 confidence 支撑。"""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class DynamicKind(str, Enum):
    """动态加载扫描命中类型，用于 WinRT/XAML/插件等无法静态判定的情形。"""

    WINRT_ACTIVATION = "WINRT_ACTIVATION"
    XAML_REFLECTION = "XAML_REFLECTION"
    DYNAMIC_PLUGIN = "DYNAMIC_PLUGIN"
    CONFIG_DRIVEN = "CONFIG_DRIVEN"
    IMPORTLIB_DYNAMIC = "IMPORTLIB_DYNAMIC"      # importlib.import_module / __import__ 字符串拼接
    SUBPROCESS = "SUBPROCESS"                    # subprocess/Popen 启动可执行文件
    CTRYPE_LOAD = "CTYPE_LOAD"                   # ctypes.CDLL / WinDLL 加载 native


@dataclass
class FileRecord:
    """exp6 发行目录中一个文件的审计记录。"""

    path: str                    # 相对发行根，如 "runtime/python/packages/sympy/..." 
    size: int                    # 字节
    rel_category: str            # "runtime" / "app" / "assets" / "sidecar" / "other"
    pkg: str = ""                # 归因 package（site-packages 包名或 app 内模块）
    static_deps: list[str] = field(default_factory=list)   # 静态依赖（PE import or python import）
    dynamic_kinds: list[str] = field(default_factory=list) # DynamicKind 命中
    runtime_loaded: str = "UNKNOWN"   # "YES"/"NO"/"UNKNOWN"（对应 layer2/layer4）
    file_class: str = "UNKNOWN"
    confidence: str = "UNKNOWN"
    reason: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PackageRecord:
    """site-packages / app 内部件归因记录。"""

    name: str
    version: str = ""
    dist_info: str = ""
    size: int = 0
    native_files: list[str] = field(default_factory=list)
    static_referrers: list[str] = field(default_factory=list)
    runtime_loaded: str = "UNKNOWN"
    file_class: str = "UNKNOWN"
    confidence: str = "UNKNOWN"
    conclusion: str = ""          # "REMOVE" / "KEEP" / "CANDIDATE" / "UNKNOWN"
    reason: list[str] = field(default_factory=list)


@dataclass
class AuditResult:
    """一次审计的聚合输出。"""

    exp_root: str
    generated_at: str = ""
    files: list[FileRecord] = field(default_factory=list)
    packages: list[PackageRecord] = field(default_factory=list)
    classes_summary: dict = field(default_factory=dict)      # class -> {mb,files}
    top_candidates: list[dict] = field(default_factory=list)
    oracle: dict = field(default_factory=dict)               # {known_keep/remove, correct, wrong}
    warnings: list[str] = field(default_factory=list)