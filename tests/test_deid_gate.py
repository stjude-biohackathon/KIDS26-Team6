"""The five-layer gate. All 2^5 layer combinations per egress class.

The exhaustive sweep is the point: **exactly one** combination may allow, per
class. Spot-checking a gate leaves the combination nobody thought of, and the
combination nobody thought of is the one that ships.

Plus the things that are not layers but will refuse anyway: per-provider ack
isolation, expiry, `deny_egress_classes`, `deny_hosts`, `allowed_endpoints`,
char budgets, the mandatory local tier, and plaintext off-box.
"""

from __future__ import annotations

import itertools
import json
from datetime import date

import pytest

from autocab.deid.config import ConfigRejected, DeidConfig, LlmConfig, load, scan_for_credentials
from autocab.deid.egress import EgressClass, classify
from autocab.deid.llm_gate import (
    NONINTERACTIVE_ENV,
    OFFBOX_FLAG,
    AckInvalid,
    confirmation_required,
    evaluate,
    load_ack,
    required_layers,
)

TODAY = date(2026, 9, 17)

HOSTS = {
    "localhost": ["127.0.0.1"],
    "vllm.internal": ["10.0.0.7"],
    "api.vendor.test": ["8.8.8.8"],
    "proxy.example": ["8.8.8.8"],
}


def resolver(host, port=None):
    if host not in HOSTS:
        raise OSError(f"unknown host {host}")
    return list(HOSTS[host])


def ack_payload(provider="anthropic", **overrides):
    payload = {
        "provider": provider,
        "org": "Example Children's Research Hospital",
        "baa_confirmed": True,
        "zero_data_retention_confirmed": True,
        "approver_name": "R. Approver",
        "approver_role": "Privacy Officer",
        "approved_at": "2026-01-05",
        "policy_reference": "IRB-2026-0042 / SEC-118",
        "expires_at": "2027-01-05",
        "allowed_endpoints": ["api.vendor.test", "localhost:11434", "10.0.0.7:8000"],
        "deny_hosts": [],
        "deny_egress_classes": [],
        "internal_endpoints_approved": True,
    }
    payload.update(overrides)
    return payload


def write_ack(home, provider="anthropic", **overrides):
    directory = home / "policy"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"deid-llm-{provider}.json"
    path.write_text(json.dumps(ack_payload(provider, **overrides), indent=2), encoding="utf-8")
    return path


def egress_for(klass, env=None, *, plaintext=False):
    """The default `internal` endpoint is **https**.

    Plaintext to an off-box endpoint is its own refusal predicate, so an http
    internal URL would make every `internal` combination refuse and the
    exhaustive sweep would prove nothing. The plaintext predicate gets its own
    test via `plaintext=True`.
    """

    url = {
        EgressClass.NONE: "http://localhost:11434",
        EgressClass.INTERNAL: "https://vllm.internal:8000/v1",
        EgressClass.EXTERNAL: "https://api.vendor.test/v1",
    }[klass]
    if plaintext:
        url = url.replace("https://", "http://")
    result = classify(url, resolver=resolver, env=env or {})
    assert result.egress_class is klass
    return result


# --------------------------------------------------------------------------
# the exhaustive sweep
# --------------------------------------------------------------------------
@pytest.mark.parametrize("klass", list(EgressClass))
def test_exactly_one_layer_combination_allows_per_egress_class(klass, tmp_path):
    """2^5 combinations, one allow. Per class."""

    egress = egress_for(klass)
    needed = required_layers(klass)
    allowed_combinations = []

    for bits in itertools.product((False, True), repeat=5):
        layer1, layer2, layer3, layer4, layer5 = bits
        ack = None
        if layer2:
            write_ack(tmp_path, provider="anthropic")
            ack = load_ack("anthropic", home=tmp_path)

        decision = evaluate(
            provider="anthropic",
            egress=egress,
            config=DeidConfig(llm=LlmConfig(enabled=layer3, source="test")),
            ack=ack,
            sdk_available=layer1,
            sdk_reason="install the deid-anthropic extra",
            invocation_llm=layer4,
            invocation_offbox=layer4,
            can_write_consent=layer5,
            local_tier_complete=True,
            model_requested="test-model",
            session_id="s-1",
            today=TODAY,
            env={},
        )
        if decision.allowed:
            allowed_combinations.append(bits)

    # The single allowing combination is "every *required* layer satisfied";
    # the layers that are not required for this class are free.
    expected = [
        bits
        for bits in itertools.product((False, True), repeat=5)
        if all(bits[index - 1] for index in needed)
    ]

    assert allowed_combinations == expected
    assert len(allowed_combinations) == 2 ** (5 - len(needed))


def test_class_none_needs_only_the_install_and_the_consent_event():
    assert required_layers(EgressClass.NONE) == {1, 5}
    assert required_layers(EgressClass.INTERNAL) == {1, 2, 3, 4, 5}
    assert required_layers(EgressClass.EXTERNAL) == {1, 2, 3, 4, 5}


def test_a_loopback_call_still_writes_a_consent_event():
    """ "Which model saw this session" is an audit question independent of
    whether anything left the machine."""

    decision = evaluate(
        provider="ollama",
        egress=egress_for(EgressClass.NONE),
        config=DeidConfig(llm=LlmConfig(enabled=False)),
        sdk_available=False,
        invocation_llm=False,
        can_write_consent=True,
        local_tier_complete=True,
        model_requested="llama3.1:8b",
        session_id="s-1",
        today=TODAY,
        env={},
    )

    assert decision.allowed
    assert decision.consent_payload["egress_class"] == "none"
    assert decision.consent_payload["model_requested"] == "llama3.1:8b"
    assert decision.consent_payload["session_id"] == "s-1"


def test_the_decision_lists_every_failed_layer_at_once():
    """Non-short-circuiting. Fixing one round-trip at a time hides the shape of
    what is being asked for."""

    decision = evaluate(
        provider="openai",
        egress=egress_for(EgressClass.EXTERNAL),
        config=DeidConfig(llm=LlmConfig(enabled=False)),
        ack=None,
        sdk_available=False,
        sdk_reason="No module named 'openai'",
        invocation_llm=False,
        invocation_offbox=False,
        can_write_consent=False,
        local_tier_complete=False,
        session_id="s-1",
        today=TODAY,
        env={},
    )
    rendered = decision.render()

    assert not decision.allowed
    assert sum(1 for layer in decision.layers if layer.required and not layer.satisfied) == 5
    for number in (1, 2, 3, 4, 5):
        assert f"layer {number}" in rendered
    assert "No module named 'openai'" in rendered
    assert "local tier" in rendered


# --------------------------------------------------------------------------
# ollama is the deliberate layer-1 exception
# --------------------------------------------------------------------------
def test_ollama_needs_no_install_extra():
    """Gating an install on a transport that reaches only loopback buys nothing.

    Its containment is `allowed_endpoints` plus the egress classifier -- both of
    which still apply, as the next test shows.
    """

    decision = evaluate(
        provider="ollama",
        egress=egress_for(EgressClass.NONE),
        sdk_available=False,
        invocation_llm=True,
        can_write_consent=True,
        local_tier_complete=True,
        today=TODAY,
        env={},
    )

    assert decision.allowed
    layer1 = decision.layers[0]
    assert layer1.required is False and layer1.satisfied is True


def test_ollama_under_a_proxy_is_still_fully_gated(tmp_path):
    """The smoke check the build spec calls the single most important one.

    `ALL_PROXY` set, base URL on loopback: the class is the proxy's, so all five
    layers apply and the refusal names the proxy.
    """

    env = {"ALL_PROXY": "http://proxy.example:3128"}
    egress = classify("http://localhost:11434", resolver=resolver, env=env)
    assert egress.egress_class is EgressClass.EXTERNAL

    decision = evaluate(
        provider="ollama",
        egress=egress,
        config=DeidConfig(llm=LlmConfig(enabled=True)),
        ack=None,
        sdk_available=True,
        invocation_llm=True,
        invocation_offbox=True,
        can_write_consent=True,
        local_tier_complete=True,
        today=TODAY,
        env=env,
    )

    assert not decision.allowed
    assert "proxy.example" in decision.render()
    assert decision.egress.egress_class is EgressClass.EXTERNAL


def test_a_non_loopback_base_url_does_not_inherit_loopbacks_exemption(tmp_path):
    egress = egress_for(EgressClass.INTERNAL)
    decision = evaluate(
        provider="openai",
        egress=egress,
        config=DeidConfig(llm=LlmConfig(enabled=True)),
        ack=None,
        sdk_available=True,
        invocation_llm=True,
        invocation_offbox=True,
        can_write_consent=True,
        local_tier_complete=True,
        today=TODAY,
        env={},
    )

    assert not decision.allowed
    assert any(
        layer.number == 2 and layer.required and not layer.satisfied for layer in decision.layers
    )


# --------------------------------------------------------------------------
# per-provider ack isolation
# --------------------------------------------------------------------------
def test_an_ack_for_one_provider_does_not_authorize_another(tmp_path):
    """A BAA is a contract with a named counterparty. It does not transfer."""

    write_ack(tmp_path, provider="openai")
    assert load_ack("anthropic", home=tmp_path) is None

    # And even if the wrong file is handed in, the provider mismatch refuses.
    openai_ack = load_ack("openai", home=tmp_path)
    decision = evaluate(
        provider="anthropic",
        egress=egress_for(EgressClass.EXTERNAL),
        config=DeidConfig(llm=LlmConfig(enabled=True)),
        ack=openai_ack,
        sdk_available=True,
        invocation_llm=True,
        invocation_offbox=True,
        can_write_consent=True,
        local_tier_complete=True,
        today=TODAY,
        env={},
    )

    assert not decision.allowed
    assert any("named counterparty" in reason for reason in decision.refusals)


def test_each_provider_gets_its_own_ack_file(tmp_path):
    write_ack(tmp_path, provider="anthropic")
    write_ack(tmp_path, provider="google")

    assert load_ack("anthropic", home=tmp_path) is not None
    assert load_ack("google", home=tmp_path) is not None
    assert load_ack("openai", home=tmp_path) is None


def test_an_expired_ack_refuses(tmp_path):
    write_ack(tmp_path, provider="anthropic", expires_at="2026-01-01")
    ack = load_ack("anthropic", home=tmp_path)

    assert ack.expired(today=TODAY)
    decision = evaluate(
        provider="anthropic",
        egress=egress_for(EgressClass.EXTERNAL),
        config=DeidConfig(llm=LlmConfig(enabled=True)),
        ack=ack,
        sdk_available=True,
        invocation_llm=True,
        invocation_offbox=True,
        can_write_consent=True,
        local_tier_complete=True,
        today=TODAY,
        env={},
    )

    assert not decision.allowed
    assert any("expired" in reason for reason in decision.refusals)


def test_an_unparsable_or_incomplete_ack_raises_rather_than_being_ignored(tmp_path):
    directory = tmp_path / "policy"
    directory.mkdir(parents=True)
    (directory / "deid-llm-anthropic.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(AckInvalid):
        load_ack("anthropic", home=tmp_path)

    (directory / "deid-llm-anthropic.json").write_text(
        json.dumps({"provider": "anthropic"}), encoding="utf-8"
    )
    with pytest.raises(AckInvalid) as excinfo:
        load_ack("anthropic", home=tmp_path)
    assert "must name a human and carry an expiry" in str(excinfo.value)


def test_an_ack_must_confirm_baa_and_zdr_for_external(tmp_path):
    for field in ("baa_confirmed", "zero_data_retention_confirmed"):
        write_ack(tmp_path, provider="anthropic", **{field: False})
        decision = evaluate(
            provider="anthropic",
            egress=egress_for(EgressClass.EXTERNAL),
            config=DeidConfig(llm=LlmConfig(enabled=True)),
            ack=load_ack("anthropic", home=tmp_path),
            sdk_available=True,
            invocation_llm=True,
            invocation_offbox=True,
            can_write_consent=True,
            local_tier_complete=True,
            today=TODAY,
            env={},
        )
        assert not decision.allowed, field


def test_internal_needs_internal_endpoints_approved_but_not_baa_or_zdr(tmp_path):
    write_ack(
        tmp_path,
        provider="openai",
        baa_confirmed=False,
        zero_data_retention_confirmed=False,
        internal_endpoints_approved=True,
    )
    decision = evaluate(
        provider="openai",
        egress=egress_for(EgressClass.INTERNAL),
        config=DeidConfig(llm=LlmConfig(enabled=True)),
        ack=load_ack("openai", home=tmp_path),
        sdk_available=True,
        invocation_llm=True,
        invocation_offbox=True,
        can_write_consent=True,
        local_tier_complete=True,
        today=TODAY,
        env={},
    )

    assert decision.allowed, decision.render()

    write_ack(tmp_path, provider="openai", internal_endpoints_approved=False)
    refused = evaluate(
        provider="openai",
        egress=egress_for(EgressClass.INTERNAL),
        config=DeidConfig(llm=LlmConfig(enabled=True)),
        ack=load_ack("openai", home=tmp_path),
        sdk_available=True,
        invocation_llm=True,
        invocation_offbox=True,
        can_write_consent=True,
        local_tier_complete=True,
        today=TODAY,
        env={},
    )
    assert not refused.allowed


# --------------------------------------------------------------------------
# refusal predicates
# --------------------------------------------------------------------------
def allowed_decision(tmp_path, provider="anthropic", klass=EgressClass.EXTERNAL, **overrides):
    write_ack(tmp_path, provider=provider, **overrides.pop("ack_overrides", {}))
    kwargs = dict(
        provider=provider,
        egress=egress_for(klass),
        config=DeidConfig(llm=LlmConfig(enabled=True)),
        ack=load_ack(provider, home=tmp_path),
        sdk_available=True,
        invocation_llm=True,
        invocation_offbox=True,
        can_write_consent=True,
        local_tier_complete=True,
        today=TODAY,
        env={},
    )
    kwargs.update(overrides)
    return evaluate(**kwargs)


def test_the_happy_path_actually_allows(tmp_path):
    assert allowed_decision(tmp_path).allowed


def test_deny_egress_classes_holds_for_every_provider_and_future_flag(tmp_path):
    """ "Loopback only, ever" written once."""

    decision = allowed_decision(
        tmp_path, ack_overrides={"deny_egress_classes": ["external", "internal"]}
    )

    assert not decision.allowed
    assert any("deny_egress_classes" in reason for reason in decision.refusals)


def test_deny_hosts_refuses(tmp_path):
    decision = allowed_decision(tmp_path, ack_overrides={"deny_hosts": ["api.vendor.test"]})

    assert not decision.allowed
    assert any("deny_hosts" in reason for reason in decision.refusals)


def test_an_endpoint_outside_allowed_endpoints_refuses(tmp_path):
    decision = allowed_decision(
        tmp_path, ack_overrides={"allowed_endpoints": ["other.vendor.test"]}
    )

    assert not decision.allowed
    assert any("allowed_endpoints" in reason for reason in decision.refusals)


def test_allowed_endpoints_is_matched_against_the_proxy_not_the_endpoint(tmp_path):
    """Otherwise `allowed_endpoints: ["localhost:11434"]` would authorize egress
    to an arbitrary proxy."""

    env = {"ALL_PROXY": "http://proxy.example:3128"}
    egress = classify("http://localhost:11434", resolver=resolver, env=env)
    write_ack(tmp_path, provider="ollama", allowed_endpoints=["localhost:11434"])

    decision = evaluate(
        provider="ollama",
        egress=egress,
        config=DeidConfig(llm=LlmConfig(enabled=True)),
        ack=load_ack("ollama", home=tmp_path),
        sdk_available=True,
        invocation_llm=True,
        invocation_offbox=True,
        can_write_consent=True,
        local_tier_complete=True,
        today=TODAY,
        env=env,
    )

    assert not decision.allowed
    assert any("allowed_endpoints" in reason for reason in decision.refusals)


def test_the_local_tier_must_have_completed(tmp_path):
    """Mandatory and non-configurable.

    Without it the disclosure is larger, a failed call can leave a session with
    no de-identification at all, and the "measured recall in CI" claim
    evaporates.
    """

    decision = allowed_decision(tmp_path, local_tier_complete=False)

    assert not decision.allowed
    assert any("not configurable" in reason for reason in decision.refusals)


def test_a_char_budget_is_enforced_from_both_the_ack_and_the_config(tmp_path):
    from_ack = allowed_decision(
        tmp_path,
        ack_overrides={"max_chars_per_session": 1000},
        chars_to_send=600,
        chars_sent_so_far=500,
    )
    assert not from_ack.allowed
    assert any("max_chars_per_session 1000" in reason for reason in from_ack.refusals)

    from_config = allowed_decision(
        tmp_path,
        config=DeidConfig(llm=LlmConfig(enabled=True, max_chars_per_session=100)),
        chars_to_send=200,
    )
    assert not from_config.allowed


def test_plaintext_to_a_non_loopback_endpoint_refuses_unless_allowed(tmp_path):
    write_ack(tmp_path, provider="openai")
    common = dict(
        provider="openai",
        egress=egress_for(EgressClass.INTERNAL, plaintext=True),
        config=DeidConfig(llm=LlmConfig(enabled=True)),
        ack=load_ack("openai", home=tmp_path),
        sdk_available=True,
        invocation_llm=True,
        invocation_offbox=True,
        can_write_consent=True,
        local_tier_complete=True,
        today=TODAY,
        env={},
    )

    refused = evaluate(**common, allow_plaintext=False)
    assert not refused.allowed
    assert any("plaintext" in reason for reason in refused.refusals)

    permitted = evaluate(**common, allow_plaintext=True)
    assert permitted.allowed


def test_plaintext_loopback_is_never_flagged():
    decision = evaluate(
        provider="ollama",
        egress=egress_for(EgressClass.NONE),
        sdk_available=True,
        invocation_llm=True,
        can_write_consent=True,
        local_tier_complete=True,
        today=TODAY,
        env={},
    )

    assert decision.allowed
    assert not any("plaintext" in reason for reason in decision.refusals)


def test_dns_failure_refuses_and_says_why():
    egress = classify("https://nonexistent.test/v1", resolver=resolver, env={})
    decision = evaluate(
        provider="anthropic",
        egress=egress,
        sdk_available=True,
        invocation_llm=True,
        invocation_offbox=True,
        can_write_consent=True,
        local_tier_complete=True,
        today=TODAY,
        env={},
    )

    assert not decision.allowed
    assert any("did not resolve" in reason for reason in decision.refusals)


# --------------------------------------------------------------------------
# the consent payload
# --------------------------------------------------------------------------
def test_the_consent_payload_names_the_ack_that_authorized_this_egress(tmp_path):
    path = write_ack(tmp_path, provider="anthropic")
    decision = allowed_decision(
        tmp_path, chars_to_send=41_000, records_to_send=200, model_requested="claude-sonnet-5"
    )
    payload = decision.consent_payload

    import hashlib

    assert payload["ack_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert payload["approver_name"] == "R. Approver"
    assert payload["policy_reference"] == "IRB-2026-0042 / SEC-118"
    assert payload["egress_class"] == "external"
    assert payload["chars_to_send"] == 41_000
    assert payload["records_to_send"] == 200
    assert payload["model_requested"] == "claude-sonnet-5"


def test_the_consent_payload_carries_no_credential_or_full_url(tmp_path):
    decision = allowed_decision(tmp_path)
    blob = json.dumps(decision.consent_payload)

    assert "sk-ant-" not in blob
    assert "/v1" not in blob, "host only -- a URL path or query can carry a token"


def test_prompt_caching_defaults_off_and_is_recorded():
    assert LlmConfig().prompt_cache is False
    assert LlmConfig().refusal_fallbacks is False


# --------------------------------------------------------------------------
# interactive confirmation -- a guard, not a layer
# --------------------------------------------------------------------------
def test_confirmation_is_required_off_box_and_needs_both_bypasses(tmp_path):
    """A script must not inherit the bypass from a stray `--yes`."""

    decision = allowed_decision(tmp_path)

    assert confirmation_required(decision, yes=False, env={})
    assert confirmation_required(decision, yes=True, env={})
    assert confirmation_required(decision, yes=False, env={NONINTERACTIVE_ENV: "1"})
    assert not confirmation_required(decision, yes=True, env={NONINTERACTIVE_ENV: "1"})


def test_confirmation_is_not_required_for_a_loopback_call():
    decision = evaluate(
        provider="ollama",
        egress=egress_for(EgressClass.NONE),
        sdk_available=True,
        invocation_llm=True,
        can_write_consent=True,
        local_tier_complete=True,
        today=TODAY,
        env={},
    )

    assert not confirmation_required(decision, yes=False, env={})


def test_the_offbox_flag_is_verbose_on_purpose():
    assert OFFBOX_FLAG == "--i-am-sending-text-offbox"


# --------------------------------------------------------------------------
# credentials are never a layer, and never in a config file
# --------------------------------------------------------------------------
def test_a_config_carrying_a_key_by_name_is_hard_rejected(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[deid.llm]\nenabled = true\napi_key = "whatever"\n', encoding="utf-8")

    with pytest.raises(ConfigRejected) as excinfo:
        load(path)

    assert "api_key" in str(excinfo.value)
    assert "no key field" in str(excinfo.value)


@pytest.mark.parametrize(
    "shape",
    [
        "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "sk-proj-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        "sk-CCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
        "AIzaDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD",
    ],
)
def test_a_key_pasted_under_an_innocuous_name_is_still_caught(tmp_path, shape):
    path = tmp_path / "config.toml"
    path.write_text(f'[deid.llm]\nendpoint_suffix = "{shape}"\n', encoding="utf-8")

    with pytest.raises(ConfigRejected):
        load(path)


def test_credential_scanning_is_recursive(tmp_path):
    problems = scan_for_credentials({"a": {"b": {"c": [{"auth_token": "x"}]}}})

    assert problems and "auth_token" in problems[0]


def test_a_clean_config_loads(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[deid]\nprofile = 'strict'\n\n[deid.llm]\nenabled = true\n"
        "provider = 'ollama'\nbase_url = 'http://localhost:11434'\nmodel = 'llama3.1:8b'\n",
        encoding="utf-8",
    )
    config = load(path)

    assert config.profile == "strict"
    assert config.llm.enabled is True
    assert config.llm.provider == "ollama"
    assert config.llm.prompt_cache is False


def test_a_missing_config_is_not_an_error(tmp_path):
    config = load(tmp_path / "nope.toml")

    assert config.llm.enabled is False
    assert config.exists is False


def test_credential_presence_is_not_a_gate_layer(tmp_path, monkeypatch):
    """An unset `ANTHROPIC_API_KEY` does not mean there are no credentials.

    The SDK also resolves `ANTHROPIC_AUTH_TOKEN`, a login profile under
    `~/.config/anthropic/`, and workload-identity variables. Gating on key
    availability would let ambient credentials silently arm egress -- so the
    gate's decision must be identical with and without a key in the
    environment.
    """

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    without = allowed_decision(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-" + "Z" * 30)
    with_key = allowed_decision(tmp_path)

    assert without.allowed is with_key.allowed is True
    assert [layer.satisfied for layer in without.layers] == [
        layer.satisfied for layer in with_key.layers
    ]
    assert "sk-ant-" not in json.dumps(with_key.consent_payload)
    assert "sk-ant-" not in with_key.render()
