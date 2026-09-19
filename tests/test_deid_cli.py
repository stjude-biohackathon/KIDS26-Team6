"""The optional model is managed explicitly from the AutoCAB terminal CLI."""

from __future__ import annotations

from pathlib import Path

from autocab.cli import main
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
