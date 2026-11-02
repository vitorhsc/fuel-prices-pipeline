"""Command-line entry point: `python -m fuel_prices <command>`."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from fuel_prices import warehouse
from fuel_prices.client import DgegClient
from fuel_prices.config import ConfigError, load_settings
from fuel_prices.pipeline import RunSummary, replay, run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fuel_prices",
        description="Portuguese fuel prices pipeline: DGEG API -> Parquet -> PostgreSQL",
    )
    parser.add_argument("--config", help="Path to pipeline YAML (default: config/pipeline.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Extract, land, load and transform")
    run.add_argument("--fuels", nargs="+", type=int, metavar="ID", help="Override fuel type ids")
    run.add_argument("--districts", nargs="+", type=int, metavar="ID", help="Override districts")
    run.add_argument("--skip-transform", action="store_true", help="Only extract and load")

    sub.add_parser("transform", help="Rebuild the analytics tables from the raw layer")
    sub.add_parser("init-db", help="Create schemas and tables")
    sub.add_parser("lookups", help="Print fuel type and district ids from the API")

    rep = sub.add_parser("replay", help="Reload landed Parquet files without calling the API")
    rep.add_argument("paths", nargs="+", type=Path, help="Parquet files or folders")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        logging.error("%s", exc)
        return 2

    if args.command == "run":
        summary = run_pipeline(
            settings,
            fuel_type_ids=args.fuels,
            district_ids=args.districts,
            transform=not args.skip_transform,
        )
        _print_summary(summary)
        return 0 if summary.ok else 1

    if args.command == "replay":
        replay(settings, args.paths)
        return 0

    if args.command == "lookups":
        client = DgegClient(settings.api_base_url, request_delay_seconds=0)
        print("Fuel types (id  name  [unit]):")
        for f in sorted(client.get("GetTiposCombustiveis"), key=lambda f: f["Id"]):
            print(f"  {f['Id']:>5}  {f['Descritivo']}  [{f['UnidadeMedida']}]")
        print("Districts (id  name):")
        for d in client.get("GetDistritos"):
            print(f"  {d['Id']:>5}  {d['Descritivo']}")
        return 0

    with warehouse.connect(settings.database_url) as conn:
        warehouse.bootstrap(conn)
        if args.command == "transform":
            warehouse.run_transforms(conn)
    return 0


def _print_summary(summary: RunSummary) -> None:
    print(f"\nRun {summary.run_id}")
    print(
        f"{'dataset':<11} {'scope':<24} {'status':<8} "
        f"{'rows':>6} {'rejected':>8} {'new prices':>10}"
    )
    for t in summary.tasks:
        print(
            f"{t.dataset:<11} {t.scope:<24} {t.status:<8} {t.rows_extracted:>6} "
            f"{t.rows_rejected:>8} {t.new_versions:>10}"
        )
    if summary.models_built:
        print(f"Analytics models rebuilt: {len(summary.models_built)}")
    for t in summary.tasks:
        if t.warning:
            print(f"WARNING {t.scope}: {t.warning}", file=sys.stderr)
    for t in summary.failed:
        print(f"FAILED {t.dataset}/{t.scope}: {t.error}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
