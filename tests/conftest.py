import os

import psycopg
import pytest
import responses

from fuel_prices.client import DgegClient
from fuel_prices.config import Settings
from tests.fake_dgeg import API, FakeDgeg


@pytest.fixture
def db_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set - skipping PostgreSQL integration test")
    if "test" not in psycopg.conninfo.conninfo_to_dict(url).get("dbname", ""):
        pytest.fail("Refusing to run: TEST_DATABASE_URL must point to a *test* database")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS raw, analytics, meta CASCADE")
    return url


@pytest.fixture
def settings(db_url, tmp_path) -> Settings:
    return Settings(
        database_url=db_url,
        fuel_type_ids=(2101, 3201),
        api_base_url=API,
        page_size=2,  # tiny pages so pagination is exercised
        request_delay_seconds=0,
        parquet_dir=tmp_path / "raw",
        quarantine_dir=tmp_path / "quarantine",
    )


@pytest.fixture
def client() -> DgegClient:
    return DgegClient(
        API, page_size=2, max_retries=1, request_delay_seconds=0, sleep=lambda s: None
    )


@pytest.fixture
def fake():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        api = FakeDgeg()
        api.install(rsps)
        yield api


@pytest.fixture
def query(db_url):
    def _query(sql: str, params=None):
        with psycopg.connect(db_url) as conn:
            return conn.execute(sql, params).fetchall()

    return _query
