"""Chart: normalised interest lines per language edition with spike ("flare") shading.

Design follows the `dataviz` skill: one indexed axis (first 8 smoothed weeks =
100) instead of comparing raw audiences; categorical hues assigned in fixed
slot order (validated palette, never cycled); 2px lines with ring-ed end
markers; hairline solid gridlines; a legend for >= 2 series plus selective
direct end labels (<= 4, dropped when they would collide); spike events shaded
as a 12 % wash of the series hue.  Text always uses ink tokens, never the
series colour.  The JSON the CLI prints is the chart's table-view twin.

Usage (CLI):

    python scripts/make_chart.py --fetched fetched.json --analysis analysis.json \\
        --resolved resolved.json --title "Intermittent fasting" --out chart.png

Usage (library):

    >>> from scripts.make_chart import load_chart_series, render_chart
    >>> series = load_chart_series(fetched, analysis, resolved)   # dicts as loaded from the JSON files
    >>> fig = render_chart(series, "Intermittent fasting", out="chart.png")
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.analyze_trend import TrendParams, smooth  # noqa: E402
from scripts.fetch_pageviews import read_json_file  # noqa: E402

# Reference palette from the dataviz skill (light mode). Slot order is the CVD-safety
# mechanism: validated adjacent pairs, never re-ordered or cycled.
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
TOKENS = {
    "surface": "#fcfcfb",
    "page": "#f9f9f7",
    "ink": "#0b0b0b",
    "ink2": "#52514e",
    "muted": "#898781",
    "grid": "#e1e0d9",
    "baseline": "#c3c2b7",
}
def font_family() -> list[str]:
    """Installed fonts from FONT_STACK, in order, ending with DejaVu Sans (always shipped with matplotlib).

    matplotlib falls back glyph by glyph along ``font.family`` only when it holds
    concrete font names, so the generic ``sans-serif`` alias is not enough for CJK.
    """
    from matplotlib import font_manager

    installed = {f.name for f in font_manager.fontManager.ttflist}
    names = [n for n in FONT_STACK if n in installed and n != "sans-serif"]
    if "DejaVu Sans" not in names:
        names.append("DejaVu Sans")
    return names


def _apply_fonts() -> None:
    plt.rcParams.update({"font.family": font_family(), "axes.unicode_minus": False})


MAX_SERIES = len(SERIES_COLORS)
MAX_DIRECT_LABELS = 4
FLARE_ALPHA = 0.12
MIN_FLARE_FRACTION = 0.008  # a spike band is drawn at least this share of the x-range wide (~6 days on 2 years)
FONT_STACK = [
    "Segoe UI", "DejaVu Sans", "Arial", "Helvetica",
    # CJK fallbacks (glyph-level fallback walks this list): Windows, macOS, Linux/Noto
    "Yu Gothic", "Meiryo", "Malgun Gothic", "Microsoft YaHei", "Hiragino Sans", "PingFang SC",
    "Noto Sans CJK JP", "Noto Sans JP", "Noto Sans CJK SC", "Noto Sans CJK KR",
    "sans-serif",
]


@dataclass
class ChartSeries:
    """One line on the chart."""

    key: str
    label: str
    dates: list[date]
    indexed: np.ndarray  # smoothed / first_level * 100
    spikes: list[tuple[date, date]] = field(default_factory=list)
    direction: str = "flat"
    confidence: str = "Low"
    headline_change: Optional[float] = None
    views: np.ndarray = field(default_factory=lambda: np.zeros(0))  # raw daily views
    smoothed: np.ndarray = field(default_factory=lambda: np.zeros(0))
    spike_details: list[dict[str, Any]] = field(default_factory=list)  # from analysis.json
    mean_views_per_day: float = 0.0
    opportunity_score: Optional[int] = None
    seasonal: bool = False

    @property
    def last_value(self) -> float:
        return float(self.indexed[-1])


def index_series(smoothed: np.ndarray, first_level: Optional[float]) -> Optional[np.ndarray]:
    """Index a smoothed series to 100 at ``first_level``; None when that level is missing or zero.

    >>> index_series(np.array([50.0, 100.0]), 50.0).tolist()
    [100.0, 200.0]
    >>> index_series(np.array([1.0]), None) is None
    True
    """
    if first_level is None or first_level <= 0:
        return None
    return np.asarray(smoothed, dtype=float) / float(first_level) * 100.0


def load_chart_series(
    fetched: dict[str, Any], analysis: dict[str, Any], resolved: Optional[dict[str, Any]] = None
) -> tuple[list[ChartSeries], dict[str, str]]:
    """Build chart series from the three pipeline outputs; returns ``(series, skipped)``."""
    granularity = fetched.get("period", {}).get("granularity", "daily")
    params = TrendParams.for_granularity(granularity)
    series: list[ChartSeries] = []
    skipped: dict[str, str] = {}
    for key, a in analysis.get("analyses", {}).items():
        result = fetched.get("results", {}).get(key)
        if not result or not result.get("csv_path"):
            skipped[key] = "no series"
            continue
        frame = pd.read_csv(result["csv_path"])
        if frame.empty:
            skipped[key] = "empty series"
            continue
        smoothed = smooth(frame["views"].to_numpy(dtype=float), params.smoothing_window)
        indexed = index_series(smoothed, a.get("first_level"))
        if indexed is None:
            skipped[key] = "cannot index: first level is zero or the series is too short"
            continue
        title = None
        if resolved:
            title = resolved.get("articles", {}).get(key, {}).get("title")
        label = f"{key} · {title}" if title else key
        if float(a.get("mean_views_per_day", 0.0)) < 10:
            label += " (very low volume)"  # an indexed line of a 1-3 views/day article moves in huge steps
        spikes = [(date.fromisoformat(s["start"]), date.fromisoformat(s["end"])) for s in a.get("spikes", [])]
        series.append(
            ChartSeries(
                key=key, label=label, dates=[date.fromisoformat(str(d)) for d in frame["date"]], indexed=indexed,
                spikes=spikes, direction=a.get("direction", "flat"), confidence=a.get("confidence", {}).get("label", "Low"),
                headline_change=a.get("headline_change"), views=frame["views"].to_numpy(dtype=float), smoothed=smoothed,
                spike_details=list(a.get("spikes", [])), mean_views_per_day=float(a.get("mean_views_per_day", 0.0)),
                opportunity_score=a.get("opportunity_score"), seasonal=bool(a.get("seasonal", False)),
            )
        )
    if len(series) > MAX_SERIES:
        for extra in series[MAX_SERIES:]:
            skipped[extra.key] = f"more than {MAX_SERIES} series: not drawn (palette has {MAX_SERIES} slots)"
        series = series[:MAX_SERIES]
    return series, skipped


def _labels_collide(values: list[float], y_range: float, min_gap_fraction: float = 0.04) -> bool:
    ordered = sorted(values)
    return any(b - a < y_range * min_gap_fraction for a, b in zip(ordered, ordered[1:]))


def render_chart(
    series: list[ChartSeries],
    title: str,
    subtitle: Optional[str] = None,
    out: Optional[str | Path] = None,
    svg: Optional[str | Path] = None,
) -> Figure:
    """Draw the chart; save PNG (200 dpi) and optionally SVG; return the Figure."""
    if not series:
        raise ValueError("nothing to draw: every series was skipped")
    _apply_fonts()
    want_labels = len(series) <= MAX_DIRECT_LABELS
    fig, ax = plt.subplots(figsize=(10, 5.6), dpi=200)
    fig.patch.set_facecolor(TOKENS["page"])
    ax.set_facecolor(TOKENS["surface"])

    top = max(float(np.nanmax(s.indexed)) for s in series)
    y_max = max(120.0, top * 1.12)
    span_days = max((max(s.dates[-1] for s in series) - min(s.dates[0] for s in series)).days, 1)
    min_band = timedelta(days=max(1, round(span_days * MIN_FLARE_FRACTION)))  # a 1-day event must still be visible
    for i, s in enumerate(series):
        color = SERIES_COLORS[i]
        for start, end in s.spikes:  # flare shading: a wash of the series hue over the event
            width = end - start + timedelta(days=1)
            if width < min_band:
                pad = (min_band - width) / 2
                start, end = start - pad, end + pad
            ax.axvspan(start, end + timedelta(days=1), color=color, alpha=FLARE_ALPHA, linewidth=0)
        ax.plot(s.dates, s.indexed, color=color, linewidth=2, solid_joinstyle="round", solid_capstyle="round", label=s.label)
        ax.plot(s.dates[-1], s.last_value, "o", color=color, markersize=8, markeredgecolor=TOKENS["surface"], markeredgewidth=2)

    # No absolute-views label here: on an indexed chart the tallest line is the edition with the largest
    # *relative* surge, which is often not the edition with the most views. Absolute spike peaks are
    # marked per edition on the small-multiples chart instead.
    ax.axhline(100, color=TOKENS["baseline"], linewidth=1, zorder=1)
    ax.set_ylim(0, y_max)
    ax.yaxis.grid(True, color=TOKENS["grid"], linewidth=1)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(TOKENS["baseline"])
    ax.tick_params(colors=TOKENS["muted"], labelsize=9, length=0)
    ax.set_ylabel(f"Index (first {'8 weeks' if len(series[0].dates) > 60 else 'window'} = 100)", color=TOKENS["ink2"], fontsize=9)
    locator = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))

    if want_labels and not _labels_collide([s.last_value for s in series], y_max):
        for i, s in enumerate(series):
            ax.annotate(
                f"{s.key} {s.last_value:.0f}", xy=(s.dates[-1], s.last_value), xytext=(8, 0), textcoords="offset points",
                va="center", fontsize=9, color=TOKENS["ink2"],
            )
        right = 0.90
    else:
        right = 0.97
    if len(series) >= 2:  # below the plot, so it never collides with lines or the spike label
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.13), frameon=False, fontsize=9, ncol=min(len(series), 3),
                  labelcolor=TOKENS["ink2"])

    fig.text(0.06, 0.95, title, fontsize=13, fontweight="bold", color=TOKENS["ink"], ha="left", va="top")
    fig.text(
        0.06, 0.905,
        subtitle or "Wikipedia pageviews (agent=user), 28-day rolling median, indexed to each edition's first 8 weeks",
        fontsize=9.5, color=TOKENS["ink2"], ha="left", va="top",
    )
    fig.text(
        0.06, 0.015, "Shaded bands = spike events (likely news or viral posts; widened to stay visible). Source: Wikimedia Pageviews API.",
        fontsize=8, color=TOKENS["muted"], ha="left", va="bottom",
    )
    fig.subplots_adjust(left=0.06, right=right, top=0.85, bottom=0.25 if len(series) >= 2 else 0.13)
    if out:
        fig.savefig(out, dpi=200, facecolor=fig.get_facecolor())
    if svg:
        fig.savefig(svg, format="svg", facecolor=fig.get_facecolor())
    return fig


# --- extra charts ----------------------------------------------------------------


def _style_axes(ax: Any, y_label: Optional[str] = None) -> None:
    ax.set_facecolor(TOKENS["surface"])
    ax.yaxis.grid(True, color=TOKENS["grid"], linewidth=1)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(TOKENS["baseline"])
    ax.tick_params(colors=TOKENS["muted"], labelsize=8, length=0)
    if y_label:
        ax.set_ylabel(y_label, color=TOKENS["ink2"], fontsize=8)


HEADER_IN = 0.9  # inches reserved above the facets for title, subtitle and legend


def _facets(n: int, row_height: float, extra: float) -> tuple[Figure, Any]:
    """Facet grid with constrained layout so headings, axis labels and ticks never overlap.

    Facet headings are axis titles (the layout engine measures them); the
    figure title block lives in a fixed band of ``HEADER_IN`` inches at the top.
    """
    cols = 1 if n <= 3 else 2
    rows = -(-n // cols)
    height = row_height * rows + extra
    fig, axes = plt.subplots(rows, cols, figsize=(10, height), dpi=200, squeeze=False, layout="constrained")
    fig.get_layout_engine().set(rect=(0.0, 0.0, 1.0, 1.0 - HEADER_IN / height), h_pad=0.15, w_pad=0.15, hspace=0.08, wspace=0.05)
    fig.patch.set_facecolor(TOKENS["page"])
    for ax in axes.flat[n:]:
        ax.set_visible(False)
    return fig, axes


def _header(fig: Figure, title: str, subtitle: str) -> None:
    fig.text(0.03, 0.985, title, fontsize=13, fontweight="bold", color=TOKENS["ink"], ha="left", va="top")
    fig.text(0.03, 0.985 - 0.3 / fig.get_figheight(), subtitle, fontsize=9, color=TOKENS["ink2"], ha="left", va="top")


def render_small_multiples(series: list[ChartSeries], title: str, out: Optional[str | Path] = None) -> Figure:
    """Raw daily views per edition, one facet each on its own scale (never a dual axis).

    The raw series is a 10 % wash of the series hue, the 28-day median a 2px
    line, spike peaks ring-ed markers; only the largest peak per facet is
    labelled.  One series per facet, so no legend.
    """
    if not series:
        raise ValueError("nothing to draw")
    _apply_fonts()
    fig, axes = _facets(len(series), 2.0, HEADER_IN + 0.3)
    for i, (s, ax) in enumerate(zip(series, axes.flat)):
        color = SERIES_COLORS[i]
        ax.fill_between(s.dates, 0, s.views, color=color, alpha=0.10, linewidth=0)
        ax.plot(s.dates, s.smoothed, color=color, linewidth=2, solid_joinstyle="round", solid_capstyle="round")
        if s.spike_details:
            peaks = [(date.fromisoformat(d["peak_date"]), d["peak_views"]) for d in s.spike_details]
            ax.plot([p[0] for p in peaks], [p[1] for p in peaks], "o", color=color, markersize=8,
                    markeredgecolor=TOKENS["surface"], markeredgewidth=2)
            top_peak = max(peaks, key=lambda p: p[1])
            ax.annotate(f"{top_peak[1]:,}", xy=top_peak, xytext=(6, -2), textcoords="offset points", fontsize=8, color=TOKENS["ink2"])
        _style_axes(ax, "views/day")
        ax.set_ylim(0, max(float(np.max(s.views)) * 1.15, 1.0))
        ax.set_title(f"{s.label} · {s.mean_views_per_day:,.0f} views/day · {s.direction}", loc="left", fontsize=9,
                     color=TOKENS["ink"], pad=5)
        locator = mdates.AutoDateLocator()
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    _header(fig, title, "Raw daily views (wash) and 28-day median (line) per edition; dots mark spike peaks. Axes differ per facet.")
    if out:
        fig.savefig(out, dpi=200, facecolor=fig.get_facecolor())
    return fig


def weekly_profile(views: np.ndarray, offset_from_end: int, weeks: int = 52) -> Optional[np.ndarray]:
    """Weekly totals for the 52 weeks ending ``offset_from_end`` days before the end of the series.

    >>> weekly_profile(np.ones(730), 0).tolist()[:3]
    [7.0, 7.0, 7.0]
    >>> weekly_profile(np.ones(100), 0) is None
    True
    """
    end = len(views) - offset_from_end
    start = end - weeks * 7
    if start < 0:
        return None
    return np.asarray(views[start:end], dtype=float).reshape(weeks, 7).sum(axis=1)


def render_seasonal_profiles(series: list[ChartSeries], title: str, out: Optional[str | Path] = None) -> Figure:
    """Last 12 months vs the previous 12 months, weekly, per edition.

    The current year in the series hue, the previous year in the de-emphasis
    gray; a single shared legend names the two periods.  Facets with less
    than two years of data say so instead of drawing.
    """
    if not series:
        raise ValueError("nothing to draw")
    _apply_fonts()
    fig, axes = _facets(len(series), 2.1, HEADER_IN + 0.3)
    drawn = 0
    for i, (s, ax) in enumerate(zip(series, axes.flat)):
        color = SERIES_COLORS[i]
        current, previous = weekly_profile(s.views, 0), weekly_profile(s.views, 364)
        _style_axes(ax, "views/week")
        heading = s.label + (" · seasonal pattern detected" if s.seasonal else "")
        ax.set_title(heading, loc="left", fontsize=9, color=TOKENS["ink"], pad=5)
        if current is None or previous is None:
            ax.text(0.5, 0.5, "less than two years of data", transform=ax.transAxes, ha="center", va="center",
                    fontsize=9, color=TOKENS["muted"])
            ax.set_xticks([])
            continue
        weeks = np.arange(1, len(current) + 1)
        ax.plot(weeks, previous, color=TOKENS["baseline"], linewidth=2, label="previous 12 months")
        ax.plot(weeks, current, color=color, linewidth=2, label="last 12 months")
        ax.set_ylim(0, max(float(max(current.max(), previous.max())) * 1.15, 1.0))
        ax.set_xlim(1, len(current))
        ax.set_xticks([1, 13, 26, 39, 52])
        ax.set_xlabel("week of the 12-month window", color=TOKENS["muted"], fontsize=8)
        drawn += 1
    _header(fig, title, "Weekly views: last 12 months (colour) vs the previous 12 months (gray), aligned by week since the window start.")
    if drawn:  # legend sits in the reserved header band, right-aligned, clear of the facets
        handles = [plt.Line2D([], [], color=TOKENS["baseline"], linewidth=2), plt.Line2D([], [], color=TOKENS["ink2"], linewidth=2)]
        fig.legend(handles, ["previous 12 months", "last 12 months (edition colour)"], loc="upper right",
                   bbox_to_anchor=(0.985, 0.995), frameon=False, fontsize=8, ncol=2, labelcolor=TOKENS["ink2"])
    if out:
        fig.savefig(out, dpi=200, facecolor=fig.get_facecolor())
    return fig


def render_ranking(series: list[ChartSeries], title: str, out: Optional[str | Path] = None) -> Figure:
    """Opportunity score per edition as thin horizontal bars, value at the tip.

    Each bar wears the colour its edition has on every other chart (colour
    follows the entity, never its rank), so the ranking reads against the
    line charts; the row label and the text beside the bar carry identity too.
    """
    if not series:
        raise ValueError("nothing to draw")
    _apply_fonts()
    slot = {s.key: SERIES_COLORS[i] for i, s in enumerate(series)}  # same slot as on the line charts
    ranked = sorted(series, key=lambda s: -(s.opportunity_score if s.opportunity_score is not None else -1))
    fig, ax = plt.subplots(figsize=(10, 0.55 * len(ranked) + 1.6), dpi=200)
    fig.patch.set_facecolor(TOKENS["page"])
    y = np.arange(len(ranked))[::-1]
    values = [s.opportunity_score or 0 for s in ranked]
    ax.barh(y, values, height=0.22, color=[slot[s.key] for s in ranked], linewidth=0)  # ~24px at 200 dpi: thin marks
    ax.set_yticks(y)
    ax.set_yticklabels([s.label for s in ranked], fontsize=9, color=TOKENS["ink"])
    for yi, s, v in zip(y, ranked, values):
        note = f"{v}" if s.opportunity_score is not None else "n/a"
        ax.text(v + 1.5, yi, f"{note}   {s.direction}, confidence {s.confidence}", va="center", fontsize=8.5, color=TOKENS["ink2"])
    ax.set_xlim(0, 100)
    _style_axes(ax)
    ax.xaxis.grid(True, color=TOKENS["grid"], linewidth=1)
    ax.yaxis.grid(False)
    ax.tick_params(axis="y", colors=TOKENS["ink"])
    ax.set_xlabel("opportunity score (growth x confidence x volume, 0-100)", color=TOKENS["muted"], fontsize=8)
    fig.text(0.06, 0.97, title, fontsize=13, fontweight="bold", color=TOKENS["ink"], ha="left", va="top")
    fig.subplots_adjust(left=0.26, right=0.97, top=0.82, bottom=0.22)
    if out:
        fig.savefig(out, dpi=200, facecolor=fig.get_facecolor())
    return fig


def series_table(series: list[ChartSeries]) -> list[dict[str, Any]]:
    """The table-view twin of the chart (every drawn value's summary)."""
    return [
        {
            "key": s.key, "label": s.label, "color": SERIES_COLORS[i], "direction": s.direction,
            "confidence": s.confidence, "headline_change": s.headline_change,
            "indexed_last": round(s.last_value, 1), "spike_events": len(s.spikes),
            "from": s.dates[0].isoformat(), "to": s.dates[-1].isoformat(),
        }
        for i, s in enumerate(series)
    ]


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render the normalised interest chart (PNG, optional SVG); JSON summary on stdout.")
    parser.add_argument("--fetched", required=True, help="JSON from fetch_pageviews.py")
    parser.add_argument("--analysis", required=True, help="JSON from analyze_trend.py")
    parser.add_argument("--resolved", help="JSON from resolve_topic.py (adds article titles to the legend)")
    parser.add_argument("--title", help="chart title (default: the resolved topic label)")
    parser.add_argument("--subtitle")
    parser.add_argument("--out", required=True, help="PNG path")
    parser.add_argument("--svg", help="also write an SVG here")
    parser.add_argument("--multiples", help="small-multiples chart path (default: multiples.png next to --out)")
    parser.add_argument("--seasonal", help="seasonal-profiles chart path (default: seasonal.png next to --out)")
    parser.add_argument("--ranking", help="opportunity-ranking chart path (default: ranking.png next to --out)")
    parser.add_argument("--no-extras", action="store_true", help="write only the main chart")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = _parse_args(argv)
    try:
        fetched = read_json_file(args.fetched)
        analysis = read_json_file(args.analysis)
        resolved = read_json_file(args.resolved) if args.resolved else None
        series, skipped = load_chart_series(fetched, analysis, resolved)
        title = args.title or (resolved or {}).get("label") or (resolved or {}).get("topic") or "Interest over time"
        fig = render_chart(series, title, args.subtitle, out=args.out, svg=args.svg)
        plt.close(fig)
        extras: dict[str, Optional[str]] = {"multiples": args.multiples, "seasonal": args.seasonal, "ranking": args.ranking}
        if not args.no_extras:  # always regenerate alongside the main chart, so a report never picks up stale files
            for key in extras:
                extras[key] = extras[key] or str(Path(args.out).with_name(f"{key}.png"))
            plt.close(render_small_multiples(series, f"{title}: views per edition", out=extras["multiples"]))
            plt.close(render_seasonal_profiles(series, f"{title}: year over year", out=extras["seasonal"]))
            plt.close(render_ranking(series, f"{title}: where to look first", out=extras["ranking"]))
        else:
            extras = {k: None for k in extras}
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"written": str(Path(args.out)), "svg": args.svg, "extras": extras, "title": title,
                      "series": series_table(series), "skipped": skipped}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
