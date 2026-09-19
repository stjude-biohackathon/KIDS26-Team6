"""``autocab deid`` -- the measurement verbs.

The seal verbs live on ``wfrec`` instead. Splitting them that way follows the
seam that already exists in this repo: ``autocab`` is pipeline and measurement,
``wfrec`` is session capture. A third console script would be a third thing to
document and install.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from autocab.output import (
    console,
    data_table,
    emit_json,
    error,
    info,
    mapping_summary,
    plain_text,
    success,
    summary,
    warning,
)

from .engines.base import EngineUnavailable
from .engines.registry import ENGINE_CHOICES
from .eval import corpus as corpus_mod
from .eval import benchmark, comparison, generate, report
from .eval.generate import DEFAULT_SEED, DIFFICULTIES
from .models import (
    MODEL_CHOICES,
    ModelWeightsError,
    download_progress_note,
    fetch_weights,
    load_bundle,
    verify_weights,
)

DIAGNOSTIC_ENGINE_CHOICES = ("gliner-only", "gliner2-pii-only")
EVAL_ENGINE_CHOICES = (*ENGINE_CHOICES, *DIAGNOSTIC_ENGINE_CHOICES)


def add_subparser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Attach ``deid`` to an existing ``autocab`` parser."""

    deid = subparsers.add_parser(
        "deid",
        help="De-identification corpus and evaluation harness.",
        description=(
            "Measure the de-identification detector against data/deid-eval. "
            "See docs/deid-evaluation.md for what the numbers do and do not prove."
        ),
    )
    verbs = deid.add_subparsers(dest="deid_command", required=True)

    gen = verbs.add_parser(
        "gen-corpus",
        help="Regenerate data/deid-eval/corpus/*.jsonl deterministically.",
    )
    gen.add_argument("--seed", type=int, default=DEFAULT_SEED)
    gen.add_argument(
        "--check",
        action="store_true",
        help=(
            "Do not write. Regenerate in memory and fail if the committed files "
            "differ byte-for-byte. This is the CI gate."
        ),
    )
    gen.add_argument(
        "--root", type=Path, default=None, help="Corpus root. Defaults to data/deid-eval."
    )

    ev = verbs.add_parser("eval", help="Score an engine against the corpus.")
    ev.add_argument("--engine", choices=EVAL_ENGINE_CHOICES, default="regex")
    ev.add_argument("--root", type=Path, default=None)
    ev.add_argument(
        "--difficulty",
        choices=DIFFICULTIES,
        action="append",
        default=None,
        help="Restrict to these difficulty tiers. Repeatable. Default: all (the gate uses easy+medium).",
    )
    ev.add_argument(
        "--write-scorecard",
        action="store_true",
        help="Overwrite this engine's snapshot and regenerate the comparison report and plot.",
    )
    ev.add_argument(
        "--report-latency",
        action="store_true",
        help="Measure and print ms/KB. Never written into the snapshot -- it would churn.",
    )
    ev.add_argument("--json", action="store_true", help="Emit the whole scorecard as JSON.")
    ev.add_argument(
        "--check-thresholds",
        action="store_true",
        help="Exit non-zero if any floor in thresholds.json is violated.",
    )

    bench = verbs.add_parser(
        "benchmark",
        help="Measure cold start, warmed inference, memory, and exact-span diagnostics.",
    )
    bench.add_argument(
        "--engine",
        choices=("regex", "gliner-only", "gliner", "gliner2-pii-only", "gliner2-pii"),
        action="append",
        dest="engines",
        help="Engine to measure. Repeatable. Default: regex, gliner-only, and gliner.",
    )
    bench.add_argument("--root", type=Path, default=None)
    bench.add_argument("--repeats", type=int, default=benchmark.DEFAULT_REPEATS)
    bench.add_argument("--output", type=Path, help="Optional JSON output path.")
    bench.add_argument("--plot", type=Path, help="Optional SVG performance plot path.")

    fetch = verbs.add_parser("fetch", help="Download and verify pinned model weights.")
    fetch.add_argument("--model", choices=MODEL_CHOICES, default="gliner")
    fetch.add_argument("--bundle", type=Path, help="Write an offline ZIP instead of installing.")

    load = verbs.add_parser("load", help="Verify and install an offline model ZIP.")
    load.add_argument("bundle", type=Path)
    load.add_argument("--model", choices=MODEL_CHOICES, default="gliner")

    verify = verbs.add_parser(
        "verify", help="Verify installed model weights without a network call."
    )
    verify.add_argument("--model", choices=MODEL_CHOICES, default="gliner")

    verbs.add_parser("labels", help="Print the taxonomy and its HIPAA rollup.")
    return deid


def _resolve_root(root: Path | None) -> Path:
    return root or corpus_mod.default_root()


def run(args: argparse.Namespace) -> int:
    if args.deid_command == "gen-corpus":
        return _gen_corpus(args)
    if args.deid_command == "eval":
        return _eval(args)
    if args.deid_command == "benchmark":
        return _benchmark(args)
    if args.deid_command == "fetch":
        return _fetch(args)
    if args.deid_command == "load":
        return _load(args)
    if args.deid_command == "verify":
        return _verify(args)
    if args.deid_command == "labels":
        return _labels()
    error(f"deid: unknown command {args.deid_command!r}")
    return 2


def _fetch(args: argparse.Namespace) -> int:
    info(download_progress_note(args.model), stderr=True)
    try:
        result = fetch_weights(args.bundle, model=args.model)
    except ModelWeightsError as exc:
        error(f"deid fetch: {exc}")
        return 1
    if isinstance(result, Path):
        success(f"Created offline model bundle: {result}")
    else:
        success(f"Downloaded and verified model: {result.path}")
    return 0


def _load(args: argparse.Namespace) -> int:
    try:
        status = load_bundle(args.bundle, model=args.model)
    except ModelWeightsError as exc:
        error(f"deid load: {exc}")
        return 1
    success(f"Installed and verified model: {status.path}")
    return 0


def _verify(args: argparse.Namespace) -> int:
    status = verify_weights(model=args.model)
    if status.valid:
        success(f"Verified model: {status.path}")
        return 0
    error(f"Model verification failed at {status.path}")
    for problem in status.problems:
        warning(problem)
    info(
        f"Run `autocab deid fetch --model {args.model}` to install or repair it.",
        stderr=True,
    )
    return 1


def _gen_corpus(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    if args.check:
        problems = generate.check_corpus(root, args.seed)
        if problems:
            error("Corpus does not match its deterministic source.")
            for problem in problems:
                warning(problem)
            return 1
        loaded = corpus_mod.load(root)
        structural = corpus_mod.validate(loaded)
        if structural:
            error("Corpus validation failed.")
            for problem in structural[:40]:
                warning(problem)
            return 1
        success(
            f"Corpus reproduces byte-for-byte: {len(loaded)} records, "
            f"fingerprint {loaded.fingerprint[:16]}"
        )
        return 0

    summary = generate.write_corpus(root, args.seed)
    emit_json(summary)
    return 0


def _eval(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    loaded = corpus_mod.load(root)
    if args.difficulty:
        wanted = set(args.difficulty)
        records = tuple(record for record in loaded if record.difficulty in wanted)
        loaded = corpus_mod.Corpus(records=records, fingerprint=loaded.fingerprint)

    try:
        predictions, latency = report.predict(loaded, engine=args.engine)
    except EngineUnavailable as exc:
        error(f"deid eval: {report.unavailable_message(exc)}")
        return 1

    from .eval.scorer import score

    card = score(loaded, predictions, engine=args.engine)
    thresholds = report.load_thresholds(root)

    if args.json:
        payload = card.to_dict()
        if args.report_latency:
            payload["latency"] = latency.to_dict()
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        head = card.headline()
        mapping_summary("De-identification evaluation", head)
        table = data_table("Label", "Gold", "Strict", "Partial", "Floor", "Gated")
        for column in table.columns[1:5]:
            column.justify = "right"
        floors = thresholds.get("engines", {}).get(args.engine, {})
        floors = floors.get("per_label_recall_strict", {}) if isinstance(floors, dict) else {}
        for label in sorted(card.by_label):
            row = card.by_label[label]
            gated = card.by_label_gated.get(label)
            floor = floors.get(label)
            table.add_row(
                plain_text(label),
                plain_text(row.gold),
                plain_text(f"{row.recall_strict:.3f}"),
                plain_text(f"{row.recall_partial:.3f}"),
                plain_text(floor if floor is not None else "-"),
                plain_text("-" if gated is None else f"{gated.recall_strict:.3f}"),
            )
        console.print(table)
        if args.report_latency:
            summary(
                "Latency",
                [
                    ("Rate", f"{latency.ms_per_kb:.2f} ms/KB"),
                    ("Evaluated", f"{latency.kilobytes:.1f} KB"),
                ],
            )

    exit_code = 0
    if args.check_thresholds:
        failures = report.check_thresholds(card, thresholds)
        if failures:
            error("Threshold checks failed.")
            for failure in failures:
                warning(failure)
            exit_code = 1
        else:
            success("All thresholds satisfied.")

    if args.write_scorecard:
        path = report.write_scorecard(card, root)
        success(f"Wrote {path}")
        docs = Path(__file__).resolve().parents[3] / "docs"
        report_path, plot_path = comparison.write_accuracy_artifacts(root, docs)
        success(f"Wrote {report_path}")
        success(f"Wrote {plot_path}")

    return exit_code


def _benchmark(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    corpus = corpus_mod.load(root)
    engines = args.engines or ["regex", "gliner-only", "gliner"]
    results = []
    try:
        for engine in engines:
            results.append(benchmark.run_engine(corpus, engine, repeats=args.repeats))
    except (EngineUnavailable, ValueError) as exc:
        message = (
            report.unavailable_message(exc) if isinstance(exc, EngineUnavailable) else str(exc)
        )
        error(f"deid benchmark: {message}")
        return 1

    payload = json.dumps([result.to_dict() for result in results], indent=2, sort_keys=True)
    if args.output:
        benchmark.write_json(results, args.output)
        success(f"Wrote {args.output}")
    else:
        print(payload)
    if args.plot:
        args.plot.parent.mkdir(parents=True, exist_ok=True)
        args.plot.write_text(comparison.render_performance_svg(results), encoding="utf-8")
        success(f"Wrote {args.plot}")
    return 0


def _labels() -> int:
    from .labels import SPECS, hipaa_rollup

    labels = data_table("Label", "HIPAA", "Prefix", "Rank", "Zero-shot description")
    labels.columns[1].justify = "right"
    labels.columns[3].justify = "right"
    for spec in SPECS.values():
        hipaa = spec.hipaa if spec.hipaa is not None else "-"
        labels.add_row(
            plain_text(spec.label.value),
            plain_text(hipaa),
            plain_text(spec.prefix),
            plain_text(spec.rank),
            plain_text(spec.description),
        )
    console.print(labels)

    safe_harbor = data_table("HIPAA Safe Harbor", "Labels")
    safe_harbor.columns[0].justify = "right"
    rollup = hipaa_rollup()
    for identifier in range(1, 19):
        covered_labels = rollup.get(identifier)
        status = ", ".join(covered_labels) if covered_labels else "NOT COVERED"
        safe_harbor.add_row(plain_text(identifier), plain_text(status))
    unmapped = rollup.get(None)
    if unmapped:
        safe_harbor.add_row(
            "--", plain_text(f"Not a Safe Harbor identifier: {', '.join(unmapped)}")
        )
    console.print(safe_harbor)
    return 0
