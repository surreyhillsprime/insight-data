#!/usr/bin/env python3
"""Build INSIGHT's persistent, evidence-linked property-record feed.

The command is deliberately offline.  It consumes the already-published local
transaction, sales-history and planning-history JS assignments, merges any
prior property records for lifecycle continuity, and atomically publishes a
new JS snapshot.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from property_records import (
    build_property_records,
    read_history_js,
    read_property_records_js,
    read_transactions_js,
    write_property_records_js,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TRANSACTIONS = ROOT / "outputs" / "surrey-transactions.js"
DEFAULT_SALES_HISTORY = ROOT / "outputs" / "sales-history.js"
DEFAULT_PLANNING_HISTORY = ROOT / "outputs" / "planning-history.js"
DEFAULT_OUTPUT = ROOT / "outputs" / "property-records.js"


def _history_or_empty(
    path: Path | None,
    assignment_name: str,
    metadata_name: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if path is None:
        return {}, {}
    if not path.exists():
        raise FileNotFoundError(f"History input does not exist: {path}")
    return read_history_js(path, assignment_name, metadata_name)


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build canonical INSIGHT property records from local JS feeds."
    )
    parser.add_argument("--transactions", type=Path, default=DEFAULT_TRANSACTIONS)
    parser.add_argument("--sales-history", type=Path, default=DEFAULT_SALES_HISTORY)
    parser.add_argument("--planning-history", type=Path, default=DEFAULT_PLANNING_HISTORY)
    parser.add_argument(
        "--prior-records",
        type=Path,
        help="Optional prior property-record JS snapshot. Defaults to --output when it exists.",
    )
    parser.add_argument(
        "--no-prior",
        action="store_true",
        help="Do not preserve lifecycle fields from an existing output snapshot.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--as-of", help="Evidence cut-off date in YYYY-MM-DD form.")
    parser.add_argument(
        "--generated-at",
        help="Reproducible generation timestamp (ISO-8601); defaults to current UTC time.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and print metadata without writing the JS snapshot.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    transactions, transaction_meta = read_transactions_js(args.transactions)
    sales_history, sales_meta = _history_or_empty(
        args.sales_history,
        "SURREY_SALES_HISTORY",
        "SURREY_SALES_HISTORY_META",
    )
    planning_history, planning_meta = _history_or_empty(
        args.planning_history,
        "SURREY_PLANNING_HISTORY",
        "SURREY_PLANNING_HISTORY_META",
    )

    prior_records: dict[str, Any] = {}
    if not args.no_prior:
        prior_path = args.prior_records or (args.output if args.output.exists() else None)
        if prior_path:
            if not prior_path.exists():
                raise FileNotFoundError(f"Prior property-record input does not exist: {prior_path}")
            prior_records, _prior_meta = read_property_records_js(prior_path)

    records, metadata = build_property_records(
        transactions,
        transaction_meta,
        sales_history=sales_history,
        sales_meta=sales_meta,
        planning_history=planning_history,
        planning_meta=planning_meta,
        prior_records=prior_records,
        as_of=args.as_of,
        generated_at=args.generated_at,
    )
    if not args.dry_run:
        write_property_records_js(args.output, records, metadata)

    summary = {
        "output": None if args.dry_run else str(args.output),
        "propertyCount": metadata["propertyCount"],
        "transactionCount": metadata["transactionCount"],
        "eventCount": metadata["eventCount"],
        "narrativeCount": metadata["narrativeCount"],
        "coverageCounts": metadata["coverageCounts"],
        "datasetFingerprint": metadata["datasetFingerprint"],
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        print(f"property-record build failed: {error}", file=sys.stderr)
        raise SystemExit(1)
