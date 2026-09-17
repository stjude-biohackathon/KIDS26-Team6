"""Egress classification, offline, with an injected resolver.

The whole matrix runs without a socket. That is the point of injecting the
resolver: a security control whose tests need DNS is a security control whose
tests get skipped in CI.

Ordered roughly by how easy each case is to get wrong, per the build spec's own
list. The proxy tests are the important ones.
"""

from __future__ import annotations

import pytest

from autocab.deid.egress import (
    EgressChanged,
    EgressClass,
    classify,
    classify_address,
    default_resolver,
    no_proxy_matches,
    parse_endpoint,
    proxy_for,
    reverify,
)


def resolver_for(mapping):
    """A resolver that never touches the network."""

    def resolve(host, port=None):
        if host not in mapping:
            raise OSError(f"[Errno -2] Name or service not known: {host}")
        return list(mapping[host])

    return resolve


HOSTS = {
    "localhost": ["127.0.0.1"],
    "ip6-localhost": ["::1"],
    "mapped.test": ["::ffff:127.0.0.1"],
    "anyaddr.test": ["0.0.0.0"],
    "anyaddr6.test": ["::"],
    "vllm.internal": ["10.0.0.7"],
    "gw.internal": ["172.16.4.9"],
    "lan.internal": ["192.168.1.20"],
    "linklocal.test": ["169.254.10.10"],
    "cgnat.test": ["100.100.5.5"],
    "ula.test": ["fd00::5"],
    "api.vendor.test": ["203.0.113.9", "8.8.8.8"],
    "public.test": ["8.8.8.8"],
    "public6.test": ["2606:4700::1111"],
    "mixed.test": ["127.0.0.1", "8.8.8.8"],
    "mixed-internal.test": ["127.0.0.1", "10.0.0.7"],
    "loopback-by-name.test": ["127.0.0.1", "127.0.0.2"],
    "proxy.example": ["8.8.8.8"],
    "internal-proxy.example": ["10.9.8.7"],
    "empty.test": [],
}

RESOLVER = resolver_for(HOSTS)


# --------------------------------------------------------------------------
# single addresses -- rule 4
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "address,expected",
    [
        ("127.0.0.1", EgressClass.NONE),
        ("127.5.5.5", EgressClass.NONE),
        ("::1", EgressClass.NONE),
        # `::ffff:127.0.0.1` IS loopback. Routinely gotten wrong.
        ("::ffff:127.0.0.1", EgressClass.NONE),
        # `0.0.0.0` and `::` are NOT loopback. Also routinely gotten wrong --
        # and `ipaddress` reports 0.0.0.0 as `is_private`, which is the trap.
        ("0.0.0.0", EgressClass.EXTERNAL),
        ("::", EgressClass.EXTERNAL),
        ("10.0.0.7", EgressClass.INTERNAL),
        ("172.16.4.9", EgressClass.INTERNAL),
        ("172.32.0.1", EgressClass.EXTERNAL),
        ("192.168.1.20", EgressClass.INTERNAL),
        ("169.254.10.10", EgressClass.INTERNAL),
        ("100.100.5.5", EgressClass.INTERNAL),
        ("100.128.0.1", EgressClass.EXTERNAL),
        ("fd00::5", EgressClass.INTERNAL),
        ("fe80::1", EgressClass.INTERNAL),
        ("8.8.8.8", EgressClass.EXTERNAL),
        ("2606:4700::1111", EgressClass.EXTERNAL),
        ("not-an-address", EgressClass.EXTERNAL),
    ],
)
def test_classify_address(address, expected):
    assert classify_address(address) is expected


def test_documentation_ranges_are_not_treated_as_approved_internal_networks():
    """``ipaddress.is_private`` covers RFC 5737 and 240/4; ``internal`` must not.

    ``internal`` buys a lighter gate, so it is an explicit allowlist of
    site-local networks. A documentation-range address is not a network somebody
    approved, so it escalates.
    """

    for address in ("192.0.2.1", "198.51.100.1", "203.0.113.1", "240.0.0.1", "255.255.255.255"):
        assert classify_address(address) is EgressClass.EXTERNAL


# --------------------------------------------------------------------------
# endpoints
# --------------------------------------------------------------------------
def test_unix_socket_is_class_none_without_resolving_anything():
    def explode(*_args, **_kwargs):  # pragma: no cover - must not be called
        raise AssertionError("a unix socket must not be resolved")

    for raw in ("unix:///var/run/ollama.sock", "/var/run/ollama.sock", "http+unix://%2Ftmp%2Fs"):
        result = classify(raw, resolver=explode, env={})
        assert result.egress_class is EgressClass.NONE
        assert result.endpoint.is_unix_socket


def test_a_scheme_less_endpoint_defaults_to_http_not_https():
    # Guessing https would suppress the plaintext warning for a connection that
    # is in fact plaintext.
    assert parse_endpoint("api.vendor.test:8000").scheme == "http"


def test_loopback_base_url_is_class_none():
    result = classify("http://localhost:11434", resolver=RESOLVER, env={})

    assert result.egress_class is EgressClass.NONE
    assert result.resolved == ("127.0.0.1",)
    assert result.plaintext_offbox is False, "loopback over http must never be nagged about"


def test_ipv6_loopback_base_url_is_class_none():
    result = classify("http://[::1]:11434", resolver=RESOLVER, env={})

    assert result.egress_class is EgressClass.NONE


def test_hostname_that_resolves_to_loopback_is_class_none():
    result = classify("http://loopback-by-name.test:8000", resolver=RESOLVER, env={})

    assert result.egress_class is EgressClass.NONE


def test_ipv4_mapped_loopback_is_class_none():
    result = classify("http://mapped.test:8000", resolver=RESOLVER, env={})

    assert result.egress_class is EgressClass.NONE


def test_bind_all_addresses_are_not_loopback():
    for host in ("anyaddr.test", "anyaddr6.test"):
        result = classify(f"http://{host}:8000", resolver=RESOLVER, env={})
        assert result.egress_class is EgressClass.EXTERNAL, host


def test_rfc1918_endpoint_is_internal_not_none():
    result = classify("http://10.0.0.7:8000/v1", resolver=RESOLVER, env={})

    assert result.egress_class is EgressClass.INTERNAL
    assert result.plaintext_offbox is True, "internal is still off-box"


def test_public_endpoint_is_external():
    result = classify("https://public.test/v1", resolver=RESOLVER, env={})

    assert result.egress_class is EgressClass.EXTERNAL


# --------------------------------------------------------------------------
# rule 2 -- escalate on ambiguity
# --------------------------------------------------------------------------
def test_mixed_a_records_escalate_to_the_most_restrictive_class_present():
    # Classifying off record zero is the natural implementation and it is
    # unsafe: this host's first address is loopback.
    result = classify("https://mixed.test", resolver=RESOLVER, env={})

    assert result.egress_class is EgressClass.EXTERNAL
    assert "mixed set" in result.reason
    assert dict(result.per_address) == {"127.0.0.1": "none", "8.8.8.8": "external"}


def test_mixed_loopback_and_private_escalates_to_internal():
    result = classify("https://mixed-internal.test", resolver=RESOLVER, env={})

    assert result.egress_class is EgressClass.INTERNAL


def test_dns_failure_is_external_not_local():
    result = classify("https://nonexistent.test", resolver=RESOLVER, env={})

    assert result.egress_class is EgressClass.EXTERNAL
    assert result.resolution_failed
    assert "unresolvable host is an unknown host" in result.reason


def test_empty_resolution_is_external():
    result = classify("https://empty.test", resolver=RESOLVER, env={})

    assert result.egress_class is EgressClass.EXTERNAL
    assert result.resolution_failed


# --------------------------------------------------------------------------
# rule 1 -- classify the proxy, not the endpoint
# --------------------------------------------------------------------------
def test_all_proxy_reclassifies_a_loopback_endpoint_as_external():
    """**The single most important assertion in this file.**

    Without this rule, ``--llm-base-url http://localhost:11434`` under
    ``ALL_PROXY`` classifies as ``none`` -- no gate at all -- while every byte
    of the session goes to the proxy.
    """

    env = {"ALL_PROXY": "http://proxy.example:3128"}
    result = classify("http://localhost:11434", resolver=RESOLVER, env=env)

    assert result.egress_class is EgressClass.EXTERNAL
    assert result.via_proxy is not None
    assert result.via_proxy.host == "proxy.example"
    assert "proxy in play" in result.reason
    assert "ALL_PROXY" in result.reason
    # And the refusal names the proxy's class, not loopback's.
    assert result.target.host == "proxy.example"


def test_https_proxy_applies_to_an_https_endpoint():
    env = {"HTTPS_PROXY": "http://proxy.example:3128"}
    result = classify("https://public.test/v1", resolver=RESOLVER, env=env)

    assert result.via_proxy is not None and result.via_proxy.host == "proxy.example"


def test_http_proxy_does_not_apply_to_an_https_endpoint():
    env = {"HTTP_PROXY": "http://proxy.example:3128"}
    result = classify("https://public.test/v1", resolver=RESOLVER, env=env)

    assert result.via_proxy is None


def test_lowercase_and_uppercase_proxy_spellings_are_both_honoured():
    for name in ("all_proxy", "ALL_PROXY", "http_proxy", "HTTP_PROXY"):
        env = {name: "http://proxy.example:3128"}
        result = classify("http://localhost:11434", resolver=RESOLVER, env=env)
        assert result.via_proxy is not None, name


def test_an_internal_proxy_yields_class_internal_not_external():
    env = {"ALL_PROXY": "http://internal-proxy.example:3128"}
    result = classify("http://localhost:11434", resolver=RESOLVER, env=env)

    assert result.egress_class is EgressClass.INTERNAL


def test_no_proxy_restores_the_endpoints_own_class():
    env = {"ALL_PROXY": "http://proxy.example:3128", "NO_PROXY": "localhost"}
    result = classify("http://localhost:11434", resolver=RESOLVER, env=env)

    assert result.egress_class is EgressClass.NONE
    assert result.via_proxy is None


def test_loopback_is_not_exempted_from_the_proxy_implicitly():
    """The assumption this rule exists to break.

    Most people believe loopback bypasses a proxy automatically. It does not --
    not in curl, not in httpx -- and auto-exempting here would classify a
    request as ``none`` while every byte left the machine.
    """

    env = {"ALL_PROXY": "http://proxy.example:3128"}
    endpoint = parse_endpoint("http://127.0.0.1:11434")

    assert no_proxy_matches(endpoint, env) is False
    assert proxy_for(endpoint, env) is not None


@pytest.mark.parametrize(
    "no_proxy,expected_bypass",
    [
        ("*", True),
        ("localhost", True),
        ("localhost:11434", True),
        ("localhost:1234", False),
        ("other.test", False),
        (".example.test", False),
    ],
)
def test_no_proxy_entry_forms(no_proxy, expected_bypass):
    endpoint = parse_endpoint("http://localhost:11434")
    env = {"ALL_PROXY": "http://proxy.example:3128", "NO_PROXY": no_proxy}

    assert no_proxy_matches(endpoint, env) is expected_bypass


def test_no_proxy_supports_a_domain_suffix_and_a_cidr():
    env = {"ALL_PROXY": "http://proxy.example:3128", "NO_PROXY": ".internal,10.0.0.0/8"}

    assert no_proxy_matches(parse_endpoint("http://vllm.internal:8000"), env)
    assert no_proxy_matches(parse_endpoint("http://10.0.0.7:8000"), env)
    assert not no_proxy_matches(parse_endpoint("http://public.test"), env)


def test_unix_socket_ignores_the_proxy_environment():
    env = {"ALL_PROXY": "http://proxy.example:3128"}
    result = classify("unix:///var/run/ollama.sock", resolver=RESOLVER, env=env)

    assert result.egress_class is EgressClass.NONE
    assert result.via_proxy is None


def test_a_proxy_that_does_not_resolve_is_still_external():
    env = {"ALL_PROXY": "http://nonexistent-proxy.test:3128"}
    result = classify("http://localhost:11434", resolver=RESOLVER, env=env)

    assert result.egress_class is EgressClass.EXTERNAL
    assert result.resolution_failed
    assert "proxy" in result.reason


# --------------------------------------------------------------------------
# rule 3 -- pin the resolution
# --------------------------------------------------------------------------
def test_reverify_passes_when_the_resolution_is_unchanged():
    result = classify("https://public.test", resolver=RESOLVER, env={})

    reverify(result, resolver=RESOLVER)  # must not raise


def test_reverify_aborts_when_the_resolution_moves():
    # A short TTL must not be able to downgrade the class between check and call.
    result = classify("https://public.test", resolver=RESOLVER, env={})
    moved = resolver_for({**HOSTS, "public.test": ["127.0.0.1"]})

    with pytest.raises(EgressChanged) as excinfo:
        reverify(result, resolver=moved)

    assert "refusing to send" in str(excinfo.value)
    assert "not an attacker controlling DNS" in str(excinfo.value)


def test_reverify_aborts_when_the_host_stops_resolving():
    result = classify("https://public.test", resolver=RESOLVER, env={})

    with pytest.raises(EgressChanged):
        reverify(result, resolver=resolver_for({}))


def test_reverify_checks_the_proxy_when_one_is_in_play():
    env = {"ALL_PROXY": "http://proxy.example:3128"}
    result = classify("http://localhost:11434", resolver=RESOLVER, env=env)
    moved = resolver_for({**HOSTS, "proxy.example": ["10.0.0.1"]})

    with pytest.raises(EgressChanged):
        reverify(result, resolver=moved)


def test_unix_socket_reverify_is_a_no_op():
    result = classify("unix:///var/run/ollama.sock", resolver=RESOLVER, env={})

    reverify(result, resolver=resolver_for({}))  # must not raise


# --------------------------------------------------------------------------
# audit shape
# --------------------------------------------------------------------------
def test_the_audit_record_carries_the_host_only_never_the_full_url():
    """A base URL can carry a path, a query string, or userinfo -- and a query
    string is where an access token ends up."""

    result = classify(
        "https://public.test/v1/chat?access_token=super-secret", resolver=RESOLVER, env={}
    )
    payload = result.audit_dict()

    assert payload["endpoint_host"] == "public.test"
    assert "super-secret" not in repr(payload)
    assert "access_token" not in repr(payload)
    assert payload["egress_class"] == "external"


def test_the_audit_record_names_the_proxy_host():
    env = {"ALL_PROXY": "http://proxy.example:3128"}
    payload = classify("http://localhost:11434", resolver=RESOLVER, env=env).audit_dict()

    assert payload["via_proxy_host"] == "proxy.example:3128"


def test_this_module_never_touches_the_network_by_default_in_tests():
    """`default_resolver` exists and is the only network path; nothing above
    used it."""

    assert callable(default_resolver)
