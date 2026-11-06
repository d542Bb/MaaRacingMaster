# -*- coding: utf-8 -*-
"""mpelb 更新器的纯逻辑锁：清单校验、首次安装、单份备份、哈希。

不联网、不碰 dev/ 下的二进制；真实下载与替换由 --check / --update 手工验证。
"""
from __future__ import annotations

import hashlib

import pytest

from tools.pipeline import mpelb_update as update


def _manifest(payload: bytes = b"mpelb", **overrides) -> dict:
    artifact = {
        "url": "https://example.test/mpelb.exe",
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    artifact.update(overrides.pop("artifact", {}))
    manifest = {
        "version": "2.1.1",
        "platforms": {"windows-amd64": {"binary": artifact}},
    }
    manifest.update(overrides)
    return manifest


def _patch_update(monkeypatch, payload: bytes) -> None:
    monkeypatch.setattr(update.platform, "system", lambda: "Windows")
    monkeypatch.setattr(update.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(update, "_fetch_json", lambda url: _manifest(payload))
    monkeypatch.setattr(update, "_download", lambda url, dest: dest.write_bytes(payload))
    monkeypatch.setattr(update, "_version_of", lambda path: "mpelb version 2.1.1")
    monkeypatch.setattr(update.subprocess, "run", lambda *args, **kwargs: None)


def test_pick_asset_reads_windows_binary() -> None:
    version, url, sha = update._pick_asset(_manifest())
    assert version == "2.1.1"
    assert url == "https://example.test/mpelb.exe"
    assert sha == hashlib.sha256(b"mpelb").hexdigest()


def test_pick_asset_rejects_missing_platform_and_bad_sha() -> None:
    with pytest.raises(ValueError):
        update._pick_asset({"version": "2.1.1", "platforms": {}})
    with pytest.raises(ValueError):
        update._pick_asset(_manifest(artifact={"sha256": "short"}))


def test_previous_path_is_single_fixed_slot(tmp_path) -> None:
    target = tmp_path / "mpelb.exe"
    assert update._previous_path(target) == tmp_path / "mpelb.previous.exe"


def test_update_installs_when_target_missing(tmp_path, monkeypatch) -> None:
    payload = b"fresh-install"
    _patch_update(monkeypatch, payload)
    target = tmp_path / "dev" / "mpelb.exe"

    assert update.main(["--update", "--target", str(target)]) == 0
    assert target.read_bytes() == payload
    assert not (target.parent / "mpelb.previous.exe").exists()


def test_update_keeps_only_one_previous_version(tmp_path, monkeypatch) -> None:
    target = tmp_path / "dev" / "mpelb.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")

    _patch_update(monkeypatch, b"new")
    assert update.main(["--update", "--target", str(target)]) == 0
    assert target.read_bytes() == b"new"
    assert (target.parent / "mpelb.previous.exe").read_bytes() == b"old"

    _patch_update(monkeypatch, b"newer")
    assert update.main(["--update", "--target", str(target)]) == 0
    assert target.read_bytes() == b"newer"
    assert (target.parent / "mpelb.previous.exe").read_bytes() == b"new"


def test_check_missing_target_does_not_install(tmp_path, monkeypatch) -> None:
    _patch_update(monkeypatch, b"payload")
    target = tmp_path / "dev" / "mpelb.exe"

    assert update.main(["--check", "--target", str(target)]) == 0
    assert not target.exists()


def test_sha256_matches_hashlib(tmp_path) -> None:
    payload = b"mpelb"
    path = tmp_path / "mpelb.exe"
    path.write_bytes(payload)
    assert update._sha256(path) == hashlib.sha256(payload).hexdigest()
