from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    status_code: int | None
    body: dict[str, Any] | str | None
    error: str | None = None


class UrllibHealthClient:
    def get(self, url: str, timeout_s: float = 2.0) -> HealthResult:
        request = Request(url, method="GET")
        try:
            with urlopen(request, timeout=timeout_s) as response:
                raw_body = response.read().decode("utf-8", errors="replace")
                return HealthResult(
                    ok=200 <= response.status < 300,
                    status_code=response.status,
                    body=parse_jsonish(raw_body),
                )
        except HTTPError as exc:
            raw_body = exc.read().decode("utf-8", errors="replace")
            return HealthResult(
                ok=False,
                status_code=exc.code,
                body=parse_jsonish(raw_body),
                error=str(exc),
            )
        except URLError as exc:
            return HealthResult(ok=False, status_code=None, body=None, error=str(exc.reason))
        except TimeoutError as exc:
            return HealthResult(ok=False, status_code=None, body=None, error=str(exc))


def parse_jsonish(value: str) -> dict[str, Any] | str | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(parsed, dict):
        return parsed
    return value
