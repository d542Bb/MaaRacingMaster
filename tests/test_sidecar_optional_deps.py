# -*- coding: utf-8 -*-
"""环境中心「可选依赖」接口的行为锁（get_optional_dependencies）。

收录判据是前端守则里的 L1 规则（缺失不阻断、降级明确、修复可引导），本文件锁接口
契约：条目形状、状态枚举、外链目标必须在 EXTERNAL_TARGETS 白名单内、字体项不经
后端返回（前端 document.fonts.check 才是渲染真源）。

构造方式沿用 test_sidecar_external_rpc / test_rpc_allowlist：`__new__` 绕过
`__init__`（避免拉起控制器），只注入 _controller 最小状态。
"""
from __future__ import annotations

import threading

import pytest

try:
    from maaracing_master.core import sidecar as sc
    from maaracing_master.core.remote_meta import EXTERNAL_TARGETS
    _OK, _ERR = True, ""
except Exception as exc:  # noqa: BLE001
    _OK, _ERR = False, str(exc)

pytestmark = pytest.mark.skipif(not _OK, reason=f"需要完整运行时依赖：{_ERR}")


class _FakeController:
    def __init__(self, available: bool, raises: bool = False):
        self._available = available
        self._raises = raises

    def gamepad_available(self) -> bool:
        if self._raises:
            raise RuntimeError("probe boom")
        return self._available


def _service(available: bool, raises: bool = False):
    svc = sc.SidecarService.__new__(sc.SidecarService)
    svc._lock = threading.RLock()
    svc._controller = _FakeController(available, raises)
    return svc


def _items(available: bool, raises: bool = False):
    ok, data, err = _service(available, raises).get_optional_dependencies({})
    assert ok and err is None
    return data["items"]


def test_vigembus_ready_state():
    items = _items(True)
    assert len(items) == 1
    it = items[0]
    assert it["id"] == "vigembus"
    assert it["state"] == "ready"


def test_vigembus_missing_state():
    assert _items(False)[0]["state"] == "missing"


def test_probe_exception_falls_back_to_missing():
    # 探测抛异常不得穿透 handler：按 missing 降级展示，引导仍可用
    assert _items(False, raises=True)[0]["state"] == "missing"


def test_guide_target_in_external_whitelist():
    # 引导外链走逻辑目标名白名单：目标不在 EXTERNAL_TARGETS 即为白名单静默失效
    for it in _items(True):
        assert it["guide_target"] in EXTERNAL_TARGETS


def test_font_item_not_from_backend():
    # 字体项由前端 document.fonts.check 自检（渲染真源），后端不得代发
    assert all(it["id"] != "noto_font" for it in _items(True))


def test_admission_criteria_shape():
    # 收录判据落地为字段完整性：缺失不阻断（effect 讲降级）、降级明确、修复可引导
    for it in _items(True):
        assert it["name"] and it["effect"] and it["degrade"]
        assert it["guide_label"] and it["official"] and it["detail"]
