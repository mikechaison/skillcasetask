# Methodology: trend, spikes, seasonality, confidence, opportunity

Everything below is implemented in `scripts/analyze_trend.py` and covered by
`tests/test_analyze_trend.py` (synthetic series with known properties plus the recorded real
series). The agent never computes any of it; it reads the JSON and repeats the numbers verbatim.

Input: a complete `(date, views)` series from `fetch_pageviews.py` (daily by default; zero-filled
inside and for leading gaps of up to 14 days, never padded before the article existed). All thresholds live in `TrendParams`.

## 1. Smoothing

`smoothed = centred rolling median, window 28 days` (3 months for monthly data).

A median barely moves for a 1–3 day spike, so a news event does not lift the "level"; a rolling
mean would. Edges use whatever points are available (`min_periods=1`).

## 2. Relative change (endpoint comparison)

```
first_level = median(smoothed[first 56 days])      # 8 weeks
last_level  = median(smoothed[last  56 days])
relative_change = (last_level - first_level) / first_level
indexed_last    = 100 * (1 + relative_change)      # first window = 100
```

Needs at least 112 points (2 × 56); shorter series are `direction: insufficient`.
If `first_level` is 0 the change is undefined (reported as `null`, direction from the last level).

`indexed_last` is the only number that may be compared across language editions: audiences differ
by orders of magnitude, so each series is indexed to its own start.

## 3. Regression

Daily values are aggregated into **complete weekly totals** (monthly data is used as is), then

```
log1p(weekly_total) ~ a + b · week
slope_per_year = exp(b · 52) - 1        # relative growth per year implied by the fit
r2, p_value    = from the OLS fit
```

Weekly totals instead of smoothed daily values because a smoothed series is heavily autocorrelated
and would make every p-value look significant. The log makes the slope a *relative* rate, so a
series growing 100 → 300 and one growing 1 000 → 3 000 get the same slope. Even weekly totals
retain some autocorrelation, so the p-value is a screening statistic, not a strict test.

## 4. Spike detection

```
resid = log1p(views) - log1p(smoothed)
z     = resid / (1.4826 · MAD(resid))             # robust z-score
spike day  ⇔  z > 4  and  views ≥ 2 · smoothed  and  views − smoothed ≥ max(10, mean views)
```

Consecutive spike days form one **event** with `start, end, peak_date, peak_views, baseline`
(smoothed level at the peak) and `excess_views = Σ(views − smoothed)`.

```
spike_share        = Σ excess over all events / total views
recent_spike_share = excess inside the last 56 days / views in the last 56 days
```

The absolute-excess rule stops 3-views-vs-1-view days on tiny articles from counting as spikes.

## 5. Seasonality (needs ≥ 2 full years)

```
yoy_change          = Σ views(last 365 d) / Σ views(previous 365 d) − 1
same_window_change  = Σ views(last 56 d) / Σ views(same 56 d one year earlier) − 1
profile_correlation = corr(detrended weekly profile of year 2, detrended weekly profile of year 1)
seasonal            = profile_correlation > 0.5
```

Each yearly profile has its own linear trend removed before correlating, so a plain rising trend
(both years increasing) does not register as a season; a real yearly shape (school year, summer,
New-Year resolutions) does. Verified on the recorded uk.wikipedia "Астрономія" series
(correlation 0.93: school-year driven).

## 6. Headline change and direction

```
if seasonal and yoy_change is defined:  headline = yoy_change      (basis: last 12 months vs previous 12)
else:                                   headline = relative_change (basis: smoothed endpoints)

direction = flat       if |headline| < 0.10
          = growing    if headline > 0
          = declining  if headline < 0
```

Why: with a strong yearly pattern, "first 8 weeks vs last 8 weeks" compares two different seasons
(the uk astronomy series read −87 % on endpoints while the year-over-year figure was −60 %; a
synthetic pure sine wave read −31 % while being flat). Both numbers are always reported; the
caveat says which one to quote.

## 7. Confidence label

Start from 0 penalty points and add:

| Factor | Rule | Points |
|---|---|---|
| Volume | mean < 10 views/day | +2 |
| | mean < 100 views/day | +1 |
| Trend consistency (only if a change is claimed) — non-seasonal | R² ≥ 0.5 and p < 0.01 | 0 |
| | R² ≥ 0.25 and p < 0.05 | +1 |
| | otherwise ("no consistent slope") | +2 |
| | regression slope and headline disagree in sign (p < 0.05) | +1 |
| Trend consistency — seasonal series | last window vs a year earlier disagrees in sign with the annual change (and moves ≥ 10 %) | +1 |
| Spikes | spike_share > 30 % | +2 |
| | spike_share > 10 % | +1 |
| | recent_spike_share > 25 % | +1 |
| History | less than 2 years (seasonality unchecked) | +1 |
| Completeness | fetch status `partial` (article younger than the period) | +1 |

```
score = max(0, 100 − 15 · points)
label = High (score ≥ 80) | Medium (score ≥ 50) | Low (score < 50)
```

Every applied rule contributes a sentence to `confidence.reasons`, so the label is never a bare
number. A `flat` verdict can be High confidence (large volume, no spikes, checked seasonality).

Calibration on synthetic series (`tests/test_analyze_trend.py`):

| Series | Direction | Label |
|---|---|---|
| linear growth 100 → 300 over 2 years, small noise | growing | High |
| flat 200/day with noise | flat | High |
| flat + one 3-day spike (12 % of views) | flat | High (spike listed, share reported) |
| flat + spike inside the last 8 weeks | flat | Medium (recent spike share flagged) |
| pure yearly sine, no trend | flat (seasonal) | High |
| growth 2 → 6 views/day | growing | Medium (very low volume) |
| 100 days of growth | insufficient | Low |
| 1 year of growth | growing | High (seasonality unchecked: −1) |

## 8. Opportunity score (ranking heuristic, 0–100)

```
growth  = (clamp(headline, −1, +3) + 1) / 4          # −100 % … +300 %  →  0 … 1
volume  = min(log10(mean views/day + 1) / 4, 1)      # 10 000/day → 1
weight  = High 1.0 | Medium 0.6 | Low 0.3
opportunity = round(100 · weight · (0.7 · growth + 0.3 · volume))
```

It ranks "which audiences to investigate next" by combining growth, how much the growth can be
trusted, and whether the audience is big enough to matter. It is a heuristic for ordering a
shortlist, not a forecast; the comparison output says so.

## 9. Limitations that the output always carries

- Pageviews measure curiosity, not willingness to pay or product-market fit.
- No article ≠ no interest; a `no_data` / `partial` series is a data gap, not a verdict.
- A spike is usually a news event or a viral post; spike events are listed with dates so they can
  be checked.
- Cross-edition comparisons use indexed levels only.
- Redirect pages accumulate their own views (about 2 % of the target in the cases measured); a
  renamed article's earlier history sits under the old title.
