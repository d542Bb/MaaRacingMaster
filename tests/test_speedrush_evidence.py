# -*- coding: utf-8 -*-
"""C 证据冻结：复制、清单、失败显式、保留上限。"""
import json
import time
from pathlib import Path

from maaracing_master.plugins.speedrush.evidence import archive_session_evidence


def _session(tmp_path, name="20261028_120000_1", trace=b'{"fid":1}\n',
             summary=None):
    d = tmp_path / "logs" / name
    (d / "trace").mkdir(parents=True)
    (d / "trace" / "speedrush.jsonl").write_bytes(trace)
    if summary is not None:
        (d / "trace" / "speedrush_summary.jsonl").write_bytes(summary)
    return d


def test_archive_copies_trace_and_manifest(tmp_path):
    d = _session(tmp_path, summary=b'{"score":1}\n')
    res = archive_session_evidence(d, 2, {"kind": "route_div", "fid": 42})
    assert res["ok"], res
    trace = json.loads(Path(res["manifest"]).read_text(encoding="utf-8"))
    assert trace["session"] == d.name and trace["phase"] == 2
    assert trace["trigger"]["fid"] == 42
    assert trace["trace"]["bytes"] == len(b'{"fid":1}\n')
    assert len(trace["trace"]["sha256"]) == 64
    assert trace["summary"]["bytes"] == len(b'{"score":1}\n')
    root = tmp_path / "evidence" / "c_gate"
    assert (root / "20261028_120000_1_p2.summary.jsonl").is_file()
    # 冻结目录在 logs 之外（普通会话清理不碰）
    assert root.parent.name == "evidence"


def test_archive_failure_is_explicit(tmp_path):
    d = tmp_path / "logs" / "x_1"
    (d / "trace").mkdir(parents=True)          # 没有 trace 文件
    res = archive_session_evidence(d, 1, {"kind": "sel_bug"})
    assert not res["ok"]
    assert "源 trace 不存在" in res["error"]
    err = tmp_path / "evidence" / "c_gate" / "x_1_p1.error.json"
    assert err.is_file()
    body = json.loads(err.read_text(encoding="utf-8"))
    assert body["trigger"]["kind"] == "sel_bug" and body["error"]


def test_archive_keeps_latest_n(tmp_path):
    for i in range(12):
        d = _session(tmp_path, name=f"20261028_1200{i:02d}_1", trace=b"x\n")
        assert archive_session_evidence(
            d, 1, {"kind": "route_div"}, keep=10)["ok"]
        time.sleep(0.01)
    root = tmp_path / "evidence" / "c_gate"
    assert len(list(root.glob("*.trace.jsonl"))) == 10
    assert not (root / "20261028_120000_1_p1.trace.jsonl").exists()
    assert (root / "20261028_120011_1_p1.trace.jsonl").exists()
