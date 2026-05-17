"""
Unit tests for the docker_client() factory and TCP transport helpers.

Covers:
  - parse_tcp_docker_host: valid and invalid inputs
  - docker_client() selects DockerTCPClient when DOCKER_HOST=tcp://…
  - docker_client() selects DockerSocketClient with explicit DOCKER_SOCKET_PATH
  - docker_client() falls back to /var/run/docker.sock when both envs are absent/empty
  - DOCKER_SOCKET_PATH="" edge-case no longer silently passes an empty string
"""

from __future__ import annotations

import pytest

from agent.observer.docker_socket import DockerSocketClient, DockerTCPClient, parse_tcp_docker_host
from agent.tools.remediation import docker_client


# ---------------------------------------------------------------------------
# parse_tcp_docker_host
# ---------------------------------------------------------------------------


class TestParseTcpDockerHost:
    def test_host_and_port(self) -> None:
        host, port = parse_tcp_docker_host("tcp://docker-socket-proxy:2375")
        assert host == "docker-socket-proxy"
        assert port == 2375

    def test_host_only_defaults_to_2375(self) -> None:
        host, port = parse_tcp_docker_host("tcp://myproxy")
        assert host == "myproxy"
        assert port == 2375

    def test_custom_port(self) -> None:
        host, port = parse_tcp_docker_host("tcp://localhost:9999")
        assert host == "localhost"
        assert port == 9999

    def test_leading_trailing_whitespace_is_stripped(self) -> None:
        host, port = parse_tcp_docker_host("  tcp://proxy:2375  ")
        assert host == "proxy"
        assert port == 2375

    @pytest.mark.parametrize(
        "bad",
        [
            "unix:///var/run/docker.sock",
            "/var/run/docker.sock",
            "http://proxy:2375",
            "",
            "tcp://",  # no host
        ],
        ids=["unix_scheme", "plain_path", "http_scheme", "empty", "tcp_no_host"],
    )
    def test_invalid_raises_value_error(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_tcp_docker_host(bad)


# ---------------------------------------------------------------------------
# docker_client() factory — uses monkeypatch to control env vars
# ---------------------------------------------------------------------------


class TestDockerClientFactory:
    def test_returns_tcp_client_when_docker_host_is_tcp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DOCKER_HOST", "tcp://docker-socket-proxy:2375")
        monkeypatch.delenv("DOCKER_SOCKET_PATH", raising=False)
        client = docker_client()
        assert isinstance(client, DockerTCPClient)
        assert client.host == "docker-socket-proxy"
        assert client.port == 2375

    def test_tcp_client_inherits_socket_client_interface(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DockerTCPClient must be a subclass of DockerSocketClient (Liskov)."""
        monkeypatch.setenv("DOCKER_HOST", "tcp://proxy:2375")
        client = docker_client()
        assert isinstance(client, DockerSocketClient)

    def test_returns_unix_client_with_explicit_socket_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DOCKER_HOST", raising=False)
        monkeypatch.setenv("DOCKER_SOCKET_PATH", "/custom/docker.sock")
        client = docker_client()
        assert isinstance(client, DockerSocketClient)
        assert not isinstance(client, DockerTCPClient)
        assert client.socket_path == "/custom/docker.sock"

    def test_falls_back_to_default_unix_socket_when_no_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DOCKER_HOST", raising=False)
        monkeypatch.delenv("DOCKER_SOCKET_PATH", raising=False)
        client = docker_client()
        assert isinstance(client, DockerSocketClient)
        assert not isinstance(client, DockerTCPClient)
        assert client.socket_path == "/var/run/docker.sock"

    def test_empty_docker_socket_path_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression: DOCKER_SOCKET_PATH='' must not create DockerSocketClient('')."""
        monkeypatch.delenv("DOCKER_HOST", raising=False)
        monkeypatch.setenv("DOCKER_SOCKET_PATH", "")
        client = docker_client()
        assert isinstance(client, DockerSocketClient)
        assert not isinstance(client, DockerTCPClient)
        assert client.socket_path == "/var/run/docker.sock"

    def test_docker_host_takes_precedence_over_socket_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DOCKER_HOST wins even when DOCKER_SOCKET_PATH is also set."""
        monkeypatch.setenv("DOCKER_HOST", "tcp://proxy:2375")
        monkeypatch.setenv("DOCKER_SOCKET_PATH", "/var/run/docker.sock")
        client = docker_client()
        assert isinstance(client, DockerTCPClient)

    def test_empty_docker_host_does_not_trigger_tcp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOCKER_HOST", "")
        monkeypatch.delenv("DOCKER_SOCKET_PATH", raising=False)
        client = docker_client()
        assert not isinstance(client, DockerTCPClient)
        assert client.socket_path == "/var/run/docker.sock"
