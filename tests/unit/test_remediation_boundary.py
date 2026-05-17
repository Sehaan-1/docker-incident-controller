"""
Security boundary unit tests for assert_within_dir (remediation.py).

Step 1A — TDD RED phase:
  Before the fix these traversal tests FAIL because assert_within_dir
  silently discards the result of resolved.relative_to() and returns
  the traversed path instead of raising ValueError.

Step 1B — After the fix every parametrized traversal case MUST raise
  ValueError and the two happy-path tests MUST pass.

Blast radius: zero — no system code changed, no I/O outside tmp_path.
Rollback: Remove-Item -LiteralPath tests/unit/test_remediation_boundary.py
"""

from __future__ import annotations

import pytest
from pathlib import Path

from agent.tools.remediation import assert_within_dir


# ---------------------------------------------------------------------------
# Path-traversal cases — MUST raise ValueError (before fix these will FAIL)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "evil_path",
    [
        "/etc/passwd",
        "../../etc/passwd",
        "/tmp/../../../etc/shadow",
        "/nginx_conf/../../../etc/hosts",
    ],
    ids=[
        "absolute_etc_passwd",
        "dotdot_relative",
        "tmp_escape_to_etc_shadow",
        "nginx_conf_escape_to_etc_hosts",
    ],
)
def test_assert_within_dir_rejects_path_traversal(tmp_path: Path, evil_path: str) -> None:
    """Traversal paths must never escape the sandbox root."""
    root = tmp_path / "sandbox"
    root.mkdir()
    with pytest.raises(ValueError):
        assert_within_dir(evil_path, root)


# ---------------------------------------------------------------------------
# Happy-path cases — MUST NOT raise after fix
# ---------------------------------------------------------------------------


def test_assert_within_dir_allows_child_absolute_path(tmp_path: Path) -> None:
    """An absolute path pointing inside root resolves correctly."""
    root = tmp_path / "sandbox"
    root.mkdir()
    child = root / "site.conf.tmp"
    child.touch()
    result = assert_within_dir(str(child), root)
    assert result == child.resolve()


def test_assert_within_dir_allows_relative_path(tmp_path: Path) -> None:
    """A bare filename (relative path) is resolved under root."""
    root = tmp_path / "sandbox"
    root.mkdir()
    child = root / "site.conf"
    child.touch()
    result = assert_within_dir("site.conf", root)
    assert result == child.resolve()


def test_assert_within_dir_allows_nested_child(tmp_path: Path) -> None:
    """A relative path with one level of nesting is allowed."""
    root = tmp_path / "sandbox"
    subdir = root / "sub"
    subdir.mkdir(parents=True)
    child = subdir / "nested.conf"
    child.touch()
    result = assert_within_dir("sub/nested.conf", root)
    assert result == child.resolve()
