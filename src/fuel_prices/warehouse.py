"""PostgreSQL side of the pipeline: bootstrap, idempotent loads, state and transforms."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from importlib import resources as pkg_resources
from typing import Any

import psycopg
import pyarrow as pa
from psycopg import sql

log = logging.getLogger(__name__)

ARROW_TO_PG = {
    pa.int16(): "SMALLINT",
    pa.int32(): "INTEGER",
    pa.int64(): "BIGINT",
    pa.float64(): "DOUBLE PRECISION",
    pa.string(): "TEXT",
    pa.bool_(): "BOOLEAN",
    pa.date32(): "DATE",
}


def connect(database_url: str) -> psycopg.Connection:
    """Autocommit connection: every unit of work opens an explicit `conn.transaction()`."""
    return psycopg.connect(database_url, autocommit=True)


def _sql_files(folder: str) -> list[tuple[str, str]]:
    root = pkg_resources.files("fuel_prices") / "sql" / folder
    files = sorted((f for f in root.iterdir() if f.name.endswith(".sql")), key=lambda f: f.name)
    return [(f.name, f.read_text(encoding="utf-8")) for f in files]


def bootstrap(conn: psycopg.Connection) -> None:
    """Create schemas and tables if they do not exist (safe to run every time)."""
    with conn.transaction():
        for _name, statement in _sql_files("init"):
            conn.execute(statement)


# ---------------------------------------------------------------------------- staging
def _pg_type(arrow_type: pa.DataType) -> str:
    if pa.types.is_timestamp(arrow_type):
        return "TIMESTAMPTZ" if arrow_type.tz else "TIMESTAMP"
    if pa.types.is_decimal(arrow_type):
        return f"NUMERIC({arrow_type.precision},{arrow_type.scale})"
    return ARROW_TO_PG[arrow_type]


def stage(cur: psycopg.Cursor, name: str, table: pa.Table) -> None:
    """Bulk-load an Arrow table into a temporary table with COPY (dropped at commit)."""
    columns = [
        sql.SQL("{} {}").format(sql.Identifier(f.name), sql.SQL(_pg_type(f.type)))
        for f in table.schema
    ]
    cur.execute(
        sql.SQL("CREATE TEMP TABLE {} ({}) ON COMMIT DROP").format(
            sql.Identifier(name), sql.SQL(", ").join(columns)
        )
    )
    copy_stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(
        sql.Identifier(name), sql.SQL(", ").join(map(sql.Identifier, table.schema.names))
    )
    with cur.copy(copy_stmt) as copy:
        for row in table.to_pylist():
            copy.write_row(list(row.values()))


# ---------------------------------------------------------------------------- loads
@dataclass
class PriceLoadResult:
    stations_upserted: int
    new_versions: int
    versions_seen: int


UPSERT_STATIONS = """
INSERT INTO raw.stations AS s (
    station_id, station_name, brand, station_type, district, municipality, locality,
    address, postal_code, latitude, longitude, first_seen_at, last_seen_at, _run_id
)
SELECT DISTINCT ON (station_id)
    station_id, station_name, brand, station_type, district, municipality, locality,
    address, postal_code, latitude, longitude, _snapshot_at, _snapshot_at, _run_id::uuid
FROM _stage_prices
ORDER BY station_id, price_updated_at DESC
ON CONFLICT (station_id) DO UPDATE SET
    station_name  = EXCLUDED.station_name,
    brand         = EXCLUDED.brand,
    station_type  = EXCLUDED.station_type,
    district      = EXCLUDED.district,
    municipality  = EXCLUDED.municipality,
    locality      = EXCLUDED.locality,
    address       = EXCLUDED.address,
    postal_code   = EXCLUDED.postal_code,
    latitude      = COALESCE(EXCLUDED.latitude, s.latitude),
    longitude     = COALESCE(EXCLUDED.longitude, s.longitude),
    first_seen_at = LEAST(s.first_seen_at, EXCLUDED.first_seen_at),
    last_seen_at  = GREATEST(s.last_seen_at, EXCLUDED.last_seen_at),
    _run_id       = EXCLUDED._run_id
"""

# Change detection on the natural key: a (station, fuel, price_updated_at) that is not in the
# history yet is a NEW price version -> inserted. One that already exists only gets its
# last_seen_at refreshed. `xmax = 0` is PostgreSQL's way of telling inserts from updates.
UPSERT_PRICE_VERSIONS = """
INSERT INTO raw.price_history AS h (
    station_id, fuel_type_id, price_updated_at, price_eur, first_seen_at, last_seen_at, _run_id
)
SELECT DISTINCT ON (station_id, fuel_type_id, price_updated_at)
    station_id, fuel_type_id, price_updated_at, price_eur, _snapshot_at, _snapshot_at, _run_id::uuid
FROM _stage_prices
ORDER BY station_id, fuel_type_id, price_updated_at, _snapshot_at DESC
ON CONFLICT (station_id, fuel_type_id, price_updated_at) DO UPDATE SET
    last_seen_at = GREATEST(h.last_seen_at, EXCLUDED.last_seen_at),
    -- a station may correct a typo without a new timestamp; never let an older replay win
    price_eur    = CASE WHEN EXCLUDED.last_seen_at >= h.last_seen_at
                        THEN EXCLUDED.price_eur ELSE h.price_eur END
RETURNING (xmax = 0) AS inserted
"""

UPSERT_FUEL_TYPES = """
INSERT INTO raw.fuel_types (fuel_type_id, fuel_type, unit, is_road_fuel, is_active,
                            _snapshot_at, _run_id)
SELECT fuel_type_id, fuel_type, unit, is_road_fuel, is_active, _snapshot_at, _run_id::uuid
FROM _stage_fuel_types
ON CONFLICT (fuel_type_id) DO UPDATE SET
    fuel_type = EXCLUDED.fuel_type, unit = EXCLUDED.unit, is_road_fuel = EXCLUDED.is_road_fuel,
    is_active = EXCLUDED.is_active, _snapshot_at = EXCLUDED._snapshot_at, _run_id = EXCLUDED._run_id
"""


def load_price_snapshot(conn: psycopg.Connection, table: pa.Table) -> PriceLoadResult:
    """Merge one price snapshot into raw.stations and raw.price_history (call in a transaction)."""
    if table.num_rows == 0:
        return PriceLoadResult(0, 0, 0)
    with conn.cursor() as cur:
        stage(cur, "_stage_prices", table)
        cur.execute(UPSERT_STATIONS)
        stations = cur.rowcount
        cur.execute(UPSERT_PRICE_VERSIONS)
        flags = [row[0] for row in cur.fetchall()]
    return PriceLoadResult(stations, sum(flags), len(flags))


def load_fuel_types(conn: psycopg.Connection, table: pa.Table) -> int:
    if table.num_rows == 0:
        return 0
    with conn.cursor() as cur:
        stage(cur, "_stage_fuel_types", table)
        cur.execute(UPSERT_FUEL_TYPES)
        return cur.rowcount


# ---------------------------------------------------------------------------- state
def get_high_water_mark(conn: psycopg.Connection, dataset: str, scope: str) -> datetime | None:
    row = conn.execute(
        "SELECT high_water_mark FROM meta.pipeline_state WHERE dataset = %s AND scope = %s",
        (dataset, scope),
    ).fetchone()
    return row[0] if row else None


def set_high_water_mark(conn: psycopg.Connection, dataset: str, scope: str, value: datetime):
    """Advance the mark; GREATEST() makes sure it never moves backwards."""
    conn.execute(
        """
        INSERT INTO meta.pipeline_state (dataset, scope, high_water_mark)
        VALUES (%s, %s, %s)
        ON CONFLICT (dataset, scope) DO UPDATE
        SET high_water_mark = GREATEST(meta.pipeline_state.high_water_mark,
                                       EXCLUDED.high_water_mark),
            updated_at      = now()
        """,
        (dataset, scope, value),
    )


def log_run(conn: psycopg.Connection, **fields: Any) -> None:
    columns = list(fields)
    conn.execute(
        sql.SQL("INSERT INTO meta.pipeline_runs ({cols}) VALUES ({vals})").format(
            cols=sql.SQL(", ").join(map(sql.Identifier, columns)),
            vals=sql.SQL(", ").join(sql.Placeholder() * len(columns)),
        ),
        list(fields.values()),
    )


# ---------------------------------------------------------------------------- transform
def run_transforms(conn: psycopg.Connection) -> list[str]:
    """Rebuild the analytics layer atomically: BI users never see a half-built model.

    Dates are computed in Lisbon time, the time zone the prices are published in.
    """
    applied = []
    with conn.transaction():
        conn.execute("SET LOCAL timezone = 'Europe/Lisbon'")
        for name, statement in _sql_files("transform"):
            log.info("Building model %s", name)
            conn.execute(statement)
            applied.append(name)
    return applied
