"""
Step 1C — Integration regression guard for the assert_within_dir security boundary.

This file tests the tool layer directly (no Docker Compose needed) so it runs
in any CI environment that has the package installed.  It is NOT decorated with
require_docker() — the docker-compose stack is NOT started.

Purpose: Catch any future regression where the sandbox guard is accidentally
removed or weakened by a refactor.

Rollback: Remove-Item -LiteralPath tests/integration/test_boundary_guard.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.remediation import assert_within_dir


# ---------------------------------------------------------------------------
# Traversal must be rejected at the tool layer
# ---------------------------------------------------------------------------


def test_agent_tool_rejects_write_outside_nginx_volume() -> None:
    """
    Simulates the attacker supplying /etc/passwd as a target path to any
    remediation tool that calls assert_within_dir(path, nginx_conf_dir()).

    The /nginx_conf volume root is used as the sandbox boundary; no Docker
    container is started for this check — we exercise the Python guard only.
    """
    with pytest.raises(ValueError):
        assert_within_dir("/etc/passwd", Path("/nginx_conf"))


def test_agent_tool_rejects_dotdot_escape() -> None:
    """Relative path escaping the sandbox via .. components must be rejected."""
    with pytest.raises(ValueError):
        assert_within_dir("../../etc/shadow", Path("/nginx_conf"))


def test_agent_tool_allows_legitimate_conf_path(tmp_path: Path) -> None:
    """A valid, in-bound conf path must NOT be rejected."""
    root = tmp_path / "nginx_conf"
    root.mkdir()
    target = root / "site.conf"
    target.touch()
    result = assert_within_dir(str(target), root)
    assert result == target.resolve()
