"""``~/.autocab/config.toml`` -- standing user intent, and nothing secret.

Gate layer 3 lives here: ``[deid.llm] enabled``. It is deliberately in a
*different file* from the org acknowledgement, because one person editing one
file must never be sufficient to arm network egress.

**The loader hard-rejects credentials.** Not a warning -- it refuses to run the
LLM path at all. The schema has no key field, and a config that carries one is
treated as a configuration error rather than as a convenience:

* a key *named* ``api_key`` / ``token`` / ``secret`` / ``password`` anywhere in
  the tree, at any depth;
* a string *value* matching a known credential shape, wherever it appears.

The **name** check is the real catch-all. The shape checks are belt and braces
for a key pasted under an innocuous name like ``endpoint_suffix``.

There is deliberately **no dotenv dependency and no ``.env`` autoload from the
working directory.** That is exactly how a key ends up inside a session folder
when somebody records while sitting in a repository -- and session folders are
designed to be zipped and handed to a teammate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

CONFIG_FILENAME = "config.toml"

#: Key names that must never appear. Matched case-insensitively against the
#: whole key, and as a substring for the unambiguous ones.
FORBIDDEN_KEY_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "api-key",
        "key",
        "token",
        "auth_token",
        "access_token",
        "secret",
        "secret_key",
        "client_secret",
        "password",
        "passwd",
        "credential",
        "credentials",
        "bearer",
    }
)

#: Credential shapes, for a key pasted under an innocuous name. Deliberately
#: short of exhaustive -- the name check above is what catches the general case.
CREDENTIAL_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("google", re.compile(r"AIza[A-Za-z0-9_-]{30,}")),
    ("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]{20,}")),
)


class ConfigRejected(RuntimeError):
    """A config that cannot be used. Fail closed, not fail loud-and-continue."""


class TomlUnavailable(RuntimeError):
    """No TOML parser. ``tomllib`` is 3.11+, and ``requires-python`` is 3.10."""


@dataclass(frozen=True, slots=True)
class LlmConfig:
    """The ``[deid.llm]`` table."""

    enabled: bool = False
    """Layer 3. Standing user intent. **Never sufficient on its own** -- a
    persistent config flag must not by itself cause egress, which is why layer 4
    exists."""

    provider: str = ""
    base_url: str = ""
    model: str = ""
    prompt_cache: bool = False
    """Default **off**. The system prompt is byte-identical every request, so
    caching is free money -- but a cache is server-side retention of a prefix,
    and an organisation whose gate is a zero-data-retention attestation deserves
    to opt in rather than discover it. Meaningless for a loopback model."""

    max_chars_per_session: int = 2_000_000
    refusal_fallbacks: bool = False
    """Off. One named model per configured policy; ``model_served`` is recorded
    regardless, so a silent substitution is visible after the fact."""

    source: str = ""


@dataclass(frozen=True, slots=True)
class DeidConfig:
    profile: str = "balanced"
    engine: str = "regex"
    llm: LlmConfig = field(default_factory=LlmConfig)
    allowlist_extra: tuple[str, ...] = ()
    source: str = ""

    @property
    def exists(self) -> bool:
        return bool(self.source)


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - only on 3.10
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError as exc:
            raise TomlUnavailable(
                "no TOML parser available (tomllib is 3.11+; install `tomli` on "
                "3.10). The LLM tier's config layer cannot be verified, so it "
                "fails closed."
            ) from exc
    with path.open("rb") as handle:
        return tomllib.load(handle)


def scan_for_credentials(payload: Mapping[str, Any], *, where: str = "") -> list[str]:
    """Walk the whole tree looking for anything credential-shaped.

    Recursive and key-name-first, so nesting a key under an unexpected table
    does not evade it.
    """

    problems: list[str] = []

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                name = str(key)
                lowered = name.casefold().replace("-", "_")
                child = f"{path}.{name}" if path else name
                if lowered in FORBIDDEN_KEY_NAMES or lowered.endswith(
                    ("_key", "_token", "_secret", "_password")
                ):
                    problems.append(
                        f"{child}: credentials must never live in a config file. "
                        "Let the provider SDK resolve its own credential from the "
                        "environment or its own profile; this file has no key field."
                    )
                walk(value, child)
        elif isinstance(node, (list, tuple)):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        elif isinstance(node, str):
            for name, pattern in CREDENTIAL_SHAPES:
                if pattern.search(node):
                    problems.append(
                        f"{path}: value matches a {name} credential shape. "
                        "Remove it; the key-name check is the catch-all and this "
                        "is the backstop for a key under an innocuous name."
                    )
                    break

    walk(payload, where)
    return problems


def load(path: Path | None = None) -> DeidConfig:
    """Read the config, or return defaults if there is none.

    A missing file is the normal case and not an error. A file containing a
    credential **is** an error, and raises.
    """

    if path is None:
        path = _default_path()
    if path is None or not path.is_file():
        return DeidConfig()

    payload = _load_toml(path)
    problems = scan_for_credentials(payload)
    if problems:
        raise ConfigRejected(
            f"{path} rejected:\n" + "\n".join(f"  - {problem}" for problem in problems)
        )

    deid = payload.get("deid")
    deid = deid if isinstance(deid, Mapping) else {}
    llm_raw = deid.get("llm")
    llm_raw = llm_raw if isinstance(llm_raw, Mapping) else {}

    llm = LlmConfig(
        enabled=bool(llm_raw.get("enabled", False)),
        provider=str(llm_raw.get("provider", "")),
        base_url=str(llm_raw.get("base_url", "")),
        model=str(llm_raw.get("model", "")),
        prompt_cache=bool(llm_raw.get("prompt_cache", False)),
        max_chars_per_session=int(llm_raw.get("max_chars_per_session", 2_000_000)),
        refusal_fallbacks=bool(llm_raw.get("refusal_fallbacks", False)),
        source=str(path),
    )
    extra = deid.get("allowlist_extra", ())
    return DeidConfig(
        profile=str(deid.get("profile", "balanced")),
        engine=str(deid.get("engine", "regex")),
        llm=llm,
        allowlist_extra=tuple(str(item) for item in extra)
        if isinstance(extra, (list, tuple))
        else (),
        source=str(path),
    )


def _default_path() -> Path | None:
    try:
        from wfrec import paths
    except Exception:  # pragma: no cover - autocab-only installs
        return None
    try:
        return paths.home() / CONFIG_FILENAME
    except Exception:  # pragma: no cover
        return None
