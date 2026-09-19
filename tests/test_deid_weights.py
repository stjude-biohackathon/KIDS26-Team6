"""Pinned GLiNER weight handling stays explicit, local, and verifiable."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from zipfile import ZipFile

import pytest

from autocab.deid import models
from autocab.deid.engines.base import EngineUnavailable
from autocab.deid.engines.gliner_onnx import GlinerOnnx


def _model_file(remote_path: str, local_name: str, content: bytes) -> models.ModelFile:
    return models.ModelFile(
        remote_path=remote_path,
        local_name=local_name,
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
    )


def test_missing_weights_never_trigger_a_download(autocab_home: Path) -> None:
    status = models.verify_weights()

    assert status.present is False
    assert status.valid is False
    with pytest.raises(EngineUnavailable, match="autocab deid fetch"):
        GlinerOnnx()


def test_fetch_bundle_load_and_verify_are_hash_checked(
    autocab_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_files = {
        "onnx/model_quantized.onnx": b"small-model-fixture",
        "tokenizer.json": b'{"fixture": true}',
        "gliner_config.json": b'{"max_width": 12}',
    }
    pinned = tuple(
        _model_file(remote, Path(remote).name, content) for remote, content in source_files.items()
    )
    monkeypatch.setattr(models, "MODEL_FILES", pinned)

    def fake_download(
        *,
        filename: str,
        cache_dir: Path,
        **_kwargs: object,
    ) -> str:
        assert Path(cache_dir) == models.model_cache_root()
        assert "local_dir" not in _kwargs
        assert os.environ[models.XET_INITIAL_CONCURRENCY_VARIABLE] == str(
            models.DEFAULT_DOWNLOAD_CONCURRENCY
        )
        assert Path(os.environ[models.XET_CACHE_VARIABLE]) == models.model_xet_cache_root()
        assert os.environ[models.HUB_DOWNLOAD_TIMEOUT_VARIABLE] == str(
            models.HTTP_DOWNLOAD_TIMEOUT_SECONDS
        )
        destination = Path(cache_dir) / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source_files[filename])
        return str(destination)

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)
    bundle = tmp_path / "gliner-model.zip"

    assert models.fetch_weights(bundle) == bundle.resolve()
    assert not models.model_root().exists()

    installed = models.load_bundle(bundle)
    assert installed.valid is True
    assert installed.path.parent.parent.parent == autocab_home
    assert models.verify_weights().valid is True
    assert models.XET_INITIAL_CONCURRENCY_VARIABLE not in os.environ
    assert models.XET_CACHE_VARIABLE not in os.environ
    assert models.HUB_DOWNLOAD_TIMEOUT_VARIABLE not in os.environ

    installed.path.joinpath(pinned[0].local_name).write_bytes(b"corrupt")
    corrupt = models.verify_weights()
    assert corrupt.valid is False
    assert any("expected" in problem for problem in corrupt.problems)


def test_interrupted_download_keeps_the_hub_cache(
    autocab_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    partial = models.model_cache_root() / "model.safetensors.incomplete"

    def interrupted_download(*, cache_dir: Path, **_kwargs: object) -> str:
        assert Path(cache_dir) == models.model_cache_root()
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(b"partial model data")
        raise ConnectionError("connection interrupted")

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", interrupted_download)

    with pytest.raises(models.ModelWeightsError, match="connection interrupted"):
        models.fetch_weights(model="gliner2-pii")

    assert partial.read_bytes() == b"partial model data"


def test_gliner2_download_uses_resumable_http(
    autocab_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b"small-gliner2-fixture"
    model_file = _model_file("model.safetensors", "model.safetensors", content)
    spec = models.ModelSpec(
        key="gliner2-pii",
        name="gliner2-fixture",
        repository="example/gliner2-fixture",
        revision="abc123",
        source_repository="example/gliner2-fixture",
        source_revision="abc123",
        license="apache-2.0",
        variant="fixture",
        files=(model_file,),
    )
    monkeypatch.setitem(models.MODEL_SPECS, "gliner2-pii", spec)

    def fake_download(*, filename: str, cache_dir: Path, **_kwargs: object) -> str:
        import huggingface_hub.constants as hub_constants

        assert os.environ[models.HUB_DISABLE_XET_VARIABLE] == "1"
        assert os.environ[models.HUB_DOWNLOAD_TIMEOUT_VARIABLE] == str(
            models.HTTP_DOWNLOAD_TIMEOUT_SECONDS
        )
        assert hub_constants.HF_HUB_DISABLE_XET is True
        assert hub_constants.HF_HUB_DOWNLOAD_TIMEOUT == models.HTTP_DOWNLOAD_TIMEOUT_SECONDS
        destination = Path(cache_dir) / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return str(destination)

    import huggingface_hub
    import huggingface_hub.constants as hub_constants

    previous_disable_xet = hub_constants.HF_HUB_DISABLE_XET
    previous_timeout = hub_constants.HF_HUB_DOWNLOAD_TIMEOUT

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake_download)

    status = models.fetch_weights(model="gliner2-pii")

    assert isinstance(status, models.WeightStatus)
    assert status.valid is True
    assert models.HUB_DISABLE_XET_VARIABLE not in os.environ
    assert models.HUB_DOWNLOAD_TIMEOUT_VARIABLE not in os.environ
    assert hub_constants.HF_HUB_DISABLE_XET is previous_disable_xet
    assert hub_constants.HF_HUB_DOWNLOAD_TIMEOUT == previous_timeout


def test_download_environment_preserves_operator_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom_cache = Path("/custom/xet-cache")
    monkeypatch.setenv(models.XET_INITIAL_CONCURRENCY_VARIABLE, "3")
    monkeypatch.setenv(models.XET_CACHE_VARIABLE, str(custom_cache))
    monkeypatch.setenv(models.HUB_DOWNLOAD_TIMEOUT_VARIABLE, "600")

    with models._download_environment():
        assert os.environ[models.XET_INITIAL_CONCURRENCY_VARIABLE] == "3"
        assert Path(os.environ[models.XET_CACHE_VARIABLE]) == custom_cache
        assert os.environ[models.HUB_DOWNLOAD_TIMEOUT_VARIABLE] == "600"

    assert os.environ[models.XET_INITIAL_CONCURRENCY_VARIABLE] == "3"
    assert Path(os.environ[models.XET_CACHE_VARIABLE]) == custom_cache
    assert os.environ[models.HUB_DOWNLOAD_TIMEOUT_VARIABLE] == "600"


def test_bundle_must_match_the_pinned_manifest(
    autocab_home: Path,
    tmp_path: Path,
) -> None:
    invalid = tmp_path / "invalid.zip"
    with ZipFile(invalid, "w") as archive:
        archive.writestr("unexpected.txt", "not a model")

    with pytest.raises(models.ModelWeightsError, match="contents"):
        models.load_bundle(invalid)


def test_model_manifest_preserves_nested_gliner2_paths() -> None:
    payload = models.manifest("gliner2-pii")

    assert payload["model_key"] == "gliner2-pii"
    assert payload["revision"] == models.GLINER2_PII_SPEC.revision
    assert "encoder_config/config.json" in {item["name"] for item in payload["files"]}


def test_verified_install_creates_nested_model_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    content = b"nested-model-config"
    nested = _model_file("encoder_config/config.json", "encoder_config/config.json", content)
    spec = models.ModelSpec(
        key="fixture",
        name="fixture-model",
        repository="example/model",
        revision="abc123",
        source_repository="example/model",
        source_revision="abc123",
        license="apache-2.0",
        variant="fixture",
        files=(nested,),
    )
    monkeypatch.setitem(models.MODEL_SPECS, "fixture", spec)
    source = tmp_path / "source"
    nested_source = source / nested.local_name
    nested_source.parent.mkdir(parents=True)
    nested_source.write_bytes(content)
    (source / models.MANIFEST_FILENAME).write_text(
        json.dumps(models.manifest("fixture")), encoding="utf-8"
    )

    installed = models._install_verified(source, tmp_path / "installed", spec)

    assert installed.valid is True
    assert (installed.path / nested.local_name).read_bytes() == content
