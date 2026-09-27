"""Tests for scripts/make_chart.py (offline: series from recorded fixtures)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import matplotlib.colors as mcolors
import matplotlib.image as mpimg
import matplotlib.pyplot as plt
import numpy as np
import pytest
from matplotlib.patches import Patch

from scripts import analyze_trend as at
from scripts import fetch_pageviews as fp
from scripts import make_chart as mc
from tests.fixture_session import FixtureSession

ARTICLES = {
    "cs": {"found": True, "project": "cs.wikipedia", "pageviews_title": "Přerušovaný_půst", "title": "Přerušovaný půst"},
    "pl": {"found": True, "project": "pl.wikipedia", "pageviews_title": "Medytacja", "title": "Medytacja"},
    "uk": {"found": True, "project": "uk.wikipedia", "pageviews_title": "Астрономія", "title": "Астрономія"},
}


@pytest.fixture
def pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Run fetch + analyze on the recorded series and return the JSON paths."""
    real = fp.PageviewsClient
    monkeypatch.setattr(fp, "PageviewsClient", lambda: real(session=FixtureSession(), backoff_seconds=0))
    resolved = tmp_path / "resolved.json"
    resolved.write_text(json.dumps({"topic": "demo", "label": "demo topic", "articles": ARTICLES}, ensure_ascii=False), encoding="utf-8")
    fetched, analysis = tmp_path / "fetched.json", tmp_path / "analysis.json"
    assert fp.main(["--resolved", str(resolved), "--start", "2024-09-01", "--end", "2026-09-01", "--cache-dir", str(tmp_path / "cache"), "--out", str(fetched)]) == 0
    assert at.main(["--fetched", str(fetched), "--out", str(analysis)]) == 0
    return {"resolved": resolved, "fetched": fetched, "analysis": analysis, "dir": tmp_path}


def flare_bands(ax) -> list[Patch]:
    return [p for p in ax.patches if p.get_alpha() is not None and abs(p.get_alpha() - mc.FLARE_ALPHA) < 1e-9]


def load(paths: dict[str, Path]) -> tuple[list[mc.ChartSeries], dict[str, str]]:
    return mc.load_chart_series(fp.read_json_file(paths["fetched"]), fp.read_json_file(paths["analysis"]), fp.read_json_file(paths["resolved"]))


def test_index_series() -> None:
    assert mc.index_series(np.array([50.0, 100.0, 25.0]), 50.0).tolist() == [100.0, 200.0, 50.0]
    assert mc.index_series(np.array([1.0]), None) is None and mc.index_series(np.array([1.0]), 0.0) is None


def test_load_chart_series_builds_three_indexed_lines(pipeline: dict[str, Path]) -> None:
    series, skipped = load(pipeline)
    assert [s.key for s in series] == ["cs", "pl", "uk"] and skipped == {}
    assert series[0].label == "cs · Přerušovaný půst (very low volume)" and series[2].label == "uk · Астрономія"
    for s in series:
        assert len(s.dates) == 731 and s.dates[0] == date(2024, 9, 1) and len(s.indexed) == 731
        assert s.direction == "declining" and s.confidence in ("High", "Medium", "Low")
    assert len(series[0].spikes) == 4 and series[0].spikes[1] == (date(2025, 4, 14), date(2025, 4, 15))
    assert series[2].last_value == pytest.approx(18.8, abs=0.1)


def test_load_skips_series_without_data(pipeline: dict[str, Path]) -> None:
    fetched = fp.read_json_file(pipeline["fetched"])
    analysis = fp.read_json_file(pipeline["analysis"])
    analysis["analyses"]["zz"] = dict(analysis["analyses"]["cs"], first_level=None)
    fetched["results"]["zz"] = fetched["results"]["cs"]
    analysis["analyses"]["yy"] = analysis["analyses"]["cs"]  # no fetched entry
    series, skipped = mc.load_chart_series(fetched, analysis)
    assert [s.key for s in series] == ["cs", "pl", "uk"]
    assert "cannot index" in skipped["zz"] and skipped["yy"] == "no series"


def test_render_chart_marks_follow_the_spec(pipeline: dict[str, Path], tmp_path: Path) -> None:
    series, _ = load(pipeline)
    out = tmp_path / "chart.png"
    fig = mc.render_chart(series, "Demo", out=out, svg=tmp_path / "chart.svg")
    ax = fig.axes[0]
    lines = [ln for ln in ax.get_lines() if len(ln.get_xdata()) > 1 and ln.get_label() != "_nolegend_" and not ln.get_label().startswith("_")]
    # fixed slot order, never re-ranked; 2px lines
    assert [ln.get_color() for ln in lines[:3]] == mc.SERIES_COLORS[:3]
    assert all(ln.get_linewidth() == 2 for ln in lines[:3])
    # one flare band per spike event, in the series hue at low alpha
    bands = flare_bands(ax)
    assert len(bands) == sum(len(s.spikes) for s in series) == 7
    assert {p.get_facecolor()[:3] for p in bands} == {tuple(mcolors.to_rgb(c)) for c in mc.SERIES_COLORS[:3]}
    # legend for >= 2 series; end labels dropped because cs and uk converge at the right edge
    assert ax.get_legend() is not None and len(ax.get_legend().get_texts()) == 3
    assert len(ax.texts) == 0  # no absolute-views label on an indexed chart; spikes are bands here, peaks on the multiples
    assert ax.get_ylim()[0] == 0 and ax.get_ylim()[1] >= 120
    assert (tmp_path / "chart.svg").exists()
    plt.close(fig)
    image = mpimg.imread(out)
    assert image.shape[0] > 900 and image.shape[1] > 1800  # 10x5.2 in at 200 dpi


def test_render_single_series_has_no_legend_but_an_end_label(pipeline: dict[str, Path], tmp_path: Path) -> None:
    series, _ = load(pipeline)
    fig = mc.render_chart(series[:1], "One", out=tmp_path / "one.png")
    ax = fig.axes[0]
    assert ax.get_legend() is None
    end_labels = [t for t in ax.texts if t.get_text().startswith("cs ")]
    assert len(end_labels) == 1 and len(ax.texts) == 1  # only the end label
    assert end_labels[0].get_color() == mc.TOKENS["ink2"]  # text wears ink, never the series colour
    plt.close(fig)


def test_render_end_labels_when_lines_separate(pipeline: dict[str, Path], tmp_path: Path) -> None:
    series, _ = load(pipeline)
    series[0].indexed = series[0].indexed + 60  # push cs well above uk
    fig = mc.render_chart(series, "Spread", out=tmp_path / "spread.png")
    assert len(fig.axes[0].texts) == 3  # three end labels
    plt.close(fig)


def test_short_flares_are_widened_to_stay_visible(pipeline: dict[str, Path], tmp_path: Path) -> None:
    series, _ = load(pipeline)
    fig = mc.render_chart(series[:1], "Bands", out=tmp_path / "bands.png")
    ax = fig.axes[0]
    widths = [p.get_bbox().width for p in flare_bands(ax)]  # data units = days
    assert len(widths) == 4 and min(widths) >= 731 * mc.MIN_FLARE_FRACTION - 1
    plt.close(fig)


def test_render_caps_series_at_palette_size(pipeline: dict[str, Path]) -> None:
    fetched = fp.read_json_file(pipeline["fetched"])
    analysis = fp.read_json_file(pipeline["analysis"])
    for i in range(9):
        analysis["analyses"][f"x{i}"] = analysis["analyses"]["cs"]
        fetched["results"][f"x{i}"] = fetched["results"]["cs"]
    series, skipped = mc.load_chart_series(fetched, analysis)
    assert len(series) == mc.MAX_SERIES and all("palette" in v for v in skipped.values()) and len(skipped) == 4


def test_render_requires_a_series() -> None:
    with pytest.raises(ValueError):
        mc.render_chart([], "empty")


def test_series_table_is_the_table_view_twin(pipeline: dict[str, Path]) -> None:
    series, _ = load(pipeline)
    rows = mc.series_table(series)
    assert [r["color"] for r in rows] == mc.SERIES_COLORS[:3]
    assert rows[0]["spike_events"] == 4 and rows[2]["indexed_last"] == pytest.approx(18.8, abs=0.1)
    json.dumps(rows)


def test_cli(pipeline: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    out = pipeline["dir"] / "cli.png"
    code = mc.main(["--fetched", str(pipeline["fetched"]), "--analysis", str(pipeline["analysis"]), "--resolved", str(pipeline["resolved"]), "--out", str(out)])
    summary = json.loads(capsys.readouterr().out)
    assert code == 0 and out.exists() and summary["title"] == "demo topic"
    assert [s["key"] for s in summary["series"]] == ["cs", "pl", "uk"] and summary["skipped"] == {}
    assert mc.main(["--fetched", str(pipeline["fetched"]), "--analysis", str(pipeline["dir"] / "missing.json"), "--out", str(out)]) == 1
    assert "error" in json.loads(capsys.readouterr().out)


# --- extra charts ------------------------------------------------------------------


def test_weekly_profile() -> None:
    assert mc.weekly_profile(np.ones(730), 0).tolist()[:2] == [7.0, 7.0] and len(mc.weekly_profile(np.ones(730), 0)) == 52
    assert mc.weekly_profile(np.ones(730), 364) is not None and mc.weekly_profile(np.ones(700), 364) is None
    assert mc.weekly_profile(np.arange(364, dtype=float), 0)[0] == sum(range(7))


def test_small_multiples_one_facet_per_series(pipeline: dict[str, Path], tmp_path: Path) -> None:
    series, _ = load(pipeline)
    fig = mc.render_small_multiples(series, "Multiples", out=tmp_path / "m.png")
    axes = [ax for ax in fig.axes if ax.get_visible()]
    assert len(axes) == 3
    for i, ax in enumerate(axes):
        assert ax.get_legend() is None  # single series per facet
        assert len(ax.collections) == 1 and abs(ax.collections[0].get_alpha() - 0.10) < 1e-9  # raw wash
        lines = [ln for ln in ax.get_lines() if len(ln.get_xdata()) > 1]
        assert lines[0].get_color() == mc.SERIES_COLORS[i] and lines[0].get_linewidth() == 2
        assert len(ax.texts) == 1  # one label on the largest peak; the heading is the axis title
        assert ax.get_title(loc="left").startswith(series[i].label)
    assert (tmp_path / "m.png").exists()
    plt.close(fig)


def test_small_multiples_two_columns_past_three(pipeline: dict[str, Path]) -> None:
    series, _ = load(pipeline)
    fig = mc.render_small_multiples(series + series[:2], "Five")
    assert len([ax for ax in fig.axes if ax.get_visible()]) == 5 and len(fig.axes) == 6
    plt.close(fig)


def test_seasonal_profiles_draw_two_years_only(pipeline: dict[str, Path], tmp_path: Path) -> None:
    series, _ = load(pipeline)
    short = mc.ChartSeries(key="xx", label="xx", dates=series[0].dates[:400], indexed=series[0].indexed[:400], views=series[0].views[:400])
    fig = mc.render_seasonal_profiles(series + [short], "Seasonal", out=tmp_path / "s.png")
    axes = [ax for ax in fig.axes if ax.get_visible()]
    assert len(axes) == 4
    for i, ax in enumerate(axes[:3]):
        lines = ax.get_lines()
        assert [ln.get_color() for ln in lines] == [mc.TOKENS["baseline"], mc.SERIES_COLORS[i]]  # previous year gray, current in hue
        assert all(len(ln.get_xdata()) == 52 for ln in lines)
    assert axes[3].get_lines() == [] and any("less than two years" in t.get_text() for t in axes[3].texts)
    assert "seasonal pattern detected" in axes[2].get_title(loc="left")  # uk
    assert fig.legends and len(fig.legends[0].get_texts()) == 2
    plt.close(fig)


def test_ranking_bars_sorted_in_edition_colours(pipeline: dict[str, Path], tmp_path: Path) -> None:
    series, _ = load(pipeline)
    fig = mc.render_ranking(series, "Ranking", out=tmp_path / "r.png")
    ax = fig.axes[0]
    bars = ax.patches
    assert len(bars) == 3
    ranked = sorted(series, key=lambda s: -s.opportunity_score)
    slot = {s.key: mc.SERIES_COLORS[i] for i, s in enumerate(series)}
    assert [b.get_facecolor()[:3] for b in bars] == [tuple(mcolors.to_rgb(slot[s.key])) for s in ranked]  # edition colours, not rank
    widths = [b.get_width() for b in bars]
    assert widths == sorted(widths, reverse=True) and widths[0] == max(s.opportunity_score for s in series)
    assert all(b.get_height() <= 0.25 for b in bars)  # thin marks
    labels = [t.get_text() for t in ax.get_yticklabels()]
    assert labels[0].startswith("uk") and labels[-1].startswith("cs")  # tick labels in ranked order, best at the top
    assert any("confidence High" in t.get_text() for t in ax.texts)
    plt.close(fig)


def test_cli_writes_extra_charts(pipeline: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    d = pipeline["dir"]
    code = mc.main(["--fetched", str(pipeline["fetched"]), "--analysis", str(pipeline["analysis"]), "--out", str(d / "c.png"),
                    "--multiples", str(d / "m.png"), "--seasonal", str(d / "s.png"), "--ranking", str(d / "r.png")])
    summary = json.loads(capsys.readouterr().out)
    assert code == 0 and all((d / f).exists() for f in ("c.png", "m.png", "s.png", "r.png"))
    assert set(summary["extras"]) == {"multiples", "seasonal", "ranking"}


def test_cli_writes_extras_by_default_and_can_skip_them(pipeline: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    d = pipeline["dir"] / "auto"
    d.mkdir()
    assert mc.main(["--fetched", str(pipeline["fetched"]), "--analysis", str(pipeline["analysis"]), "--out", str(d / "chart.png")]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert all((d / f"{k}.png").exists() for k in ("multiples", "seasonal", "ranking"))
    assert summary["extras"]["ranking"].endswith("ranking.png")
    assert mc.main(["--fetched", str(pipeline["fetched"]), "--analysis", str(pipeline["analysis"]), "--out", str(d / "only.png"), "--no-extras"]) == 0
    assert json.loads(capsys.readouterr().out)["extras"] == {"multiples": None, "seasonal": None, "ranking": None}


def test_font_stack_has_cjk_fallbacks() -> None:
    assert "Yu Gothic" in mc.FONT_STACK and "Noto Sans CJK JP" in mc.FONT_STACK and mc.FONT_STACK[-1] == "sans-serif"
