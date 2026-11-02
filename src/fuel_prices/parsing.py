"""Turn raw API rows into clean, typed records - and reject the ones we cannot trust.

Real-world quirks handled here:
  * prices are Portuguese-formatted strings: "1,973 €" or "1,973 €/litro"  -> Decimal("1.973")
  * timestamps are local Lisbon time without offset: "2026-09-28 13:00"   -> aware UTC datetime
  * names contain stray whitespace: "OZ ENERGIA -  SANTA CATARINA"         -> single spaces
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pyarrow as pa

LISBON = ZoneInfo("Europe/Lisbon")
PRICE_PATTERN = re.compile(r"(\d+)[,.](\d{1,4})")
MIN_PRICE = Decimal("0.30")  # €/unit - anything outside this range is a data error
MAX_PRICE = Decimal("5.00")
MAINLAND_BBOX = (36.8, 42.2, -9.6, -6.1)  # lat_min, lat_max, lon_min, lon_max
FUTURE_TOLERANCE = timedelta(hours=2)  # clock skew / updates made while the run is going

TS = pa.timestamp("us", tz="UTC")
PRICE = pa.decimal128(6, 3)

PRICE_SNAPSHOT_SCHEMA = pa.schema(
    [
        pa.field("station_id", pa.int64(), nullable=False),
        pa.field("station_name", pa.string()),
        pa.field("brand", pa.string()),
        pa.field("station_type", pa.string()),
        pa.field("district", pa.string()),
        pa.field("municipality", pa.string()),
        pa.field("locality", pa.string()),
        pa.field("address", pa.string()),
        pa.field("postal_code", pa.string()),
        pa.field("latitude", pa.float64()),
        pa.field("longitude", pa.float64()),
        pa.field("fuel_type_id", pa.int32(), nullable=False),
        pa.field("fuel_type", pa.string()),
        pa.field("price_eur", PRICE, nullable=False),
        pa.field("price_updated_at", TS, nullable=False),
        pa.field("_snapshot_at", TS, nullable=False),
        pa.field("_run_id", pa.string(), nullable=False),
    ]
)

FUEL_TYPES_SCHEMA = pa.schema(
    [
        pa.field("fuel_type_id", pa.int32(), nullable=False),
        pa.field("fuel_type", pa.string()),
        pa.field("unit", pa.string()),
        pa.field("is_road_fuel", pa.bool_()),
        pa.field("is_active", pa.bool_()),
        pa.field("_snapshot_at", TS, nullable=False),
        pa.field("_run_id", pa.string(), nullable=False),
    ]
)


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def parse_price(value: Any) -> Decimal | None:
    """'1,973 €' / '1,973 €/litro' / '1.973' -> Decimal('1.973'); anything else -> None."""
    if value is None:
        return None
    match = PRICE_PATTERN.search(str(value))
    return Decimal(f"{match.group(1)}.{match.group(2)}") if match else None


def parse_local_datetime(value: Any, tz: ZoneInfo = LISBON) -> datetime | None:
    """'2026-09-28 13:00' (Lisbon wall-clock time) -> timezone-aware UTC datetime."""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(value).strip(), fmt).replace(tzinfo=tz).astimezone(UTC)
        except ValueError:
            continue
    return None


def _coordinate(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number or None  # 0.0 means "unknown" in this API


def normalize_price_row(
    item: dict[str, Any], fuel_type_id: int, snapshot_at: datetime, run_id: str
) -> dict[str, Any]:
    return {
        "station_id": item.get("Id"),
        "station_name": clean_text(item.get("Nome")),
        "brand": clean_text(item.get("Marca")),
        "station_type": clean_text(item.get("TipoPosto")),
        "district": clean_text(item.get("Distrito")),
        "municipality": clean_text(item.get("Municipio")),
        "locality": clean_text(item.get("Localidade")),
        "address": clean_text(item.get("Morada")),
        "postal_code": clean_text(item.get("CodPostal")),
        "latitude": _coordinate(item.get("Latitude")),
        "longitude": _coordinate(item.get("Longitude")),
        "fuel_type_id": fuel_type_id,
        "fuel_type": clean_text(item.get("Combustivel")),
        "price_eur": parse_price(item.get("Preco")),
        "price_updated_at": parse_local_datetime(item.get("DataAtualizacao")),
        "_snapshot_at": snapshot_at,
        "_run_id": run_id,
    }


def validate_price_row(row: dict[str, Any], snapshot_at: datetime) -> str | None:
    """Return the reason a row must be rejected, or None when it is valid."""
    if not isinstance(row["station_id"], int):
        return "missing station id"
    if row["price_eur"] is None:
        return "unparseable price"
    if not MIN_PRICE <= row["price_eur"] <= MAX_PRICE:
        return f"price out of range ({row['price_eur']} €)"
    if row["price_updated_at"] is None:
        return "unparseable update timestamp"
    if row["price_updated_at"] > snapshot_at + FUTURE_TOLERANCE:
        return "update timestamp in the future"
    return None


def clean_coordinates(row: dict[str, Any]) -> dict[str, Any]:
    """Coordinates outside mainland Portugal are nulled (kept row, flagged value)."""
    lat, lon = row["latitude"], row["longitude"]
    lat_min, lat_max, lon_min, lon_max = MAINLAND_BBOX
    if lat is None or lon is None or not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
        row["latitude"] = row["longitude"] = None
    return row


def normalize_fuel_type(item: dict[str, Any], snapshot_at: datetime, run_id: str) -> dict[str, Any]:
    return {
        "fuel_type_id": item["Id"],
        "fuel_type": clean_text(item.get("Descritivo")),
        "unit": clean_text(item.get("UnidadeMedida")),
        "is_road_fuel": bool(item.get("fl_rodoviario")),
        "is_active": bool(item.get("fl_ativo")),
        "_snapshot_at": snapshot_at,
        "_run_id": run_id,
    }
