from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest

from fuel_prices import storage
from fuel_prices.config import ConfigError, load_settings
from fuel_prices.parsing import PRICE_SNAPSHOT_SCHEMA, normalize_price_row
from fuel_prices.pipeline import freshness_warning, price_scope, split_valid_rows
from tests.test_parsing import REAL_ROW, SNAPSHOT


def test_split_valid_rows_keeps_good_rows_and_explains_bad_ones():
    items = [REAL_ROW, REAL_ROW | {"Id": 2, "Preco": "sem preço"}]
    valid, rejected = split_valid_rows(items, 2101, SNAPSHOT, "run-1")
    assert [r["station_id"] for r in valid] == [94483]
    assert rejected == [("unparseable price", items[1])]


def test_freshness_warning_only_when_source_is_stale():
    now = datetime(2026, 9, 30, 12, tzinfo=UTC)
    assert freshness_warning(now - timedelta(hours=10), now, 72) is None
    assert "stale source" in freshness_warning(now - timedelta(hours=100), now, 72)
    assert freshness_warning(None, now, 72) is None


def test_price_scope_names():
    assert price_scope(2101, None) == "fuel=2101_district=all"
    assert price_scope(3201, 3) == "fuel=3201_district=3"


def test_parquet_layout_and_roundtrip(tmp_path):
    rows = [normalize_price_row(REAL_ROW, 2101, SNAPSHOT, "run-1")]
    path = storage.parquet_path(
        tmp_path, "prices", date(2026, 9, 30), "fuel=2101_district=all", "r1"
    )
    assert (
        path == tmp_path / "dgeg/prices/snapshot_date=2026-09-30/fuel=2101_district=all__r1.parquet"
    )
    assert storage.dataset_from_path(path) == "prices"

    storage.write_parquet(rows, PRICE_SNAPSHOT_SCHEMA, path)
    back = storage.read_parquet(path, PRICE_SNAPSHOT_SCHEMA).to_pylist()[0]

    assert back["price_eur"] == rows[0]["price_eur"]  # exact decimal, no float rounding
    assert back["price_updated_at"] == rows[0]["price_updated_at"]
    assert not list(path.parent.glob("*.tmp"))


def test_schema_drift_fails_fast(tmp_path):
    bad = normalize_price_row(REAL_ROW, 2101, SNAPSHOT, "run-1") | {"station_id": "abc"}
    with pytest.raises((pa.ArrowInvalid, pa.ArrowTypeError)):
        storage.write_parquet([bad], PRICE_SNAPSHOT_SCHEMA, tmp_path / "bad.parquet")


def test_quarantine_is_json_lines(tmp_path):
    path = storage.write_quarantine([("unparseable price", {"Id": 1})], tmp_path / "q.jsonl")
    assert path.read_text(encoding="utf-8").strip() == (
        '{"reason": "unparseable price", "row": {"Id": 1}}'
    )


def _config(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "pipeline.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def test_settings_from_yaml_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db/x")
    cfg = _config(tmp_path, "source:\n  fuel_type_ids: [2101, 3201]\n  district_ids: [3]\n")
    settings = load_settings(cfg)
    assert settings.fuel_type_ids == (2101, 3201)
    assert settings.district_ids == (3,)


def test_invalid_fuel_ids_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db/x")
    with pytest.raises(ConfigError, match="integer ids"):
        load_settings(_config(tmp_path, "source:\n  fuel_type_ids: [gasoleo]\n"))
    with pytest.raises(ConfigError, match="at least one"):
        load_settings(_config(tmp_path, "source:\n  fuel_type_ids: []\n"))


def test_missing_database_url_is_reported(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("fuel_prices.config.load_dotenv", lambda: None)
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        load_settings(_config(tmp_path, "source:\n  fuel_type_ids: [2101]\n"))
