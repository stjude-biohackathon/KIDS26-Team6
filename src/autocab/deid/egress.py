"""Where do the bytes actually go?

**Egress class comes from the resolved address, not the provider name.** A
provider label carries no information about where text travels: ``openai`` with
a ``base_url`` of ``http://localhost:8000/v1`` is a vLLM server on the same
machine, and ``ollama`` behind ``ALL_PROXY`` egresses every byte to a corporate
proxy. Only the address can tell you which.

Six rules, in rough order of how easy they are to get wrong:

1. **Classify the proxy, not the endpoint.** If ``HTTP_PROXY`` / ``HTTPS_PROXY``
   / ``ALL_PROXY`` is set and the endpoint is not matched by ``NO_PROXY``, the
   text goes to the **proxy**. Without this rule
   ``--llm-base-url http://localhost:11434`` under ``ALL_PROXY`` classifies as
   ``none`` while egressing every byte. This is the easiest bypass in the design
   to miss and the single most important function in this module.
2. **Escalate on ambiguity.** Mixed A records, DNS failure, or any non-loopback
   in the resolved set yields the most restrictive class present. Classifying
   off record zero is the natural implementation and it is unsafe.
3. **Pin the resolution.** :func:`classify` returns the addresses it resolved so
   the caller can hand them to the HTTP client, and :func:`reverify` re-resolves
   immediately before the request and aborts on change. Honest limit: this
   closes the accidental case -- a short TTL flipping between check and call --
   not an attacker who controls both DNS and the box.
4. **``::ffff:127.0.0.1`` is loopback; ``0.0.0.0`` and ``[::]`` are not.** Both
   directions are routinely gotten wrong. The mapped form really is the loopback
   interface. The unspecified addresses mean "every interface", which is the
   opposite of containment.
5. **Plaintext to non-loopback is refused** unless explicitly allowed. Loopback
   over ``http`` is completely fine and must never be nagged about.
6. **The resolver is injected.** No network anywhere in this module, so the
   whole matrix is testable offline -- which is why there *is* a whole matrix.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Mapping, Sequence
from urllib.parse import urlsplit

#: ``(host, port) -> list of address strings``. Injected everywhere.
Resolver = Callable[[str, "int | None"], Sequence[str]]


class EgressClass(str, Enum):
    """How far the text travels. Ordered least to most restrictive gate."""

    NONE = "none"
    """Unix socket, or **every** resolved address is loopback. No egress gate --
    but a consent event is still written, because "which model saw this session"
    is an audit question independent of whether anything left the machine."""

    INTERNAL = "internal"
    """**Every** resolved address is private, site-local, link-local or CGNAT.
    Needs an acknowledgement carrying ``internal_endpoints_approved``; the BAA
    and zero-data-retention clauses are not required, since nothing reached a
    third party."""

    EXTERNAL = "external"
    """Any resolved address is globally routable, **or** resolution failed,
    **or** the set is mixed. All five gate layers."""


#: Most restrictive last. Used to escalate on ambiguity.
_SEVERITY = {EgressClass.NONE: 0, EgressClass.INTERNAL: 1, EgressClass.EXTERNAL: 2}

PROXY_ENV_VARS = ("all_proxy", "https_proxy", "http_proxy")
NO_PROXY_ENV_VARS = ("no_proxy", "NO_PROXY")


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A parsed provider endpoint."""

    raw: str
    scheme: str
    host: str
    port: int | None
    unix_path: str | None = None

    @property
    def is_unix_socket(self) -> bool:
        return self.unix_path is not None

    @property
    def is_plaintext(self) -> bool:
        return self.scheme in ("http", "ws")

    def host_port(self) -> str:
        return f"{self.host}:{self.port}" if self.port else self.host


def parse_endpoint(raw: str) -> Endpoint:
    """Parse a base URL or socket path.

    Accepts ``unix:///path``, ``http+unix://...``, a bare filesystem path, and
    ordinary ``http(s)://host:port`` URLs. A bare ``host:port`` with no scheme is
    treated as ``http`` -- guessing ``https`` would be the *less* safe default,
    because it would suppress the plaintext warning for a connection that is in
    fact plaintext.
    """

    value = raw.strip()
    if not value:
        raise ValueError("endpoint is empty")

    lowered = value.lower()
    if lowered.startswith(("unix://", "http+unix://", "unix:")):
        _, _, path = value.partition("://")
        if not path:
            path = value.partition(":")[2]
        return Endpoint(raw=raw, scheme="unix", host="", port=None, unix_path=path or "/")
    if value.startswith("/") or value.startswith("./"):
        return Endpoint(raw=raw, scheme="unix", host="", port=None, unix_path=value)

    if "://" not in value:
        value = "http://" + value
    parts = urlsplit(value)
    host = parts.hostname or ""
    if not host:
        raise ValueError(f"endpoint {raw!r} has no host")
    return Endpoint(raw=raw, scheme=(parts.scheme or "http").lower(), host=host, port=parts.port)


@dataclass(frozen=True, slots=True)
class Classification:
    """The result. Everything the gate and the audit need, and nothing more."""

    egress_class: EgressClass
    endpoint: Endpoint
    resolved: tuple[str, ...]
    """The addresses actually resolved, for pinning. Hand these to the client."""

    reason: str
    """Human-readable, and it names the proxy when a proxy is in play. This
    string is what a refusal message shows, so it has to be specific enough that
    somebody can act on it."""

    via_proxy: Endpoint | None = None
    resolution_failed: bool = False
    per_address: tuple[tuple[str, str], ...] = ()
    """``(address, class)`` pairs. The evidence behind an escalation."""

    @property
    def target(self) -> Endpoint:
        """Whatever the bytes actually reach: the proxy if there is one."""

        return self.via_proxy or self.endpoint

    @property
    def plaintext_offbox(self) -> bool:
        """Plaintext to something that is not loopback."""

        return self.target.is_plaintext and self.egress_class is not EgressClass.NONE

    def audit_dict(self) -> dict[str, object]:
        """**Host only, never the full URL.** A base URL can carry a path
        segment, a query string or userinfo, and a query string is where an
        access token ends up."""

        out: dict[str, object] = {
            "egress_class": self.egress_class.value,
            "endpoint_host": self.endpoint.host_port() or self.endpoint.unix_path or "",
            "scheme": self.endpoint.scheme,
            "resolved_addresses": list(self.resolved),
            "reason": self.reason,
        }
        if self.via_proxy is not None:
            out["via_proxy_host"] = self.via_proxy.host_port()
        if self.resolution_failed:
            out["resolution_failed"] = True
        return out


# --------------------------------------------------------------------------
# address classification
# --------------------------------------------------------------------------
#: The **only** networks that earn the lighter ``internal`` gate. An explicit
#: allowlist, deliberately not ``ipaddress.is_private``:
#:
#: * ``is_private`` covers a grab-bag that includes the RFC 5737 documentation
#:   ranges, ``240.0.0.0/4``, and ``255.255.255.255``. None of those is a
#:   site-local network somebody has approved, and quietly granting them the
#:   relaxed gate would be exactly the ambiguity rule 2 forbids;
#: * ``0.0.0.0`` is inside ``0.0.0.0/8`` and therefore reports ``is_private`` --
#:   so a naive check classifies "listen on every interface" as ``internal``.
#:
#: Escalate on ambiguity means: positively recognised as site-local, or
#: ``external``.
INTERNAL_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),   # IPv4 link-local
    ipaddress.ip_network("100.64.0.0/10"),    # CGNAT
    ipaddress.ip_network("fc00::/7"),         # IPv6 unique-local
    ipaddress.ip_network("fe80::/10"),        # IPv6 link-local
)


def classify_address(address: str) -> EgressClass:
    """Classify one literal address. The core of rule 4."""

    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        # Not an address at all. Escalate rather than guess.
        return EgressClass.EXTERNAL

    # `::ffff:127.0.0.1` *is* the loopback interface. Unwrap before judging.
    mapped = getattr(parsed, "ipv4_mapped", None)
    if mapped is not None:
        parsed = mapped

    if parsed.is_unspecified:
        # 0.0.0.0 / :: -- every interface, which is the opposite of containment.
        return EgressClass.EXTERNAL
    if parsed.is_loopback:
        return EgressClass.NONE
    for network in INTERNAL_NETWORKS:
        if parsed.version == network.version and parsed in network:
            return EgressClass.INTERNAL
    return EgressClass.EXTERNAL


def _as_literal(host: str) -> str | None:
    """``host`` as an IP literal, or ``None`` if it is a name.

    ``urlsplit().hostname`` already strips the brackets from ``[::1]``, so the
    value arrives in a form ``ipaddress`` accepts.
    """

    candidate = host.strip().strip("[]")
    # Drop an IPv6 zone index: `fe80::1%eth0`.
    candidate = candidate.split("%", 1)[0]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


def default_resolver(host: str, port: int | None = None) -> list[str]:
    """``socket.getaddrinfo``. The only place this module touches the network,
    and it is injectable precisely so the tests never reach it."""

    infos = socket.getaddrinfo(host, port or None, proto=socket.IPPROTO_TCP)
    seen: list[str] = []
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.append(address)
    return seen


# --------------------------------------------------------------------------
# proxy detection
# --------------------------------------------------------------------------
def _no_proxy_entries(env: Mapping[str, str]) -> list[str]:
    for name in NO_PROXY_ENV_VARS:
        raw = env.get(name)
        if raw:
            return [entry.strip() for entry in raw.split(",") if entry.strip()]
    return []


def no_proxy_matches(endpoint: Endpoint, env: Mapping[str, str]) -> bool:
    """Whether ``NO_PROXY`` exempts this endpoint.

    **Loopback is not exempted implicitly.** Many people assume it is, and that
    assumption is exactly the bypass rule 1 exists to close: under
    ``ALL_PROXY``, a request to ``http://localhost:11434`` really is sent to the
    proxy unless ``NO_PROXY`` says otherwise. ``curl`` and ``httpx`` behave the
    same way; auto-exempting here would mean classifying a request as ``none``
    while every byte left the machine.
    """

    host = endpoint.host.lower().strip("[]")
    for entry in _no_proxy_entries(env):
        if entry == "*":
            return True
        candidate = entry.lower().lstrip("[").rstrip("]")
        candidate, _, entry_port = candidate.rpartition(":")
        if not candidate or (entry_port and not entry_port.isdigit()):
            candidate, entry_port = entry.lower(), ""
        if entry_port and endpoint.port is not None and entry_port != str(endpoint.port):
            continue
        candidate = candidate.strip("[]")
        if candidate == host:
            return True
        if candidate.startswith(".") and host.endswith(candidate):
            return True
        if not candidate.startswith(".") and host.endswith("." + candidate):
            return True
        try:
            network = ipaddress.ip_network(candidate, strict=False)
        except ValueError:
            continue
        try:
            if ipaddress.ip_address(host) in network:
                return True
        except ValueError:
            continue
    return False


def proxy_for(endpoint: Endpoint, env: Mapping[str, str]) -> Endpoint | None:
    """The proxy this endpoint's traffic would traverse, if any.

    Checked case-insensitively over both the lowercase and uppercase spellings,
    because tooling is inconsistent about which it sets and honouring only one
    would leave a bypass open.
    """

    if endpoint.is_unix_socket:
        return None
    if no_proxy_matches(endpoint, env):
        return None

    scheme = endpoint.scheme
    candidates: list[str] = ["all_proxy"]
    if scheme in ("https", "wss"):
        candidates.insert(0, "https_proxy")
    else:
        candidates.insert(0, "http_proxy")

    for name in candidates:
        for spelling in (name, name.upper()):
            raw = env.get(spelling)
            if raw and raw.strip():
                try:
                    return parse_endpoint(raw.strip())
                except ValueError:
                    continue
    return None


# --------------------------------------------------------------------------
# classify
# --------------------------------------------------------------------------
def classify(
    raw_endpoint: str,
    *,
    resolver: Resolver | None = None,
    env: Mapping[str, str] | None = None,
) -> Classification:
    """Classify where text sent to ``raw_endpoint`` actually goes."""

    env = env if env is not None else os.environ
    resolver = resolver or default_resolver
    endpoint = parse_endpoint(raw_endpoint)

    if endpoint.is_unix_socket:
        return Classification(
            egress_class=EgressClass.NONE,
            endpoint=endpoint,
            resolved=(),
            reason=f"unix socket {endpoint.unix_path}: cannot leave the machine",
        )

    proxy = proxy_for(endpoint, env)
    target = proxy or endpoint

    literal = _as_literal(target.host)
    if literal is not None:
        # A literal address needs no resolution, and resolving one would be
        # worse than pointless: it introduces a DNS answer that could disagree
        # with the address the operator actually typed. Nothing can move
        # between the check and the call either, so `reverify` is free.
        worst = classify_address(literal)
        reason = f"{literal} is a literal address -- `{worst.value}`, no resolution involved"
        if proxy is not None:
            reason = (
                f"**proxy in play**: {env_proxy_note(endpoint, env)} sends traffic for "
                f"{endpoint.host_port()} to {proxy.host_port()}, so the class is the "
                f"proxy's, not the endpoint's. " + reason
            )
        return Classification(
            egress_class=worst,
            endpoint=endpoint,
            resolved=(literal,),
            reason=reason,
            via_proxy=proxy,
            per_address=((literal, worst.value),),
        )

    try:
        addresses = tuple(resolver(target.host, target.port))
    except Exception as exc:
        # Rule 2. A DNS failure tells us nothing, and "nothing" is not "local".
        detail = f"{type(exc).__name__}: {exc}"
        reason = (
            f"could not resolve {target.host}: {detail}. Classified `external` "
            "because an unresolvable host is an unknown host."
        )
        if proxy is not None:
            reason = (
                f"traffic to {endpoint.host_port()} goes via proxy "
                f"{proxy.host_port()}, which did not resolve ({detail}). " + reason
            )
        return Classification(
            egress_class=EgressClass.EXTERNAL,
            endpoint=endpoint,
            resolved=(),
            reason=reason,
            via_proxy=proxy,
            resolution_failed=True,
        )

    if not addresses:
        return Classification(
            egress_class=EgressClass.EXTERNAL,
            endpoint=endpoint,
            resolved=(),
            reason=(
                f"{target.host} resolved to no addresses; classified `external` "
                "rather than assuming anything about an empty answer"
            ),
            via_proxy=proxy,
            resolution_failed=True,
        )

    per_address = tuple((address, classify_address(address).value) for address in addresses)
    worst = max(
        (classify_address(address) for address in addresses),
        key=lambda item: _SEVERITY[item],
    )

    classes = {klass for _address, klass in per_address}
    if len(classes) > 1:
        reason = (
            f"{target.host} resolved to a mixed set ("
            + ", ".join(f"{address}={klass}" for address, klass in per_address)
            + f"); escalated to `{worst.value}`, the most restrictive class present"
        )
    else:
        reason = (
            f"{target.host} resolved to "
            + ", ".join(address for address, _ in per_address)
            + f" -- all `{worst.value}`"
        )

    if proxy is not None:
        reason = (
            f"**proxy in play**: {env_proxy_note(endpoint, env)} sends traffic for "
            f"{endpoint.host_port()} to {proxy.host_port()}, so the class is the "
            f"proxy's, not the endpoint's. " + reason
        )

    return Classification(
        egress_class=worst,
        endpoint=endpoint,
        resolved=addresses,
        reason=reason,
        via_proxy=proxy,
        per_address=per_address,
    )


def env_proxy_note(endpoint: Endpoint, env: Mapping[str, str]) -> str:
    """Which environment variable is routing this request. For the refusal text."""

    scheme_var = "https_proxy" if endpoint.scheme in ("https", "wss") else "http_proxy"
    for name in (scheme_var, "all_proxy"):
        for spelling in (name, name.upper()):
            if env.get(spelling):
                return spelling
    return "a proxy environment variable"


class EgressChanged(RuntimeError):
    """The resolution moved between the gate check and the request. Rule 3."""


def reverify(classification: Classification, *, resolver: Resolver | None = None) -> None:
    """Re-resolve and abort if anything changed.

    Called immediately before the first request. A short DNS TTL can otherwise
    downgrade the class between the check and the call; this makes that a loud
    failure rather than a silent one.
    """

    if classification.endpoint.is_unix_socket:
        return
    resolver = resolver or default_resolver
    target = classification.target
    if _as_literal(target.host) is not None:
        # Nothing to re-resolve, so nothing can have moved.
        return
    try:
        addresses = tuple(resolver(target.host, target.port))
    except Exception as exc:
        raise EgressChanged(
            f"{target.host} no longer resolves ({type(exc).__name__}: {exc}); "
            "refusing to send"
        ) from exc
    if set(addresses) != set(classification.resolved):
        raise EgressChanged(
            f"{target.host} resolved to {classification.resolved} at gate time and "
            f"{addresses} now; refusing to send. This closes the accidental "
            "short-TTL case, not an attacker controlling DNS."
        )
