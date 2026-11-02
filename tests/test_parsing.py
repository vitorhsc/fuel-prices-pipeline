from datetime import UTC, datetime
from decimal import Decimal

import pytest

from fuel_prices.parsing import (
    clean_coordinates,
    clean_text,
    normalize_fuel_type,
    normalize_price_row,
    parse_local_datetime,
    parse_price,
    validate_price_row,
)
from tests.fake_dgeg import FUEL_TYPES

SNAPSHOT = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

# A real row returned by PesquisarPostos (Barcelos, 28 Sep 2026)
REAL_ROW = {
    "Id": 94483,
    "Nome": "BP Barcelos - Ucha (Carbuiberia)",
    "TipoPosto": "Outro",
    "Municipio": "Barcelos",
    "Preco": "1,973 €",
    "Marca": "BP",
    "Combustivel": "Gasóleo simples",
    "DataAtualizacao": "2026-09-28 13:00",
    "Distrito": "Braga",
    "Morada": "EN 205, km 33,300 - Lugar de Crasto  - Ucha de São Romão",
    "Localidade": "Ucha",
    "CodPostal": "4750-767",
    "Latitude": 41.57254,
    "Longitude": -8.523811,
    "Quantidade": 247,
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,973 €", Decimal("1.973")),
        ("1,973 €/litro", Decimal("1.973")),
        ("0,899 €/kg", Decimal("0.899")),
        ("2.065", Decimal("2.065")),
        ("n/d", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_price_handles_portuguese_formats(raw, expected):
    assert parse_price(raw) == expected


def test_local_lisbon_time_is_converted_to_utc_in_summer_and_winter():
    summer = parse_local_datetime("2026-09-28 13:00")  # WEST = UTC+1
    winter = parse_local_datetime("2026-12-01 13:00")  # WET  = UTC+0
    assert summer == datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    assert winter == datetime(2026, 12, 1, 13, 0, tzinfo=UTC)
    assert parse_local_datetime("not a date") is None


def test_clean_text_collapses_whitespace():
    assert clean_text("OZ ENERGIA -  SANTA CATARINA ") == "OZ ENERGIA - SANTA CATARINA"
    assert clean_text("   ") is None


def test_normalize_real_row():
    row = normalize_price_row(REAL_ROW, 2101, SNAPSHOT, "run-1")
    assert row["station_id"] == 94483
    assert row["price_eur"] == Decimal("1.973")
    assert row["price_updated_at"] == datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    assert row["address"] == "EN 205, km 33,300 - Lugar de Crasto - Ucha de São Romão"
    assert row["fuel_type_id"] == 2101
    assert validate_price_row(row, SNAPSHOT) is None


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"Preco": "n/d"}, "unparseable price"),
        ({"Preco": "19,73 €"}, "price out of range"),
        ({"Preco": "0,010 €"}, "price out of range"),
        ({"DataAtualizacao": ""}, "unparseable update timestamp"),
        ({"DataAtualizacao": "2026-10-05 10:00"}, "in the future"),
        ({"Id": None}, "missing station id"),
    ],
)
def test_invalid_rows_are_rejected_with_a_reason(change, reason):
    row = normalize_price_row(REAL_ROW | change, 2101, SNAPSHOT, "run-1")
    assert reason in validate_price_row(row, SNAPSHOT)


def test_coordinates_outside_mainland_are_nulled():
    row = normalize_price_row(REAL_ROW | {"Latitude": 0, "Longitude": 0}, 2101, SNAPSHOT, "r")
    assert clean_coordinates(row)["latitude"] is None
    row = normalize_price_row(REAL_ROW | {"Latitude": 48.85}, 2101, SNAPSHOT, "r")
    assert clean_coordinates(row)["longitude"] is None
    row = normalize_price_row(REAL_ROW, 2101, SNAPSHOT, "r")
    assert clean_coordinates(row)["latitude"] == pytest.approx(41.57254)


def test_normalize_fuel_type():
    row = normalize_fuel_type(FUEL_TYPES[0], SNAPSHOT, "run-1")
    assert row == {
        "fuel_type_id": 2101,
        "fuel_type": "Gasóleo simples",
        "unit": "litro",
        "is_road_fuel": True,
        "is_active": True,
        "_snapshot_at": SNAPSHOT,
        "_run_id": "run-1",
    }
