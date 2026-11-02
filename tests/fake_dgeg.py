"""An in-memory fake of the DGEG API with the same envelope, pagination and field formats.

Payload shapes are copied from real responses of precoscombustiveis.dgeg.gov.pt/api/PrecoComb.
Used by the integration tests (through `responses`) and handy for offline demos.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

API = "https://dgeg.test/api/PrecoComb"
LISBON = ZoneInfo("Europe/Lisbon")

FUEL_TYPES = [
    {"Descritivo": "Gasóleo simples", "UnidadeMedida": "litro", "fl_ViewWebSite": True,
     "fl_rodoviario": True, "fl_ativo": True, "BackGroundColor": None, "Id": 2101},
    {"Descritivo": "Gasóleo especial", "UnidadeMedida": "litro", "fl_ViewWebSite": True,
     "fl_rodoviario": True, "fl_ativo": True, "BackGroundColor": None, "Id": 2105},
    {"Descritivo": "Gasolina simples 95", "UnidadeMedida": "litro", "fl_ViewWebSite": True,
     "fl_rodoviario": True, "fl_ativo": True, "BackGroundColor": None, "Id": 3201},
    {"Descritivo": "GPL Auto", "UnidadeMedida": "litro", "fl_ViewWebSite": True,
     "fl_rodoviario": True, "fl_ativo": True, "BackGroundColor": None, "Id": 1120},
    {"Descritivo": "Gasóleo de aquecimento", "UnidadeMedida": "litro", "fl_ViewWebSite": True,
     "fl_rodoviario": False, "fl_ativo": True, "BackGroundColor": None, "Id": 2155},
]  # fmt: skip
FUEL_NAMES = {f["Id"]: f["Descritivo"] for f in FUEL_TYPES}
DISTRICTS = {3: "Braga", 13: "Porto"}


def lisbon_text(dt: datetime) -> str:
    """UTC datetime -> the API's local wall-clock format, e.g. '2026-09-28 13:00'."""
    return dt.astimezone(LISBON).strftime("%Y-%m-%d %H:%M")


def pt_price(value: Decimal | str) -> str:
    """Decimal('1.973') -> '1,973 €' (the API's format)."""
    return value if isinstance(value, str) else f"{value:.3f}".replace(".", ",") + " €"


def minutes_ago(minutes: int = 0, hours: int = 0, days: int = 0) -> datetime:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    return now - timedelta(minutes=minutes, hours=hours, days=days)


@dataclass
class Station:
    id: int
    name: str
    brand: str
    municipality: str
    district: str = "Braga"
    station_type: str = "Outro"
    address: str = "EN 205, km 33"
    locality: str = "Ucha"
    postal_code: str = "4750-767"
    lat: float = 41.57254
    lon: float = -8.523811


@dataclass
class FakeDgeg:
    base_url: str = API
    stations: dict[int, Station] = field(default_factory=dict)
    prices: dict[tuple[int, int], tuple[Decimal | str, datetime]] = field(default_factory=dict)
    failing_fuels: set[int] = field(default_factory=set)
    error_fuels: set[int] = field(default_factory=set)
    calls: list[tuple[str, dict]] = field(default_factory=list)

    # ------------------------------------------------------------------ test helpers
    def add_station(self, station: Station) -> None:
        self.stations[station.id] = station

    def set_price(self, station_id: int, fuel_id: int, price, updated: datetime) -> None:
        self.prices[(station_id, fuel_id)] = (Decimal(price) if _num(price) else price, updated)

    # ------------------------------------------------------------------ API emulation
    def handle(self, url: str) -> tuple[int, dict]:
        parsed = urlparse(url)
        endpoint = parsed.path.rsplit("/", 1)[-1]
        params = {k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}
        self.calls.append((endpoint, params))

        if endpoint == "GetTiposCombustiveis":
            return 200, envelope(FUEL_TYPES)
        if endpoint == "GetDistritos":
            return 200, envelope([{"Descritivo": n, "Id": i} for i, n in DISTRICTS.items()])
        if endpoint == "PesquisarPostos":
            return self._search(params)
        return 404, {"message": "not found"}

    def _search(self, params: dict[str, str]) -> tuple[int, dict]:
        fuel_id = int(params["idsTiposComb"])
        if fuel_id in self.failing_fuels:
            return 503, {"message": "Service Unavailable"}
        if fuel_id in self.error_fuels:
            return 200, {"status": False, "mensagem": "Erro interno", "resultado": None}
        district = DISTRICTS.get(int(params["idDistrito"])) if params.get("idDistrito") else None

        rows = []
        for (station_id, fuel), (price, updated) in self.prices.items():
            station = self.stations[station_id]
            if fuel != fuel_id or (district and station.district != district):
                continue
            rows.append(self._row(station, fuel, price, updated))
        rows.sort(key=lambda r: (r["Preco"], r["Id"]))
        for row in rows:
            row["Quantidade"] = len(rows)

        size, page = int(params.get("qtdPorPagina", 10)), int(params.get("pagina", 1))
        return 200, envelope(rows[(page - 1) * size : page * size])

    @staticmethod
    def _row(station: Station, fuel_id: int, price, updated: datetime) -> dict:
        return {
            "Id": station.id,
            "Nome": station.name,
            "TipoPosto": station.station_type,
            "Municipio": station.municipality,
            "Preco": pt_price(price),
            "Marca": station.brand,
            "Combustivel": FUEL_NAMES[fuel_id],
            "DataAtualizacao": lisbon_text(updated),
            "Distrito": station.district,
            "Morada": station.address,
            "Localidade": station.locality,
            "CodPostal": station.postal_code,
            "Latitude": station.lat,
            "Longitude": station.lon,
            "Quantidade": 0,
        }

    # ------------------------------------------------------------------ adapters
    def install(self, rsps) -> None:
        """Register this fake on a `responses` mock."""

        def callback(request):
            status, body = self.handle(request.url)
            return status, {"Content-Type": "application/json"}, json.dumps(body)

        rsps.add_callback("GET", re.compile(re.escape(self.base_url) + r"/\w+"), callback=callback)


def envelope(resultado, status: bool = True, mensagem: str = "sucesso") -> dict:
    return {"status": status, "mensagem": mensagem, "resultado": resultado, "token": None}


def _num(value) -> bool:
    try:
        Decimal(str(value))
        return True
    except Exception:
        return False
