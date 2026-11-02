"""Pipeline settings: a YAML file for behaviour, environment variables for connections."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

DEFAULT_CONFIG_PATH = "config/pipeline.yaml"
DEFAULT_API_URL = "https://precoscombustiveis.dgeg.gov.pt/api/PrecoComb"


class ConfigError(ValueError):
    """Raised when the configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    database_url: str
    fuel_type_ids: tuple[int, ...]
    district_ids: tuple[int, ...] = ()  # empty = the whole country in one query
    api_base_url: str = DEFAULT_API_URL
    page_size: int = 500
    request_delay_seconds: float = 0.5
    stale_source_hours: int = 72
    parquet_dir: Path = field(default_factory=lambda: Path("data/raw"))
    quarantine_dir: Path = field(default_factory=lambda: Path("data/quarantine"))


def _int_list(value, name: str) -> tuple[int, ...]:
    try:
        return tuple(int(v) for v in (value or []))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} must be a list of integer ids") from exc


def load_settings(config_path: str | os.PathLike[str] | None = None) -> Settings:
    """Build Settings from the YAML file plus environment variables (.env is supported)."""
    load_dotenv()
    path = Path(config_path or os.getenv("PIPELINE_CONFIG", DEFAULT_CONFIG_PATH))
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    source = raw.get("source", {})
    quality = raw.get("quality", {})
    storage = raw.get("storage", {})

    fuel_type_ids = _int_list(source.get("fuel_type_ids"), "source.fuel_type_ids")
    if not fuel_type_ids:
        raise ConfigError("source.fuel_type_ids must list at least one fuel type id")

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise ConfigError("DATABASE_URL environment variable is not set (see .env.example)")

    page_size = int(source.get("page_size", 500))
    if not 1 <= page_size <= 2000:
        raise ConfigError("source.page_size must be between 1 and 2000")

    return Settings(
        database_url=database_url,
        fuel_type_ids=fuel_type_ids,
        district_ids=_int_list(source.get("district_ids"), "source.district_ids"),
        api_base_url=os.getenv("DGEG_API_URL", source.get("api_base_url", DEFAULT_API_URL)),
        page_size=page_size,
        request_delay_seconds=float(source.get("request_delay_seconds", 0.5)),
        stale_source_hours=int(quality.get("stale_source_hours", 72)),
        parquet_dir=Path(os.getenv("PARQUET_DIR", storage.get("parquet_dir", "data/raw"))),
        quarantine_dir=Path(storage.get("quarantine_dir", "data/quarantine")),
    )
