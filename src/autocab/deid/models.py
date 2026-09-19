"""Pinned local model artifacts for optional de-identification tiers.

Model code ships with the package, but model data does not. A user explicitly
downloads the pinned artifacts with ``wfrec deid fetch``. Every later use is
offline and verifies the files before inference.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import BinaryIO
from zipfile import ZIP_STORED, BadZipFile, ZipFile

MODEL_NAME = "gliner-small-v2.1"
MODEL_REPOSITORY = "onnx-community/gliner_small-v2.1"
MODEL_REVISION = "8142fb00740ccea973e64b1272949ff48653df5e"
SOURCE_REPOSITORY = "urchade/gliner_small-v2.1"
SOURCE_REVISION = "4e091416cf7c3481db542c2a3d26156916f3a47f"
MODEL_LICENSE = "apache-2.0"
MANIFEST_FILENAME = "manifest.json"


@dataclass(frozen=True, slots=True)
class ModelFile:
    """One required file in the pinned model snapshot."""

    remote_path: str
    local_name: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """One immutable, reviewable model snapshot."""

    key: str
    name: str
    repository: str
    revision: str
    source_repository: str
    source_revision: str
    license: str
    variant: str
    files: tuple[ModelFile, ...]


MODEL_FILES: tuple[ModelFile, ...] = (
    ModelFile(
        "onnx/model_quantized.onnx",
        "model-int8.onnx",
        "c76c90920547fd937aaf505e7f2de5ec73168bf1c25abbb55a298104cb061400",
        183_403_734,
    ),
    ModelFile(
        "tokenizer.json",
        "tokenizer.json",
        "677203884d026e721115cf0daccf70ec4239545a13d6619e3e66d7151e0c9ce3",
        8_657_198,
    ),
    ModelFile(
        "gliner_config.json",
        "gliner_config.json",
        "8e8b59de124a256a3f3de67879d0da686fe3f73ecc05093506ba525e451b920d",
        731,
    ),
)

GLINER2_PII_FILES: tuple[ModelFile, ...] = (
    ModelFile(
        "config.json",
        "config.json",
        "164f17362bcf9d114067d3465e7374bfdd79ce6b605acb745de5a49dabb9595c",
        252,
    ),
    ModelFile(
        "encoder_config/config.json",
        "encoder_config/config.json",
        "f27dd63cc43a248d2566f0b6ad7a115db353676ce0561dcbca45bac766464c1a",
        895,
    ),
    ModelFile(
        "model.safetensors",
        "model.safetensors",
        "0280f6f39f6012da50b6640bad438d9b7e763a1b0102094115d1b710c4dd79b6",
        1_228_421_964,
    ),
    ModelFile(
        "tokenizer.json",
        "tokenizer.json",
        "f6df10ec83bea993035b2dd7c39345a3d4fcf23421c2adb6cb4ffc1e6d1bc4b5",
        16_020_604,
    ),
    ModelFile(
        "tokenizer_config.json",
        "tokenizer_config.json",
        "233beed1f1095cccfc7907cde31a8d90a0c6aa4fdfaf6493f8e55fd162e81ae6",
        711,
    ),
)

GLINER_SPEC = ModelSpec(
    key="gliner",
    name=MODEL_NAME,
    repository=MODEL_REPOSITORY,
    revision=MODEL_REVISION,
    source_repository=SOURCE_REPOSITORY,
    source_revision=SOURCE_REVISION,
    license=MODEL_LICENSE,
    variant="int8",
    files=MODEL_FILES,
)
GLINER2_PII_SPEC = ModelSpec(
    key="gliner2-pii",
    name="gliner2-privacy-filter-PII-multi",
    repository="fastino/gliner2-privacy-filter-PII-multi",
    revision="c153999da5f4c509df4322b0c6a1baf3d2c284d7",
    source_repository="fastino/gliner2-privacy-filter-PII-multi",
    source_revision="c153999da5f4c509df4322b0c6a1baf3d2c284d7",
    license="apache-2.0",
    variant="pytorch-safetensors",
    files=GLINER2_PII_FILES,
)
MODEL_SPECS = {spec.key: spec for spec in (GLINER_SPEC, GLINER2_PII_SPEC)}
MODEL_CHOICES = tuple(MODEL_SPECS)


class ModelWeightsError(RuntimeError):
    """Pinned model artifacts are absent, corrupt, or incompatible."""


@dataclass(frozen=True, slots=True)
class WeightStatus:
    """Verification result suitable for CLI and doctor output."""

    path: Path
    present: bool
    valid: bool
    problems: tuple[str, ...] = ()
    spec: ModelSpec = GLINER_SPEC

    def to_dict(self) -> dict[str, object]:
        return {
            "model": self.spec.name,
            "model_key": self.spec.key,
            "repository": self.spec.repository,
            "revision": self.spec.revision,
            "license": self.spec.license,
            "path": str(self.path),
            "present": self.present,
            "valid": self.valid,
            "problems": list(self.problems),
        }


def _model_spec(model: str) -> ModelSpec:
    try:
        spec = MODEL_SPECS[model]
    except KeyError as exc:
        raise ValueError(
            f"unknown model {model!r}; expected one of {', '.join(MODEL_CHOICES)}"
        ) from exc
    # Preserve the long-standing MODEL_FILES seam used by small fixture tests.
    if model == "gliner" and spec.files is not MODEL_FILES:
        return ModelSpec(
            spec.key,
            spec.name,
            spec.repository,
            spec.revision,
            spec.source_repository,
            spec.source_revision,
            spec.license,
            spec.variant,
            MODEL_FILES,
        )
    return spec


def model_root(model: str = "gliner") -> Path:
    """Return the application-owned model directory under ``WFREC_HOME``."""

    from autocab.recording import paths

    spec = _model_spec(model)
    return paths.home() / "models" / spec.name / spec.revision


def manifest(model: str = "gliner") -> dict[str, object]:
    """Return the exact metadata written beside installed files and bundles."""

    spec = _model_spec(model)
    return {
        "schema_version": 1,
        "model": spec.name,
        "model_key": spec.key,
        "repository": spec.repository,
        "revision": spec.revision,
        "source_repository": spec.source_repository,
        "source_revision": spec.source_revision,
        "license": spec.license,
        "variant": spec.variant,
        "files": [
            {
                "name": item.local_name,
                "source": item.remote_path,
                "sha256": item.sha256,
                "size": item.size,
            }
            for item in spec.files
        ],
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify_directory(directory: Path, *, spec: ModelSpec, require_manifest: bool) -> WeightStatus:
    problems: list[str] = []
    present = directory.is_dir()
    if not present:
        return WeightStatus(directory, False, False, ("model directory is missing",), spec)

    for item in spec.files:
        path = directory / item.local_name
        if not path.is_file():
            problems.append(f"{item.local_name}: missing")
            continue
        if path.stat().st_size != item.size:
            problems.append(f"{item.local_name}: expected {item.size} bytes")
            continue
        if _sha256(path) != item.sha256:
            problems.append(f"{item.local_name}: sha256 mismatch")

    manifest_path = directory / MANIFEST_FILENAME
    if require_manifest and not manifest_path.is_file():
        problems.append(f"{MANIFEST_FILENAME}: missing")
    elif manifest_path.is_file():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            problems.append(f"{MANIFEST_FILENAME}: unreadable")
        else:
            for key, expected in (
                ("model", spec.name),
                ("repository", spec.repository),
                ("revision", spec.revision),
                ("license", spec.license),
            ):
                if payload.get(key) != expected:
                    problems.append(f"{MANIFEST_FILENAME}: unexpected {key}")

    return WeightStatus(directory, True, not problems, tuple(problems), spec)


def verify_weights(directory: Path | None = None, *, model: str = "gliner") -> WeightStatus:
    """Verify every required artifact without network access."""

    spec = _model_spec(model)
    return _verify_directory(directory or model_root(model), spec=spec, require_manifest=True)


def resolve_weights(directory: Path | None = None, *, model: str = "gliner") -> Path:
    """Return verified local weights or fail with the explicit setup command."""

    status = verify_weights(directory, model=model)
    if status.valid:
        return status.path
    detail = "; ".join(status.problems)
    raise ModelWeightsError(
        f"{status.spec.name} weights are unavailable at {status.path}: {detail}. "
        f"Run `wfrec deid fetch --model {model}`, then "
        f"`wfrec deid verify --model {model}`."
    )


def _download_to(directory: Path, spec: ModelSpec) -> None:
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:  # pragma: no cover - dependency metadata prevents this
        raise ModelWeightsError("huggingface-hub is required for `wfrec deid fetch`") from exc

    for item in spec.files:
        try:
            downloaded = Path(
                hf_hub_download(
                    repo_id=spec.repository,
                    filename=item.remote_path,
                    revision=spec.revision,
                    local_dir=directory,
                    cache_dir=directory / ".hub-cache",
                )
            )
        except Exception as exc:
            raise ModelWeightsError(
                f"could not download {item.remote_path} from pinned revision {spec.revision}: {exc}"
            ) from exc
        destination = directory / item.local_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        # macOS exposes /tmp through /private/tmp, so lexical Path equality can
        # describe the same inode with two names.
        if downloaded.resolve() != destination.resolve():
            shutil.copyfile(downloaded, destination)


def _write_manifest(directory: Path, spec: ModelSpec) -> None:
    (directory / MANIFEST_FILENAME).write_text(
        json.dumps(manifest(spec.key), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _install_verified(source: Path, destination: Path, spec: ModelSpec) -> WeightStatus:
    status = _verify_directory(source, spec=spec, require_manifest=True)
    if not status.valid:
        raise ModelWeightsError("model verification failed: " + "; ".join(status.problems))

    destination.mkdir(parents=True, exist_ok=True)
    for name in (MANIFEST_FILENAME, *(item.local_name for item in spec.files)):
        temporary = destination / f".{name}.tmp"
        temporary.parent.mkdir(parents=True, exist_ok=True)
        (destination / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, temporary)
        temporary.replace(destination / name)
    return verify_weights(destination, model=spec.key)


def fetch_weights(bundle: Path | None = None, *, model: str = "gliner") -> WeightStatus | Path:
    """Download pinned files, then install them or create an offline ZIP."""

    spec = _model_spec(model)
    destination = model_root(model)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=f"{model}-download-", dir=destination.parent) as temporary:
        staged = Path(temporary)
        _download_to(staged, spec)
        _write_manifest(staged, spec)
        status = _verify_directory(staged, spec=spec, require_manifest=True)
        if not status.valid:
            raise ModelWeightsError("download verification failed: " + "; ".join(status.problems))

        if bundle is None:
            return _install_verified(staged, destination, spec)

        bundle = bundle.expanduser().resolve()
        bundle.parent.mkdir(parents=True, exist_ok=True)
        temporary_bundle = bundle.with_name(f".{bundle.name}.tmp")
        with ZipFile(temporary_bundle, "w", compression=ZIP_STORED, allowZip64=True) as archive:
            for name in (MANIFEST_FILENAME, *(item.local_name for item in spec.files)):
                archive.write(staged / name, arcname=name)
        temporary_bundle.replace(bundle)
        return bundle


def _copy_bundle_member(source: BinaryIO, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    with temporary.open("wb") as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)
    temporary.replace(destination)


def load_bundle(bundle: Path, *, model: str = "gliner") -> WeightStatus:
    """Verify and install an offline bundle without contacting the network."""

    bundle = bundle.expanduser().resolve()
    if not bundle.is_file():
        raise ModelWeightsError(f"model bundle does not exist: {bundle}")

    spec = _model_spec(model)
    destination = model_root(model)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with TemporaryDirectory(prefix=f"{model}-bundle-", dir=destination.parent) as temporary:
            staged = Path(temporary)
            with ZipFile(bundle, "r") as archive:
                expected = {MANIFEST_FILENAME, *(item.local_name for item in spec.files)}
                members = archive.infolist()
                names = {member.filename for member in members}
                if names != expected or len(members) != len(expected):
                    raise ModelWeightsError(
                        "bundle contents do not match the pinned model manifest"
                    )
                expected_sizes = {
                    MANIFEST_FILENAME: None,
                    **{item.local_name: item.size for item in spec.files},
                }
                for member in members:
                    expected_size = expected_sizes[member.filename]
                    if expected_size is not None and member.file_size != expected_size:
                        raise ModelWeightsError(
                            f"bundle member {member.filename} has an unexpected size"
                        )
                    if expected_size is None and member.file_size > 64 * 1024:
                        raise ModelWeightsError("bundle manifest is unexpectedly large")
                for name in sorted(expected):
                    with archive.open(name, "r") as source:
                        _copy_bundle_member(source, staged / name)
            return _install_verified(staged, destination, spec)
    except BadZipFile as exc:
        raise ModelWeightsError(f"model bundle is not a valid ZIP file: {bundle}") from exc
