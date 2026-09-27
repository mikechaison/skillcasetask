"""Fixture plumbing shared by the resolver tests and the fixture recorder.

Every MediaWiki / Wikidata request is reduced to a *route key* such as
``"pl:prefix:intermittent fasting"`` or ``"wikidata:Q333"``.  ``index.json``
in the fixtures directory maps route keys to recorded JSON files.

- :class:`FixtureSession` serves recorded responses and raises on any route
  that is not in the index, so tests can never fall through to the network.
- :class:`RecordingSession` (used by ``record_fixtures.py``) performs real
  requests, saves each response under a new key, and updates the index.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests

FIXTURES = Path(__file__).parent / "fixtures" / "resolve"
INDEX = FIXTURES / "index.json"


def route_key(url: str, params: dict[str, str] | None = None) -> str:
    """Stable key describing one API request (host + call + query/title/ids)."""
    params = params or {}
    if "/metrics/pageviews/per-article/" in url:
        return "pageviews:" + unquote(url.split("/per-article/", 1)[1])
    host = urlparse(url).netloc.removesuffix(".wikipedia.org")  # "en", "zh-yue", "www.wikidata.org"
    if params.get("list") == "search":
        return f"{host}:search:{params['srsearch']}"
    if params.get("list") == "prefixsearch":
        return f"{host}:prefix:{params['pssearch']}"
    if params.get("action") == "wbgetentities":
        kind = "describe" if "descriptions" in params.get("props", "") else ""
        return f"wikidata:{kind}{params['ids']}"
    if params.get("prop") == "revisions":
        return f"{host}:content:{params['titles']}"
    if params.get("prop") == "redirects":
        return f"{host}:redirects:{params['titles']}"
    if "titles" in params:
        return f"{host}:page:{params['titles']}"
    return f"{host}:{params.get('action')}"


def load_index() -> dict[str, str]:
    return json.loads(INDEX.read_text(encoding="utf-8"))


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200, headers: dict[str, str] | None = None) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = json.dumps(payload, ensure_ascii=False) if payload is not None else ""

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FixtureSession:
    """Serves recorded responses; unknown routes raise."""

    def __init__(self, routes: dict[str, str] | None = None) -> None:
        self.routes = load_index() if routes is None else routes
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, params: dict[str, str] | None = None, **_kwargs: Any) -> FakeResponse:
        self.calls.append((url, dict(params or {})))
        key = route_key(url, params)
        if key not in self.routes:
            raise AssertionError(f"unexpected request {key!r}; record it with tests/record_fixtures.py")
        payload = load_fixture(self.routes[key])
        if isinstance(payload, dict) and "__status__" in payload:  # recorded non-200 response
            return FakeResponse(payload["__body__"], status_code=int(payload["__status__"]))
        return FakeResponse(payload)

    def keys(self) -> list[str]:
        return [route_key(u, p) for u, p in self.calls]

    def search_keys(self) -> list[str]:
        return [k for k in self.keys() if ":prefix:" in k or ":search:" in k]


def _slug(key: str) -> str:
    text = key.replace("www.wikidata.org", "wikidata")
    text = re.sub(r"[^\w]+", "_", text, flags=re.UNICODE).strip("_")
    return text[:120]


class RecordingSession:
    """Real HTTP session that records every response it has not seen before."""

    def __init__(self, user_agent: str, delay: float = 1.2) -> None:
        self.session = requests.Session()
        self.headers = self.session.headers
        self.headers["User-Agent"] = user_agent
        self.delay = delay
        self.index = load_index() if INDEX.exists() else {}
        self.recorded: list[str] = []

    def get(self, url: str, params: dict[str, str] | None = None, timeout: float = 30.0) -> Any:
        key = route_key(url, params)
        if key in self.index:
            payload = load_fixture(self.index[key])
            if isinstance(payload, dict) and "__status__" in payload:
                return FakeResponse(payload["__body__"], status_code=int(payload["__status__"]))
            return FakeResponse(payload)
        for attempt in range(6):
            response = self.session.get(url, params=params, timeout=timeout)
            if response.status_code != 429:
                break
            time.sleep(4 * (attempt + 1))
        if response.status_code == 404:  # meaningful for the Pageviews API: "no data"
            payload: Any = {"__status__": 404, "__body__": response.json()}
        else:
            response.raise_for_status()
            payload = response.json()
        name = _slug(key)
        (FIXTURES / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.index[key] = name
        self.recorded.append(key)
        self.save()
        time.sleep(self.delay)
        if isinstance(payload, dict) and "__status__" in payload:
            return FakeResponse(payload["__body__"], status_code=int(payload["__status__"]))
        return FakeResponse(payload)

    def save(self) -> None:
        INDEX.write_text(json.dumps(dict(sorted(self.index.items())), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
