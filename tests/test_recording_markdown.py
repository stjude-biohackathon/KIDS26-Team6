"""Security and formatting tests for event Markdown rendering."""

from __future__ import annotations

from autocab.recording.markdown import render_markdown


def test_render_markdown_supports_prose_and_code() -> None:
    rendered = render_markdown(
        "# Result\n\n- **Passed**\n- `sample.tsv`\n\n```python\nvalue = 1\n```"
    )

    assert "<h1>Result</h1>" in rendered
    assert "<strong>Passed</strong>" in rendered
    assert "<code>sample.tsv</code>" in rendered
    assert "<pre><code>value = 1" in rendered


def test_render_markdown_blocks_active_content_and_remote_images() -> None:
    rendered = render_markdown(
        '<script>alert("x")</script>\n\n'
        '[unsafe](javascript:alert("x"))\n\n'
        "[embedded](data:text/html,unsafe)\n\n"
        "![tracker](https://example.org/pixel.gif)\n\n"
        '<a href="https://example.org" onclick="alert(1)">raw link</a>'
    )

    assert "<script" not in rendered
    assert 'href="javascript:' not in rendered
    assert 'href="data:' not in rendered
    assert "<img" not in rendered
    assert '<a href="https://example.org" onclick' not in rendered
    assert "&lt;a href=" in rendered


def test_render_markdown_protects_safe_links() -> None:
    rendered = render_markdown("[Documentation](https://example.org/docs)")

    assert 'href="https://example.org/docs"' in rendered
    assert 'target="_blank"' in rendered
    assert 'rel="noopener noreferrer"' in rendered


def test_render_markdown_opens_mailto_with_the_mail_handler() -> None:
    rendered = render_markdown("[Email](mailto:analyst@example.org)")

    assert 'href="mailto:analyst@example.org"' in rendered
    assert 'target="_blank"' not in rendered


def test_render_markdown_unwraps_redacted_and_relative_links() -> None:
    rendered = render_markdown(
        "[Docs]([REDACTED_URL]) and [local](README.md) and "
        "[redacted email](mailto:[REDACTED_EMAIL])"
    )

    assert "Docs [link redacted]" in rendered
    assert "local" in rendered
    assert "redacted email [link redacted]" in rendered
    assert "<a " not in rendered
    assert "%5BREDACTED" not in rendered
    assert "README.md" not in rendered
