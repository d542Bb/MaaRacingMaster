# -*- coding: utf-8 -*-
"""选择层影子的一致性契约（C 下游取证的信任根）。

锁三件事：预分歧同输入必须逐位一致；同输入不一致要留首次现场（字段/输入/
路线状态/对象里含金币排序用的枚数与置信度）；输入分歧后预期差异安静记录。
"""
from maaracing_master.plugins.speedrush.decision import SelectionShadowWatch
from maaracing_master.plugins.speedrush.route import RouteVerdict
from maaracing_master.plugins.speedrush.selection import SelectionReport


def _rep(layer=4, target=0.0, why="center", req=None):
    return SelectionReport(layer, (), target, why, True, False, req)


def _verdict(phase="keep", ref=0.0, vd="keep", why="no_request", ep=0):
    return RouteVerdict(phase, ref, None, vd, why, 0.1, None, None,
                        None, 0.0, ep)


def _step(w, *, fid=1, prod=None, shadow=None, prod_in=None, shadow_in=None,
          prod_route=None, shadow_route=None, snap=None):
    return w.step(fid=fid, prod=prod or _rep(), shadow=shadow or _rep(),
                  prod_in=prod_in or (0.0, False, 0.0),
                  shadow_in=shadow_in or (0.0, False, 0.0),
                  prod_route=prod_route, shadow_route=shadow_route,
                  snapshot=snap or (lambda: {"ego": 0.0}))


def test_equal_inputs_and_reports_are_silent():
    w = SelectionShadowWatch()
    calls = []
    assert _step(w, snap=lambda: calls.append(1) or {}) is None
    assert calls == []          # 没有差异就不取证（不白建对象快照）
    assert w.first_mismatch is None and not w.diverged


def test_same_inputs_different_reports_records_first_site():
    w = SelectionShadowWatch()
    prod = _rep(target=0.1, why="center")
    shadow = _rep(target=0.2, why="coin")
    rec = _step(w, fid=7, prod=prod, shadow=shadow,
                snap=lambda: {"ego": 0.5, "coin": [[0.1, 0.2, 1.0, 0.9]]})
    assert rec["bug"] == 1 and rec["n"] == 1
    first = rec["first"]
    assert first["fid"] == 7
    assert first["prod"]["why"] == "center"
    assert first["shadow"]["why"] == "coin"
    assert first["in"] == {"coarse": 0.0, "commit": False, "cx": 0.0}
    # 金币排序依据（枚数/置信度）必须在现场里
    assert first["objects"]["coin"][0][2:] == [1.0, 0.9]
    rec2 = _step(w, fid=8, prod=prod, shadow=shadow)
    assert rec2["bug"] == 1 and rec2["n"] == 2 and "first" not in rec2
    assert w.mismatch_ticks == 2


def test_input_divergence_records_first_context_then_quiet_when_equal():
    w = SelectionShadowWatch()
    prod = _rep()
    shadow = _rep(why="commit")
    rec = _step(w, fid=3, prod=prod, shadow=shadow,
                prod_in=(0.0, False, 0.0), shadow_in=(0.0, True, None),
                prod_route=_verdict(),
                shadow_route=_verdict(phase="merging"))
    assert rec["div"] == 1
    assert rec["first"]["in"]["prod"]["commit"] is False
    assert rec["first"]["in"]["shadow"]["commit"] is True
    assert rec["first"]["route"]["shadow"]["ph"] == "merging"
    assert w.diverged and w.first_mismatch is None
    # 分歧后同一份输出（影子仍在承诺道）→ 不再刷行
    assert _step(w, fid=4, prod=prod, shadow=prod,
                 prod_in=(0.0, False, 0.0), shadow_in=(0.0, True, None)) is None
    # 输出仍不同 → 紧凑行，不重复首次现场
    rec3 = _step(w, fid=5, prod=prod, shadow=_rep(why="coin", target=0.4,
                                                 req=1.0),
                 prod_in=(0.0, False, 0.0), shadow_in=(0.0, True, None))
    assert rec3.get("div") == 1 and "first" not in rec3
    assert rec3["req"] == 1.0
    assert "objects" in rec3      # 分歧窗口每拍都留对象快照（排序依据）


def test_reset_clears_divergence_state():
    w = SelectionShadowWatch()
    _step(w, fid=3, prod_in=(0.0, False, 0.0), shadow_in=(0.0, True, None))
    assert w.diverged
    w.reset()
    assert not w.diverged and w.first_div is None and w.first_mismatch is None
