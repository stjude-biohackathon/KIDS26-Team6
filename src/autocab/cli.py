"""CLI entry point for the AutoCAB demo."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .orchestrator import run_pipeline
from .terminal_logs import convert_terminal_log, write_trace_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autocab",
        description="AutoCAB BioHackathon proof of concept.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="Run the bundled end-to-end demo.")
    demo.add_argument(
        "--output-dir",
        type=Path,
        default=Path("skills/generated-drafts"),
        help="Directory used for PR-ready draft exports.",
    )
    demo.add_argument(
        "--reviewer",
        default="CAB Maintainer",
        help="Reviewer name used for the approval event.",
    )
    demo.add_argument(
        "--no-approve",
        action="store_true",
        help="Leave proposals in the pending review state.",
    )
    demo.add_argument(
        "--input-mode",
        choices=("trace", "screen-capture", "terminal-log"),
        default="trace",
        help="Workflow input mode. The PoC supports trace mode today and reserves screen-capture as a future adapter.",
    )
    demo.add_argument(
        "--trace-file",
        type=Path,
        help="Optional JSON trace file. Defaults to the bundled synthetic workflow traces.",
    )
    demo.add_argument(
        "--capture-file",
        type=Path,
        help="Reserved path for a future consented screen-capture adapter.",
    )
    demo.add_argument(
        "--log-file",
        type=Path,
        help="Optional terminal workflow log file used when --input-mode terminal-log.",
    )

    ingest = subparsers.add_parser(
        "ingest-terminal-log",
        help="Convert a timestamped terminal workflow log into trace JSON.",
    )
    ingest.add_argument(
        "log_file",
        type=Path,
        help="Path to the terminal workflow log.",
    )
    ingest.add_argument(
        "--output",
        type=Path,
        default=Path("data/generated_terminal_trace.json"),
        help="Destination trace JSON file.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "demo":
        try:
            proposals = run_pipeline(
                output_dir=args.output_dir,
                input_mode=args.input_mode,
                trace_path=args.trace_file,
                capture_path=args.capture_file,
                log_path=args.log_file,
                reviewer=args.reviewer,
                approve=not args.no_approve,
            )
        except (NotImplementedError, ValueError) as exc:
            print(f"autocab: {exc}", file=sys.stderr)
            return 1

        print(json.dumps([proposal.to_dict() for proposal in proposals], indent=2))
        return 0

    if args.command == "ingest-terminal-log":
        try:
            trace = convert_terminal_log(args.log_file)
            write_trace_json(trace, args.output)
        except ValueError as exc:
            print(f"autocab: {exc}", file=sys.stderr)
            return 1

        print(json.dumps([trace.to_dict()], indent=2))
        return 0

    parser.error(f"Unsupported command: {args.command}")
    return 2
