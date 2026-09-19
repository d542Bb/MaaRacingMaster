# -*- coding: utf-8 -*-
"""发布分发剔除判定的纯逻辑单测：打包时刻已过 VALID_UNTIL 的模块不随包分发。

被测对象是 scripts/release/list_ship_excluded_modules.py 的 scan()/main()——
判据真源是各插件 manifest 的 VALID_UNTIL，解析与过期判定复用 core/module_validity
（与运行期 registry 置灰同口径）。本文件全部用 tmp 插件树，不依赖仓库现存插件，
未来插件增删不影响本测试。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "scripts" / "release" / "list_ship_excluded_modules.py"
VALIDITY = REPO / "maaracing_master" / "core" / "module_validity.py"

# 判定时刻固定（含时区），使过期/存活断言不随机器时钟漂移
NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
PAST = (NOW - timedelta(days=1)).isoformat()
FUTURE = (NOW + timedelta(days=1)).isoformat()


def _load_tool():
    import importlib.util

    spec = importlib.util.spec_from_file_location("list_ship_excluded_modules", TOOL)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_plugin(root: Path, name: str, body: str) -> Path:
    """在 tmp 插件树里造一个最小插件目录（manifest 即全部）。"""
    d = root / name
    d.mkdir(parents=True)
    (d / "manifest.py").write_text(body, encoding="utf-8")
    return d


def _scan(root: Path, now: datetime | None = NOW) -> dict:
    return _load_tool().scan(root, VALIDITY, now)


def test_no_valid_until_ships(tmp_path):
    _make_plugin(tmp_path, "alpha", 'ID = "alpha"\n')
    assert _scan(tmp_path)["kept"] == [{"id": "alpha", "dir": "alpha"}]
    assert _scan(tmp_path)["expired"] == []


def test_expired_until_is_excluded(tmp_path):
    _make_plugin(tmp_path, "alpha", f'ID = "alpha"\nVALID_UNTIL = "{PAST}"\n')
    assert _scan(tmp_path)["expired"] == [{"id": "alpha", "dir": "alpha"}]


def test_until_is_closed_interval(tmp_path):
    # 打包时刻恰等于端点：闭区间含端点 → 仍在窗口内 → 随包（is_expired 为严格大于）
    _make_plugin(tmp_path, "alpha", f'ID = "alpha"\nVALID_UNTIL = "{NOW.isoformat()}"\n')
    assert [e["id"] for e in _scan(tmp_path)["expired"]] == []


def test_invalid_until_ships_with_warning(tmp_path):
    _make_plugin(tmp_path, "alpha", 'ID = "alpha"\nVALID_UNTIL = "不是时间"\n')
    result = _scan(tmp_path)
    assert [e["id"] for e in result["expired"]] == []
    assert any("alpha" in w for w in result["warnings"])


def test_non_literal_until_ships_with_warning(tmp_path):
    # 非字面量（动态计算）无法静态判读 → 按未声明处理，宁可多分发不静默吞模块
    _make_plugin(tmp_path, "alpha", 'ID = "alpha"\nVALID_UNTIL = str(1)\n')
    result = _scan(tmp_path)
    assert result["kept"] == [{"id": "alpha", "dir": "alpha"}]
    assert any("非字面量" in w for w in result["warnings"])


def test_future_valid_from_still_ships(tmp_path):
    # 活动未开放不构成剔除：随包分发，运行期按窗口置灰
    _make_plugin(tmp_path, "alpha", f'ID = "alpha"\nVALID_FROM = "{FUTURE}"\n')
    assert _scan(tmp_path)["kept"] == [{"id": "alpha", "dir": "alpha"}]


def test_dir_without_manifest_ignored(tmp_path):
    (tmp_path / "not_a_plugin").mkdir()
    _make_plugin(tmp_path, "alpha", 'ID = "alpha"\n')
    assert [i["dir"] for i in _scan(tmp_path)["kept"]] == ["alpha"]


def test_missing_id_reported_as_dirname(tmp_path):
    _make_plugin(tmp_path, "alpha", 'NAME = "无名"\n')
    result = _scan(tmp_path)
    assert result["kept"] == [{"id": "alpha", "dir": "alpha"}]
    assert any("ID" in w for w in result["warnings"])


def test_naive_now_interpreted_as_game_tz(tmp_path):
    # naive 判定时刻按游戏服时区（UTC+8）解释：09-19 12:00+08 = 04:00Z，晚于 PAST(+08) 前一天
    _make_plugin(tmp_path, "alpha", 'ID = "alpha"\nVALID_UNTIL = "2026-09-19T11:59:59+08:00"\n')
    assert _scan(tmp_path, NOW.replace(tzinfo=None))["expired"] == [{"id": "alpha", "dir": "alpha"}]


def test_bom_manifest_tolerated(tmp_path):
    # importlib 对带 BOM 的 manifest 照常 import，工具口径必须一致（utf-8-sig）
    d = _make_plugin(tmp_path, "alpha", 'ID = "alpha"\n')
    raw = (d / "manifest.py").read_bytes()
    (d / "manifest.py").write_bytes(b"\xef\xbb\xbf" + raw)
    assert _scan(tmp_path)["kept"] == [{"id": "alpha", "dir": "alpha"}]


def test_main_cli_json_contract(tmp_path, capsys):
    _make_plugin(tmp_path, "gone", f'ID = "gone"\nVALID_UNTIL = "{PAST}"\n')
    _make_plugin(tmp_path, "stay", 'ID = "stay"\n')
    code = _load_tool().main(
        [str(tmp_path), "--validity-module", str(VALIDITY),
         "--now", NOW.isoformat()])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [e["dir"] for e in payload["expired"]] == ["gone"]
    assert [k["dir"] for k in payload["kept"]] == ["stay"]


def test_main_cli_rejects_missing_root(tmp_path):
    # main 返回退出码（sys.exit(main()) 只在命令行入口生效），非 0 = plugins_root 缺失
    code = _load_tool().main([str(tmp_path / "nope"), "--validity-module", str(VALIDITY)])
    assert code == 3
