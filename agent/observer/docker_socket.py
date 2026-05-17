from __future__ import annotations

import http.client
import json
import re
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode


# ---------------------------------------------------------------------------
# Unix-socket HTTP transport (existing)
# ---------------------------------------------------------------------------


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str):
        super().__init__("localhost")
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.socket_path)
        self.sock = sock


class DockerAPIError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str):
        super().__init__(f"Docker API {method} {path} failed with {status}: {body}")
        self.method = method
        self.path = path
        self.status = status
        self.body = body


class DockerSocketClient:
    def __init__(self, socket_path: str | Path = "/var/run/docker.sock", timeout_s: float = 3.0):
        self.socket_path = str(socket_path)
        self.timeout_s = timeout_s

    def list_labeled_containers(
        self,
        label: str | list[str] = "sre.demo=true",
    ) -> list[dict[str, Any]]:
        labels = [label] if isinstance(label, str) else label
        filters = quote(json.dumps({"label": labels}))
        return self._request_json("GET", f"/containers/json?all=true&filters={filters}")

    def inspect_container(self, container_id: str) -> dict[str, Any]:
        return self._request_json("GET", f"/containers/{container_id}/json")

    def find_labeled_container_by_role(self, role: str, project: str | None = None) -> str:
        labels = ["sre.demo=true"]
        if project:
            labels.append(f"com.docker.compose.project={project}")
        for container in self.list_labeled_containers(labels):
            container_id = container["Id"]
            try:
                inspect = self.inspect_container(container_id)
            except DockerAPIError as exc:
                if exc.status == 404:
                    continue
                raise
            labels = inspect.get("Config", {}).get("Labels") or {}
            if labels.get("sre.demo.role") == role:
                return container_id
        raise RuntimeError(f"no labeled sandbox container found for role={role}")

    def container_logs(self, container_id: str, tail: int = 100) -> str:
        query = urlencode({"stdout": 1, "stderr": 1, "tail": tail, "timestamps": 0})
        body = self._request("GET", f"/containers/{container_id}/logs?{query}")
        return decode_docker_log_bytes(body)

    def restart_container(self, container_id: str, timeout_s: int = 10) -> None:
        query = urlencode({"t": timeout_s})
        self._request("POST", f"/containers/{container_id}/restart?{query}")

    def create_container(
        self,
        *,
        image: str,
        name: str,
        command: list[str],
        binds: list[str],
        network_mode: str,
    ) -> str:
        payload = {
            "Image": image,
            "Cmd": command,
            "HostConfig": {
                "Binds": binds,
                "NetworkMode": network_mode,
                "AutoRemove": False,
            },
        }
        query = urlencode({"name": name})
        result = self._request_json("POST", f"/containers/create?{query}", payload)
        return result["Id"]

    def start_container(self, container_id: str) -> None:
        self._request("POST", f"/containers/{container_id}/start")

    def wait_container(self, container_id: str, timeout_s: float = 20.0) -> int:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            inspect = self.inspect_container(container_id)
            state = inspect.get("State", {})
            if not state.get("Running"):
                return int(state.get("ExitCode") or 0)
            time.sleep(0.2)
        raise TimeoutError(f"container did not exit within {timeout_s}s: {container_id}")

    def remove_container(self, container_id: str, force: bool = False) -> None:
        query = urlencode({"force": 1 if force else 0})
        try:
            self._request("DELETE", f"/containers/{container_id}?{query}")
        except DockerAPIError as exc:
            if exc.status != 404:
                raise

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = self._request(method, path, payload)
        if not body:
            return None
        return json.loads(body.decode("utf-8"))

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> bytes:
        conn = UnixHTTPConnection(self.socket_path)
        conn.timeout = self.timeout_s
        try:
            body = json.dumps(payload).encode("utf-8") if payload is not None else None
            headers = {"Content-Type": "application/json"} if payload is not None else {}
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            body = response.read()
            if response.status >= 400:
                raise DockerAPIError(
                    method,
                    path,
                    response.status,
                    body.decode("utf-8", errors="replace"),
                )
            return body
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# TCP transport — used when DOCKER_HOST is a tcp:// URL (e.g. socket proxy)
# ---------------------------------------------------------------------------

_TCP_RE = re.compile(r"^tcp://([^/:]+)(?::([0-9]+))?$")


def parse_tcp_docker_host(docker_host: str) -> tuple[str, int]:
    """Parse a ``tcp://host[:port]`` DOCKER_HOST value.

    Returns ``(host, port)``.
    Raises ``ValueError`` if the URL is not a valid tcp:// address.
    """
    m = _TCP_RE.match(docker_host.strip())
    if not m:
        raise ValueError(f"DOCKER_HOST {docker_host!r} is not a supported tcp:// address")
    host = m.group(1)
    port = int(m.group(2)) if m.group(2) else 2375
    return host, port


class DockerTCPClient(DockerSocketClient):
    """DockerSocketClient variant that dials a plain TCP endpoint.

    Used when the Docker daemon is exposed via the tecnativa/docker-socket-proxy
    (or any other HTTP-over-TCP forwarder) instead of a Unix socket.
    """

    def __init__(self, host: str, port: int = 2375, timeout_s: float = 3.0):
        # Initialise with an empty socket_path — _request is fully overridden.
        super().__init__(socket_path="", timeout_s=timeout_s)
        self.host = host
        self.port = port

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> bytes:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout_s)
        try:
            body = json.dumps(payload).encode("utf-8") if payload is not None else None
            headers = {"Content-Type": "application/json"} if payload is not None else {}
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            resp_body = response.read()
            if response.status >= 400:
                raise DockerAPIError(
                    method,
                    path,
                    response.status,
                    resp_body.decode("utf-8", errors="replace"),
                )
            return resp_body
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def decode_docker_log_bytes(body: bytes) -> str:
    if not body:
        return ""

    chunks: list[bytes] = []
    index = 0
    while index + 8 <= len(body) and body[index] in (0, 1, 2):
        size = int.from_bytes(body[index + 4 : index + 8], "big")
        next_index = index + 8 + size
        if size < 0 or next_index > len(body):
            break
        chunks.append(body[index + 8 : next_index])
        index = next_index

    if chunks and index == len(body):
        return b"".join(chunks).decode("utf-8", errors="replace")
    return body.decode("utf-8", errors="replace")
