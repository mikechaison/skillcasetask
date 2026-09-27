"""Offline tests for scripts/fetch_pageviews.py against recorded Pageviews API responses.

Fixtures (tests/fixtures/resolve/index.json, keys starting with "pageviews:")
were recorded on 2026-09-27 with tests/record_fixtures.py using fixed date
ranges, so the cache keys are deterministic.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import requests

from scripts import fetch_pageviews as fp
from tests.fixture_session import FakeResponse, FixtureSession

D = date


@pytest.fixture
def session() -> FixtureSession:
    return FixtureSession()


@pytest.fixture
def client(session: FixtureSession) -> fp.PageviewsClient:
    return fp.PageviewsClient(session=session, backoff_seconds=0)


def q(project: str, article: str, granularity: str, start: date, end: date) -> fp.PageviewsQuery:
    return fp.PageviewsQuery(project, article, granularity, start, end)


CS_FASTING = ("cs.wikipedia", "Přerušovaný_půst", "daily", D(2024, 9, 1), D(2026, 9, 1))
EN_MONTHLY = ("en.wikipedia", "Intermittent_fasting", "monthly", D(2024, 9, 1), D(2026, 9, 1))


# --- helpers -------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("pl", "pl.wikipedia"),
        ("cs.wikipedia", "cs.wikipedia"),
        ("cs.wikipedia.org", "cs.wikipedia"),
        (" UK.Wikipedia ", "uk.wikipedia"),
        ("nb.wikipedia", "no.wikipedia"),  # nb.wikipedia -> 404 on the API; no.wikipedia works
        ("nb", "no.wikipedia"),
        ("zh-yue", "zh-yue.wikipedia"),
    ],
)
def test_normalize_project(raw: str, expected: str) -> None:
    assert fp.normalize_project(raw) == expected


def test_shift_years_handles_leap_day() -> None:
    assert fp.shift_years(D(2024, 2, 29), 1) == D(2023, 2, 28)
    assert fp.shift_years(D(2026, 9, 25), 2) == D(2024, 9, 25)


def test_default_period_ends_before_data_lag_and_floors_start() -> None:
    assert fp.default_period(2, today=D(2026, 9, 27)) == (D(2024, 9, 25), D(2026, 9, 25))
    assert fp.default_period(20, today=D(2026, 9, 27)) == (fp.EARLIEST_DATA, D(2026, 9, 25))
    with pytest.raises(ValueError):
        fp.default_period(0, today=D(2026, 9, 27))


def test_query_normalises_and_validates() -> None:
    query = q("nb", "Astronomi hist", "daily", D(2026, 8, 1), D(2026, 8, 31))
    assert query.project == "no.wikipedia" and query.article == "Astronomi_hist"
    with pytest.raises(ValueError, match="granularity"):
        q("en", "A", "weekly", D(2025, 1, 1), D(2025, 1, 2))
    with pytest.raises(ValueError, match="after end"):
        q("en", "A", "daily", D(2025, 1, 2), D(2025, 1, 1))


def test_query_url_percent_encodes_title() -> None:
    assert q("en", "AC/DC", "monthly", D(2025, 1, 1), D(2025, 6, 30)).url().endswith(
        "/en.wikipedia/all-access/user/AC%2FDC/monthly/20250101/20250630"
    )
    assert "/user/%D0%90%D1%81%D1%82%D1%80%D0%BE%D0%BD%D0%BE%D0%BC%D1%96%D1%8F/" in q(
        "uk", "Астрономія", "daily", D(2024, 9, 1), D(2026, 9, 1)
    ).url()
    assert "Who_Wants_to_Be_a_Millionaire%3F" in q("en", "Who Wants to Be a Millionaire?", "daily", D(2025, 1, 1), D(2025, 1, 2)).url()


def test_query_cache_key_is_stable_and_parameter_sensitive() -> None:
    a = q("en", "A", "daily", D(2025, 1, 1), D(2025, 1, 2))
    assert a.cache_key() == q("en.wikipedia", "A", "daily", D(2025, 1, 1), D(2025, 1, 2)).cache_key()
    assert len(a.cache_key()) == 20 and a.cache_key().isalnum()
    assert a.cache_key() != q("en", "A", "monthly", D(2025, 1, 1), D(2025, 1, 2)).cache_key()
    assert a.cache_key() != q("en", "A", "daily", D(2025, 1, 1), D(2025, 1, 3)).cache_key()
    assert a.cache_key() != q("pl", "A", "daily", D(2025, 1, 1), D(2025, 1, 2)).cache_key()
    assert a.to_dict()["start"] == "2025-01-01"


def test_zero_fill_daily_and_monthly() -> None:
    df = pd.DataFrame({"date": [D(2025, 1, 1), D(2025, 1, 3)], "views": [5, 7]})
    filled = fp.zero_fill(df, D(2025, 1, 1), D(2025, 1, 4), "daily")
    assert filled["views"].tolist() == [5, 0, 7, 0] and filled["date"].tolist()[1] == D(2025, 1, 2)
    monthly = fp.zero_fill(pd.DataFrame({"date": [D(2025, 3, 1)], "views": [9]}), D(2025, 1, 15), D(2025, 3, 20), "monthly")
    assert monthly["views"].tolist() == [0, 0, 9] and monthly["date"].tolist()[0] == D(2025, 1, 1)
    assert fp.zero_fill(pd.DataFrame(columns=["date", "views"]), D(2025, 1, 1), D(2025, 1, 2), "daily")["views"].tolist() == [0, 0]


# --- fetching (recorded) -------------------------------------------------------


def test_fetch_daily_series(client: fp.PageviewsClient, tmp_path: Path) -> None:
    result = fp.fetch_pageviews(client, q(*CS_FASTING), cache_dir=tmp_path)
    assert result.status == "ok" and result.from_cache is False
    # 729 items over 731 days: the API omits days with zero views, the module fills them
    assert result.points_requested == 731 and result.points_with_data == 729 and result.zero_filled_points == 2
    assert (result.data_from, result.data_through) == (D(2024, 9, 1), D(2026, 9, 1))
    assert result.total_views == 6940 and result.mean_views_per_day == round(6940 / 731, 2)
    assert list(result.series.columns) == ["date", "views"]
    assert result.series["date"].iloc[0] == D(2024, 9, 1) and result.series["date"].iloc[-1] == D(2026, 9, 1)
    assert result.series["views"].dtype.kind == "i"
    assert (result.series["views"] == 0).sum() == 10  # 8 explicit zeros from the API + 2 omitted days
    assert result.caveats == ["2 daily point(s) had no entry in the API (zero views) and were filled with 0."]


def test_fetch_writes_csv_and_raw_cache(client: fp.PageviewsClient, tmp_path: Path) -> None:
    query = q(*CS_FASTING)
    result = fp.fetch_pageviews(client, query, cache_dir=tmp_path)
    csv = Path(result.csv_path)
    assert csv.parent == tmp_path and csv.stem == query.cache_key()
    frame = pd.read_csv(csv)
    assert len(frame) == 731 and frame["views"].sum() == 6940
    raw = json.loads((tmp_path / f"{query.cache_key()}.json").read_text(encoding="utf-8"))
    assert raw["status_code"] == 200 and len(raw["items"]) == 729  # raw response, gaps included
    assert raw["query"] == query.to_dict() and raw["url"] == query.url()
    assert raw["items"][0]["agent"] == "user" and raw["items"][0]["access"] == "all-access"


def test_fetch_uses_cache_on_second_call(client: fp.PageviewsClient, session: FixtureSession, tmp_path: Path) -> None:
    fp.fetch_pageviews(client, q(*CS_FASTING), cache_dir=tmp_path)
    assert len(session.calls) == 1
    again = fp.fetch_pageviews(client, q(*CS_FASTING), cache_dir=tmp_path)
    assert again.from_cache is True and len(session.calls) == 1
    assert again.total_views == 6940 and again.points_requested == 731
    refreshed = fp.fetch_pageviews(client, q(*CS_FASTING), cache_dir=tmp_path, refresh=True)
    assert refreshed.from_cache is False and len(session.calls) == 2


def test_fetch_ignores_corrupt_or_foreign_cache_file(client: fp.PageviewsClient, session: FixtureSession, tmp_path: Path) -> None:
    query = q(*CS_FASTING)
    (tmp_path / f"{query.cache_key()}.json").write_text("not json", encoding="utf-8")
    assert fp.fetch_pageviews(client, query, cache_dir=tmp_path).from_cache is False
    other = {"query": {"project": "x"}, "status_code": 200, "fetched_at": "", "items": []}
    (tmp_path / f"{query.cache_key()}.json").write_text(json.dumps(other), encoding="utf-8")
    assert fp.fetch_pageviews(client, query, cache_dir=tmp_path).from_cache is False
    assert len(session.calls) == 2


def test_fetch_monthly_series(client: fp.PageviewsClient, tmp_path: Path) -> None:
    # end 2026-09-01 snaps to 2026-09-30; September 2026 is incomplete on 2026-09-27 and is dropped
    result = fp.fetch_pageviews(client, q(*EN_MONTHLY), cache_dir=tmp_path, today=D(2026, 9, 27))
    assert result.status == "ok" and result.points_requested == 24
    assert result.query.end == D(2026, 9, 30)
    assert result.series["date"].iloc[0] == D(2024, 9, 1) and result.series["date"].iloc[-1] == D(2026, 8, 1)
    assert result.total_views == 343065


def test_fetch_title_with_slash(client: fp.PageviewsClient, session: FixtureSession, tmp_path: Path) -> None:
    result = fp.fetch_pageviews(client, q("en", "AC/DC", "monthly", D(2025, 1, 1), D(2025, 6, 30)), cache_dir=tmp_path)
    assert result.status == "ok" and result.points_requested == 6 and result.total_views == 1391755
    assert "/user/AC%2FDC/" in session.calls[0][0]


def test_fetch_article_younger_than_period_is_partial(client: fp.PageviewsClient, tmp_path: Path) -> None:
    # cs "Ice bathing" was created in March 2026; the API returns 200 with items from then on.
    result = fp.fetch_pageviews(client, q("cs.wikipedia", "Ice_bathing", "daily", D(2025, 1, 1), D(2026, 9, 1)), cache_dir=tmp_path)
    assert result.status == "partial"
    assert result.data_from == D(2026, 3, 17) and result.series["date"].iloc[0] == D(2026, 3, 17)
    assert result.points_requested == (D(2026, 9, 1) - D(2026, 3, 17)).days + 1  # no leading zeros
    assert result.total_views == 350
    assert any("younger than the period" in c and "2026-03-17" in c for c in result.caveats)


def test_fetch_no_data_returns_status_not_exception(client: fp.PageviewsClient, session: FixtureSession, tmp_path: Path) -> None:
    query = q("cs.wikipedia", "Xqzvv_qwrtyplk_zzz", "daily", D(2025, 1, 1), D(2025, 1, 31))
    result = fp.fetch_pageviews(client, query, cache_dir=tmp_path)
    assert result.status == "no_data" and result.series.empty and result.csv_path is None
    assert result.total_views == 0 and result.points_requested == 0
    assert any("no data for this article" in c for c in result.caveats)
    assert len(session.calls) == 1  # no extra probing
    # the 404 is cached too
    again = fp.fetch_pageviews(client, query, cache_dir=tmp_path)
    assert again.status == "no_data" and again.from_cache is True and len(session.calls) == 1


def test_fetch_project_alias_nb(client: fp.PageviewsClient, session: FixtureSession, tmp_path: Path) -> None:
    result = fp.fetch_pageviews(client, q("nb.wikipedia", "Astronomi", "daily", D(2026, 8, 1), D(2026, 8, 31)), cache_dir=tmp_path)
    assert result.status == "ok" and result.points_requested == 31 and result.total_views == 192
    assert "/no.wikipedia/" in session.calls[0][0]
    assert result.query.project == "no.wikipedia"


def test_fetch_before_data_starts_trims_and_warns(client: fp.PageviewsClient, tmp_path: Path) -> None:
    result = fp.fetch_pageviews(client, q("en", "Intermittent_fasting", "daily", D(2015, 6, 1), D(2015, 7, 31)), cache_dir=tmp_path)
    assert result.status == "partial" and result.series["date"].iloc[0] == D(2015, 7, 1)
    assert result.points_requested == 31 and result.total_views == 29477
    joined = " ".join(result.caveats)
    assert "2015-07-01" in joined and "bot filtering" in joined


# --- fetching (synthetic) ------------------------------------------------------


class ItemsSession:
    """Serves one hand-made Pageviews payload."""

    headers: dict[str, str] = {}

    def __init__(self, items: list[tuple[str, int]], status: int = 200) -> None:
        self.payload = {"items": [{"project": "en.wikipedia", "article": "X", "granularity": "daily", "timestamp": ts + "00",
                                   "access": "all-access", "agent": "user", "views": v} for ts, v in items]}
        self.status = status
        self.calls = 0

    def get(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
        self.calls += 1
        return FakeResponse(self.payload, status_code=self.status)


def test_fetch_zero_fills_internal_gaps(tmp_path: Path) -> None:
    session = ItemsSession([("20250101", 3), ("20250102", 4), ("20250104", 6)])  # 3 Jan omitted by the API
    result = fp.fetch_pageviews(fp.PageviewsClient(session=session), q("en", "X", "daily", D(2025, 1, 1), D(2025, 1, 4)), cache_dir=tmp_path)
    assert result.status == "ok" and result.series["views"].tolist() == [3, 4, 0, 6]
    assert result.zero_filled_points == 1 and result.points_with_data == 3
    assert any("filled with 0" in c for c in result.caveats)


def test_fetch_trailing_gap_is_reported_not_filled(tmp_path: Path) -> None:
    session = ItemsSession([("20250101", 3), ("20250102", 4)])  # requested through 5 Jan
    result = fp.fetch_pageviews(fp.PageviewsClient(session=session), q("en", "X", "daily", D(2025, 1, 1), D(2025, 1, 5)), cache_dir=tmp_path)
    assert result.series["date"].tolist() == [D(2025, 1, 1), D(2025, 1, 2)] and result.data_through == D(2025, 1, 2)
    assert any("Data ends on 2025-01-02" in c for c in result.caveats)


def test_fetch_monthly_partial_starts_at_first_month(tmp_path: Path) -> None:
    session = ItemsSession([("20250301", 10), ("20250401", 12)])
    result = fp.fetch_pageviews(fp.PageviewsClient(session=session), q("en", "X", "monthly", D(2025, 1, 1), D(2025, 4, 30)), cache_dir=tmp_path)
    assert result.status == "partial" and result.series["date"].tolist() == [D(2025, 3, 1), D(2025, 4, 1)]


def test_fetch_for_resolved_skips_missing_articles(client: fp.PageviewsClient, tmp_path: Path) -> None:
    resolved = {
        "articles": {
            "cs": {"found": True, "project": "cs.wikipedia", "pageviews_title": "Přerušovaný_půst"},
            "pl": {"found": False, "project": "pl.wikipedia", "pageviews_title": None},
        }
    }
    results = fp.fetch_for_resolved(client, resolved, D(2024, 9, 1), D(2026, 9, 1), cache_dir=tmp_path)
    assert list(results) == ["cs"] and results["cs"].total_views == 6940


def test_result_to_dict_is_json_serialisable(client: fp.PageviewsClient, tmp_path: Path) -> None:
    result = fp.fetch_pageviews(client, q(*CS_FASTING), cache_dir=tmp_path)
    data = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))
    assert "series" not in data and data["query"]["article"] == "Přerušovaný_půst"
    assert data["data_from"] == "2024-09-01" and data["csv_path"].endswith(".csv")
    assert data["mean_views_per_day"] == round(6940 / 731, 2)


# --- client --------------------------------------------------------------------


def test_client_returns_404_body_as_value() -> None:
    session = ItemsSession([], status=404)
    status, body = fp.PageviewsClient(session=session).get("https://wikimedia.org/x")
    assert status == 404 and body == {"items": []}


def test_client_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(fp.time, "sleep", sleeps.append)

    class Flaky:
        headers: dict[str, str] = {}
        calls = 0

        def get(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
            self.calls += 1
            if self.calls < 3:
                return FakeResponse({}, status_code=429, headers={"Retry-After": "5"})
            return FakeResponse({"items": []})

    flaky = Flaky()
    assert fp.PageviewsClient(session=flaky, backoff_seconds=1).get("https://wikimedia.org/x")[0] == 200
    assert flaky.calls == 3 and sleeps == [5.0, 5.0]


def test_client_gives_up_on_persistent_503(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fp.time, "sleep", lambda _s: None)
    with pytest.raises(fp.PageviewsError, match="3 attempts"):
        fp.PageviewsClient(session=ItemsSession([], status=503), max_retries=2).get("https://wikimedia.org/x")


def test_client_raises_immediately_on_client_error() -> None:
    session = ItemsSession([], status=403)
    with pytest.raises(fp.PageviewsError, match="HTTP 403"):
        fp.PageviewsClient(session=session).get("https://wikimedia.org/x")
    assert session.calls == 1


def test_client_fails_fast_on_dns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(fp.time, "sleep", sleeps.append)

    class NoDns:
        headers: dict[str, str] = {}

        def get(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
            raise requests.ConnectionError("Max retries exceeded (Caused by NameResolutionError(...))")

    with pytest.raises(fp.PageviewsError, match="Cannot resolve host"):
        fp.PageviewsClient(session=NoDns()).get("https://wikimedia.org/x")
    assert sleeps == []


def test_client_rejects_non_json_success() -> None:
    class Html:
        headers: dict[str, str] = {}

        def get(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
            response = FakeResponse(None)
            response.json = lambda: (_ for _ in ()).throw(ValueError("no json"))  # type: ignore[method-assign]
            return response

    with pytest.raises(fp.PageviewsError, match="Non-JSON"):
        fp.PageviewsClient(session=Html()).get("https://wikimedia.org/x")


# --- CLI -----------------------------------------------------------------------


def _patch_client(monkeypatch: pytest.MonkeyPatch) -> None:
    real = fp.PageviewsClient
    monkeypatch.setattr(fp, "PageviewsClient", lambda: real(session=FixtureSession(), backoff_seconds=0))


def test_cli_single_article(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    _patch_client(monkeypatch)
    code = fp.main(["--project", "cs", "--article", "Přerušovaný půst", "--start", "2024-09-01", "--end", "2026-09-01",
                    "--cache-dir", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["period"] == {"start": "2024-09-01", "end": "2026-09-01", "granularity": "daily"}
    result = out["results"]["cs.wikipedia"]
    assert result["status"] == "ok" and result["total_views"] == 6940 and Path(result["csv_path"]).exists()
    assert any("willingness to pay" in line for line in out["limitations"])


def test_cli_resolved_file(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    _patch_client(monkeypatch)
    resolved = tmp_path / "resolved.json"
    resolved.write_text(json.dumps({"articles": {
        "cs": {"found": True, "project": "cs.wikipedia", "pageviews_title": "Přerušovaný_půst"},
        "pl": {"found": False, "project": "pl.wikipedia", "pageviews_title": None, "reason": "no sitelink"},
    }}), encoding="utf-8")
    code = fp.main(["--resolved", str(resolved), "--start", "2024-09-01", "--end", "2026-09-01", "--cache-dir", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and list(out["results"]) == ["cs"] and out["skipped"] == ["pl"]


def test_cli_years_with_end(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    _patch_client(monkeypatch)
    code = fp.main(["--project", "en", "--article", "Intermittent_fasting", "--granularity", "monthly", "--years", "2",
                    "--end", "2026-09-01", "--today", "2026-09-27", "--cache-dir", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["period"]["start"] == "2024-09-01" and out["results"]["en.wikipedia"]["points_requested"] == 24
    assert any("incomplete month" in c for c in out["results"]["en.wikipedia"]["caveats"])


def test_cli_errors_are_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    _patch_client(monkeypatch)
    assert fp.main(["--article", "X", "--cache-dir", str(tmp_path)]) == 1
    assert "--project is required" in json.loads(capsys.readouterr().out)["error"]
    assert fp.main(["--project", "en", "--article", "X", "--start", "2025-13-01", "--cache-dir", str(tmp_path)]) == 1
    assert "error" in json.loads(capsys.readouterr().out)


# --- edge cases found by live probing (2026-09-27) ----------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("intermittent_fasting", "Intermittent_fasting"),  # lower-case first letter -> 404 on the API
        ("Intermittent__fasting", "Intermittent_fasting"),  # double underscore -> 404
        ("Intermittent_fasting ", "Intermittent_fasting"),  # trailing space -> 404
        ("  intermittent  fasting ", "Intermittent_fasting"),
        ("AC/DC", "AC/DC"),
        ("100% (song)", "100%_(song)"),
        ("Přerušovaný půst", "Přerušovaný_půst"),
    ],
)
def test_normalize_title(raw: str, expected: str) -> None:
    assert fp.normalize_title(raw) == expected


def test_normalize_title_uses_nfc() -> None:
    import unicodedata

    nfd = unicodedata.normalize("NFD", "Přerušovaný_půst")
    assert nfd != "Přerušovaný_půst"  # decomposed form -> 404 on the API
    assert fp.normalize_title(nfd) == "Přerušovaný_půst"
    assert unicodedata.is_normalized("NFC", fp.normalize_title(nfd))


def test_normalize_project_drops_mobile_segment() -> None:
    assert fp.normalize_project("en.m.wikipedia") == "en.wikipedia"  # en.m.wikipedia -> 404 on per-article
    assert fp.normalize_project("nb.m.wikipedia.org") == "no.wikipedia"


def test_query_rejects_empty_title() -> None:
    with pytest.raises(ValueError, match="empty"):
        q("en", "  _ ", "daily", D(2025, 1, 1), D(2025, 1, 2))


def test_monthly_query_snaps_to_whole_months() -> None:
    # a mid-month single day would be HTTP 400 "no full months between dates"
    query = q("en", "Astronomy", "monthly", D(2026, 9, 15), D(2026, 9, 15))
    assert (query.start, query.end) == (D(2026, 9, 1), D(2026, 9, 30))
    assert q("en", "A", "monthly", D(2024, 2, 10), D(2024, 2, 10)).end == D(2024, 2, 29)
    assert q("en", "A", "daily", D(2026, 9, 15), D(2026, 9, 15)).end == D(2026, 9, 15)  # daily untouched


def test_fetch_entirely_before_data_starts_is_no_data(client: fp.PageviewsClient, tmp_path: Path) -> None:
    result = fp.fetch_pageviews(client, q("en", "Astronomy", "daily", D(2015, 1, 1), D(2015, 6, 30)), cache_dir=tmp_path)
    assert result.status == "no_data"
    assert any("only exist from 2015-07-01" in c for c in result.caveats)


def test_fetch_single_day(client: fp.PageviewsClient, tmp_path: Path) -> None:
    result = fp.fetch_pageviews(client, q("en", "Astronomy", "daily", D(2026, 9, 1), D(2026, 9, 1)), cache_dir=tmp_path)
    assert result.status == "ok" and result.points_requested == 1 and result.total_views == 844


def test_fetch_monthly_drops_current_incomplete_month(client: fp.PageviewsClient, tmp_path: Path) -> None:
    query = q("en", "Astronomy", "monthly", D(2025, 1, 1), D(2026, 9, 25))
    result = fp.fetch_pageviews(client, query, cache_dir=tmp_path, today=D(2026, 9, 27))
    assert result.points_requested == 20  # Jan 2025 .. Aug 2026; Sep 2026 (21st item) dropped
    assert result.series["date"].iloc[-1] == D(2026, 8, 1) and result.data_through == D(2026, 8, 1)
    assert any("incomplete month" in c and "2026-09" in c for c in result.caveats)
    # once the month is over it is kept
    later = fp.fetch_pageviews(client, query, cache_dir=tmp_path, today=D(2026, 10, 3))
    assert later.points_requested == 21 and not any("incomplete" in c for c in later.caveats)


def test_fetch_monthly_only_incomplete_month_is_no_data(tmp_path: Path) -> None:
    session = ItemsSession([("20260901", 500)])
    result = fp.fetch_pageviews(fp.PageviewsClient(session=session), q("en", "X", "monthly", D(2026, 9, 1), D(2026, 9, 30)),
                                cache_dir=tmp_path, today=D(2026, 9, 27))
    assert result.status == "no_data" and result.series.empty


def test_redirect_pages_have_their_own_views(client: fp.PageviewsClient, tmp_path: Path) -> None:
    jan = (D(2025, 1, 1), D(2025, 1, 31))
    redirect = fp.fetch_pageviews(client, q("en", "5:2_diet", "monthly", *jan), cache_dir=tmp_path)
    budgeting = fp.fetch_pageviews(client, q("en", "Budgeting", "monthly", *jan), cache_dir=tmp_path)
    budget = fp.fetch_pageviews(client, q("en", "Budget", "monthly", *jan), cache_dir=tmp_path)
    assert redirect.status == "ok" and redirect.total_views == 409  # not counted on Intermittent_fasting
    assert budgeting.total_views == 194 and budget.total_views == 9222
    assert 0.01 < budgeting.total_views / budget.total_views < 0.05


@pytest.mark.parametrize(
    "title, encoded, views",
    [("C++", "C%2B%2B", 71753), ("100% (song)", "100%25_%28song%29", 7)],
)
def test_fetch_titles_with_special_characters(
    client: fp.PageviewsClient, session: FixtureSession, tmp_path: Path, title: str, encoded: str, views: int
) -> None:
    result = fp.fetch_pageviews(client, q("en", title, "monthly", D(2025, 1, 1), D(2025, 1, 31)), cache_dir=tmp_path)
    assert result.status == "ok" and result.total_views == views
    assert f"/user/{encoded}/" in session.calls[0][0]


# --- Windows file encodings and --out ------------------------------------------


@pytest.mark.parametrize("encoding", ["utf-16", "utf-8-sig", "utf-8"])
def test_read_json_file_accepts_powershell_and_bom_encodings(tmp_path: Path, encoding: str) -> None:
    # PowerShell 5.1 `>` writes UTF-16 with BOM; editors may add a UTF-8 BOM
    path = tmp_path / "in.json"
    path.write_bytes(json.dumps({"title": "Přerušovaný půst"}, ensure_ascii=False).encode(encoding))
    assert fp.read_json_file(path)["title"] == "Přerušovaný půst"


def test_cli_reads_utf16_resolved_and_writes_utf8_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _patch_client(monkeypatch)
    resolved = tmp_path / "resolved.json"
    resolved.write_bytes(json.dumps({"articles": {
        "cs": {"found": True, "project": "cs.wikipedia", "pageviews_title": "Přerušovaný_půst"},
    }}, ensure_ascii=False).encode("utf-16"))
    out = tmp_path / "fetched.json"
    code = fp.main(["--resolved", str(resolved), "--start", "2024-09-01", "--end", "2026-09-01", "--cache-dir", str(tmp_path), "--out", str(out)])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["written"] == str(out)
    raw = out.read_bytes()
    assert not raw.startswith((b"\xff\xfe", b"\xef\xbb\xbf"))  # plain UTF-8, no BOM
    assert json.loads(raw.decode("utf-8"))["results"]["cs"]["total_views"] == 6940


def test_short_leading_gap_is_zero_filled_not_partial(tmp_path: Path) -> None:
    # bg "Периодично гладуване": the first requested day had zero views (omitted), the article is not younger
    session = ItemsSession([("20250104", 2), ("20250105", 3), ("20250106", 1)])
    result = fp.fetch_pageviews(fp.PageviewsClient(session=session), q("bg", "X", "daily", D(2025, 1, 1), D(2025, 1, 6)), cache_dir=tmp_path)
    assert result.status == "ok" and result.series["views"].tolist() == [0, 0, 0, 2, 3, 1]
    assert result.zero_filled_points == 3 and not any("younger" in c for c in result.caveats)
    long_gap = ItemsSession([("20250201", 2), ("20250202", 3)])
    result = fp.fetch_pageviews(fp.PageviewsClient(session=long_gap), q("bg", "X", "daily", D(2025, 1, 1), D(2025, 2, 2)), cache_dir=tmp_path)
    assert result.status == "partial" and result.series["date"].iloc[0] == D(2025, 2, 1)
