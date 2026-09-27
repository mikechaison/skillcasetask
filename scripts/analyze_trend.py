"""Trend analysis for a pageviews series: smoothing, regression, spikes, seasonality, confidence.

Everything numeric is computed here; the agent only reads the JSON.  The
method is documented in ``references/methodology.md``.  In short:

1. Smooth with a centred rolling *median* (28 days) — spikes barely move it.
2. Relative change = median of the last 8 smoothed weeks vs the first 8.
3. Regression on log(weekly totals) vs time (weekly points are far less
   autocorrelated than smoothed daily values), giving slope/year, R², p.
4. Spikes = days whose log-residual from the smoothed level is an outlier
   (robust z via MAD) *and* at least twice the level; grouped into events.
5. With ≥ 2 years: year-over-year change, same-window-last-year change and
   the correlation of the two yearly weekly profiles (seasonality strength).
6. Confidence = High/Medium/Low from a penalty rubric with named reasons.

Usage (CLI):

    python scripts/analyze_trend.py --csv data/pageviews/<hash>.csv
    python scripts/analyze_trend.py --fetched fetched.json     # output of fetch_pageviews.py

Usage (library):

    >>> import pandas as pd, numpy as np
    >>> from scripts.analyze_trend import analyze_series
    >>> days = pd.date_range("2024-01-01", periods=730, freq="D").date
    >>> df = pd.DataFrame({"date": days, "views": np.linspace(100, 300, 730).round()})
    >>> a = analyze_series(df)
    >>> a.direction, a.confidence.label
    ('growing', 'High')
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # allow `python scripts/analyze_trend.py`
from scripts.fetch_pageviews import read_json_file, write_json_output  # noqa: E402

CONFIDENCE_WEIGHT = {"High": 1.0, "Medium": 0.6, "Low": 0.3}


@dataclass(frozen=True)
class TrendParams:
    """Tunable thresholds; ``for_granularity`` gives the documented defaults."""

    granularity: str = "daily"
    points_per_year: int = 365
    smoothing_window: int = 28  # centred rolling median
    window: int = 56  # first/last comparison window (8 weeks)
    min_points: int = 112  # below this: "insufficient"
    spike_z: float = 4.0  # robust z threshold on log residuals
    spike_ratio: float = 2.0  # spike day must be >= ratio * smoothed level
    flat_threshold: float = 0.10  # |relative change| below this = flat
    low_volume: float = 10.0  # mean views/day
    modest_volume: float = 100.0

    @staticmethod
    def for_granularity(granularity: str) -> "TrendParams":
        if granularity == "daily":
            return TrendParams()
        if granularity == "monthly":
            return TrendParams(
                granularity="monthly", points_per_year=12, smoothing_window=3, window=3, min_points=8,
                spike_z=4.0, spike_ratio=1.8,  # volume thresholds are per day for both granularities
            )
        raise ValueError("granularity must be 'daily' or 'monthly'")


@dataclass
class Spike:
    start: date
    end: date
    peak_date: date
    peak_views: int
    baseline: float  # smoothed level at the peak
    excess_views: int  # sum(views - smoothed) over the event
    points: int


@dataclass
class Regression:
    slope_per_year: Optional[float]  # relative growth implied per year, e.g. 0.25 = +25 %/yr
    r2: Optional[float]
    p_value: Optional[float]
    n_points: int
    unit: str  # "weeks" or "months"


@dataclass
class Seasonality:
    yoy_change: Optional[float]  # last 12 months total vs previous 12 months
    same_window_change: Optional[float]  # last window vs the same window one year earlier
    profile_correlation: Optional[float]  # correlation of the two yearly profiles (0 = none, 1 = identical shape)


@dataclass
class Confidence:
    label: str  # High | Medium | Low
    score: int  # 0-100 (100 - 15 * penalty points)
    reasons: list[str]


@dataclass
class TrendAnalysis:
    granularity: str
    points: int
    start: Optional[date]
    end: Optional[date]
    mean_views_per_day: float
    total_views: int
    direction: str  # growing | declining | flat | insufficient
    first_level: Optional[float]
    last_level: Optional[float]
    relative_change: Optional[float]  # smoothed last window vs first window
    indexed_last: Optional[float]  # last level with the first level = 100
    headline_change: Optional[float]  # the change `direction` is based on
    headline_basis: str  # which comparison produced it
    seasonal: bool  # strong yearly pattern detected
    regression: Regression
    spikes: list[Spike]
    spike_share: float  # excess views in spikes / total views
    recent_spike_share: float  # spike excess inside the last window / views in the last window
    seasonality: Optional[Seasonality]
    confidence: Confidence
    opportunity_score: Optional[int]
    caveats: list[str] = field(default_factory=list)
    smoothed: list[float] = field(default_factory=list, repr=False)  # same length as the series

    def to_dict(self, include_smoothed: bool = False) -> dict[str, Any]:
        out = asdict(self)
        if not include_smoothed:
            out.pop("smoothed")
        return json.loads(json.dumps(out, default=_json_default))


def _json_default(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if math.isnan(value) else float(value)
    raise TypeError(f"not serialisable: {type(value)}")


# --- building blocks -----------------------------------------------------------


def smooth(views: np.ndarray, window: int) -> np.ndarray:
    """Centred rolling median; edges use whatever is available.

    >>> smooth(np.array([1, 1, 1, 100, 1, 1, 1], dtype=float), 3).tolist()
    [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    """
    return pd.Series(views, dtype=float).rolling(window, center=True, min_periods=1).median().to_numpy()


def relative_change(smoothed: np.ndarray, window: int) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """``(first_level, last_level, change)`` from the medians of the first/last ``window`` points.

    >>> relative_change(np.linspace(100, 200, 200), 20)[2] > 0.8
    True
    """
    if len(smoothed) < 2 * window:
        return None, None, None
    first = float(np.median(smoothed[:window]))
    last = float(np.median(smoothed[-window:]))
    if first <= 0:
        return first, last, None
    return first, last, (last - first) / first


def aggregate(dates: pd.Series, views: np.ndarray, granularity: str) -> pd.Series:
    """Weekly totals (complete weeks only) for daily data; the series itself for monthly.

    >>> d = pd.Series(pd.date_range("2024-01-01", periods=21, freq="D"))
    >>> aggregate(d, np.ones(21), "daily").tolist()
    [7.0, 7.0, 7.0]
    """
    if granularity == "monthly":
        return pd.Series(np.asarray(views, dtype=float))
    stamps = pd.to_datetime(pd.Series(dates)).reset_index(drop=True)
    offsets = (stamps - stamps.iloc[0]).dt.days.to_numpy()
    frame = pd.DataFrame({"week": offsets // 7, "views": np.asarray(views, dtype=float)})
    weekly = frame.groupby("week")["views"].agg(["sum", "count"])
    return weekly.loc[weekly["count"] == 7, "sum"].astype(float)


def regression(points: pd.Series, points_per_year: int, unit: str) -> Regression:
    """OLS of log1p(points) on time; slope re-expressed as relative growth per year.

    >>> r = regression(pd.Series(1000 * np.exp(np.linspace(0, 1, 52))), 52, "weeks")
    >>> round(r.slope_per_year, 2), round(r.r2, 2)   # e**(52/51) - 1
    (1.77, 1.0)
    """
    n = int(len(points))
    if n < 4:
        return Regression(None, None, None, n, unit)
    x = np.arange(n, dtype=float)
    y = np.log1p(points.to_numpy(dtype=float))
    if np.allclose(y, y[0]):
        return Regression(0.0, 0.0, 1.0, n, unit)
    fit = stats.linregress(x, y)
    return Regression(
        slope_per_year=float(math.exp(fit.slope * points_per_year) - 1),
        r2=float(fit.rvalue**2),
        p_value=float(fit.pvalue),
        n_points=n,
        unit=unit,
    )


def detect_spikes(
    dates: list[date], views: np.ndarray, smoothed: np.ndarray, z_threshold: float, ratio: float
) -> list[Spike]:
    """Outlier days above the smoothed level, grouped into consecutive events.

    A day counts when its log residual is a robust outlier (MAD-based z above
    ``z_threshold``), it is at least ``ratio`` times the smoothed level, and
    the excess is material (>= max(10, mean views)).

    >>> v = np.full(100, 50.0); v[40:43] = [400, 900, 300]
    >>> s = detect_spikes(list(pd.date_range("2025-01-01", periods=100).date), v, smooth(v, 28), 4.0, 2.0)
    >>> len(s), s[0].points, s[0].peak_views
    (1, 3, 900)
    """
    if len(views) == 0:
        return []
    resid = np.log1p(views) - np.log1p(smoothed)
    mad = float(np.median(np.abs(resid - np.median(resid)))) * 1.4826
    z = resid / max(mad, 1e-6)
    min_excess = max(10.0, float(np.mean(views)))
    mask = (z > z_threshold) & (views >= ratio * np.maximum(smoothed, 1.0)) & (views - smoothed >= min_excess)
    spikes: list[Spike] = []
    i = 0
    while i < len(mask):
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(mask) and mask[j + 1]:
            j += 1
        seg = slice(i, j + 1)
        peak = i + int(np.argmax(views[seg]))
        spikes.append(
            Spike(
                start=dates[i], end=dates[j], peak_date=dates[peak], peak_views=int(views[peak]),
                baseline=round(float(smoothed[peak]), 2),
                excess_views=int(round(float(np.sum(views[seg] - smoothed[seg])))),
                points=j - i + 1,
            )
        )
        i = j + 1
    return spikes


def _detrend(profile: np.ndarray) -> np.ndarray:
    x = np.arange(len(profile), dtype=float)
    slope, intercept = np.polyfit(x, profile, 1)
    return profile - (slope * x + intercept)


def seasonality(views: np.ndarray, params: TrendParams) -> Optional[Seasonality]:
    """Year-over-year comparisons; needs at least two full years.

    The profile correlation is computed on *detrended* yearly profiles so
    that a plain linear trend (both years rising) does not count as a season.

    >>> two_years = np.tile(np.sin(np.linspace(0, 2 * np.pi, 365)) * 50 + 100, 2)
    >>> s = seasonality(two_years, TrendParams())
    >>> round(s.yoy_change, 3), round(s.profile_correlation, 2)
    (0.0, 1.0)
    >>> abs(seasonality(np.linspace(100, 300, 730), TrendParams()).profile_correlation) < 0.3
    True
    """
    period = params.points_per_year
    if len(views) < 2 * period:
        return None
    last, prev = views[-period:], views[-2 * period : -period]
    yoy = (last.sum() - prev.sum()) / prev.sum() if prev.sum() > 0 else None
    w = params.window
    recent, year_ago = views[-w:], views[-period - w : -period]
    same_window = (recent.sum() - year_ago.sum()) / year_ago.sum() if year_ago.sum() > 0 else None
    if params.granularity == "daily":
        bins = period // 7
        a = last[: bins * 7].reshape(bins, 7).sum(axis=1)
        b = prev[: bins * 7].reshape(bins, 7).sum(axis=1)
    else:
        a, b = last, prev
    a, b = _detrend(a.astype(float)), _detrend(b.astype(float))
    corr = float(np.corrcoef(a, b)[0, 1]) if a.std() > 0 and b.std() > 0 else None
    return Seasonality(
        yoy_change=None if yoy is None else float(yoy),
        same_window_change=None if same_window is None else float(same_window),
        profile_correlation=None if corr is None or math.isnan(corr) else corr,
    )


# --- confidence ------------------------------------------------------------------


def is_seasonal(seas: Optional[Seasonality], threshold: float = 0.5) -> bool:
    """True when the detrended yearly profiles correlate above ``threshold``."""
    return seas is not None and seas.profile_correlation is not None and seas.profile_correlation > threshold


def score_confidence(
    direction: str,
    headline: Optional[float],
    reg: Regression,
    spike_share: float,
    recent_spike_share: float,
    mean_per_day: float,
    seas: Optional[Seasonality],
    params: TrendParams,
    status: Optional[str],
) -> Confidence:
    """Penalty rubric -> label with specific reasons (see methodology.md)."""
    penalty = 0
    reasons: list[str] = []
    seasonal = is_seasonal(seas)
    claims_change = direction in ("growing", "declining")

    # 1. data volume
    if mean_per_day < params.low_volume:
        penalty += 2
        reasons.append(f"very low volume ({mean_per_day:.1f} views/day): a handful of readers moves the curve")
    elif mean_per_day < params.modest_volume:
        penalty += 1
        reasons.append(f"modest volume ({mean_per_day:.0f} views/day)")

    # 2. trend consistency (only meaningful when a change is claimed)
    if claims_change and seasonal and seas is not None:
        # a seasonal series defeats a straight regression line; use year-over-year agreement instead
        same = seas.same_window_change
        prefix = f"seasonal pattern (profile correlation {seas.profile_correlation:.2f}); annual change {headline:+.0%}"
        if same is None:
            reasons.append(prefix + "; the same window a year earlier had no views")
        elif abs(same) < params.flat_threshold:
            reasons.append(prefix + f", but the last window is about level with a year earlier ({same:+.0%}): the move may be flattening")
        elif (same > 0) != ((headline or 0) > 0):
            penalty += 1
            reasons.append(prefix + f" but the last window vs a year earlier is {same:+.0%}: the trend may be turning")
        else:
            reasons.append(prefix + f" and the last window vs a year earlier ({same:+.0%}) agree")
    elif claims_change:
        if reg.r2 is None:
            penalty += 2
            reasons.append("too few aggregated points for a regression")
        elif reg.r2 >= 0.5 and (reg.p_value or 1) < 0.01:
            reasons.append(f"consistent slope (R² {reg.r2:.2f}, p {reg.p_value:.3g})")
        elif reg.r2 >= 0.25 and (reg.p_value or 1) < 0.05:
            penalty += 1
            reasons.append(f"noisy slope (R² {reg.r2:.2f})")
        else:
            penalty += 2
            reasons.append(f"no consistent slope (R² {reg.r2:.2f}, p {reg.p_value:.2g}): the change is not a steady trend")
        if reg.slope_per_year is not None and headline is not None and (reg.p_value or 1) < 0.05:
            if (reg.slope_per_year > 0) != (headline > 0):
                penalty += 1
                reasons.append("regression slope and first-vs-last change point in opposite directions")

    # 3. spikes
    if spike_share > 0.30:
        penalty += 2
        reasons.append(f"{spike_share:.0%} of all views come from short spikes (news/viral), not steady interest")
    elif spike_share > 0.10:
        penalty += 1
        reasons.append(f"{spike_share:.0%} of views come from short spikes")
    if recent_spike_share > 0.25:
        penalty += 1
        reasons.append(f"a spike inside the last window supplies {recent_spike_share:.0%} of its views")

    # 4. history length
    if seas is None:
        penalty += 1
        reasons.append("less than two years of data: seasonality cannot be checked")

    # 5. completeness
    if status == "partial":
        penalty += 1
        reasons.append("series is shorter than requested (article younger than the period)")

    score = max(0, 100 - 15 * penalty)
    label = "High" if score >= 80 else "Medium" if score >= 50 else "Low"
    if not reasons:
        reasons.append("large volume, steady slope, no spikes, seasonality checked")
    return Confidence(label=label, score=score, reasons=reasons)


def opportunity_score(change: Optional[float], mean_per_day: float, label: str) -> Optional[int]:
    """Heuristic 0-100 ranking of "audiences to investigate next" (see methodology.md).

    >>> opportunity_score(0.5, 1000, "High") > opportunity_score(0.5, 1000, "Low")
    True
    """
    if change is None:
        return None
    growth = (min(max(change, -1.0), 3.0) + 1.0) / 4.0  # -100 %..+300 % -> 0..1
    volume = min(math.log10(mean_per_day + 1) / 4.0, 1.0)  # 10 000/day -> 1
    return int(round(100 * CONFIDENCE_WEIGHT[label] * (0.7 * growth + 0.3 * volume)))


# --- main entry point ----------------------------------------------------------


def analyze_series(
    frame: pd.DataFrame,
    granularity: str = "daily",
    status: Optional[str] = None,
    params: Optional[TrendParams] = None,
) -> TrendAnalysis:
    """Analyse a ``(date, views)`` series produced by ``fetch_pageviews``.

    ``status`` is the fetch status (``"partial"`` adds a completeness penalty).
    """
    p = params or TrendParams.for_granularity(granularity)
    if granularity != p.granularity:
        raise ValueError("params granularity does not match")
    frame = frame.sort_values("date").reset_index(drop=True)
    dates = [pd.Timestamp(d).date() for d in frame["date"]]
    views = frame["views"].to_numpy(dtype=float)
    n = len(views)
    span_days = max((dates[-1] - dates[0]).days + 1, 1) if n else 0
    total = int(views.sum()) if n else 0
    mean_per_day = total / span_days if span_days else 0.0
    if granularity == "monthly" and n:
        mean_per_day = float(views.mean() / 30.44)

    if n < p.min_points:
        caveat = f"Only {n} {granularity} points; at least {p.min_points} are needed for a trend."
        return TrendAnalysis(
            granularity=granularity, points=n, start=dates[0] if n else None, end=dates[-1] if n else None,
            mean_views_per_day=round(mean_per_day, 2), total_views=total, direction="insufficient",
            first_level=None, last_level=None, relative_change=None, indexed_last=None,
            headline_change=None, headline_basis="none", seasonal=False,
            regression=Regression(None, None, None, 0, "weeks" if granularity == "daily" else "months"),
            spikes=[], spike_share=0.0, recent_spike_share=0.0, seasonality=None,
            confidence=Confidence("Low", 0, [caveat]), opportunity_score=None, caveats=[caveat],
            smoothed=[],
        )

    smoothed = smooth(views, p.smoothing_window)
    first, last, change = relative_change(smoothed, p.window)
    agg = aggregate(pd.Series(dates), views, granularity)
    reg = regression(agg, 52 if granularity == "daily" else 12, "weeks" if granularity == "daily" else "months")
    spikes = detect_spikes(dates, views, smoothed, p.spike_z, p.spike_ratio)
    excess = sum(s.excess_views for s in spikes)
    spike_share = excess / total if total else 0.0
    window_start = dates[-p.window]
    recent_excess = sum(s.excess_views for s in spikes if s.end >= window_start)
    recent_total = float(views[-p.window :].sum())
    recent_spike_share = recent_excess / recent_total if recent_total else 0.0
    seas = seasonality(views, p)

    # Headline: with a strong yearly pattern the first/last windows sit in different seasons,
    # so the year-over-year total is the fair comparison; otherwise the smoothed endpoints.
    seasonal = is_seasonal(seas)
    if seasonal and seas is not None and seas.yoy_change is not None:
        headline, basis = seas.yoy_change, "last 12 months vs previous 12 months (seasonal pattern detected)"
    else:
        headline, basis = change, f"smoothed level, last {p.window} points vs first {p.window}"

    if headline is None:
        direction = "flat" if (last or 0) == 0 else "growing"  # first level was zero
    elif abs(headline) < p.flat_threshold:
        direction = "flat"
    else:
        direction = "growing" if headline > 0 else "declining"

    conf = score_confidence(direction, headline, reg, spike_share, recent_spike_share, mean_per_day, seas, p, status)

    caveats = [
        "Wikipedia pageviews measure curiosity, not willingness to pay or product-market fit.",
    ]
    if seasonal and change is not None and headline is not None and abs(change - headline) > p.flat_threshold:
        caveats.append(
            f"Strong yearly pattern: comparing the first and last window gives {change:+.0%}, but that mixes seasons; "
            f"the year-over-year figure ({headline:+.0%}) is the one to quote."
        )
    if spikes:
        biggest = max(spikes, key=lambda s: s.excess_views)
        caveats.append(
            f"Largest spike: {biggest.peak_views} views on {biggest.peak_date:%Y-%m-%d} against a level of "
            f"{biggest.baseline:.0f} ({biggest.points} {granularity} point(s)); likely a news event or viral post."
        )
    if change is None and first is not None:
        caveats.append("The first window has a zero level, so a relative change cannot be computed.")

    return TrendAnalysis(
        granularity=granularity, points=n, start=dates[0], end=dates[-1],
        mean_views_per_day=round(mean_per_day, 2), total_views=total, direction=direction,
        first_level=None if first is None else round(first, 2),
        last_level=None if last is None else round(last, 2),
        relative_change=None if change is None else round(change, 4),
        indexed_last=None if change is None else round(100 * (1 + change), 1),
        headline_change=None if headline is None else round(headline, 4), headline_basis=basis, seasonal=seasonal,
        regression=reg, spikes=spikes, spike_share=round(spike_share, 4), recent_spike_share=round(recent_spike_share, 4),
        seasonality=seas, confidence=conf,
        opportunity_score=opportunity_score(headline, mean_per_day, conf.label),
        caveats=caveats, smoothed=[round(float(v), 2) for v in smoothed],
    )


def compare(analyses: dict[str, TrendAnalysis]) -> dict[str, Any]:
    """Cross-series comparison on normalised (indexed) levels, ranked by opportunity.

    Editions differ hugely in audience size, so only ``indexed_last`` (first
    level = 100) and ``relative_change`` are comparable; absolute views are
    reported for context only.
    """
    rows = []
    for key, a in analyses.items():
        rows.append({
            "key": key, "direction": a.direction, "headline_change": a.headline_change, "headline_basis": a.headline_basis,
            "relative_change": a.relative_change, "indexed_last": a.indexed_last, "seasonal": a.seasonal,
            "mean_views_per_day": a.mean_views_per_day, "confidence": a.confidence.label,
            "opportunity_score": a.opportunity_score, "spike_share": a.spike_share,
        })
    ranked = sorted(rows, key=lambda r: (-(r["opportunity_score"] or -1), r["key"]))
    return {
        "ranking": ranked,
        "note": "Ranked by opportunity score (growth x confidence x volume). Levels are indexed to 100 at the start "
                "of each series; absolute audiences are not comparable across editions.",
    }


# --- CLI ----------------------------------------------------------------------


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyse a pageviews series: trend, spikes, seasonality, confidence (JSON).")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", help="a (date,views) CSV written by fetch_pageviews.py")
    source.add_argument("--fetched", help="JSON output of fetch_pageviews.py; analyses every ok/partial result")
    parser.add_argument("--granularity", choices=["daily", "monthly"], default="daily", help="for --csv (default daily)")
    parser.add_argument("--label", default="series", help="name for the --csv series")
    parser.add_argument("--smoothed", action="store_true", help="include the smoothed values in the output")
    parser.add_argument("--out", help="write the JSON to this file (UTF-8) instead of stdout")
    return parser.parse_args(argv)


def _load_csv(path: str) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if not {"date", "views"} <= set(frame.columns):
        raise ValueError(f"{path}: expected columns date,views")
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    return frame


def _force_utf8_stdout() -> None:
    """Emit UTF-8 regardless of the console code page (Windows defaults to cp1251/cp1252)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point."""
    _force_utf8_stdout()
    args = _parse_args(argv)
    try:
        analyses: dict[str, TrendAnalysis] = {}
        skipped: dict[str, str] = {}
        if args.csv:
            analyses[args.label] = analyze_series(_load_csv(args.csv), args.granularity)
        else:
            fetched = read_json_file(args.fetched)
            granularity = fetched.get("period", {}).get("granularity", "daily")
            for key, result in fetched.get("results", {}).items():
                if result.get("status") not in ("ok", "partial") or not result.get("csv_path"):
                    skipped[key] = result.get("status", "unknown")
                    continue
                analyses[key] = analyze_series(_load_csv(result["csv_path"]), granularity, status=result["status"])
    except (ValueError, OSError, KeyError) as exc:
        json.dump({"error": str(exc)}, sys.stdout, ensure_ascii=False)
        print()
        return 1
    out = {
        "analyses": {k: a.to_dict(include_smoothed=args.smoothed) for k, a in analyses.items()},
        "comparison": compare(analyses) if len(analyses) > 1 else None,
        "skipped": skipped,
        "method": "references/methodology.md",
    }
    write_json_output(out, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
