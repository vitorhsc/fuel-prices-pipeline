"""Orchestration: extract -> validate -> land as Parquet -> load into PostgreSQL -> transform.

Tasks:
  * fuel_types                    reference data (full refresh)
  * prices, one per fuel/district current price of every station (a full snapshot)

The API has no "changed since" filter - it only returns the price in force right now. So the
pipeline always pulls the current snapshot and computes the delta itself: a price version is
new when (station, fuel, price_updated_at) is not in raw.price_history yet. Only those rows
are inserted; everything else is a no-op refresh of `last_seen_at`.

A failing task is recorded in meta.pipeline_runs and does not stop the others.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from fuel_prices import storage, warehouse
from fuel_prices.client import DgegClient
from fuel_prices.config import Settings
from fuel_prices.parsing import (
    FUEL_TYPES_SCHEMA,
    LISBON,
    PRICE_SNAPSHOT_SCHEMA,
    clean_coordinates,
    normalize_fuel_type,
    normalize_price_row,
    validate_price_row,
)

log = logging.getLogger(__name__)

PRICES_ENDPOINT = "PesquisarPostos"
FUEL_TYPES_ENDPOINT = "GetTiposCombustiveis"
SCHEMAS = {"prices": PRICE_SNAPSHOT_SCHEMA, "fuel_types": FUEL_TYPES_SCHEMA}


@dataclass
class TaskResult:
    dataset: str
    scope: str
    status: str
    rows_extracted: int = 0
    rows_rejected: int = 0
    new_versions: int = 0
    stations_upserted: int = 0
    warning: str | None = None
    error: str | None = None


@dataclass
class RunSummary:
    run_id: str
    tasks: list[TaskResult] = field(default_factory=list)
    models_built: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[TaskResult]:
        return [t for t in self.tasks if t.status == "failed"]

    @property
    def ok(self) -> bool:
        return not self.failed


def price_scope(fuel_type_id: int, district_id: int | None) -> str:
    return f"fuel={fuel_type_id}_district={district_id if district_id else 'all'}"


def split_valid_rows(
    items: list[dict[str, Any]], fuel_type_id: int, snapshot_at: datetime, run_id: str
) -> tuple[list[dict[str, Any]], list[tuple[str, dict[str, Any]]]]:
    valid, rejected = [], []
    for item in items:
        row = normalize_price_row(item, fuel_type_id, snapshot_at, run_id)
        reason = validate_price_row(row, snapshot_at)
        if reason:
            rejected.append((reason, item))
        else:
            valid.append(clean_coordinates(row))
    return valid, rejected


def freshness_warning(
    high_water_mark: datetime | None, snapshot_at: datetime, max_age_hours: int
) -> str | None:
    """The source is 'stale' when no station reported a price change for too long."""
    if high_water_mark and snapshot_at - high_water_mark > timedelta(hours=max_age_hours):
        age = (snapshot_at - high_water_mark).total_seconds() / 3600
        return f"stale source: newest price update is {age:.0f}h old (> {max_age_hours}h)"
    return None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _record(conn, task: TaskResult, run_id, started_at, client, requests_before, **extra) -> None:
    warehouse.log_run(
        conn,
        run_id=run_id,
        dataset=task.dataset,
        scope=task.scope,
        status=task.status,
        started_at=started_at,
        finished_at=_utcnow(),
        rows_extracted=task.rows_extracted,
        rows_rejected=task.rows_rejected,
        new_versions=task.new_versions,
        stations_upserted=task.stations_upserted,
        api_requests=client.requests_made - requests_before,
        warning_message=task.warning,
        error_message=task.error,
        **extra,
    )


def run_fuel_types_task(
    conn: psycopg.Connection, client: DgegClient, settings: Settings, run_id: str, now: datetime
) -> TaskResult:
    task = TaskResult(dataset="fuel_types", scope="all", status="success")
    started_at, requests_before, path = _utcnow(), client.requests_made, None
    try:
        rows = [normalize_fuel_type(i, now, run_id) for i in client.get(FUEL_TYPES_ENDPOINT)]
        task.rows_extracted = len(rows)
        path = storage.parquet_path(
            settings.parquet_dir, "fuel_types", now.astimezone(LISBON).date(), "all", run_id
        )
        storage.write_parquet(rows, FUEL_TYPES_SCHEMA, path)
        with conn.transaction():
            warehouse.load_fuel_types(conn, storage.read_parquet(path, FUEL_TYPES_SCHEMA))
        log.info("fuel_types: %d loaded", len(rows))
    except Exception as exc:  # one bad task must not stop the whole run
        log.exception("fuel_types: task failed")
        task.status, task.error = "failed", f"{type(exc).__name__}: {exc}"
    _record(
        conn,
        task,
        run_id,
        started_at,
        client,
        requests_before,
        parquet_path=str(path) if path else None,
    )
    return task


def run_prices_task(
    conn: psycopg.Connection,
    client: DgegClient,
    settings: Settings,
    fuel_type_id: int,
    district_id: int | None,
    run_id: str,
    now: datetime,
) -> TaskResult:
    scope = price_scope(fuel_type_id, district_id)
    task = TaskResult(dataset="prices", scope=scope, status="success")
    started_at, requests_before = _utcnow(), client.requests_made
    parquet_file: Path | None = None
    hwm_before = hwm_after = None

    try:
        hwm_before = hwm_after = warehouse.get_high_water_mark(conn, "prices", scope)
        params = {
            "idsTiposComb": fuel_type_id,
            "idMarca": "",
            "idTipoPosto": "",
            "idDistrito": district_id or "",
            "idsMunicipios": "",
        }
        items = list(client.paginate(PRICES_ENDPOINT, params))
        valid, rejected = split_valid_rows(items, fuel_type_id, now, run_id)
        task.rows_extracted, task.rows_rejected = len(items), len(rejected)
        snapshot_date = now.astimezone(LISBON).date()

        if rejected:
            quarantine = storage.parquet_path(
                settings.quarantine_dir, "prices", snapshot_date, scope, run_id
            ).with_suffix(".jsonl")
            storage.write_quarantine(rejected, quarantine)
            log.warning("[%s] %d rows rejected -> %s", scope, len(rejected), quarantine)

        if valid:
            parquet_file = storage.parquet_path(
                settings.parquet_dir, "prices", snapshot_date, scope, run_id
            )
            storage.write_parquet(valid, PRICE_SNAPSHOT_SCHEMA, parquet_file)
            table = storage.read_parquet(parquet_file, PRICE_SNAPSHOT_SCHEMA)

            with conn.transaction():  # load + state commit together, or not at all
                result = warehouse.load_price_snapshot(conn, table)
                newest = max(r["price_updated_at"] for r in valid)
                warehouse.set_high_water_mark(conn, "prices", scope, newest)
                hwm_after = warehouse.get_high_water_mark(conn, "prices", scope)
            task.new_versions, task.stations_upserted = (
                result.new_versions,
                result.stations_upserted,
            )

        task.warning = freshness_warning(hwm_after, now, settings.stale_source_hours)
        if task.warning:
            log.warning("[%s] %s", scope, task.warning)
        log.info(
            "[%s] extracted=%d rejected=%d new_price_versions=%d",
            scope,
            task.rows_extracted,
            task.rows_rejected,
            task.new_versions,
        )
    except Exception as exc:  # one bad task must not stop the whole run
        log.exception("[%s] task failed", scope)
        task.status, task.error = "failed", f"{type(exc).__name__}: {exc}"
        task.new_versions = task.stations_upserted = 0
        hwm_after = hwm_before

    _record(
        conn,
        task,
        run_id,
        started_at,
        client,
        requests_before,
        high_water_before=hwm_before,
        high_water_after=hwm_after,
        parquet_path=str(parquet_file) if parquet_file else None,
    )
    return task


def run_pipeline(
    settings: Settings,
    fuel_type_ids: list[int] | None = None,
    district_ids: list[int] | None = None,
    transform: bool = True,
    client: DgegClient | None = None,
) -> RunSummary:
    run_id = str(uuid.uuid4())
    now = _utcnow().replace(microsecond=0)
    summary = RunSummary(run_id=run_id)
    client = client or DgegClient(
        base_url=settings.api_base_url,
        page_size=settings.page_size,
        request_delay_seconds=settings.request_delay_seconds,
    )
    fuels = fuel_type_ids or list(settings.fuel_type_ids)
    districts = district_ids or list(settings.district_ids) or [None]
    log.info("Run %s started: %d fuel(s) x %d scope(s)", run_id, len(fuels), len(districts))

    with warehouse.connect(settings.database_url) as conn:
        warehouse.bootstrap(conn)
        summary.tasks.append(run_fuel_types_task(conn, client, settings, run_id, now))
        for fuel_type_id in fuels:
            for district_id in districts:
                summary.tasks.append(
                    run_prices_task(conn, client, settings, fuel_type_id, district_id, run_id, now)
                )
        if transform:
            summary.models_built = warehouse.run_transforms(conn)

    log.info(
        "Run %s finished: %d tasks, %d failed, %d API requests",
        run_id,
        len(summary.tasks),
        len(summary.failed),
        client.requests_made,
    )
    return summary


def replay(settings: Settings, paths: list[Path], transform: bool = True) -> int:
    """Reload landed Parquet files into PostgreSQL without calling the API (idempotent)."""
    files = sorted(f for p in paths for f in ([p] if p.is_file() else Path(p).rglob("*.parquet")))
    new_versions = 0
    with warehouse.connect(settings.database_url) as conn:
        warehouse.bootstrap(conn)
        for file in files:
            dataset = storage.dataset_from_path(file)
            table = storage.read_parquet(file, SCHEMAS[dataset])
            with conn.transaction():
                if dataset == "prices":
                    new_versions += warehouse.load_price_snapshot(conn, table).new_versions
                else:
                    warehouse.load_fuel_types(conn, table)
            log.info("Replayed %s", file)
        if transform:
            warehouse.run_transforms(conn)
    return new_versions
