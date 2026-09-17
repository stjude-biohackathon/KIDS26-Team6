"""Tests for optional non-loopback daemon bind."""

from __future__ import annotations

import pytest

from wfrec.bind import (
    DaemonBind,
    daemon_summary_rows,
    default_public_url,
    node_browse_urls,
    remote_bind_allowed,
    resolve_daemon_bind,
    resolve_listen_host,
    validate_remote_bind,
)
from wfrec.paths import ENV_ALLOW_REMOTE, ENV_ADVERTISE_URL, ENV_BIND_HOST


def test_resolve_listen_host_defaults() -> None:
    assert resolve_listen_host() == "127.0.0.1"
    assert resolve_listen_host(bind_all=True) == "0.0.0.0"
    assert resolve_listen_host(host="10.0.0.2") == "10.0.0.2"


def test_validate_remote_bind_requires_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_ALLOW_REMOTE, raising=False)
    validate_remote_bind("127.0.0.1")
    with pytest.raises(ValueError, match="--allow-remote"):
        validate_remote_bind("0.0.0.0")
    with pytest.raises(ValueError, match="--allow-remote"):
        validate_remote_bind("10.0.0.2")


def test_validate_remote_bind_with_flag() -> None:
    validate_remote_bind("0.0.0.0", allow_remote_flag=True)


def test_validate_remote_bind_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_ALLOW_REMOTE, "1")
    validate_remote_bind("0.0.0.0")


def test_default_public_url_loopback() -> None:
    assert default_public_url("127.0.0.1", 8787) == "http://127.0.0.1:8787"


def test_resolve_daemon_bind_advertise_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_ALLOW_REMOTE, "1")
    bind = resolve_daemon_bind(
        9999,
        bind_all=True,
        advertise_url="http://node.example:9999",
        allow_remote_flag=True,
    )
    assert bind == DaemonBind(
        listen_host="0.0.0.0",
        port=9999,
        public_url="http://node.example:9999",
    )


def test_resolve_daemon_bind_env_advertise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_ALLOW_REMOTE, "1")
    monkeypatch.setenv(ENV_ADVERTISE_URL, "http://hpc.local:8787")
    bind = resolve_daemon_bind(
        8787,
        bind_all=True,
        allow_remote_flag=True,
    )
    assert bind.public_url == "http://hpc.local:8787"


def test_resolve_listen_host_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_BIND_HOST, "0.0.0.0")
    assert resolve_listen_host() == "0.0.0.0"


def test_remote_bind_allowed_flag() -> None:
    assert remote_bind_allowed(allow_remote_flag=True) is True


def test_node_browse_urls_from_ips() -> None:
    access = node_browse_urls(8787, "127.0.0.1")
    assert access.local == "http://127.0.0.1:8787"


def test_daemon_summary_rows_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "wfrec.bind.hostname_interface_ips",
        lambda: ("10.220.17.6", "192.168.186.6"),
    )
    monkeypatch.setattr("wfrec.bind.socket.gethostname", lambda: "noderome106")
    bind = DaemonBind(listen_host="127.0.0.1", port=8787, public_url="http://127.0.0.1:8787")
    rows = dict(daemon_summary_rows(bind))
    assert rows["Host"] == "noderome106"
    assert rows["Local"] == "http://127.0.0.1:8787"
    assert "http://192.168.186.6:8787" in rows["Private"]
    assert "http://10.220.17.6:8787" in rows["Public"]
    assert "bind-all" in rows["Remote access"].lower()


def test_daemon_summary_rows_bind_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "wfrec.bind.hostname_interface_ips",
        lambda: ("10.220.17.6", "192.168.186.6"),
    )
    monkeypatch.setattr("wfrec.bind.socket.gethostname", lambda: "noderome106")
    bind = DaemonBind(
        listen_host="0.0.0.0",
        port=8787,
        public_url="http://10.220.17.6:8787",
    )
    rows = dict(daemon_summary_rows(bind))
    assert rows["Listen (all interfaces)"] == "http://0.0.0.0:8787"
    assert rows["Public"].startswith("http://10.220.17.6:8787")
    assert "Remote access" not in rows
