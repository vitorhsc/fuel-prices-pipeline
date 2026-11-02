"""End-to-end: fake DGEG API -> validation -> Parquet -> real PostgreSQL -> analytics tables."""

from decimal import Decimal

import pytest

from fuel_prices.pipeline import replay, run_pipeline
from tests.fake_dgeg import LISBON, FakeDgeg, Station, minutes_ago

pytestmark = pytest.mark.integration

DIESEL, PETROL = 2101, 3201
BP, INTERMARCHE, GALP, PORTO = 94483, 67027, 93507, 93455


def seed(fake: FakeDgeg) -> dict:
    """Three stations in Braga district + one in Porto, two fuels, plus one broken row."""
    t = {"old": minutes_ago(days=3), "recent": minutes_ago(hours=5)}
    fake.add_station(Station(BP, "BP Barcelos - Ucha", "BP", "Barcelos"))
    fake.add_station(
        Station(INTERMARCHE, "Intermarché Barcelos", "Intermarché", "Barcelos",
                station_type="Área comercial (Hipermercados)")
    )  # fmt: skip
    fake.add_station(Station(GALP, "Galp Braga", "GALP", "Braga", lat=41.5413, lon=-8.4257))
    fake.add_station(Station(PORTO, "OZ Energia Sanfins", "OZ Energia", "Paços de Ferreira",
                             district="Porto"))  # fmt: skip
    fake.set_price(BP, DIESEL, "1.973", t["old"])
    fake.set_price(INTERMARCHE, DIESEL, "1.899", t["recent"])
    fake.set_price(GALP, DIESEL, "1.989", t["recent"])
    fake.set_price(PORTO, DIESEL, "1.919", t["recent"])
    fake.set_price(BP, PETROL, "2.065", t["recent"])
    fake.set_price(PORTO, PETROL, "sem preço", t["recent"])  # broken price text
    fake.set_price(GALP, PETROL, "2.059", t["recent"])
    return t


def test_first_run_builds_warehouse_from_scratch(fake, settings, client, query):
    t = seed(fake)

    summary = run_pipeline(settings, client=client)

    assert summary.ok, summary.failed
    by_scope = {s.scope: s for s in summary.tasks}
    diesel, petrol = by_scope["fuel=2101_district=all"], by_scope["fuel=3201_district=all"]
    assert (diesel.rows_extracted, diesel.new_versions) == (4, 4)
    assert (petrol.rows_extracted, petrol.rows_rejected, petrol.new_versions) == (3, 1, 2)

    # landing zone + quarantine
    assert len(list(settings.parquet_dir.rglob("*.parquet"))) == 3  # fuel types + 2 fuels
    quarantined = list(settings.quarantine_dir.rglob("*.jsonl"))
    assert len(quarantined) == 1 and "unparseable price" in quarantined[0].read_text()

    # raw layer
    assert query("SELECT count(*) FROM raw.stations") == [(4,)]
    assert query("SELECT count(*) FROM raw.price_history") == [(6,)]
    assert query(
        "SELECT price_eur FROM raw.price_history WHERE station_id = %s AND fuel_type_id = %s",
        (BP, DIESEL),
    ) == [(Decimal("1.973"),)]

    # analytics layer
    assert query("SELECT brand_segment FROM analytics.dim_station ORDER BY station_id") == [
        ("Hypermarket",),  # 67027 Intermarché
        ("Independent / low-cost",),  # 93455 OZ Energia
        ("Major oil company",),  # 93507 Galp
        ("Major oil company",),  # 94483 BP
    ]
    cheapest_barcelos = query(
        "SELECT price_rank, station_id FROM analytics.rpt_cheapest_stations "
        "WHERE municipality = 'Barcelos' AND fuel_type_id = %s ORDER BY price_rank",
        (DIESEL,),
    )
    assert cheapest_barcelos == [(1, INTERMARCHE), (2, BP)]
    national = query(
        "SELECT stations, min_price_eur, max_price_eur FROM analytics.agg_daily_prices "
        "WHERE geo_level = 'national' AND fuel_type_id = %s AND price_date = current_date",
        (DIESEL,),
    )
    assert national == [(4, Decimal("1.899"), Decimal("1.989"))]

    # a price set 3 days ago is expanded into one row per day until today
    days = query(
        "SELECT count(*) FROM analytics.fct_daily_station_price "
        "WHERE station_id = %s AND fuel_type_id = %s",
        (BP, DIESEL),
    )[0][0]
    assert days in (3, 4)  # 4 calendar days, or 3 when "3 days ago" crosses midnight in Lisbon

    # state and audit
    assert query(
        "SELECT high_water_mark FROM meta.pipeline_state WHERE scope = 'fuel=2101_district=all'"
    ) == [(t["recent"],)]
    assert query("SELECT count(*) FROM meta.pipeline_runs WHERE status = 'success'") == [(3,)]


def test_second_run_only_inserts_changed_prices(fake, settings, client, query):
    t = seed(fake)
    run_pipeline(settings, client=client)

    changed_at = minutes_ago(minutes=30)
    fake.set_price(BP, DIESEL, "1.949", changed_at)  # BP lowers diesel by 2.4 cents
    summary = run_pipeline(settings, client=client)

    new = {s.scope: s.new_versions for s in summary.tasks if s.dataset == "prices"}
    assert new == {"fuel=2101_district=all": 1, "fuel=3201_district=all": 0}
    assert query("SELECT count(*) FROM raw.price_history") == [(7,)]

    history = query(
        "SELECT price_eur, previous_price_eur, price_change_eur, is_current "
        "FROM analytics.fct_price_changes WHERE station_id = %s AND fuel_type_id = %s "
        "ORDER BY valid_from",
        (BP, DIESEL),
    )
    assert history == [
        (Decimal("1.973"), None, None, False),
        (Decimal("1.949"), Decimal("1.973"), Decimal("-0.024"), True),
    ]
    # the new price is today's price; the old one is still valid for the previous days
    assert query(
        "SELECT price_eur FROM analytics.fct_daily_station_price "
        "WHERE station_id = %s AND fuel_type_id = %s AND price_date = %s",
        (BP, DIESEL, t["old"].astimezone(LISBON).date()),
    ) == [(Decimal("1.973"),)]
    assert query(
        "SELECT high_water_mark FROM meta.pipeline_state WHERE scope = 'fuel=2101_district=all'"
    ) == [(changed_at,)]


def test_replaying_old_snapshots_is_idempotent(fake, settings, client, query):
    seed(fake)
    run_pipeline(settings, client=client)
    first_files = list(settings.parquet_dir.rglob("*.parquet"))

    fake.set_price(BP, DIESEL, "1.949", minutes_ago(minutes=30))
    run_pipeline(settings, client=client)
    before = query("SELECT * FROM raw.price_history ORDER BY 1, 2, 3")

    replayed_new_versions = replay(settings, first_files)

    assert replayed_new_versions == 0
    assert query("SELECT * FROM raw.price_history ORDER BY 1, 2, 3") == before


def test_failures_are_isolated_per_task(fake, settings, client, query):
    seed(fake)
    fake.failing_fuels.add(PETROL)  # HTTP 503 on every retry

    summary = run_pipeline(settings, client=client)

    assert [(t.scope, t.status) for t in summary.failed] == [("fuel=3201_district=all", "failed")]
    assert query(
        "SELECT status, error_message LIKE 'HTTPError%%' FROM meta.pipeline_runs "
        "WHERE scope = 'fuel=3201_district=all'"
    ) == [("failed", True)]
    assert query("SELECT count(*) FROM analytics.fct_price_changes") == [(4,)]  # diesel only
    assert query(
        "SELECT count(*) FROM meta.pipeline_state WHERE scope = 'fuel=3201_district=all'"
    ) == [(0,)]


def test_api_error_envelope_fails_the_task(fake, settings, client, query):
    seed(fake)
    fake.error_fuels.add(DIESEL)
    summary = run_pipeline(settings, client=client)
    assert summary.failed[0].error.startswith("DgegApiError")
    assert "Erro interno" in summary.failed[0].error


def test_district_scope_and_stale_source_warning(fake, settings, client, query):
    fake.add_station(Station(BP, "BP Barcelos - Ucha", "BP", "Barcelos"))
    fake.add_station(Station(PORTO, "OZ Sanfins", "OZ Energia", "Paços de Ferreira", "Porto"))
    fake.set_price(BP, DIESEL, "1.973", minutes_ago(days=5))
    fake.set_price(PORTO, DIESEL, "1.919", minutes_ago(days=5))

    summary = run_pipeline(settings, fuel_type_ids=[DIESEL], district_ids=[3], client=client)

    task = next(t for t in summary.tasks if t.dataset == "prices")
    assert task.scope == "fuel=2101_district=3" and task.rows_extracted == 1  # Braga only
    assert "stale source" in task.warning
    assert query("SELECT warning_message IS NOT NULL FROM meta.pipeline_runs "
                 "WHERE dataset = 'prices'") == [(True,)]  # fmt: skip
