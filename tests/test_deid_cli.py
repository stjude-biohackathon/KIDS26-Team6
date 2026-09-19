"""The optional model is managed explicitly from the AutoCAB terminal CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from autocab.cli import main
from autocab.deid import cli as deid_cli
from autocab.deid.config import load as load_config


def test_regex_is_the_default_without_user_configuration(autocab_home: Path) -> None:
    assert load_config().engine == "regex"


def test_verify_missing_model_is_offline_and_names_the_setup_command(
    autocab_home: Path,
    capfd,
) -> None:
    assert main(["deid", "verify"]) == 1

    captured = capfd.readouterr()
    output = captured.out + captured.err
    assert "verification failed" in output
    assert "autocab deid fetch" in output


def test_verify_selects_the_gliner2_model(autocab_home: Path, capfd) -> None:
    assert main(["deid", "verify", "--model", "gliner2-pii"]) == 1

    captured = capfd.readouterr()
    output = captured.out + captured.err
    assert "gliner2-privacy-filter-PII-multi" in output
    assert "fetch --model gliner2-pii" in output


def test_fetch_explains_xet_progress_before_downloading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    bundle = tmp_path / "model.zip"
    monkeypatch.setattr(deid_cli, "fetch_weights", lambda *_args, **_kwargs: bundle)

    assert main(["deid", "fetch", "--bundle", str(bundle)]) == 0

    captured = capfd.readouterr()
    assert "progress may remain at 0%" in captured.err
