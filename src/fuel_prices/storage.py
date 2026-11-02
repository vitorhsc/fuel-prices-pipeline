"""Landing zone: every extraction is persisted as an immutable, typed Parquet file.

Layout (Hive-style partitions, readable by Spark, DuckDB, Athena, pandas...):
    <parquet_dir>/dgeg/<dataset>/snapshot_date=YYYY-MM-DD/<scope>__<run_id>.parquet

Rows that fail validation go to a quarantine folder as JSON Lines, so nothing is silently lost.
Keeping the raw snapshots means the warehouse can be rebuilt (`replay`) without calling the API.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

SOURCE = "dgeg"


def parquet_path(
    base_dir: Path, dataset: str, snapshot_date: date, scope: str, run_id: str
) -> Path:
    return (
        Path(base_dir)
        / SOURCE
        / dataset
        / f"snapshot_date={snapshot_date.isoformat()}"
        / f"{scope}__{run_id}.parquet"
    )


def write_parquet(records: list[dict[str, Any]], schema: pa.Schema, path: Path) -> Path:
    """Write records with an explicit schema (fails fast on type drift), atomically."""
    table = pa.Table.from_pylist(records, schema=schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(path)  # readers never see a half-written file
    return path


def read_parquet(path: Path, schema: pa.Schema) -> pa.Table:
    return pq.read_table(path, schema=schema)


def dataset_from_path(path: Path) -> str:
    """Infer the dataset name from a landing-zone path (used by `replay`)."""
    parts = Path(path).parts
    try:
        return parts[parts.index(SOURCE) + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError(f"Not a landing-zone path: {path}") from exc


def write_quarantine(rejected: list[tuple[str, dict[str, Any]]], path: Path) -> Path:
    """Persist rejected rows with the reason, as JSON Lines for easy inspection."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for reason, raw in rejected:
            fh.write(json.dumps({"reason": reason, "row": raw}, ensure_ascii=False) + "\n")
    return path
