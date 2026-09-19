"""Safe Markdown rendering for prose-oriented event details."""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

import nh3
from markdown_it import MarkdownIt
from markdown_it.token import Token


_ALLOWED_TAGS = {
    "a",
    "blockquote",
    "br",
    "code",
    "del",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "ul",
}
_ALLOWED_ATTRIBUTES = {"a": {"href", "target", "title"}}
_ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}

# The js-default preset disables raw HTML. Images are disabled separately so a
# rendered event cannot make the browser retrieve an external tracking pixel.
_MARKDOWN = MarkdownIt(
    "js-default",
    {"breaks": True, "linkify": False},
).disable("image")


def _link_destination(href: str) -> tuple[bool, bool, str]:
    """Return whether a destination is allowed, redacted, and its scheme."""

    decoded_href = unquote(href)
    redacted = "REDACTED_" in decoded_href.upper()
    if redacted:
        return False, True, ""

    try:
        destination = urlsplit(href)
    except ValueError:
        return False, False, ""

    scheme = destination.scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        return False, False, scheme
    if scheme in {"http", "https"} and not destination.netloc:
        return False, False, scheme
    if scheme == "mailto" and not destination.path:
        return False, False, scheme
    return True, False, scheme


def _plain_text_token(token: Token, content: str = "") -> None:
    """Turn a link delimiter into inert text while preserving its label tokens."""

    token.type = "text"
    token.tag = ""
    token.nesting = 0
    token.attrs.clear()
    token.content = content


def _apply_link_policy(tokens: list[Token]) -> None:
    """Keep only absolute safe links and unwrap every rejected destination."""

    for token in tokens:
        if not token.children:
            continue
        link_stack: list[tuple[bool, bool]] = []
        for child in token.children:
            if child.type == "link_open":
                href = str(child.attrGet("href") or "")
                allowed, redacted, scheme = _link_destination(href)
                link_stack.append((allowed, redacted))
                if not allowed:
                    _plain_text_token(child)
                elif scheme in {"http", "https"}:
                    child.attrSet("target", "_blank")
            elif child.type == "link_close" and link_stack:
                allowed, redacted = link_stack.pop()
                if not allowed:
                    suffix = " [link redacted]" if redacted else ""
                    _plain_text_token(child, suffix)


def render_markdown(text: str) -> str:
    """Render Markdown to a deliberately small, sanitized HTML vocabulary."""

    environment: dict[str, object] = {}
    tokens = _MARKDOWN.parse(text, environment)
    _apply_link_policy(tokens)
    rendered = _MARKDOWN.renderer.render(tokens, _MARKDOWN.options, environment)
    return nh3.clean(
        rendered,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer",
        strip_comments=True,
    )
