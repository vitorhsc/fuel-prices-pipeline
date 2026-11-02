import pytest
import requests
import responses

from fuel_prices.client import DgegApiError, DgegClient
from tests.fake_dgeg import API, envelope

URL = f"{API}/PesquisarPostos"


def make_client(sleeps: list[float], **kwargs) -> DgegClient:
    defaults = {"page_size": 2, "request_delay_seconds": 0, "sleep": sleeps.append}
    return DgegClient(API, **(defaults | kwargs))


def rows(*ids: int, total: int) -> list[dict]:
    return [{"Id": i, "Quantidade": total} for i in ids]


@responses.activate
def test_paginates_until_quantidade_is_reached():
    responses.get(URL, json=envelope(rows(1, 2, total=5)))
    responses.get(URL, json=envelope(rows(3, 4, total=5)))
    responses.get(URL, json=envelope(rows(5, total=5)))

    items = list(make_client([]).paginate("PesquisarPostos", {"idsTiposComb": 2101}))

    assert [i["Id"] for i in items] == [1, 2, 3, 4, 5]
    assert len(responses.calls) == 3  # no extra request for an empty 4th page
    last = responses.calls[2].request.url
    assert "pagina=3" in last and "qtdPorPagina=2" in last and "idsTiposComb=2101" in last


@responses.activate
def test_stops_on_exact_multiple_without_requesting_an_empty_page():
    responses.get(URL, json=envelope(rows(1, 2, total=4)))
    responses.get(URL, json=envelope(rows(3, 4, total=4)))
    assert len(list(make_client([]).paginate("PesquisarPostos"))) == 4
    assert len(responses.calls) == 2


@responses.activate
def test_empty_result_is_not_an_error():
    responses.get(URL, json=envelope([]))
    assert list(make_client([]).paginate("PesquisarPostos")) == []


@responses.activate
def test_envelope_with_status_false_raises():
    responses.get(URL, json=envelope(None, status=False, mensagem="Erro interno"))
    with pytest.raises(DgegApiError, match="Erro interno"):
        make_client([]).get("PesquisarPostos")


@responses.activate
def test_unexpected_payload_shape_raises():
    responses.get(URL, json=["not", "an", "envelope"])
    with pytest.raises(DgegApiError, match="unexpected response shape"):
        make_client([]).get("PesquisarPostos")


@responses.activate
def test_pauses_between_requests_to_be_polite():
    responses.get(URL, json=envelope(rows(1, 2, total=3)))
    responses.get(URL, json=envelope(rows(3, total=3)))
    sleeps: list[float] = []
    list(make_client(sleeps, request_delay_seconds=0.5).paginate("PesquisarPostos"))
    assert sleeps == [0.5]  # no pause before the very first request


@responses.activate
def test_retries_server_errors_with_exponential_backoff():
    responses.get(URL, status=502)
    responses.get(URL, status=503)
    responses.get(URL, json=envelope([]))
    sleeps: list[float] = []
    make_client(sleeps, backoff_seconds=1.0).get("PesquisarPostos")
    assert sleeps == [1.0, 2.0]


@responses.activate
def test_honours_retry_after_on_429():
    responses.get(URL, status=429, headers={"Retry-After": "7"})
    responses.get(URL, json=envelope([]))
    sleeps: list[float] = []
    make_client(sleeps).get("PesquisarPostos")
    assert sleeps == [7.0]


@responses.activate
def test_gives_up_after_max_retries():
    responses.get(URL, status=503)
    sleeps: list[float] = []
    with pytest.raises(requests.HTTPError):
        make_client(sleeps, max_retries=2, backoff_seconds=1.0).get("PesquisarPostos")
    assert sleeps == [1.0, 2.0]


@responses.activate
def test_client_errors_are_not_retried():
    responses.get(URL, status=404)
    sleeps: list[float] = []
    with pytest.raises(requests.HTTPError):
        make_client(sleeps).get("PesquisarPostos")
    assert sleeps == [] and len(responses.calls) == 1


@responses.activate
def test_page_safety_limit():
    responses.get(URL, json=envelope(rows(1, 2, total=999)))
    with pytest.raises(DgegApiError, match="safety limit"):
        list(make_client([], max_pages=3).paginate("PesquisarPostos"))
