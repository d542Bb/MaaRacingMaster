"""oracle：把人工已证实的 KEEP/REMOVE 结论固化，作为 Auditor 回归测试基准。"""
from __future__ import annotations

# 断言方向，用于验证 Auditor 的 classifier 是否与人工结论一致。
# 每一项: (路径前缀/substring, 期望分类, 期望confidence主档位, 说明)
KNOWN = {
    "KEEP": [
        ("runtime/python/python.exe", "REQUIRED", "KEEP"),
        ("runtime/python/pythonw.exe", "REQUIRED", "KEEP"),
        ("runtime/python/python311.dll", "REQUIRED", "KEEP"),
        ("MaaRacingMaster.exe", "REQUIRED", "KEEP"),
        ("runtime/python/packages/numpy/_core", "REQUIRED", "KEEP"),
        ("runtime/python/packages/numpy/lib", "RUNTIME-LOADED", "KEEP"),
        ("runtime/python/packages/cv2/__init__.py", "RUNTIME-LOADED", "KEEP"),
        ("runtime/python/packages/onnxruntime/capi/onnxruntime_pybind11_state.pyd", "RUNTIME-LOADED", "KEEP"),
        ("runtime/python/packages/maa/bin/onnxruntime_maa.dll", "RUNTIME-LOADED", "KEEP"),
        ("runtime/python/packages/maa/bin/DirectML.dll", "RUNTIME-LOADED", "KEEP"),
        ("runtime/python/packages/rapidocr/models", "REQUIRED", "KEEP"),
        ("runtime/python/packages/maa", "RUNTIME-LOADED", "KEEP"),
        ("runtime/python/packages/PIL", "RUNTIME-LOADED", "KEEP"),
    ],
    "REMOVE": [
        ("runtime/python/packages/sympy", "UNUSED-CANDIDATE", "REMOVE"),
        ("runtime/python/packages/onnxruntime/capi/onnxruntime.dll", "UNUSED-CANDIDATE", "REMOVE"),
        ("runtime/python/packages/PIL/_avif", "UNUSED-CANDIDATE", "REMOVE"),
        ("runtime/python/packages/MaaAgentBinary", "UNUSED-CANDIDATE", "MEDIUM"),
        ("runtime/python/packages/google/protobuf", "UNUSED-CANDIDATE", "REMOVE"),
        ("runtime/python/packages/flatbuffers", "UNUSED-CANDIDATE", "REMOVE"),
        ("runtime/python/packages/cv2/opencv_videoio_ffmpeg500_64.dll", "UNUSED-CANDIDATE", "REMOVE"),
        ("runtime/python/app/widgets", "UNUSED-CANDIDATE", "REMOVE"),
        ("runtime/python/packages/numpy/f2py", "DEV-ONLY", "REMOVE"),
        ("runtime/python/packages/numpy/distutils", "DEV-ONLY", "REMOVE"),
        ("runtime/python/packages/bin", "DEV-ONLY", "REMOVE"),
    ],
}


def check(package_name: str, file_class: str, path: str, expect_dict: dict) -> dict:
    """对单个文件跑 oracle 断言。返回该断言是否命中。
    expect_dict: {"KEEP":[...], "REMOVE":[...]} 取自闭环 policy，简化版本见 KNOWN 分组。
    """
    results = []
    for keep in KNOWN["KEEP"]:
        prefix, cls, _ = keep
        if path.startswith(prefix):
            results.append({"expect": "KEEP", "prefix": prefix, "matched": file_class in ("REQUIRED", "RUNTIME-LOADED"), "file_class": file_class})
    for rem in KNOWN["REMOVE"]:
        prefix, cls, _ = rem
        if path.startswith(prefix):
            # Dev-only 文件若被归为 DEV-ONLY 即通过；unused 亦然
            results.append({"expect": "REMOVE", "prefix": prefix, "matched": file_class in ("DEV-ONLY", "UNUSED-CANDIDATE"), "file_class": file_class})
    return results[0] if results else None