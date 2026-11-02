"""HTTP client for the DGEG fuel prices API (the API behind precoscombustiveis.dgeg.gov.pt).

Every response is wrapped in an envelope:
    {"status": true, "mensagem": "sucesso", "resultado": [...], "token": null}

The client takes care of:
  * envelope validation - `status: false` or an unexpected shape raises DgegApiError
  * page-number pagination - `pagina` / `qtdPorPagina`, stopping at the `Quantidade` total
  * polite usage        - a fixed delay between requests (it is a public service)
  * transient errors    - retries 5xx / 429 / network errors with exponential backoff
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

import requests

log = logging.getLogger(__name__)

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
USER_AGENT = "fuel-prices-pipeline (portfolio project; daily batch; +https://github.com/vitorhsc)"


class DgegApiError(RuntimeError):
    """The API answered, but not with the data we expected."""


class DgegClient:
    def __init__(
        self,
        base_url: str,
        page_size: int = 500,
        timeout: float = 30.0,
        max_retries: int = 5,
        backoff_seconds: float = 2.0,
        request_delay_seconds: float = 0.5,
        max_pages: int = 500,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.page_size = page_size
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.request_delay_seconds = request_delay_seconds
        self.max_pages = max_pages
        self._sleep = sleep
        self.requests_made = 0

        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": USER_AGENT})

    # ------------------------------------------------------------------ public API
    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        """Call an endpoint and return the `resultado` part of the envelope."""
        payload = self._request(f"{self.base_url}/{endpoint}", params).json()
        if not isinstance(payload, dict) or "resultado" not in payload:
            raise DgegApiError(f"{endpoint}: unexpected response shape")
        if payload.get("status") is not True:
            raise DgegApiError(f"{endpoint}: API returned an error: {payload.get('mensagem')!r}")
        return payload["resultado"]

    def paginate(self, endpoint: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
        """Yield every row of a paginated endpoint.

        Each row carries `Quantidade` (the total number of rows for the query), which tells
        us when to stop without requesting an extra empty page.
        """
        fetched = 0
        for page in range(1, self.max_pages + 1):
            query = {**(params or {}), "qtdPorPagina": self.page_size, "pagina": page}
            rows = self.get(endpoint, query) or []
            if not isinstance(rows, list):
                raise DgegApiError(
                    f"{endpoint}: expected a list of rows, got {type(rows).__name__}"
                )
            yield from rows
            fetched += len(rows)
            total = rows[0].get("Quantidade") if rows else None
            log.debug("%s page %d: %d rows (%d/%s)", endpoint, page, len(rows), fetched, total)
            if not rows or len(rows) < self.page_size or (total is not None and fetched >= total):
                return
        raise DgegApiError(f"{endpoint}: stopped after {self.max_pages} pages (safety limit)")

    # ------------------------------------------------------------------ internals
    def _request(self, url: str, params: dict[str, Any] | None) -> requests.Response:
        attempt = 0
        while True:
            if self.requests_made and self.request_delay_seconds:
                self._sleep(self.request_delay_seconds)  # be gentle with a public service
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                self.requests_made += 1
            except (requests.ConnectionError, requests.Timeout) as exc:
                self.requests_made += 1
                attempt = self._backoff_or_raise(attempt, exc, f"network error on {url}")
                continue

            if response.status_code in RETRYABLE_STATUSES:
                error = requests.HTTPError(f"{response.status_code} for {url}", response=response)
                retry_after = response.headers.get("Retry-After")
                attempt = self._backoff_or_raise(
                    attempt,
                    error,
                    f"HTTP {response.status_code}",
                    float(retry_after) if retry_after and retry_after.isdigit() else None,
                )
                continue

            response.raise_for_status()
            return response

    def _backoff_or_raise(
        self, attempt: int, exc: Exception, reason: str, delay: float | None = None
    ) -> int:
        if attempt >= self.max_retries:
            raise exc
        delay = delay if delay is not None else self.backoff_seconds * (2**attempt)
        log.warning("%s - retry %d/%d in %.1fs", reason, attempt + 1, self.max_retries, delay)
        self._sleep(delay)
        return attempt + 1
