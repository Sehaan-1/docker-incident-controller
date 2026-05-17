from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent.tools.remediation import (
    atomic_replace,
    nginx_configtest,
    render_known_good_nginx_config,
    restart_container,
    verify_health_stable,
    write_runtime_flags,
)


ToolFn = Callable[..., dict[str, Any]]


TOOL_REGISTRY: dict[str, ToolFn] = {
    "render_known_good_nginx_config": render_known_good_nginx_config,
    "nginx_configtest": nginx_configtest,
    "atomic_replace": atomic_replace,
    "restart_container": restart_container,
    "write_runtime_flags": write_runtime_flags,
    "verify_health_stable": verify_health_stable,
}


def get_tool(name: str) -> ToolFn:
    try:
        return TOOL_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"tool is not allowlisted: {name}") from exc
