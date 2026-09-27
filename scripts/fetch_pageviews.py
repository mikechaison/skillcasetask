"""Fetch daily/monthly pageviews for one article from the Wikimedia Pageviews REST API.

- ``agent=user`` and ``all-access`` are fixed (bots/spiders excluded).
- Retries on 429/5xx with backoff; DNS failures fail fast.
- A 404 from the API means "no data anywhere in that range": the article may not
  exist, may have had zero views, or may be newer than the range
  (``status: "no_data"``).  An article younger than the range returns items from
  its creation onward: the series starts there (``status: "partial"``).
- Days the API omits *inside* the series (zero views) are filled with 0; a
  leading gap is never zero-filled because it would fake a growth trend.
- Every raw response is cached in ``data/pageviews/<hash>.json`` (hash of the
  query parameters) and the zero-filled series in ``<hash>.csv``.

Usage (CLI):

    python scripts/fetch_pageviews.py --project cs.wikipedia --article "Přerušovaný půst" --years 2
    python scripts/fetch_pageviews.py --resolved resolved.json --years 2     # output of resolve_topic.py

Usage (library):

    >>> from scripts.fetch_pageviews import PageviewsClient, PageviewsQuery, fetch_pageviews
    >>> from datetime import date
    >>> q = PageviewsQuery("en.wikipedia", "Intermittent_fasting", "monthly", date(2025, 1, 1), date(2025, 6, 30))
    >>> r = fetch_pageviews(PageviewsClient(), q)
    >>> r.status, len(r.series)
    ('ok', 6)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow `python scripts/fetch_pageviews.py`
from scripts.resolve_topic import USER_AGENT, host_lang, to_pageviews_title  # noqa: E402

PAGEVIEWS_API = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
EARLIEST_DATA = date(2015, 7, 1)  # per-article data starts here
BOT_FILTER_STABLE = date(2016, 7, 1)  # agent=user filtering only reliable from about here
DATA_LAG_DAYS = 2  # the API silently stops ~2 days before today
LEADING_GAP_DAYS = 14  # a leading gap up to this long is zero views (omitted by the API), not a younger article
# A redirect is merged into the series when its views over the period reach BOTH this share of the
# target's views and this absolute count: former titles after a page move carry most of the pre-move
# history (uk Einstein: 3.8x the target), while typo redirects on huge articles stay below 0.5 %.
REDIRECT_MIN_SHARE = 0.02
REDIRECT_MIN_VIEWS = 200
DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[1] / "data" / "pageviews"
_DNS_FAILURE_MARKERS = ("NameResolutionError", "getaddrinfo", "Name or service not known", "nodename nor servname")


class PageviewsError(RuntimeError):
    """Raised for HTTP/network failures that persist after retries (404 is *not* an error)."""


class PageviewsClient:
    """HTTP client for the Pageviews REST API with retry on 429/5xx.

    ``get`` returns ``(status_code, body)`` for 200 and 404 so callers can
    treat "no data" as a value; other statuses raise :class:`PageviewsError`.
    Inject a fake ``session`` in tests.

    >>> status, body = PageviewsClient().get(PAGEVIEWS_API + "/en.wikipedia/all-access/user/Astronomy/monthly/20250101/20250131")
    >>> status, body["items"][0]["views"] > 0
    (200, True)
    """

    def __init__(
        self,
        session: Optional[Any] = None,
        user_agent: str = USER_AGENT,
        max_retries: int = 3,
        backoff_seconds: float = 2.0,
        timeout: float = 30.0,
    ) -> None:
        self.session = session or requests.Session()
        if hasattr(self.session, "headers"):
            self.session.headers["User-Agent"] = user_agent
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.timeout = timeout

    def get(self, url: str) -> tuple[int, dict[str, Any]]:
        """GET ``url``; return ``(200, body)`` or ``(404, body)``; raise on persistent failure."""
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(url, timeout=self.timeout)
            except requests.ConnectionError as exc:
                if any(marker in str(exc) for marker in _DNS_FAILURE_MARKERS):
                    raise PageviewsError(f"Cannot resolve host for {url}") from exc
                last_error = exc
                self._sleep(attempt, None)
                continue
            except requests.RequestException as exc:
                last_error = exc
                self._sleep(attempt, None)
                continue
            if response.status_code in (200, 404):
                try:
                    return response.status_code, response.json()
                except ValueError as exc:
                    raise PageviewsError(f"Non-JSON response ({response.status_code}) from {url}") from exc
            if response.status_code == 429 or response.status_code >= 500:
                last_error = PageviewsError(f"HTTP {response.status_code} from {url}")
                self._sleep(attempt, response.headers.get("Retry-After"))
                continue
            raise PageviewsError(f"HTTP {response.status_code} from {url}: {response.text[:200]}")
        raise PageviewsError(f"Giving up after {self.max_retries + 1} attempts: {last_error}")

    def _sleep(self, attempt: int, retry_after: Optional[str]) -> None:
        delay = self.backoff_seconds * (2**attempt)
        if retry_after and retry_after.isdigit():
            delay = max(delay, float(retry_after))
        time.sleep(delay)


# --- query ---------------------------------------------------------------------


def normalize_project(project: str) -> str:
    """Canonical Pageviews project name.

    Accepts a bare language code, a project, or a domain; applies host aliases
    (``nb.wikipedia`` returns 404 from the API, ``no.wikipedia`` works) and
    drops the mobile ``.m`` segment (``en.m.wikipedia`` is 404 on this endpoint;
    mobile views are already inside ``all-access``).

    >>> normalize_project("pl"), normalize_project("nb.wikipedia"), normalize_project("cs.wikipedia.org")
    ('pl.wikipedia', 'no.wikipedia', 'cs.wikipedia')
    >>> normalize_project("en.m.wikipedia")
    'en.wikipedia'
    """
    text = project.strip().lower().removesuffix(".org")
    parts = [p for p in text.split(".") if p and p != "m"]
    lang, family = parts[0], (parts[1] if len(parts) > 1 else "wikipedia")
    return f"{host_lang(lang)}.{family}"


def normalize_title(title: str) -> str:
    """Exact title form the Pageviews API matches on.

    The API is strict: ``intermittent_fasting`` (lower-case first letter),
    ``Intermittent__fasting``, a trailing space, or the decomposed (NFD)
    Unicode form of ``Přerušovaný_půst`` all return 404 for an article that
    exists.  MediaWiki stores titles NFC-normalised with a capital first letter
    and single underscores.

    >>> normalize_title("  intermittent  fasting ")
    'Intermittent_fasting'
    >>> import unicodedata
    >>> normalize_title(unicodedata.normalize("NFD", "přerušovaný půst")) == "Přerušovaný_půst"
    True
    """
    text = unicodedata.normalize("NFC", to_pageviews_title(title))
    text = re.sub(r"[\s_]+", "_", text).strip("_")
    return text[:1].upper() + text[1:]


def month_start(day: date) -> date:
    return day.replace(day=1)


def month_end(day: date) -> date:
    """Last day of ``day``'s month.

    >>> month_end(date(2024, 2, 10))
    datetime.date(2024, 2, 29)
    """
    nxt = day.replace(day=28) + timedelta(days=4)
    return nxt - timedelta(days=nxt.day)


def shift_years(day: date, years: int) -> date:
    """Same calendar day ``years`` earlier (29 Feb falls back to 28 Feb).

    >>> shift_years(date(2024, 2, 29), 1)
    datetime.date(2023, 2, 28)
    """
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def default_period(years: int, today: Optional[date] = None) -> tuple[date, date]:
    """``(start, end)`` covering the last ``years`` years, ending before the data lag.

    ``end`` is ``today - DATA_LAG_DAYS`` (the API's last complete day) and
    ``start`` is floored at the earliest available data.

    >>> default_period(2, today=date(2026, 9, 27))
    (datetime.date(2024, 9, 25), datetime.date(2026, 9, 25))
    """
    if years < 1:
        raise ValueError("years must be >= 1")
    end = (today or date.today()) - timedelta(days=DATA_LAG_DAYS)
    return max(shift_years(end, years), EARLIEST_DATA), end


@dataclass(frozen=True)
class PageviewsQuery:
    """One per-article request; hashable and JSON-serialisable for the cache."""

    project: str
    article: str
    granularity: str
    start: date
    end: date
    access: str = "all-access"
    agent: str = "user"

    def __post_init__(self) -> None:
        if self.granularity not in ("daily", "monthly"):
            raise ValueError("granularity must be 'daily' or 'monthly'")
        if self.start > self.end:
            raise ValueError(f"start {self.start} is after end {self.end}")
        object.__setattr__(self, "project", normalize_project(self.project))
        object.__setattr__(self, "article", normalize_title(self.article))
        if not self.article:
            raise ValueError("article title is empty")
        if self.granularity == "monthly":
            # The API needs at least one full month between the dates (else HTTP 400) and
            # returns the whole months containing them anyway, so snap to month bounds.
            object.__setattr__(self, "start", month_start(self.start))
            object.__setattr__(self, "end", month_end(self.end))

    def url(self) -> str:
        """REST URL; the title is percent-encoded so ``AC/DC`` and ``…?`` work.

        >>> PageviewsQuery("en", "AC/DC", "monthly", date(2025, 1, 1), date(2025, 1, 31)).url().rsplit("/", 3)[0][-7:]
        'AC%2FDC'
        """
        return (
            f"{PAGEVIEWS_API}/{self.project}/{self.access}/{self.agent}/{quote(self.article, safe='')}"
            f"/{self.granularity}/{self.start:%Y%m%d}/{self.end:%Y%m%d}"
        )

    def to_dict(self) -> dict[str, str]:
        return {**asdict(self), "start": self.start.isoformat(), "end": self.end.isoformat()}

    def cache_key(self) -> str:
        """Stable hash of all parameters.

        >>> PageviewsQuery("en", "A", "daily", date(2025, 1, 1), date(2025, 1, 2)).cache_key() == \\
        ...     PageviewsQuery("en.wikipedia", "A", "daily", date(2025, 1, 1), date(2025, 1, 2)).cache_key()
        True
        """
        canonical = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]


# --- result --------------------------------------------------------------------


@dataclass
class PageviewsResult:
    """Zero-filled series plus everything the agent needs to interpret it."""

    query: PageviewsQuery
    status: str  # "ok" | "partial" | "no_data"
    series: pd.DataFrame = field(repr=False)  # columns: date, views
    data_from: Optional[date] = None
    data_through: Optional[date] = None
    points_requested: int = 0
    points_with_data: int = 0
    zero_filled_points: int = 0
    total_views: int = 0
    mean_views_per_day: float = 0.0
    csv_path: Optional[str] = None
    from_cache: bool = False
    fetched_at: Optional[str] = None
    caveats: list[str] = field(default_factory=list)
    merged_titles: list[dict[str, Any]] = field(default_factory=list)  # redirects whose views were added
    target_views: Optional[int] = None  # views under the current title alone (when redirects were merged)

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly summary (the series itself lives in ``csv_path``)."""
        out = asdict(self)
        out.pop("series")
        out["query"] = self.query.to_dict()
        out["data_from"] = self.data_from.isoformat() if self.data_from else None
        out["data_through"] = self.data_through.isoformat() if self.data_through else None
        return out


def _parse_items(items: list[dict[str, Any]]) -> pd.DataFrame:
    """API items -> DataFrame(date, views); timestamps are ``YYYYMMDDHH``."""
    rows = [(datetime.strptime(item["timestamp"][:8], "%Y%m%d").date(), int(item["views"])) for item in items]
    return pd.DataFrame(rows, columns=["date", "views"])


def zero_fill(series: pd.DataFrame, start: date, end: date, granularity: str) -> pd.DataFrame:
    """Complete the series over ``[start, end]``; missing points (omitted by the API) become 0.

    >>> df = pd.DataFrame({"date": [date(2025, 1, 1), date(2025, 1, 3)], "views": [5, 7]})
    >>> zero_fill(df, date(2025, 1, 1), date(2025, 1, 3), "daily")["views"].tolist()
    [5, 0, 7]
    """
    if granularity == "daily":
        index = pd.date_range(start, end, freq="D")
    else:
        index = pd.date_range(start.replace(day=1), end.replace(day=1), freq="MS")
    views = pd.Series(series["views"].values, index=pd.to_datetime(series["date"])) if len(series) else pd.Series(dtype="int64")
    filled = views.reindex(index, fill_value=0).astype("int64")
    return pd.DataFrame({"date": [d.date() for d in filled.index], "views": filled.values})


# --- cache ---------------------------------------------------------------------


def _cache_paths(cache_dir: Path, query: PageviewsQuery) -> tuple[Path, Path]:
    key = query.cache_key()
    return cache_dir / f"{key}.json", cache_dir / f"{key}.csv"


def _read_cache(cache_dir: Path, query: PageviewsQuery) -> Optional[dict[str, Any]]:
    raw_path, _ = _cache_paths(cache_dir, query)
    if not raw_path.exists():
        return None
    try:
        payload = json.loads(raw_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if payload.get("query") != query.to_dict():  # hash collision or stale format
        return None
    return payload


def _write_cache(cache_dir: Path, query: PageviewsQuery, status_code: int, items: list[dict[str, Any]], fetched_at: str) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    raw_path, _ = _cache_paths(cache_dir, query)
    payload = {"query": query.to_dict(), "url": query.url(), "status_code": status_code, "fetched_at": fetched_at, "items": items}
    raw_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _get_items(
    client: PageviewsClient, query: PageviewsQuery, cache_dir: Path, refresh: bool
) -> tuple[int, list[dict[str, Any]], bool, str]:
    """``(status_code, items, from_cache, fetched_at)`` for one query, via the cache."""
    if not refresh:
        cached = _read_cache(cache_dir, query)
        if cached is not None:
            return int(cached["status_code"]), list(cached["items"]), True, str(cached["fetched_at"])
    status_code, body = client.get(query.url())
    items = list(body.get("items", [])) if status_code == 200 else []
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write_cache(cache_dir, query, status_code, items, fetched_at)
    return status_code, items, False, fetched_at


def read_json_file(path: str | Path) -> Any:
    """Load JSON written by any Windows tool.

    PowerShell 5.1's ``>`` redirection writes UTF-16 with a byte-order mark
    and some editors add a UTF-8 BOM; both would fail a plain UTF-8 read.

    >>> import tempfile, os
    >>> f = tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False); _ = f.write('{"a": "ř"}'.encode("utf-16")); f.close()
    >>> read_json_file(f.name)["a"]
    'ř'
    """
    raw = Path(path).read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig")
    return json.loads(text)


def write_json_output(payload: Any, out: Optional[str]) -> None:
    """Print ``payload`` as JSON, or write it as UTF-8 to ``out`` (safer than shell redirection on Windows)."""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if out:
        Path(out).write_text(text + "\n", encoding="utf-8")
        print(json.dumps({"written": str(Path(out)), "keys": list(payload) if isinstance(payload, dict) else None}))
    else:
        print(text)


# --- main entry points ---------------------------------------------------------


def fetch_pageviews(
    client: PageviewsClient,
    query: PageviewsQuery,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    today: Optional[date] = None,
) -> PageviewsResult:
    """Fetch one article's series, using and filling the local cache.

    A 404 means no data anywhere in the range (``status="no_data"``).  An
    article younger than the range comes back with items starting at its
    creation: the series then starts there (``status="partial"``) rather than
    being padded with zeros, which would fake a growth trend.  A monthly series
    drops the current, incomplete month (``today`` is injectable for tests).

    >>> q = PageviewsQuery("cs.wikipedia", "Xqzvv_qwrtyplk_zzz", "daily", date(2025, 1, 1), date(2025, 1, 31))
    >>> fetch_pageviews(PageviewsClient(), q).status
    'no_data'
    """
    caveats: list[str] = []
    if query.start < BOT_FILTER_STABLE:
        caveats.append(
            f"Data before {BOT_FILTER_STABLE:%Y-%m-%d} predates reliable bot filtering; treat early values with caution."
        )

    if query.start < EARLIEST_DATA:
        caveats.append(f"Per-article pageviews only exist from {EARLIEST_DATA:%Y-%m-%d}; earlier dates return nothing.")

    status_code, items, from_cache, fetched_at = _get_items(client, query, cache_dir, refresh)

    if status_code == 404 or not items:
        caveats.append(
            "The Pageviews API has no data for this article in the whole period: it may not exist under this "
            "exact title, may have had no views at all, or may be newer than the period."
        )
        return PageviewsResult(
            query=query, status="no_data", series=pd.DataFrame(columns=["date", "views"]),
            from_cache=from_cache, fetched_at=fetched_at, caveats=caveats,
        )

    raw = _parse_items(items)
    data_from, data_through = raw["date"].min(), raw["date"].max()
    status = "ok"
    # Leading gap longer than LEADING_GAP_DAYS: the API returned nothing before `data_from`, which
    # means "no page yet" (or no data yet, before 2015-07): never fill it with zeros, trim instead.
    first = max(query.start, data_from if query.granularity == "daily" else data_from.replace(day=1))
    if query.granularity == "daily" and timedelta(0) < first - query.start <= timedelta(days=LEADING_GAP_DAYS):
        first = query.start  # a few silent days at the start: zero views, fill them like any other gap
    if first > query.start:
        status = "partial"
        caveats.append(
            f"No data before {data_from:%Y-%m-%d} (requested from {query.start:%Y-%m-%d}): the article is probably "
            "younger than the period, so the trend covers a shorter window than asked."
        )
    series = zero_fill(raw, first, query.end, query.granularity)
    if data_through < query.end and query.granularity == "daily":
        caveats.append(
            f"Data ends on {data_through:%Y-%m-%d}, before the requested end {query.end:%Y-%m-%d}: the most recent "
            "days are not published yet or the article stopped receiving views."
        )
        series = series[series["date"] <= data_through].reset_index(drop=True)
    if query.granularity == "monthly":
        # The API returns the current month with only the days published so far.
        complete_through = (today or date.today()) - timedelta(days=DATA_LAG_DAYS)
        incomplete = [d for d in series["date"] if month_end(d) > complete_through]
        if incomplete:
            series = series[~series["date"].isin(incomplete)].reset_index(drop=True)
            raw = raw[~raw["date"].isin(incomplete)].reset_index(drop=True)
            caveats.append(
                f"Dropped {len(incomplete)} incomplete month(s) from {incomplete[0]:%Y-%m}: the current month only "
                "contains the days published so far and would distort the trend."
            )
            if series.empty:
                return PageviewsResult(query=query, status="no_data", series=series, from_cache=from_cache,
                                       fetched_at=fetched_at, caveats=caveats)
            data_through = raw["date"].max()
    zero_filled = int(len(series) - len(raw))
    if zero_filled > 0:
        caveats.append(f"{zero_filled} {query.granularity} point(s) had no entry in the API (zero views) and were filled with 0.")
    total = int(series["views"].sum())
    span_days = max((series["date"].iloc[-1] - series["date"].iloc[0]).days + 1, 1)

    cache_dir.mkdir(parents=True, exist_ok=True)
    _, csv_path = _cache_paths(cache_dir, query)
    series.to_csv(csv_path, index=False)

    return PageviewsResult(
        query=query,
        status=status,
        series=series,
        data_from=data_from,
        data_through=data_through,
        points_requested=int(len(series)),
        points_with_data=int(len(raw)),
        zero_filled_points=zero_filled,
        total_views=total,
        mean_views_per_day=round(total / span_days, 2),
        csv_path=str(csv_path),
        from_cache=from_cache,
        fetched_at=fetched_at,
        caveats=caveats,
    )


def fetch_with_redirects(
    client: PageviewsClient,
    query: PageviewsQuery,
    redirects: list[str],
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    today: Optional[date] = None,
) -> PageviewsResult:
    """Fetch the article plus the views recorded under its redirect titles that carry real traffic.

    Pageviews are per title.  After a page move the history before the move
    sits under the old title (now a redirect): uk "Альберт Ейнштейн" carried
    ~7 000 views/month until the move to "Альберт Айнштайн" in February 2026,
    after which the new title took over.  Each redirect is probed monthly
    (cheap, cached); those with at least ``REDIRECT_MIN_SHARE`` of the target's
    views or ``REDIRECT_MIN_VIEWS`` views are fetched daily and added to the
    series.  ``merged_titles`` and a caveat record what was added.
    """
    target = fetch_pageviews(client, query, cache_dir=cache_dir, refresh=refresh, today=today)
    if not redirects:
        return target
    target_total = target.total_views
    probes: list[tuple[str, int]] = []
    for title in redirects:
        try:
            probe_q = PageviewsQuery(query.project, title, "monthly", query.start, query.end)
        except ValueError:
            continue
        probe = fetch_pageviews(client, probe_q, cache_dir=cache_dir, refresh=refresh, today=today)
        if probe.total_views > 0:
            probes.append((title, probe.total_views))
    floor = max(REDIRECT_MIN_VIEWS, REDIRECT_MIN_SHARE * target_total)
    material = sorted(((t, v) for t, v in probes if v >= floor), key=lambda tv: -tv[1])
    if not material:
        return target
    if target.status == "no_data":
        base = zero_fill(pd.DataFrame(columns=["date", "views"]), query.start, query.end, query.granularity)
    else:
        base = target.series.copy()
    merged_series = base.set_index("date")["views"].astype("int64")
    merged: list[dict[str, Any]] = []
    for title, monthly_views in material:
        daily = fetch_pageviews(client, PageviewsQuery(query.project, title, query.granularity, query.start, query.end),
                                cache_dir=cache_dir, refresh=refresh, today=today)
        if daily.status == "no_data":
            continue
        extra = daily.series.set_index("date")["views"].astype("int64")
        merged_series = merged_series.add(extra, fill_value=0).astype("int64")
        merged.append({"title": to_pageviews_title(title), "views": int(daily.total_views),
                       "share_of_target": round(daily.total_views / target_total, 4) if target_total else None,
                       "first_seen": daily.data_from.isoformat() if daily.data_from else None})
    if not merged:
        return target
    series = merged_series.reset_index().rename(columns={"index": "date"})
    series["date"] = [pd.Timestamp(d).date() for d in series["date"]]
    series = series.sort_values("date").reset_index(drop=True)
    total = int(series["views"].sum())
    span_days = max((series["date"].iloc[-1] - series["date"].iloc[0]).days + 1, 1)
    raw_points = target.points_with_data + sum(1 for _ in merged)  # informational only
    cache_dir.mkdir(parents=True, exist_ok=True)
    csv_path = cache_dir / f"{query.cache_key()}-merged.csv"
    series.to_csv(csv_path, index=False)
    merged.sort(key=lambda m: -m["views"])
    shown = [f"'{m['title']}' ({m['views']:,} views)" for m in merged[:5]]
    if len(merged) > 5:
        shown.append(f"and {len(merged) - 5} more")
    caveats = [c for c in target.caveats
               if "no data for this article" not in c and "younger than the period" not in c and "filled with 0" not in c]
    caveats.append(
        f"Views recorded under redirect title(s) {', '.join(shown)} were added to the series: a redirect is usually a "
        "former name (page move) or a common spelling, and pageviews are counted per title."
    )
    zero_days = int((series["views"] == 0).sum())
    if zero_days:
        caveats.append(f"{zero_days} {query.granularity} point(s) had no views under any title and were filled with 0.")
    if merged[0]["views"] > target_total:
        caveats.append(
            f"'{merged[0]['title']}' carried more views than the current title '{query.article}': the article was "
            "probably renamed during the period. Its full history is included."
        )
    return PageviewsResult(
        query=query, status="ok" if target.status != "partial" else "ok", series=series,
        data_from=series["date"].iloc[0], data_through=series["date"].iloc[-1],
        points_requested=int(len(series)), points_with_data=raw_points, zero_filled_points=zero_days,
        total_views=total, mean_views_per_day=round(total / span_days, 2), csv_path=str(csv_path),
        from_cache=target.from_cache, fetched_at=target.fetched_at, caveats=caveats,
        merged_titles=merged, target_views=target_total,
    )


def fetch_for_resolved(
    client: PageviewsClient,
    resolved: dict[str, Any],
    start: date,
    end: date,
    granularity: str = "daily",
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    today: Optional[date] = None,
    merge_redirects: bool = True,
) -> dict[str, PageviewsResult]:
    """Fetch every ``found`` article from a ``resolve_topic`` result (keyed by language).

    Redirect titles listed by the resolver are merged in (see ``fetch_with_redirects``).

    >>> resolved = {"articles": {"cs": {"found": True, "project": "cs.wikipedia", "pageviews_title": "Přerušovaný_půst"}}}
    >>> list(fetch_for_resolved(PageviewsClient(), resolved, date(2025, 1, 1), date(2025, 1, 31)))
    ['cs']
    """
    results: dict[str, PageviewsResult] = {}
    for lang, ref in resolved.get("articles", {}).items():
        if not ref.get("found"):
            continue
        query = PageviewsQuery(ref["project"], ref["pageviews_title"], granularity, start, end)
        redirects = list(ref.get("redirects", [])) if merge_redirects else []
        results[lang] = fetch_with_redirects(client, query, redirects, cache_dir=cache_dir, refresh=refresh, today=today)
    return results


# --- CLI ----------------------------------------------------------------------


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Wikimedia pageviews (agent=user) with local caching; JSON output.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--article", help="article title (spaces or underscores); requires --project")
    source.add_argument("--resolved", help="JSON file produced by resolve_topic.py; fetches every found article")
    parser.add_argument("--project", help="e.g. cs.wikipedia (or just 'cs')")
    parser.add_argument("--years", type=int, default=2, help="period length ending before the data lag (default 2)")
    parser.add_argument("--start", help="YYYY-MM-DD (overrides --years)")
    parser.add_argument("--end", help="YYYY-MM-DD (default: today minus the data lag)")
    parser.add_argument("--granularity", choices=["daily", "monthly"], default="daily")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--refresh", action="store_true", help="ignore cached responses")
    parser.add_argument("--today", help="override today's date (YYYY-MM-DD) for reproducible runs")
    parser.add_argument("--out", help="write the JSON to this file (UTF-8) instead of stdout")
    parser.add_argument("--no-redirects", action="store_true", help="do not merge views recorded under redirect titles (--resolved only)")
    return parser.parse_args(argv)


def _period_from_args(args: argparse.Namespace) -> tuple[date, date]:
    today = date.fromisoformat(args.today) if args.today else None
    start, end = default_period(args.years, today=today)
    if args.end:
        end = date.fromisoformat(args.end)
    if args.start:
        start = date.fromisoformat(args.start)
    elif args.end:
        start = max(shift_years(end, args.years), EARLIEST_DATA)
    return start, end


def _force_utf8_stdout() -> None:
    """Emit UTF-8 regardless of the console code page (Windows defaults to cp1251/cp1252)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point: print a JSON object with one result per article."""
    _force_utf8_stdout()
    args = _parse_args(argv)
    cache_dir = Path(args.cache_dir)
    try:
        start, end = _period_from_args(args)
        today = date.fromisoformat(args.today) if args.today else None
        client = PageviewsClient()
        if args.resolved:
            resolved = read_json_file(args.resolved)
            results = fetch_for_resolved(client, resolved, start, end, args.granularity, cache_dir, args.refresh, today=today,
                                         merge_redirects=not args.no_redirects)
            skipped = [lang for lang, ref in resolved.get("articles", {}).items() if not ref.get("found")]
        else:
            if not args.project:
                raise ValueError("--project is required with --article")
            query = PageviewsQuery(args.project, args.article, args.granularity, start, end)
            results = {query.project: fetch_pageviews(client, query, cache_dir, args.refresh, today=today)}
            skipped = []
    except (PageviewsError, ValueError, OSError, requests.RequestException) as exc:
        json.dump({"error": str(exc)}, sys.stdout, ensure_ascii=False)
        print()
        return 1
    out = {
        "period": {"start": start.isoformat(), "end": end.isoformat(), "granularity": args.granularity},
        "results": {key: result.to_dict() for key, result in results.items()},
        "skipped": skipped,
        "limitations": [
            "Pageviews are a signal of curiosity, not willingness to pay or product-market fit.",
            "Compare editions only after indexing each series to its own start; audience sizes differ hugely.",
            "A spike may be a news event or a viral post, not organic growth.",
        ],
    }
    write_json_output(out, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
