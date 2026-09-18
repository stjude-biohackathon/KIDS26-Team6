"""The five-layer gate. Pure logic -- no SDK, no network, no I/O beyond the ack.

Sending a recorded clinical session's text to a model is a disclosure. Five
independent layers have to agree, and they are independent on purpose: each one
is owned by a different person, lives in a different file, or is satisfied at a
different time.

===  ====================================  ===========================================
 #   Layer                                 Where
===  ====================================  ===========================================
 1   Install opt-in                        the ``deid-<provider>`` extra -- an ImportError
 2   Org-policy acknowledgement            ``~/.wfrec/policy/deid-llm-<provider>.json``
 3   Config flag ``[deid.llm] enabled``    ``~/.wfrec/config.toml``
 4   Per-invocation flags                  ``--llm --i-am-sending-text-offbox``
 5   Consent event, **pre-flight**         ``session.deid.llm.consent`` in events.jsonl
===  ====================================  ===========================================

Scoping by egress class, because the layers answer different questions:

``external``  all five.
``internal``  1, 3, 4, 5, plus an ack carrying ``internal_endpoints_approved``.
              The BAA and zero-data-retention clauses are not required: nothing
              reached a third party.
``none``      1 and 5 only. A consent event is still written for a loopback
              call -- "which model saw this session" is an audit question
              independent of whether anything left the machine.

**Non-short-circuiting.** :func:`evaluate` checks every layer and reports every
failure at once. A gate that stops at the first failure turns one
fix-and-rerun cycle into five, and the person running it never gets to see the
shape of what they are being asked for.

**One ack per provider.** A BAA is a contract with a named counterparty. An
attestation naming Anthropic must not authorize egress to OpenAI or Google, so
the ack file is per-provider and a provider mismatch is a hard refusal.

**Credential presence is deliberately not a layer, for any provider.** An unset
``ANTHROPIC_API_KEY`` does not mean there are no credentials: the SDK also
resolves ``ANTHROPIC_AUTH_TOKEN``, a login profile under
``~/.config/anthropic/``, and workload-identity variables. Gating on key
availability would let ambient credentials silently arm network egress.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from .config import DeidConfig, LlmConfig
from .egress import Classification, EgressClass

POLICY_DIRNAME = "policy"


def ack_filename(provider: str) -> str:
    return f"deid-llm-{provider}.json"


#: Fields an acknowledgement must carry. It is an attestation **naming a human**
#: and it **expires** -- a policy decision with no name on it and no end date is
#: a policy decision nobody has to revisit.
REQUIRED_ACK_FIELDS = (
    "provider",
    "org",
    "baa_confirmed",
    "zero_data_retention_confirmed",
    "approver_name",
    "approver_role",
    "approved_at",
    "policy_reference",
    "expires_at",
    "allowed_endpoints",
    "deny_hosts",
    "deny_egress_classes",
)

#: The per-invocation flag. Deliberately verbose, and deliberately kept out of
#: the short ``--help``, so it cannot become muscle memory.
OFFBOX_FLAG = "--i-am-sending-text-offbox"

NONINTERACTIVE_ENV = "WFREC_DEID_LLM_NONINTERACTIVE"


class AckInvalid(RuntimeError):
    """The acknowledgement file exists but cannot be used."""


@dataclass(frozen=True, slots=True)
class Ack:
    """A parsed org-policy acknowledgement."""

    provider: str
    org: str
    baa_confirmed: bool
    zero_data_retention_confirmed: bool
    approver_name: str
    approver_role: str
    approved_at: str
    policy_reference: str
    expires_at: str
    allowed_endpoints: tuple[str, ...]
    deny_hosts: tuple[str, ...]
    deny_egress_classes: tuple[str, ...]
    internal_endpoints_approved: bool
    max_chars_per_session: int | None
    sha256: str
    """Digest of the file's exact bytes. Recorded in the consent event so the
    audit says which acknowledgement authorized *this* egress -- not merely that
    one existed."""

    path: str

    def expired(self, *, today: date | None = None) -> bool:
        today = today or datetime.now(timezone.utc).date()
        try:
            return _parse_date(self.expires_at) < today
        except ValueError:
            return True

    def consent_fields(self) -> dict[str, object]:
        return {
            "ack_sha256": self.sha256,
            "approver_name": self.approver_name,
            "approver_role": self.approver_role,
            "org": self.org,
            "policy_reference": self.policy_reference,
            "ack_expires_at": self.expires_at,
        }


def _parse_date(value: str) -> date:
    text = str(value).strip()
    if not text:
        raise ValueError("empty date")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return date.fromisoformat(text[:10])


def policy_dir(home: Path | None = None) -> Path | None:
    if home is not None:
        return home / POLICY_DIRNAME
    try:
        from wfrec import paths
    except Exception:  # pragma: no cover - autocab-only installs
        return None
    return paths.home() / POLICY_DIRNAME


def load_ack(provider: str, *, home: Path | None = None) -> Ack | None:
    """Read ``deid-llm-<provider>.json``. ``None`` if absent.

    Its own file -- **not** ``state.json`` and **not** the config. ``state.json``
    is machine-written and would be rewritten by the recorder; the config is
    user-owned, and the whole point of layer 2 is that it is *not* the same
    person who set layer 3.
    """

    directory = policy_dir(home)
    if directory is None:
        return None
    path = directory / ack_filename(provider)
    if not path.is_file():
        return None

    raw_bytes = path.read_bytes()
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AckInvalid(f"{path}: not valid JSON ({exc})") from exc
    if not isinstance(payload, dict):
        raise AckInvalid(f"{path}: expected a JSON object")

    missing = [name for name in REQUIRED_ACK_FIELDS if name not in payload]
    if missing:
        raise AckInvalid(
            f"{path}: missing required field(s) {', '.join(missing)}. "
            "An acknowledgement must name a human and carry an expiry."
        )

    def strings(name: str) -> tuple[str, ...]:
        value = payload.get(name) or ()
        if isinstance(value, str):
            value = [value]
        return tuple(str(item) for item in value)

    return Ack(
        provider=str(payload["provider"]),
        org=str(payload["org"]),
        baa_confirmed=bool(payload["baa_confirmed"]),
        zero_data_retention_confirmed=bool(payload["zero_data_retention_confirmed"]),
        approver_name=str(payload["approver_name"]),
        approver_role=str(payload["approver_role"]),
        approved_at=str(payload["approved_at"]),
        policy_reference=str(payload["policy_reference"]),
        expires_at=str(payload["expires_at"]),
        allowed_endpoints=strings("allowed_endpoints"),
        deny_hosts=strings("deny_hosts"),
        deny_egress_classes=strings("deny_egress_classes"),
        internal_endpoints_approved=bool(payload.get("internal_endpoints_approved", False)),
        max_chars_per_session=(
            int(payload["max_chars_per_session"])
            if payload.get("max_chars_per_session") is not None
            else None
        ),
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        path=str(path),
    )


# --------------------------------------------------------------------------
# layers
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class LayerResult:
    number: int
    name: str
    required: bool
    satisfied: bool
    detail: str

    def render(self) -> str:
        if not self.required:
            return f"  [n/a]  layer {self.number} ({self.name}): {self.detail}"
        mark = "ok  " if self.satisfied else "FAIL"
        return f"  [{mark}] layer {self.number} ({self.name}): {self.detail}"


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    provider: str
    egress: Classification
    layers: tuple[LayerResult, ...]
    refusals: tuple[str, ...]
    consent_payload: dict[str, object] = field(default_factory=dict)
    """The exact ``session.deid.llm.consent`` record to append **before the
    first byte leaves**. Built here rather than by the caller so a provider
    cannot omit a field, and so the ack digest that authorized this specific
    egress is not something a call site has to remember to include."""

    def render(self) -> str:
        head = (
            f"LLM tier {'ALLOWED' if self.allowed else 'REFUSED'} for provider "
            f"{self.provider!r}, egress class `{self.egress.egress_class.value}`"
        )
        lines = [head, f"  endpoint: {self.egress.reason}"]
        lines.extend(layer.render() for layer in self.layers)
        if self.refusals:
            lines.append("  refusal predicates:")
            lines.extend(f"    - {reason}" for reason in self.refusals)
        if not self.allowed:
            lines.append(
                "  Every unsatisfied requirement is listed above at once, on "
                "purpose: fixing them one round-trip at a time hides the shape "
                "of what is being asked for."
            )
        return "\n".join(lines)


def required_layers(egress_class: EgressClass) -> frozenset[int]:
    """Which layers apply. See the scoping table in the module docstring."""

    if egress_class is EgressClass.NONE:
        return frozenset({1, 5})
    return frozenset({1, 2, 3, 4, 5})


def evaluate(
    *,
    provider: str,
    egress: Classification,
    config: DeidConfig | LlmConfig | None = None,
    ack: Ack | None = None,
    sdk_available: bool = False,
    sdk_reason: str = "",
    invocation_llm: bool = False,
    invocation_offbox: bool = False,
    can_write_consent: bool = False,
    local_tier_complete: bool = False,
    chars_to_send: int = 0,
    chars_sent_so_far: int = 0,
    records_to_send: int = 0,
    model_requested: str = "",
    session_id: str = "",
    allow_plaintext: bool = False,
    today: date | None = None,
    env: Mapping[str, str] | None = None,
) -> GateDecision:
    """Evaluate every layer and every refusal predicate. Never short-circuits."""

    env = env if env is not None else os.environ
    llm = config.llm if isinstance(config, DeidConfig) else (config or LlmConfig())
    klass = egress.egress_class
    needed = required_layers(klass)
    internal_only = klass is EgressClass.INTERNAL

    layers: list[LayerResult] = []

    # -- layer 1: install opt-in -------------------------------------------
    # With the SDK absent the most reliable gate is an ImportError. Ollama is
    # the deliberate exception: gating an install on a transport that reaches
    # only loopback buys nothing, so its containment is layer 2's
    # allowed_endpoints plus the egress classifier.
    if provider == "ollama":
        layers.append(
            LayerResult(
                1,
                "install opt-in",
                required=False,
                satisfied=True,
                detail=(
                    "ollama needs no extra -- plain HTTP over the httpx already in "
                    "base deps. Containment is allowed_endpoints plus the "
                    "egress classifier, not the install"
                ),
            )
        )
    else:
        layers.append(
            LayerResult(
                1,
                "install opt-in",
                required=1 in needed,
                satisfied=sdk_available,
                detail=(
                    f"{provider} SDK importable"
                    if sdk_available
                    else sdk_reason or f"install the deid-{provider} extra"
                ),
            )
        )

    # -- layer 2: org-policy acknowledgement -------------------------------
    ack_detail: str
    ack_ok = False
    if 2 not in needed:
        ack_detail = f"not required for egress class `{klass.value}`"
    elif ack is None:
        ack_detail = (
            f"no acknowledgement at {policy_dir()}/{ack_filename(provider)} -- "
            "an attestation naming a human, with an expiry"
        )
    elif internal_only:
        ack_ok = ack.internal_endpoints_approved
        ack_detail = (
            f"{ack.org} / {ack.approver_name} ({ack.approver_role}); "
            "internal_endpoints_approved="
            f"{ack.internal_endpoints_approved}. BAA and zero-data-retention "
            "clauses are not required for class `internal`"
        )
    else:
        ack_ok = ack.baa_confirmed and ack.zero_data_retention_confirmed
        ack_detail = (
            f"{ack.org} / {ack.approver_name} ({ack.approver_role}), "
            f"ref {ack.policy_reference}; baa_confirmed={ack.baa_confirmed}, "
            f"zero_data_retention_confirmed={ack.zero_data_retention_confirmed}"
        )
    layers.append(
        LayerResult(2, "org acknowledgement", required=2 in needed, satisfied=ack_ok, detail=ack_detail)
    )

    # -- layer 3: config flag ----------------------------------------------
    layers.append(
        LayerResult(
            3,
            "config flag",
            required=3 in needed,
            satisfied=llm.enabled,
            detail=(
                f"[deid.llm] enabled = {llm.enabled}"
                + (f" ({llm.source})" if llm.source else " (no config file found)")
                + ". In a different file from the acknowledgement on purpose: "
                "one person editing one file is never sufficient"
            ),
        )
    )

    # -- layer 4: per-invocation flags -------------------------------------
    # A persistent config flag must never by itself cause egress.
    invocation_ok = invocation_llm and (invocation_offbox or klass is EgressClass.NONE)
    layers.append(
        LayerResult(
            4,
            "per-invocation flags",
            required=4 in needed,
            satisfied=invocation_ok,
            detail=(
                f"--llm={invocation_llm}, {OFFBOX_FLAG}={invocation_offbox}"
                + (
                    f" ({OFFBOX_FLAG} not required for class `none`)"
                    if klass is EgressClass.NONE
                    else ""
                )
            ),
        )
    )

    # -- layer 5: consent event, pre-flight --------------------------------
    layers.append(
        LayerResult(
            5,
            "pre-flight consent event",
            required=5 in needed,
            satisfied=can_write_consent,
            detail=(
                "session.deid.llm.consent will be appended before the first byte "
                "leaves; append() already fsyncs, so a crash mid-call still "
                "leaves evidence that egress was attempted"
                if can_write_consent
                else "no writable session timeline to record consent on"
            ),
        )
    )

    # -- refusal predicates (not layers) -----------------------------------
    refusals: list[str] = []
    target_host = egress.target.host

    if ack is not None:
        if ack.provider != provider:
            refusals.append(
                f"acknowledgement at {ack.path} names provider "
                f"{ack.provider!r} but this invocation names {provider!r}. A BAA "
                "is a contract with a named counterparty; it does not transfer."
            )
        if ack.expired(today=today):
            refusals.append(
                f"acknowledgement expired on {ack.expires_at}. Re-approval is the "
                "point of an expiry."
            )
        if klass.value in {item.casefold() for item in ack.deny_egress_classes}:
            refusals.append(
                f"egress class `{klass.value}` is in deny_egress_classes -- an "
                "organisation can write 'loopback only, ever' once and have it "
                "hold for every provider and every future flag."
            )
        denied = {item.casefold() for item in ack.deny_hosts}
        if target_host.casefold() in denied:
            refusals.append(f"{target_host} is in deny_hosts")
        if ack.allowed_endpoints and not _endpoint_allowed(egress, ack.allowed_endpoints):
            refusals.append(
                f"{egress.target.host_port()} is not in allowed_endpoints "
                f"({', '.join(ack.allowed_endpoints)})"
            )
        if ack.max_chars_per_session is not None:
            total = chars_sent_so_far + chars_to_send
            if total > ack.max_chars_per_session:
                refusals.append(
                    f"max_chars_per_session {ack.max_chars_per_session} would be "
                    f"exceeded ({total} chars)"
                )

    if llm.max_chars_per_session and (chars_sent_so_far + chars_to_send) > llm.max_chars_per_session:
        refusals.append(
            f"[deid.llm] max_chars_per_session {llm.max_chars_per_session} would be "
            f"exceeded ({chars_sent_so_far + chars_to_send} chars)"
        )

    if not local_tier_complete:
        # Mandatory and non-configurable. Without it the disclosure is larger; a
        # failed call can leave a session with *no* de-identification and a
        # `sealed` flag; and the "measured recall in CI" claim evaporates, since
        # CI can enforce no floor for somebody else's model.
        refusals.append(
            "the local tier (surrogate_guard + regex_rules + the model tier) has "
            "not completed for this session. It runs first, always, and that is "
            "not configurable."
        )

    if egress.plaintext_offbox and not allow_plaintext:
        refusals.append(
            f"plaintext http:// to a non-loopback endpoint ({egress.target.host_port()}); "
            "pass --allow-plaintext-llm to proceed, which stamps sealed=partial. "
            "Loopback over http is fine and is never flagged."
        )

    if egress.resolution_failed:
        refusals.append(
            f"{target_host} did not resolve, so the class was escalated to "
            "`external`; an unresolvable host is an unknown host."
        )

    layer_ok = all(layer.satisfied for layer in layers if layer.required)
    allowed = layer_ok and not refusals

    consent = _consent_payload(
        provider=provider,
        egress=egress,
        ack=ack,
        model_requested=model_requested or llm.model,
        session_id=session_id,
        records_to_send=records_to_send,
        chars_to_send=chars_to_send,
        prompt_cache=llm.prompt_cache,
    )

    return GateDecision(
        allowed=allowed,
        provider=provider,
        egress=egress,
        layers=tuple(layers),
        refusals=tuple(refusals),
        consent_payload=consent,
    )


def _endpoint_allowed(egress: Classification, allowed: Sequence[str]) -> bool:
    """Match the **target** against ``allowed_endpoints``.

    The target, not the nominal endpoint: under a proxy the bytes reach the
    proxy, so that is what an allowlist has to authorize. Matching the nominal
    endpoint would let ``allowed_endpoints: ["localhost:11434"]`` authorize
    egress to an arbitrary proxy -- and there is a test for exactly that.

    An entry matches either the host as typed, **or** every resolved address. An
    allowlist entry names a *machine*, and an address identifies a machine more
    precisely than a name does -- so an ack listing ``10.0.0.7:8000`` should
    authorize ``--llm-base-url http://vllm.internal:8000`` when that is where
    the name points. ``every``, not ``any``: under a mixed answer, one approved
    address must not license the rest.
    """

    from .egress import parse_endpoint

    target = egress.target
    entries = []
    for entry in allowed:
        candidate = entry.strip()
        if not candidate:
            continue
        if candidate == "*":
            return True
        try:
            entries.append(parse_endpoint(candidate))
        except ValueError:
            continue

    def matches(host: str) -> bool:
        for parsed in entries:
            if parsed.host.casefold() != host.casefold():
                continue
            if parsed.port is not None and target.port is not None and parsed.port != target.port:
                continue
            return True
        return False

    if matches(target.host):
        return True
    return bool(egress.resolved) and all(
        matches(address) for address in egress.resolved
    )


def _consent_payload(
    *,
    provider: str,
    egress: Classification,
    ack: Ack | None,
    model_requested: str,
    session_id: str,
    records_to_send: int,
    chars_to_send: int,
    prompt_cache: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "provider": provider,
        "session_id": session_id,
        "model_requested": model_requested,
        "records_to_send": records_to_send,
        "chars_to_send": chars_to_send,
        "prompt_cache": prompt_cache,
    }
    payload.update(egress.audit_dict())
    if ack is not None:
        payload.update(ack.consent_fields())
    return payload


# --------------------------------------------------------------------------
# interactive confirmation -- a guard, not a layer
# --------------------------------------------------------------------------
def confirmation_required(
    decision: GateDecision, *, yes: bool, env: Mapping[str, str] | None = None
) -> bool:
    """Whether the operator must type the session id back.

    Bypassable only by ``--yes`` **and** ``WFREC_DEID_LLM_NONINTERACTIVE=1``
    **together**, so a wrapper script cannot inherit the bypass from a stray
    flag somebody added for an unrelated reason.
    """

    if decision.egress.egress_class is EgressClass.NONE:
        return False
    env = env if env is not None else os.environ
    bypass = yes and env.get(NONINTERACTIVE_ENV) == "1"
    return not bypass


def confirmation_prompt(decision: GateDecision, session_id: str) -> str:
    return (
        f"About to send {decision.consent_payload.get('records_to_send', 0)} records "
        f"({decision.consent_payload.get('chars_to_send', 0)} chars) from session "
        f"{session_id} to {decision.provider} at "
        f"{decision.egress.target.host_port()} "
        f"(egress class `{decision.egress.egress_class.value}`).\n"
        f"Type the session id to confirm: "
    )
