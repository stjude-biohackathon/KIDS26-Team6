"""Listen address and published URL for the control API daemon."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass

from . import paths

DEFAULT_BIND_HOST = "127.0.0.1"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class DaemonBind:
    """Where uvicorn listens and which base URL clients should use."""

    listen_host: str
    port: int
    public_url: str


@dataclass(frozen=True)
class NodeBrowseUrls:
    """Human-facing URLs for opening the control UI on a cluster node."""

    hostname: str
    local: str
    private: str | None
    public: str | None


def hostname_interface_ips() -> tuple[str | None, str | None]:
    """Return ``(public, private)`` IPv4s from ``hostname -I`` fields 1 and 2.

    Matches the convention used on many HPC nodes: the first address is
    reachable across the site (public / routable), the second is an internal
    address for same-node or in-firewall access.
    """

    import subprocess

    try:
        proc = subprocess.run(
            ["hostname", "-I"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    if proc.returncode != 0:
        return None, None
    parts = proc.stdout.strip().split()
    public = parts[0] if parts else None
    private = parts[1] if len(parts) > 1 else None
    return public, private


def node_browse_urls(port: int, listen_host: str) -> NodeBrowseUrls:
    """Build local / private / public browser URLs for this node and port."""

    public_ip, private_ip = hostname_interface_ips()
    hostname = socket.gethostname()
    if is_loopback_host(listen_host) or listen_host == "0.0.0.0":
        local = f"http://127.0.0.1:{port}"
    else:
        local = f"http://{listen_host}:{port}"

    private_url = f"http://{private_ip}:{port}" if private_ip else None
    public_url = f"http://{public_ip}:{port}" if public_ip else None
    return NodeBrowseUrls(
        hostname=hostname,
        local=local,
        private=private_url,
        public=public_url,
    )


def daemon_summary_rows(bind: DaemonBind) -> list[tuple[str, str]]:
    """Key-value rows for the ``wfrec daemon`` startup summary."""

    access = node_browse_urls(bind.port, bind.listen_host)
    rows: list[tuple[str, str]] = [
        ("Host", access.hostname),
        ("Bind", f"{bind.listen_host}:{bind.port}"),
    ]
    if bind.listen_host == "0.0.0.0":
        rows.append(("Listen (all interfaces)", f"http://0.0.0.0:{bind.port}"))
    rows.extend(
        [
            ("Open in browser", bind.public_url),
            ("Local", access.local),
        ]
    )
    if access.private:
        rows.append(
            (
                "Private",
                f"{access.private} (same node / internal network)",
            )
        )
    if access.public:
        rows.append(
            (
                "Public",
                f"{access.public} (other nodes / site network)",
            )
        )
    if is_loopback_host(bind.listen_host):
        rows.append(
            (
                "Remote access",
                "Use --bind-all --allow-remote for Private/Public URLs",
            )
        )
    rows.append(("Sessions", str(paths.sessions_dir())))
    return rows


def is_loopback_host(host: str) -> bool:
    return host.strip().lower() in _LOOPBACK_HOSTS


def remote_bind_allowed(*, allow_remote_flag: bool = False) -> bool:
    if allow_remote_flag:
        return True
    value = os.environ.get(paths.ENV_ALLOW_REMOTE, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def resolve_listen_host(
    host: str | None = None,
    *,
    bind_all: bool = False,
) -> str:
    if bind_all:
        return "0.0.0.0"
    if host:
        return host.strip()
    env = os.environ.get(paths.ENV_BIND_HOST, "").strip()
    return env or DEFAULT_BIND_HOST


def validate_remote_bind(listen_host: str, *, allow_remote_flag: bool = False) -> None:
    if is_loopback_host(listen_host) or listen_host == "0.0.0.0":
        if listen_host == "0.0.0.0" and not remote_bind_allowed(
            allow_remote_flag=allow_remote_flag
        ):
            raise ValueError(_REMOTE_BIND_ERROR)
        return
    if not remote_bind_allowed(allow_remote_flag=allow_remote_flag):
        raise ValueError(_REMOTE_BIND_ERROR)


_REMOTE_BIND_ERROR = (
    "Binding beyond loopback requires --allow-remote or WFREC_ALLOW_REMOTE=1. "
    "Anyone who can reach the control port can use the UI token; prefer SSH "
    "port forwarding when you can."
)


def default_public_url(listen_host: str, port: int) -> str:
    if is_loopback_host(listen_host):
        return f"http://127.0.0.1:{port}"
    if listen_host == "0.0.0.0":
        public_ip, private_ip = hostname_interface_ips()
        for candidate in (public_ip, private_ip):
            if candidate:
                return f"http://{candidate}:{port}"
        guessed = _guess_routable_ipv4()
        if guessed:
            return f"http://{guessed}:{port}"
        return f"http://127.0.0.1:{port}"
    return f"http://{listen_host}:{port}"


def _guess_routable_ipv4() -> str | None:
    try:
        hostname = socket.gethostname()
        infos = socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return None
    for info in infos:
        addr = info[4][0]
        if not addr.startswith("127."):
            return addr
    return None


def resolve_public_url(
    listen_host: str,
    port: int,
    advertise_url: str | None = None,
) -> str:
    if advertise_url:
        return advertise_url.rstrip("/")
    env = os.environ.get(paths.ENV_ADVERTISE_URL, "").strip()
    if env:
        return env.rstrip("/")
    return default_public_url(listen_host, port)


def resolve_daemon_bind(
    port: int | None = None,
    *,
    host: str | None = None,
    bind_all: bool = False,
    advertise_url: str | None = None,
    allow_remote_flag: bool = False,
    preferred_port: int = 8787,
) -> DaemonBind:
    listen_host = resolve_listen_host(host, bind_all=bind_all)
    validate_remote_bind(listen_host, allow_remote_flag=allow_remote_flag)
    chosen = port if port is not None else free_port(preferred_port, listen_host)
    public = resolve_public_url(listen_host, chosen, advertise_url)
    return DaemonBind(listen_host=listen_host, port=chosen, public_url=public)


def free_port(preferred: int, listen_host: str = DEFAULT_BIND_HOST) -> int:
    """Return ``preferred`` if bindable on ``listen_host``, else an OS-assigned port."""

    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((listen_host, candidate))
                return sock.getsockname()[1]
            except OSError:
                continue
    return preferred


def daemon_spawn_argv() -> list[str]:
    """Extra ``wfrec daemon`` arguments for auto-spawn from environment."""

    import sys

    argv = [sys.executable, "-m", "autocab.recording", "daemon"]
    bind_all = False
    host = os.environ.get(paths.ENV_BIND_HOST, "").strip()
    if host == "0.0.0.0":
        bind_all = True
        host = ""
    if bind_all:
        argv.append("--bind-all")
    elif host:
        argv.extend(["--host", host])
    advertise = os.environ.get(paths.ENV_ADVERTISE_URL, "").strip()
    if advertise:
        argv.extend(["--advertise-url", advertise])
    if remote_bind_allowed():
        argv.append("--allow-remote")
    return argv
