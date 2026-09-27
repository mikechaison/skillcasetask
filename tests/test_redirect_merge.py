"""Merging pageviews recorded under redirect titles (former names, spellings).

Synthetic sessions for the rules, recorded fixtures for the real case: the
Ukrainian Albert Einstein article, moved from "Альберт Ейнштейн" to
"Альберт Айнштайн" on 2026-02-10.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from scripts import cli
from scripts import fetch_pageviews as fp
from scripts import resolve_topic as rt
from tests.fixture_session import FakeResponse, FixtureSession

D = date


class TitlesSession:
    """Serves per-title daily/monthly payloads; anything else is 404."""

    headers: dict[str, str] = {}

    def __init__(self, daily: dict[str, list[tuple[str, int]]]) -> None:
        self.daily = daily
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs: Any) -> FakeResponse:
        self.calls.append(url)
        from urllib.parse import unquote

        parts = unquote(url).split("/")
        title, granularity = parts[-4], parts[-3]
        if title not in self.daily:
            return FakeResponse({"detail": "not found"}, status_code=404)
        rows = self.daily[title]
        if granularity == "monthly":
            months: dict[str, int] = {}
            for ts, v in rows:
                months[ts[:6]] = months.get(ts[:6], 0) + v
            items = [{"timestamp": m + "0100", "views": v} for m, v in sorted(months.items())]
        else:
            items = [{"timestamp": ts + "00", "views": v} for ts, v in rows]
        return FakeResponse({"items": items})


def days(start: date, values: list[int]) -> list[tuple[str, int]]:
    return [((start + fp.timedelta(days=i)).strftime("%Y%m%d"), v) for i, v in enumerate(values)]


def test_former_title_history_is_merged(tmp_path: Path) -> None:
    # current title: nothing, then 100/day after the move; old title: 100/day, then a trickle
    session = TitlesSession({
        "New_name": days(D(2025, 1, 1), [0] * 10 + [100] * 10),
        "Old_name": days(D(2025, 1, 1), [100] * 10 + [3] * 10),
        "Typo": days(D(2025, 1, 1), [1] * 20),
    })
    q = fp.PageviewsQuery("uk", "New_name", "daily", D(2025, 1, 1), D(2025, 1, 20))
    r = fp.fetch_with_redirects(fp.PageviewsClient(session=session), q, ["Old_name", "Typo", "Missing"], cache_dir=tmp_path)
    assert r.status == "ok" and r.series["views"].tolist() == [100] * 10 + [103] * 10
    assert r.total_views == 2030 and r.target_views == 1000 and r.zero_filled_points == 0
    assert [m["title"] for m in r.merged_titles] == ["Old_name"]  # Typo (20 views) is below both floors, Missing is 404
    assert r.merged_titles[0]["views"] == 1030 and r.merged_titles[0]["share_of_target"] == pytest.approx(1.03)
    assert r.csv_path.endswith("-merged.csv") and Path(r.csv_path).exists()
    assert any("probably renamed" in c for c in r.caveats) and any("Old_name" in c for c in r.caveats)
    assert not any("filled with 0" in c for c in r.caveats)
    # one monthly probe per redirect, one daily fetch only for the material one
    assert sum("/monthly/" in u for u in session.calls) == 3 and sum("/daily/" in u for u in session.calls) == 2


def test_small_redirects_are_ignored_and_result_is_unchanged(tmp_path: Path) -> None:
    session = TitlesSession({
        "Big": days(D(2025, 1, 1), [10000] * 20),
        "Alias": days(D(2025, 1, 1), [50] * 20),  # 1000 views: above 200 but below 2 % of 200 000
    })
    q = fp.PageviewsQuery("en", "Big", "daily", D(2025, 1, 1), D(2025, 1, 20))
    r = fp.fetch_with_redirects(fp.PageviewsClient(session=session), q, ["Alias"], cache_dir=tmp_path)
    assert r.merged_titles == [] and r.target_views is None and r.total_views == 200000
    assert not r.csv_path.endswith("-merged.csv")


def test_no_redirects_short_circuits(tmp_path: Path) -> None:
    session = TitlesSession({"X": days(D(2025, 1, 1), [5] * 20)})
    q = fp.PageviewsQuery("en", "X", "daily", D(2025, 1, 1), D(2025, 1, 20))
    r = fp.fetch_with_redirects(fp.PageviewsClient(session=session), q, [], cache_dir=tmp_path)
    assert r.total_views == 100 and len(session.calls) == 1


def test_target_without_data_but_material_redirect(tmp_path: Path) -> None:
    session = TitlesSession({"Old": days(D(2025, 1, 1), [40] * 20)})
    q = fp.PageviewsQuery("en", "New", "daily", D(2025, 1, 1), D(2025, 1, 20))
    r = fp.fetch_with_redirects(fp.PageviewsClient(session=session), q, ["Old"], cache_dir=tmp_path)
    assert r.status == "ok" and r.total_views == 800 and r.target_views == 0
    assert r.series["views"].tolist() == [40] * 20


def test_fetch_for_resolved_passes_redirects(tmp_path: Path) -> None:
    session = TitlesSession({"New_name": days(D(2025, 1, 1), [0] * 10 + [100] * 10), "Old_name": days(D(2025, 1, 1), [100] * 10 + [3] * 10)})
    resolved = {"articles": {"uk": {"found": True, "project": "uk.wikipedia", "pageviews_title": "New_name", "redirects": ["Old_name"]}}}
    client = fp.PageviewsClient(session=session)
    merged = fp.fetch_for_resolved(client, resolved, D(2025, 1, 1), D(2025, 1, 20), cache_dir=tmp_path)
    assert merged["uk"].total_views == 2030
    plain = fp.fetch_for_resolved(client, resolved, D(2025, 1, 1), D(2025, 1, 20), cache_dir=tmp_path, merge_redirects=False)
    assert plain["uk"].total_views == 1000


# --- the real case, from recorded fixtures ------------------------------------------


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    real_wiki, real_pv = rt.WikiClient, fp.PageviewsClient
    monkeypatch.setattr(rt, "WikiClient", lambda: real_wiki(session=FixtureSession(), backoff_seconds=0))
    monkeypatch.setattr(fp, "PageviewsClient", lambda: real_pv(session=FixtureSession(), backoff_seconds=0))


def test_resolver_lists_redirects(offline: None) -> None:
    client = rt.WikiClient()
    assert "Альберт Ейнштейн" in rt.get_redirects(client, "uk", "Альберт Айнштайн")
    result = rt.resolve_topic(client, "Albert Einstein", ["uk"])
    assert "Альберт Ейнштейн" in result.articles["uk"].redirects
    assert "redirects" in result.to_dict()["articles"]["uk"]


def test_einstein_uk_renamed_article(offline: None, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["compare", "--topic", "Albert Einstein", "--langs", "uk", "--start", "2024-09-01", "--end", "2026-09-01",
                     "--today", "2026-09-27", "--no-charts", "--out-dir", str(tmp_path / "run"), "--cache-dir", str(tmp_path / "cache"),
                     "--state", str(tmp_path / "state.json")])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    uk = out["results"]["uk"]
    assert uk["article"] == "Альберт Айнштайн"
    merged = {m["title"]: m for m in uk["data"]["merged_redirect_titles"]}
    assert "Альберт_Ейнштейн" in merged and merged["Альберт_Ейнштейн"]["views"] > 50_000
    assert merged["Альберт_Ейнштейн"]["views"] > uk["data"]["views_under_current_title"]  # the old title carried the history
    assert uk["direction"] != "growing" and uk["mean_views_per_day"] > 100
    assert uk["data"]["zero_filled_points"] < 10  # the 162 zero days were an artefact of the split history
    assert any("probably renamed" in c for c in uk["caveats"])
    plain = fp.read_json_file(tmp_path / "run" / "fetched.json")["results"]["uk"]
    assert plain["csv_path"].endswith("-merged.csv")
