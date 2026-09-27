"""Tests for scripts/analyze_trend.py.

Synthetic series with known properties (seeded noise) validate each building
block and the confidence rubric; the recorded real series (see
tests/record_fixtures.py) act as golden cases.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scripts import analyze_trend as at
from scripts import fetch_pageviews as fp
from tests.fixture_session import FixtureSession

DAYS = pd.date_range("2024-09-01", periods=730, freq="D").date
T = np.arange(730, dtype=float)


def rng() -> np.random.Generator:
    return np.random.default_rng(0)


def series(views: np.ndarray, start_index: int = 0) -> pd.DataFrame:
    views = np.asarray(views, dtype=float).round()
    return pd.DataFrame({"date": DAYS[start_index : start_index + len(views)], "views": views})


def linear(a: float, b: float, n: int = 730, noise: float = 5.0) -> np.ndarray:
    return np.linspace(a, b, n) + rng().normal(0, noise, n)


def flat(level: float = 200.0, noise: float = 10.0) -> np.ndarray:
    return np.full(730, level) + rng().normal(0, noise, 730)


def seasonal(amplitude: float = 80.0, growth: float = 0.0) -> np.ndarray:
    return (200 + amplitude * np.sin(2 * np.pi * T / 365)) * (1 + growth * T / 730) + rng().normal(0, 10, 730)


# --- building blocks -----------------------------------------------------------


def test_smooth_ignores_short_spikes() -> None:
    v = np.full(60, 50.0)
    v[30:33] = [500, 900, 400]
    assert at.smooth(v, 28).max() == 50.0
    assert len(at.smooth(v, 28)) == 60


def test_relative_change() -> None:
    # medians of the first/last 20 points of 100..200: ~104.8 and ~195.2
    assert at.relative_change(np.linspace(100, 200, 200), 20)[2] == pytest.approx(0.863, abs=0.01)
    assert at.relative_change(np.ones(10), 20) == (None, None, None)  # too short
    first, last, change = at.relative_change(np.concatenate([np.zeros(20), np.ones(20)]), 20)
    assert (first, last, change) == (0.0, 1.0, None)  # zero start: undefined


def test_aggregate_weekly_drops_incomplete_weeks() -> None:
    d = pd.Series(pd.date_range("2024-01-01", periods=20, freq="D"))
    assert at.aggregate(d, np.ones(20), "daily").tolist() == [7.0, 7.0]
    monthly = at.aggregate(pd.Series(pd.date_range("2024-01-01", periods=5, freq="MS")), np.arange(5), "monthly")
    assert monthly.tolist() == [0.0, 1.0, 2.0, 3.0, 4.0]


def test_regression_recovers_exponential_growth() -> None:
    r = at.regression(pd.Series(1000 * np.exp(np.linspace(0, 1, 52))), 52, "weeks")
    assert r.slope_per_year == pytest.approx(np.exp(52 / 51) - 1, abs=0.01)
    assert r.r2 > 0.999 and r.p_value < 1e-30 and r.n_points == 52 and r.unit == "weeks"


def test_regression_edge_cases() -> None:
    assert at.regression(pd.Series([5.0, 5.0, 5.0, 5.0]), 52, "weeks").slope_per_year == 0.0
    r = at.regression(pd.Series([1.0, 2.0]), 52, "weeks")
    assert r.slope_per_year is None and r.r2 is None and r.n_points == 2


def test_detect_spikes_groups_consecutive_days() -> None:
    v = np.full(200, 50.0)
    v[40:43] = [400, 900, 300]
    v[120] = 700
    spikes = at.detect_spikes(list(DAYS[:200]), v, at.smooth(v, 28), 4.0, 2.0)
    assert [(s.points, s.peak_views, s.peak_date) for s in spikes] == [(3, 900, DAYS[41]), (1, 700, DAYS[120])]
    assert spikes[0].start == DAYS[40] and spikes[0].end == DAYS[42] and spikes[0].baseline == 50.0
    assert spikes[0].excess_views == 400 + 900 + 300 - 150


def test_detect_spikes_none_on_noise_and_tiny_series() -> None:
    v = flat()
    assert at.detect_spikes(list(DAYS), v, at.smooth(v, 28), 4.0, 2.0) == []
    tiny = np.ones(200)
    tiny[100] = 3  # 3 views vs 1: not a spike (absolute excess rule)
    assert at.detect_spikes(list(DAYS[:200]), tiny, at.smooth(tiny, 28), 4.0, 2.0) == []


def test_seasonality_detects_yearly_shape_but_not_a_trend() -> None:
    assert at.seasonality(np.ones(700), at.TrendParams()) is None  # < 2 years
    sine = at.seasonality(np.tile(np.sin(np.linspace(0, 2 * np.pi, 365)) * 50 + 100, 2), at.TrendParams())
    assert sine.yoy_change == pytest.approx(0.0, abs=1e-9) and sine.profile_correlation > 0.99
    trend = at.seasonality(np.linspace(100, 300, 730), at.TrendParams())
    assert abs(trend.profile_correlation) < 0.3 and trend.yoy_change == pytest.approx(0.667, abs=0.01)
    assert at.is_seasonal(sine) and not at.is_seasonal(trend) and not at.is_seasonal(None)
    zeros_then_ones = at.seasonality(np.concatenate([np.zeros(365), np.ones(365)]), at.TrendParams())
    assert zeros_then_ones.yoy_change is None and zeros_then_ones.same_window_change is None


def test_opportunity_score() -> None:
    assert at.opportunity_score(None, 100, "High") is None
    assert at.opportunity_score(0.5, 1000, "High") > at.opportunity_score(0.5, 1000, "Medium") > at.opportunity_score(0.5, 1000, "Low")
    assert at.opportunity_score(0.5, 10000, "High") > at.opportunity_score(0.5, 10, "High")
    assert at.opportunity_score(5.0, 1e7, "High") == 100 and at.opportunity_score(-1.0, 0, "Low") == 0


def test_params_for_granularity() -> None:
    assert at.TrendParams.for_granularity("monthly").points_per_year == 12
    with pytest.raises(ValueError):
        at.TrendParams.for_granularity("weekly")
    with pytest.raises(ValueError, match="granularity"):
        at.analyze_series(series(flat()), "monthly", params=at.TrendParams())


# --- analyze_series on synthetic series ---------------------------------------


def test_linear_growth_is_high_confidence() -> None:
    a = at.analyze_series(series(linear(100, 300)))
    assert a.direction == "growing" and a.confidence.label == "High" and a.confidence.score == 100
    assert a.relative_change > 1.5 and a.indexed_last > 250 and a.headline_change == a.relative_change
    assert a.regression.r2 > 0.9 and a.regression.slope_per_year > 0.5
    assert a.spikes == [] and a.spike_share == 0.0 and a.seasonal is False
    assert a.headline_basis.startswith("smoothed level")
    assert any("consistent slope" in r for r in a.confidence.reasons)


def test_flat_noise_is_flat_and_high_confidence() -> None:
    a = at.analyze_series(series(flat()))
    assert a.direction == "flat" and a.confidence.label == "High"
    assert abs(a.relative_change) < 0.05 and a.spikes == []
    assert a.confidence.reasons == ["large volume, steady slope, no spikes, seasonality checked"]


def test_single_spike_on_flat_baseline() -> None:
    v = flat()
    v[400:403] = [5000, 12000, 3000]
    a = at.analyze_series(series(v))
    assert a.direction == "flat"  # the median smoothing does not move
    assert len(a.spikes) == 1 and a.spikes[0].peak_views == 12000 and a.spikes[0].peak_date == DAYS[401]
    assert a.spikes[0].points == 3 and 0.10 < a.spike_share < 0.15 and a.recent_spike_share == 0.0
    assert a.confidence.label == "High" and any("short spikes" in r for r in a.confidence.reasons)
    assert any(f"Largest spike: 12000 views on {DAYS[401]:%Y-%m-%d}" in c for c in a.caveats)


def test_spike_inside_last_window_lowers_confidence() -> None:
    v = flat()
    v[-20:-17] = [5000, 12000, 3000]
    a = at.analyze_series(series(v))
    assert a.direction == "flat" and a.recent_spike_share > 0.5
    assert a.confidence.label == "Medium" and any("inside the last window" in r for r in a.confidence.reasons)


def test_exponential_growth_slope_per_year() -> None:
    a = at.analyze_series(series(100 * np.exp(np.log(4) * T / 730) * rng().lognormal(0, 0.1, 730)))
    assert a.direction == "growing" and a.confidence.label == "High"
    assert a.regression.slope_per_year == pytest.approx(1.0, abs=0.15)  # x4 over 2 years = x2 per year


def test_decline_is_declining() -> None:
    a = at.analyze_series(series(linear(300, 100)))
    assert a.direction == "declining" and a.confidence.label == "High"
    assert a.relative_change < -0.5 and a.regression.slope_per_year < -0.3


def test_seasonal_without_trend_is_flat_via_year_over_year() -> None:
    a = at.analyze_series(series(seasonal()))
    assert a.seasonal is True and a.seasonality.profile_correlation > 0.9
    assert a.relative_change < -0.2  # endpoints sit in different seasons
    assert abs(a.headline_change) < 0.05 and a.direction == "flat"
    assert "12 months" in a.headline_basis
    assert a.confidence.label == "High"
    assert any("mixes seasons" in c for c in a.caveats)


def test_seasonal_with_growth() -> None:
    a = at.analyze_series(series(seasonal(growth=1.0)))
    assert a.seasonal is True and a.direction == "growing"
    assert a.headline_change == pytest.approx(a.seasonality.yoy_change, abs=1e-4) and a.headline_change > 0.3
    assert a.confidence.label == "High"
    assert any("seasonal pattern" in r and "agree" in r for r in a.confidence.reasons)


def test_low_volume_growth_is_medium() -> None:
    a = at.analyze_series(series(rng().poisson(np.linspace(2, 6, 730))))
    assert a.direction == "growing" and a.confidence.label == "Medium"
    assert any("very low volume" in r for r in a.confidence.reasons)
    assert a.mean_views_per_day < 10


def test_step_change_counts_as_growth() -> None:
    a = at.analyze_series(series(np.where(T < 365, 100.0, 200.0) + rng().normal(0, 5, 730)))
    assert a.direction == "growing" and a.relative_change == pytest.approx(1.0, abs=0.05)


def test_one_year_of_data_costs_one_point() -> None:
    a = at.analyze_series(series(linear(100, 200, n=365)))
    assert a.direction == "growing" and a.confidence.label == "High" and a.confidence.score == 85
    assert a.seasonality is None and any("less than two years" in r for r in a.confidence.reasons)


def test_short_series_is_insufficient() -> None:
    a = at.analyze_series(series(np.linspace(100, 200, 100)))
    assert a.direction == "insufficient" and a.confidence == at.Confidence("Low", 0, ["Only 100 daily points; at least 112 are needed for a trend."])
    assert a.opportunity_score is None and a.relative_change is None and a.points == 100


def test_noise_dominated_drift_is_flat() -> None:
    # +15 views over two years buried in noise (sd 30): no trend should be claimed
    a = at.analyze_series(series(200 + 0.02 * T + rng().normal(0, 30, 730)))
    assert a.direction == "flat" and a.regression.r2 < 0.2


def test_zero_start_has_no_relative_change() -> None:
    a = at.analyze_series(series(np.concatenate([np.zeros(200), np.linspace(1, 100, 530)])))
    assert a.relative_change is None and a.indexed_last is None and a.direction == "growing"
    assert any("zero level" in c for c in a.caveats)


def test_partial_status_costs_one_point() -> None:
    full = at.analyze_series(series(linear(100, 300)))
    partial = at.analyze_series(series(linear(100, 300)), status="partial")
    assert full.confidence.score - partial.confidence.score == 15
    assert any("younger than the period" in r for r in partial.confidence.reasons)


def test_monthly_series() -> None:
    months = pd.date_range("2024-09-01", periods=24, freq="MS").date
    a = at.analyze_series(pd.DataFrame({"date": months, "views": np.linspace(3000, 9000, 24).round()}), granularity="monthly")
    assert a.direction == "growing" and a.confidence.label == "High" and a.regression.unit == "months"
    assert a.mean_views_per_day == pytest.approx(6000 / 30.44, rel=0.01)
    short = at.analyze_series(pd.DataFrame({"date": months[:6], "views": np.ones(6)}), granularity="monthly")
    assert short.direction == "insufficient"


def test_to_dict_is_json_and_omits_smoothed_by_default() -> None:
    a = at.analyze_series(series(linear(100, 300)))
    data = a.to_dict()
    assert "smoothed" not in data and data["start"] == "2024-09-01" and data["end"] == "2026-08-31"
    assert isinstance(data["regression"]["r2"], float) and data["confidence"]["label"] == "High"
    assert len(a.to_dict(include_smoothed=True)["smoothed"]) == 730
    json.dumps(data)


def test_compare_ranks_by_opportunity() -> None:
    analyses = {
        "growing": at.analyze_series(series(linear(100, 300))),
        "flat": at.analyze_series(series(flat())),
        "declining": at.analyze_series(series(linear(300, 100))),
    }
    ranking = at.compare(analyses)["ranking"]
    assert [r["key"] for r in ranking] == ["growing", "flat", "declining"]
    assert ranking[0]["indexed_last"] > 250 and ranking[0]["confidence"] == "High"


# --- golden cases on recorded real series --------------------------------------


@pytest.fixture
def real(tmp_path: Path):
    client = fp.PageviewsClient(session=FixtureSession(), backoff_seconds=0)

    def load(project: str, article: str) -> at.TrendAnalysis:
        query = fp.PageviewsQuery(project, article, "daily", date(2024, 9, 1), date(2026, 9, 1))
        result = fp.fetch_pageviews(client, query, cache_dir=tmp_path)
        return at.analyze_series(result.series, "daily", status=result.status)

    return load


def test_real_cs_intermittent_fasting(real) -> None:
    # tiny article with a news spike: declining and Low confidence
    a = real("cs.wikipedia", "Přerušovaný_půst")
    assert a.direction == "declining" and a.confidence.label == "Low"
    assert a.mean_views_per_day < 10 and a.seasonal is False
    assert len(a.spikes) == 4 and max(a.spikes, key=lambda s: s.excess_views).peak_date == date(2025, 4, 14)
    assert max(s.peak_views for s in a.spikes) == 508 and 0.12 < a.spike_share < 0.15
    assert any("very low volume" in r for r in a.confidence.reasons)


def test_real_pl_meditation(real) -> None:
    a = real("pl.wikipedia", "Medytacja")
    assert a.direction == "declining" and a.confidence.label == "Medium"
    assert a.spike_share < 0.01 and 50 < a.mean_views_per_day < 70
    assert any("noisy slope" in r for r in a.confidence.reasons)


def test_real_uk_astronomy_is_seasonal(real) -> None:
    # school-year pattern: endpoints exaggerate the decline, year-over-year is quoted instead
    a = real("uk.wikipedia", "Астрономія")
    assert a.seasonal is True and a.seasonality.profile_correlation > 0.9
    assert a.relative_change < -0.8 and a.headline_change == pytest.approx(a.seasonality.yoy_change, abs=1e-4)
    assert -0.7 < a.headline_change < -0.5 and a.direction == "declining"
    assert "12 months" in a.headline_basis and any("mixes seasons" in c for c in a.caveats)
    assert a.confidence.label == "High"
    assert any("flattening" in r for r in a.confidence.reasons)


# --- CLI -----------------------------------------------------------------------


def test_cli_csv(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    csv = tmp_path / "s.csv"
    series(linear(100, 300)).to_csv(csv, index=False)
    assert at.main(["--csv", str(csv), "--label", "demo"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["analyses"]["demo"]["direction"] == "growing" and out["comparison"] is None
    assert out["method"] == "references/methodology.md" and "smoothed" not in out["analyses"]["demo"]
    assert at.main(["--csv", str(csv), "--smoothed"]) == 0
    assert len(json.loads(capsys.readouterr().out)["analyses"]["series"]["smoothed"]) == 730


def test_cli_fetched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    real_client = fp.PageviewsClient
    monkeypatch.setattr(fp, "PageviewsClient", lambda: real_client(session=FixtureSession(), backoff_seconds=0))
    resolved = tmp_path / "resolved.json"
    resolved.write_text(json.dumps({"articles": {
        "cs": {"found": True, "project": "cs.wikipedia", "pageviews_title": "Přerušovaný_půst"},
        "uk": {"found": True, "project": "uk.wikipedia", "pageviews_title": "Астрономія"},
    }}), encoding="utf-8")
    assert fp.main(["--resolved", str(resolved), "--start", "2024-09-01", "--end", "2026-09-01", "--cache-dir", str(tmp_path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    payload["results"]["xx"] = {"status": "no_data", "csv_path": None}  # a language without data is skipped
    fetched = tmp_path / "fetched.json"
    fetched.write_text(json.dumps(payload), encoding="utf-8")

    assert at.main(["--fetched", str(fetched)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out["analyses"]) == {"cs", "uk"} and out["skipped"] == {"xx": "no_data"}
    assert out["analyses"]["uk"]["seasonal"] is True
    assert [r["key"] for r in out["comparison"]["ranking"]] == ["uk", "cs"]


def test_cli_rejects_bad_csv(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    assert at.main(["--csv", str(bad)]) == 1
    assert "expected columns" in json.loads(capsys.readouterr().out)["error"]


def test_cli_fetched_utf16_and_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    real_client = fp.PageviewsClient
    monkeypatch.setattr(fp, "PageviewsClient", lambda: real_client(session=FixtureSession(), backoff_seconds=0))
    resolved = tmp_path / "resolved.json"
    resolved.write_text(json.dumps({"articles": {"cs": {"found": True, "project": "cs.wikipedia", "pageviews_title": "Přerušovaný_půst"}}}), encoding="utf-8")
    assert fp.main(["--resolved", str(resolved), "--start", "2024-09-01", "--end", "2026-09-01", "--cache-dir", str(tmp_path)]) == 0
    fetched = tmp_path / "fetched.json"
    fetched.write_bytes(capsys.readouterr().out.encode("utf-16"))  # as PowerShell `>` would write it
    out = tmp_path / "analysis.json"
    assert at.main(["--fetched", str(fetched), "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["written"] == str(out)
    assert json.loads(out.read_text(encoding="utf-8"))["analyses"]["cs"]["direction"] == "declining"
