---
name: wikipedia-trends
description: 'Wikipedia interest trends. Use for prompts like Compare the growth of interest in X on the Polish and Czech Wikipedias, Is interest in X growing on Ukrainian Wikipedia and can it be trusted, Which language editions show rising interest in X, Compare X and Y on Ukrainian Wikipedia - any question about how popular, trending or growing a topic is on Wikipedia, or how interest compares across countries, languages or editions. Also for Give me the report / PDF of the last research, I want it in my directory, save the report here - the skill keeps the last query and copies its PDF into the working directory. Do NOT fetch or read Wikipedia pages for these (page text cannot answer them); this skill runs local scripts on pageview statistics and produces numbers, charts and a PDF report.'
---

# Wikipedia Trends

## When to use this skill

Trigger on requests like:

- "Is interest in intermittent fasting growing on Polish and Czech Wikipedia?"
- "Compare astronomy on Ukrainian Wikipedia over the last two years — can I trust the trend?"
- "Which language editions show rising interest in learning English? Where should we look next?"
- "Compare WW1 and WW2 on Ukrainian Wikipedia", "is pickleball a fad or a trend in Germany?"
- Any mention of Wikipedia pageviews, Wikimedia pageview stats, or "interest over time" for a
  topic in one or more countries/languages.
- "Give me the report on the last research", "I want the PDF in my directory", "save it here" —
  the skill remembers the last query; see *Delivering the report*.

The phrases above are trigger patterns only; the topics in them are not suggestions.

Do not use it for: Google Trends or search volume (different data), article *content*
questions, edit counts or editor activity, or revenue/market-size estimates. If the user wants
market size, say that pageviews cannot give it and offer the trend instead.

## Topics come from the user, never from this file

Every topic, language edition and period in an analysis must come from the user's request, or
from a follow-up the user makes to their own previous query. Any topic named in this document,
in `references/manual_gate.md` or in the tests is a trigger phrase or a regression fixture; it
carries no information about what is popular, expected or worth analysing, and it is never a
default. The skill treats every article on every edition the same way.

When a request is open-ended ("try another example", "pick something interesting", "show me what
this can do", "what else could we look at?"), do not invent a topic and do not run anything.
Ask which topic, which editions and what period they want, and whether to build on the current
query or start fresh. The hypothesis has to be the founder's; a trend on a topic you chose for
them is noise.

If the user explicitly asks you to choose, pick a topic that appears nowhere in this document,
in the session so far or in `state.json`, say that the choice is arbitrary, and offer to switch.

`state.json` and `history` record the user's earlier queries. Reuse them only when the user
refers back to them ("add Slovak to that", "five years instead"); a request with a new topic is a
new `compare`, not an `add-topic` onto whatever happens to be saved.

## The one rule

**All numbers come from the scripts.** Never compute a change, average, share, slope, index or
confidence yourself, not even "roughly". Never estimate pageviews from memory. Run the CLI, read
its JSON, and quote the values (rounding is fine; inventing or extrapolating is not). If a number
the user wants is not in the JSON, say it is not available rather than deriving it.

## Delivering the report

The report is a file the user must find in **their own folder**, never a path inside the skill
directory and never a summary, artifact or HTML rendering offered instead of the file.

- `compare` and every follow-up copy `wikipedia-trends-<topics>-<editions>.pdf` and `.html` into
  the directory the command was run from; the paths are in `outputs.delivered`. Quote
  `outputs.delivered.pdf` in the answer. `outputs.report_pdf` (inside the skill) is never shown.
- Whenever the user asks for the report in any words ("give me the report", "the PDF of the last
  research", "I want to see it in my directory", "save it here", "send me the file"), **run**
  ```
  python scripts/cli.py report --html
  ```
  and quote the `report.pdf` path it prints. Run it even if you believe the file is already there:
  it is instant, idempotent, and it is the only way to be sure the path you give is real and
  current. Never answer such a request from memory, and never offer alternatives (an artifact, a
  written breakdown) *instead* of the file. `--to DIR` puts it elsewhere ("on my desktop"),
  `--name` renames it.
- `report` exit 1 "run `compare` first" means there is no previous query: ask what to analyse (do
  not pick a topic). Exit 2 means a clarification is pending: ask it.

## Paths and setup

This skill is installed under `~/.claude/skills/wikipedia-trends` (a link to the project) and runs from
whatever directory the user is in. **Always call the scripts by their path inside the skill's base
directory** (shown when the skill loads), using the skill's own virtual environment:

```
SKILL=<base directory of this skill>
"$SKILL/.venv/Scripts/python" "$SKILL/scripts/cli.py" compare ...     # Windows
"$SKILL/.venv/bin/python"     "$SKILL/scripts/cli.py" compare ...     # macOS / Linux
```

Outputs, the pageviews cache and `state.json` are written inside the skill directory (`output/`,
`data/`, `state.json`), not in the user's working directory, so follow-ups work from anywhere.

One-time setup if `.venv` is missing (Python 3.11+): create it with `python -m venv "$SKILL/.venv"`,
then install `"$SKILL/requirements.txt"` with that interpreter's pip.

All scripts print UTF-8 JSON and accept `--out <file>`; on Windows PowerShell never chain scripts
with `>` (it writes UTF-16) — use `--out`. The command lines below omit the `$SKILL` prefix for
brevity; always add it.

## Step-by-step recipe

1. **Extract from the request**: the topic phrase(s), the language editions (as Wikipedia codes:
   `pl`, `cs`, `uk`, `de`, `ja`, `zh-yue` …; `nb` is accepted for Norwegian), the period (default
   2 years), and which language the topic phrase is written in.
2. **Run one command.** One topic across editions:
   ```
   python scripts/cli.py compare --topic "<topic>" --langs pl,cs,uk --years 2
   ```
   Several topics within one edition (or several):
   ```
   python scripts/cli.py compare --topics "<topic A>,<topic B>" --langs uk --years 2
   ```
   Add `--seed-lang en` when the phrase is English and the editions are not (and `--seed-lang pl`
   for a Polish phrase, etc.). Add `--start YYYY-MM-DD --end YYYY-MM-DD` for an exact period.
   Add `--qid Q…` to pin a Wikidata item after an ambiguity.
   **Do not pass `--out-dir` or `--out`**: `cli.py` has no `--out`, and it already writes everything
   to `output/<topic>/` inside the skill directory (the paths come back in `outputs`).
3. **Check the exit code and `status`** (see *Error handling*). Only continue on `status: ok`.
4. **Read `results.<key>`** for each edition/topic (also saved as `summary.json`):
   - `direction`, `headline_change`, `headline_basis` — quote all three together. When
     `seasonal` is true the headline is the year-over-year change, not first-vs-last; mention
     `endpoint_change` too if it differs a lot ("first-vs-last says −87 %, but that mixes seasons").
     "First 8 weeks" is the comparison window inside the period, not the period itself.
   - `confidence` **with** `confidence_reasons` — always the reasons, never the label alone.
   - `mean_views_per_day` — under 10 is a tiny audience; say so.
   - `spike_events`, `spike_share`, `largest_spike` — if `spike_share` > 0.10, say the trend
     includes spikes and give the largest date; spikes are usually news or viral posts.
   - `data.merged_redirect_titles` — if a former title carried more views than the current one,
     say the article was renamed and its full history was merged.
   - `no_article` — editions without an article. **Always list them with their reason**; a missing
     series is never a bug to hide. No article ≠ no interest. If the topic resolved through a
     redirect (a caveat says "'X' is a redirect to 'Y'") and editions are missing, Y may be a
     narrower article than the user meant: say so and offer the broader title — e.g. the phrase
     the user typed lands on a narrower sub-article that only some editions have, while the
     broader article on the subject exists in all of them.
   - `comparison.ranking` (2+ series) — rank by `opportunity_score`; compare editions only via
     `indexed_last` / `headline_change`, never absolute views across editions. Within one edition
     (topics mode) absolute `mean_views_per_day` is comparable.
5. **Answer** using the template below, with the report path from `outputs.delivered.pdf` (see
   *Delivering the report*; if `delivered` is missing, run `report --to "<user's directory>"`).
   Say which files exist; do not describe charts you have not opened.
6. **Follow-ups** reuse the saved query in `state.json` (cached pageviews make them fast) and
   print the same summary plus a `follow_up.changed` block:
   - "add Slovak" → `python scripts/cli.py add-language --lang sk`; "drop German" → `remove-language --lang de`
   - "and compare it with <other topic>" → `add-topic --topic "<other topic>"` (switches to topics mode);
     `remove-topic --topic "…"` goes back
   - "over five years instead" → `extend-range --years 5`; exact dates: `extend-range --start 2019-01-01`
   - "show me again" → `show`; "what have we looked at" → `history`
   - "give me the report" / "the PDF" / "in my directory" → `report --html` (always run it; see
     *Delivering the report*). A follow-up that changes the topics or the editions writes a new
     file next to the earlier one (both names differ); one that only changes the period
     overwrites it.
   Always tell the user what changed (the `follow_up.changed` block) and re-state the headline
   for every series, since the ranking and indexed levels are recomputed.
7. **Optional: causes of spikes.** The report's "Likely cause" column is blank by design. You may
   web-search `<topic> <peak date>`; state a cause only if a specific event on or just before that
   date turns up, and label it as your inference from a search, not as pipeline output.

## Talking to the user: plain language only

The user is a product founder, not an engineer. Flags, file names, JSON fields, Wikidata ids and
exit codes are for you; never let them into the answer. Translate:

| Never say | Say instead |
|---|---|
| "re-run with --qid Q1070684" | "I can switch to a different article — say which" |
| "Wikidata item", "QID", "sitelink" | "the article", "the same topic on the Czech edition" |
| "the seed language", "--seed-lang en" | "I looked the phrase up in English first" |
| "status: ambiguous / exit code 2" | "That phrase can mean several things — which one do you mean: A, B or C?" |
| "no_article: pl" | "Polish Wikipedia has no article on this (which is not the same as no interest)" |
| "headline_change −0.55, basis year-over-year" | "down about 55 % compared with the previous 12 months" |
| "confidence High, R² 0.62, p 1e-23" | "a reliable signal: a steady decline across the whole period, on a decent audience" |
| "spike_share 0.14" | "about 14 % of all views came in a few short bursts, probably news" |
| "indexed_last 22" | "at roughly a fifth of where it started" |
| "run add-language --lang sk" | "Want me to add Slovak?" |
| "summary.json", "output/<topic>/report.pdf" | "the report" (then give the file path once, at the end) |

Rule of thumb: describe articles by their title and edition ("the Czech article *<title>*"),
describe choices as questions, describe numbers with their meaning. Keep the technical terms in the
JSON you read, not in the text you write. When offering an alternative, name the alternative
articles, e.g. "I used *<article A>*, the Czech article on <the topic as the user phrased it>. If
you meant <the related concept>, there is a separate article, *<article B>* — want that one
instead?"

## Answer template

```
<Topic> on <editions>, <start> to <end> (Wikipedia pageviews, bots excluded)

• <edition/article>: <direction>, <headline_change> (<basis>). Confidence <label>: <reason 1>; <reason 2>.
  <mean views/day>; <n> spike events, largest <date> (<views> views vs level <baseline>).
• … one bullet per edition/topic …
• No article in: <langs> — no article does not mean no interest.

Where to look first: <ranking by opportunity_score with the one-line why>.

Caveats: pageviews measure curiosity, not willingness to pay; spikes are usually news;
editions are compared on indexed levels, not audience size; <any data caveats from the JSON>.
Report: <outputs.delivered.pdf> (in your folder; a web page version is next to it)
```

Keep the numbers exactly as printed (percentages may be rounded to whole numbers).

## Error handling

| Signal | Meaning | What to do |
|---|---|---|
| exit `2`, `status: ambiguous` | The phrase is a disambiguation page, or means different things in two editions (e.g. "Running" is a song on pl.wikipedia). The query is saved as `pending`. | Ask in plain words: "That can mean several things — did you mean *A* (the planet), *B* (the element) or *C*?", using the first few `candidates` (best first) as article titles with a short gloss — or, when one is obviously right, pick it and say so. Then finish with `python scripts/cli.py choose --title "<candidate title>"` (or `choose --qid Q…`); the user never sees the command. In topics mode only the ambiguous topic is replaced. Never analyse a disambiguation page. `show` reprints the pending question. |
| exit `1`, `status: not_found` | No requested edition has an article for the topic. | Report `message` verbatim. Offer to try the topic as written in the user's language, or a nearby concrete topic from `candidates`. Do not substitute a related article silently. |
| exit `1`, `status: no_data` | Articles exist but the Pageviews API had nothing for the period. | Say so; offer a longer or different period. |
| exit `1`, `status: error` | Bad input (unknown language code, bad date) or network failure after retries. | Quote `error`; fix the input or retry later. Unknown codes fail fast ("Cannot resolve host xx.wikipedia.org"). |
| `no_article` has entries | Some editions lack the article, Wikidata links it elsewhere, or the phrase redirected to a narrower article (e.g. "<X> diet" → "<X> nutrition", missing in one edition). | List every missing edition with its reason and "no article ≠ no interest". If a redirect caveat is present, offer the broader/other title (`remove-topic` + `add-topic`, or a new `compare`). Never present a 4-of-6 result as if it were complete. |
| `data.status: partial` | The article is younger than the period; the series starts at `data.from`. | Say the trend covers `data.from` to `data.through` only. |
| `direction: insufficient` | Fewer than 112 daily points. | Make no trend claim; the article simply lacks history. |
| `confidence: Low` | Tiny audience, spike-dominated, no consistent slope, or short history (the reasons say which). | State the reasons; frame the result as a weak signal, not a finding. |
| `mean_views_per_day` < 10 | A handful of readers moves the curve; indexed lines jump in steps. | Say the audience is tiny; point to the small-multiples chart rather than the indexed one. |
| `seasonal: true` | Yearly pattern (school year, holidays, anniversaries). | Quote the year-over-year headline; mention the pattern. |
| `spike_share` > 0.30 | Most views come from short spikes. | Say interest is event-driven; give the spike dates for checking. |
| `merged_redirect_titles` non-empty | Views under former titles/spellings were added. | Mention it if a former title dominated (page rename). |
| `outputs.chart_skipped` / `extra_charts_missing` | A series could not be indexed, or a chart file is missing/stale. | Report which; re-run `compare` if charts are stale. |
| `UnicodeDecodeError … 0xff` | A JSON file was written with PowerShell `>` (UTF-16). | Re-run using `--out`; the scripts also read UTF-16/BOM files. |

## Limitations to state every time

- Wikipedia pageviews measure curiosity, not willingness to pay or product-market fit.
- No article in a language edition does not mean no interest.
- A spike is usually a news event or a viral post, not organic growth.
- Cross-edition comparison is valid only on indexed series; audiences differ by orders of magnitude.
- Data starts July 2015 and bot filtering is reliable only from mid-2016; the last two days are
  never included.

## Running the modules separately (rarely needed)

`resolve_topic.py` → `fetch_pageviews.py --resolved` → `analyze_trend.py --fetched` →
`make_chart.py` → `make_report.py`, each printing JSON and accepting `--out`. Use this only to
debug one step. Method details: `references/methodology.md`. API behaviour and data findings:
`references/api_notes.md`. Offline tests: `pytest tests/ -q`.
