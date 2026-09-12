# -*- coding: utf-8 -*-
"""版本双轨契约：源码工作副本 vs 包内固化快照。

锁住两件事：
  1. 「源码模式」判据不能被外层 .git 骗到 —— 正式包只要落在某个 git 仓库的子目录
     里（开发机 build/ 下就是这个形态），旧判据会把它当成源码运行，版本号被外层
     仓库的 git describe 劫持，包内快照形同虚设（真机事故：dev.1 包显示成
     0.21.0.dev2+102）。
  2. UI 显示串的形状 —— 源码模式带「+领先次数（源码模式）」，打包模式原样呈现快照。

本文件只依赖标准库：maaracing_master/__init__.py 不触碰 core/plugins 导入链。
"""

from __future__ import annotations

import re

import pytest

import maaracing_master as pkg

_is_source_checkout = pkg._is_source_checkout
_display_version = pkg._display_version


def _make_repo(tmp_path, *, with_git: bool = True):
    """造一个「仓库根 + 根下 maaracing_master 包」的最小布局，返回 (root, __init__.py)。"""
    root = tmp_path / "repo"
    pkg_dir = root / "maaracing_master"
    pkg_dir.mkdir(parents=True)
    if with_git:
        (root / ".git").mkdir()
    init = pkg_dir / "__init__.py"
    init.write_text("", encoding="utf-8")
    return root, init


def _drop_nested_package(root, rel_dir: str):
    """把一份「发布包」形态的包体放进 root 的某个子目录里（外层 .git 依然罩着它）。"""
    init = root / rel_dir / "maaracing_master" / "__init__.py"
    init.parent.mkdir(parents=True)
    init.write_text("", encoding="utf-8")
    version = init.parent / "_version.py"
    version.write_text('version = "0.22.0-dev.1"\n', encoding="utf-8")
    return init


class TestSourceModeDiscrimination:
    def test_repo_working_copy_is_source_mode(self, tmp_path):
        _root, init = _make_repo(tmp_path)
        assert _is_source_checkout(init) is True

    def test_packaged_copy_nested_in_foreign_git_is_not_source_mode(self, tmp_path):
        """本次事故的回归锁：包在 .git 笼罩的目录树里，也必须走快照轨。"""
        root, _init = _make_repo(tmp_path)
        nested = _drop_nested_package(root, "build/MaaRacingMaster-0.22.0-dev.1-win-x64")
        assert pkg._find_git_root(nested.parent) is not None, "外层 .git 必须仍然可见（前提条件）"
        assert _is_source_checkout(nested) is False

    def test_untracked_layout_without_git_is_not_source_mode(self, tmp_path):
        root, init = _make_repo(tmp_path, with_git=False)
        if pkg._find_git_root(root) is not None:
            pytest.skip("临时目录上层存在外部 .git，无法构造「完全无 git」场景")
        assert _is_source_checkout(init) is False

    def test_git_root_without_this_package_is_not_source_mode(self, tmp_path):
        """仓库根存在但根下没有本包（比如误装进第三方仓库）：判非源码模式。"""
        root = tmp_path / "repo"
        (root / ".git").mkdir(parents=True)
        foreign = root / "some_other_project" / "maaracing_master" / "__init__.py"
        foreign.parent.mkdir(parents=True)
        foreign.write_text("", encoding="utf-8")
        assert _is_source_checkout(foreign) is False


class TestDisplayVersion:
    def test_source_mode_appends_mode_note_and_keeps_lead_count(self):
        assert _display_version("0.21.0.dev2+102", True) == "0.21.0.dev2+102（源码模式）"

    def test_source_mode_exactly_on_tag_has_no_lead_count(self):
        assert _display_version("0.22.0.dev1", True) == "0.22.0.dev1（源码模式）"

    def test_packaged_mode_shows_snapshot_verbatim(self):
        assert _display_version("0.22.0-dev.1", False) == "0.22.0-dev.1"

    def test_mode_note_is_the_only_difference_between_tracks(self):
        assert _display_version("1.2.3", True) == _display_version("1.2.3", False) + "（源码模式）"


class TestCurrentCheckout:
    """对「正在被导入的这份包」本身的断言（测试跑在仓库工作副本里）。"""

    def test_imported_package_is_recognised_as_working_copy(self):
        assert pkg.SOURCE_MODE is True

    def test_display_string_tracks_version_plus_mode_note(self):
        assert pkg.__display_version__ == pkg.__version__ + "（源码模式）"

    def test_lead_count_separator_is_plus(self):
        """领先次数以 +N 表达（PEP440 local 段），显示串不得把它吃掉。"""
        assert "+" not in pkg.__version__ or re.search(r"\+\d+$", pkg.__version__.split("（")[0])

    def test_clean_version_regex_when_describe_succeeded(self):
        described = pkg._git_describe_version()
        if described is None:
            pytest.skip("当前环境 git describe 不可用（浅克隆/无 tag），跳过形状断言")
        assert re.match(r"^\d+\.\d+\.\d+\.dev\d+(\+\d+)?$", described), described
