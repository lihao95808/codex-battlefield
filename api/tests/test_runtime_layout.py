"""长跑链路的解释器探测：`api/.venv` 与仓库根 `.venv` 都要认。

**为什么值得单独测**：goal 模式下的分离式 scheduler 与看门狗都靠这个探测拿解释器。
一旦它退回裸 `python`，结果是"起了个进程、立刻缺依赖失败"——而长时间自主运行的前提就是
**人不在场**，这种失败特别难发现。原先它只认 `api/.venv`，而文档让人建在仓库根，两者正好错开。
"""

from __future__ import annotations

import os

import pytest

from app import orchestrator as orchestrator_module
from app.orchestrator import Orchestrator


def _make_venv(root, relative: str) -> str:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return str(path)


@pytest.fixture
def probe(monkeypatch, tmp_path):
    def _probe() -> str:
        monkeypatch.setattr(orchestrator_module, "ROOT_DIR", tmp_path)
        return Orchestrator.__new__(Orchestrator)._repo_python_executable()

    return _probe


def test_prefers_the_conventional_api_venv(probe, tmp_path):
    expected = _make_venv(tmp_path, os.path.join("api", ".venv", "Scripts", "python.exe"))
    _make_venv(tmp_path, os.path.join(".venv", "Scripts", "python.exe"))
    assert probe() == expected


def test_accepts_a_repo_root_venv_too(probe, tmp_path):
    """README 曾经让人建在仓库根；两种布局都不能变成"静默退回裸 python"。"""
    expected = _make_venv(tmp_path, os.path.join(".venv", "Scripts", "python.exe"))
    assert probe() == expected


def test_accepts_posix_layouts(probe, tmp_path):
    expected = _make_venv(tmp_path, os.path.join("api", ".venv", "bin", "python"))
    assert probe() == expected


def test_only_falls_back_to_bare_python_when_there_is_no_venv(probe, tmp_path):
    assert probe() == "python"
