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

from .engines.base import EngineUnavailable
from .engines.registry import ENGINE_CHOICES
from .eval import corpus as corpus_mod
from .eval import benchmark, comparison, generate, report
from .eval.generate import DEFAULT_SEED, DIFFICULTIES
from .models import (
    DOWNLOAD_PROGRESS_NOTE,
    MODEL_CHOICES,
    ModelWeightsError,
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

    verify = verbs.add_parser("verify", help="Verify installed model weights without a network call.")
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
    print(f"autocab deid: unknown command {args.deid_command!r}", file=sys.stderr)
    return 2


def _fetch(args: argparse.Namespace) -> int:
    print(DOWNLOAD_PROGRESS_NOTE, file=sys.stderr)
    try:
        result = fetch_weights(args.bundle, model=args.model)
    except ModelWeightsError as exc:
        print(f"autocab deid fetch: {exc}", file=sys.stderr)
        return 1
    if isinstance(result, Path):
        print(f"created offline model bundle: {result}")
    else:
        print(f"downloaded and verified model: {result.path}")
    return 0


def _load(args: argparse.Namespace) -> int:
    try:
        status = load_bundle(args.bundle, model=args.model)
    except ModelWeightsError as exc:
        print(f"autocab deid load: {exc}", file=sys.stderr)
        return 1
    print(f"installed and verified model: {status.path}")
    return 0


def _verify(args: argparse.Namespace) -> int:
    status = verify_weights(model=args.model)
    if status.valid:
        print(f"verified model: {status.path}")
        return 0
    print(f"autocab deid verify: model verification failed at {status.path}", file=sys.stderr)
    for problem in status.problems:
        print(f"  - {problem}", file=sys.stderr)
    print(
        f"Run `autocab deid fetch --model {args.model}` to install or repair it.",
        file=sys.stderr,
    )
    return 1


def _gen_corpus(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    if args.check:
        problems = generate.check_corpus(root, args.seed)
        if problems:
            print("autocab deid gen-corpus --check FAILED:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        loaded = corpus_mod.load(root)
        structural = corpus_mod.validate(loaded)
        if structural:
            print("autocab deid gen-corpus --check FAILED (structural):", file=sys.stderr)
            for problem in structural[:40]:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        print(
            f"corpus reproduces byte-for-byte: {len(loaded)} records, "
            f"fingerprint {loaded.fingerprint[:16]}"
        )
        return 0

    summary = generate.write_corpus(root, args.seed)
    print(json.dumps(summary, indent=2))
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
        print(f"autocab deid eval: {report.unavailable_message(exc)}", file=sys.stderr)
        return 1

    from .eval.scorer import score

    card = score(loaded, predictions, engine=args.engine)
    thresholds = report.load_thresholds(root)

    if args.json:
        payload = card.to_dict()
        if args.report_latency:
            payload["latency"] = latency.to_dict()
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        head = card.headline()
        for key, value in head.items():
            print(f"{key:44s} {value}")
        print()
        print(f"{'label':20s} {'gold':>5s} {'strict':>8s} {'partial':>8s} {'floor':>7s}")
        floors = thresholds.get("engines", {}).get(args.engine, {})
        floors = floors.get("per_label_recall_strict", {}) if isinstance(floors, dict) else {}
        for label in sorted(card.by_label):
            row = card.by_label[label]
            gated = card.by_label_gated.get(label)
            floor = floors.get(label)
            print(
                f"{label:20s} {row.gold:5d} {row.recall_strict:8.3f} "
                f"{row.recall_partial:8.3f} {floor if floor is not None else '-':>7}"
                + ("" if gated is None else f"   (gated {gated.recall_strict:.3f})")
            )
        if args.report_latency:
            print()
            print(f"latency: {latency.ms_per_kb:.2f} ms/KB over {latency.kilobytes:.1f} KB")

    exit_code = 0
    if args.check_thresholds:
        failures = report.check_thresholds(card, thresholds)
        if failures:
            print("\nthreshold failures:", file=sys.stderr)
            for failure in failures:
                print(f"  - {failure}", file=sys.stderr)
            exit_code = 1
        else:
            print("\nall thresholds satisfied")

    if args.write_scorecard:
        path = report.write_scorecard(card, root)
        print(f"\nwrote {path}")
        docs = Path(__file__).resolve().parents[3] / "docs"
        report_path, plot_path = comparison.write_accuracy_artifacts(root, docs)
        print(f"wrote {report_path}")
        print(f"wrote {plot_path}")

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
        print(f"autocab deid benchmark: {message}", file=sys.stderr)
        return 1

    payload = json.dumps([result.to_dict() for result in results], indent=2, sort_keys=True)
    if args.output:
        benchmark.write_json(results, args.output)
        print(f"wrote {args.output}")
    else:
        print(payload)
    if args.plot:
        args.plot.parent.mkdir(parents=True, exist_ok=True)
        args.plot.write_text(comparison.render_performance_svg(results), encoding="utf-8")
        print(f"wrote {args.plot}")
    return 0


def _labels() -> int:
    from .labels import SPECS, hipaa_rollup

    print(f"{'label':18s} {'HIPAA':>5s} {'prefix':7s} rank  zero-shot description")
    for spec in SPECS.values():
        hipaa = spec.hipaa if spec.hipaa is not None else "-"
        print(
            f"{spec.label.value:18s} {str(hipaa):>5s} {spec.prefix:7s} {spec.rank:4d}  "
            f"{spec.description}"
        )
    print("\nHIPAA Safe Harbor rollup:")
    rollup = hipaa_rollup()
    for identifier in range(1, 19):
        labels = rollup.get(identifier)
        status = ", ".join(labels) if labels else "NOT COVERED"
        print(f"  {identifier:2d}  {status}")
    unmapped = rollup.get(None)
    if unmapped:
        print(f"  --  not a Safe Harbor identifier: {', '.join(unmapped)}")
    return 0
