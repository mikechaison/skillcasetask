# wikipedia-trends

An [Agent Skill](https://docs.claude.com/en/docs/agents-and-tools/agent-skills) for Claude Code (and
other tool-calling agents) that lets B2C-product founders answer questions like:

- "Compare the growth of interest in intermittent fasting on the Polish and Czech Wikipedias over
  the last two years."
- "Is interest in astronomy growing on Ukrainian Wikipedia, and how much can that growth be trusted?"
- "Compare interest in learning English across a set of language editions and recommend which
  audiences to investigate next."

It answers them from **Wikimedia pageview statistics** (bots excluded), not from article text:
it resolves the topic to the right article in every edition, fetches daily pageviews, scores the
trend and how much it can be trusted, draws charts and assembles a shareable PDF report.

**One rule drives the design: all math and network calls live in code, never in the model.**
`SKILL.md` only tells the agent which command to run and how to read its JSON. The numbers are
therefore reproducible, there are no hallucinated statistics, and the skill runs well on cheap,
fast models (Claude Haiku 4.5 and similar).

## What you get

For every topic × edition pair the CLI prints (and saves to `summary.json`):

| Field | Meaning |
|---|---|
| `direction`, `headline_change`, `headline_basis` | growing / declining / flat / insufficient, the relative change, and whether it is first-vs-last or year-over-year (for seasonal series) |
| `confidence` + `confidence_reasons` | High / Medium / Low with the specific reasons (volume, slope consistency, spike share, history length, completeness) |
| `mean_views_per_day`, `spike_events`, `spike_share`, `largest_spike` | audience size and how much of it is news-driven bursts |
| `seasonal` | yearly pattern detected (needs at least 2 full years) |
| `no_article` | editions without an article, each with a reason (no article does not mean no interest) |
| `comparison.ranking` | series ordered by an `opportunity_score` (growth × trust × audience size) |

Plus an indexed comparison chart, small multiples per series, a PDF report and a self-contained
HTML version of it. The formulas are documented in
[`references/methodology.md`](references/methodology.md).

## Quick start

Requires Python 3.11 or newer. There are no compiled binaries in the repository: everything
installs from PyPI with pinned versions, and the fonts the report needs ship with matplotlib
(DejaVu) or are built into reportlab (CJK CID fonts).

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate      macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
pytest tests/ -q                          # offline, fixture-based; no network needed
```

Then run a comparison. Output is JSON on stdout (UTF-8), meant to be read by an agent but readable
by a human:

```bash
# one topic across editions
python scripts/cli.py compare --topic "intermittent fasting" --langs pl,cs --years 2

# several topics within one edition
python scripts/cli.py compare --topics "World War I,World War II" --langs uk --years 2

# follow-ups reuse the saved query (state.json) and the local pageviews cache
python scripts/cli.py add-language --lang sk        # remove-language, add-topic, remove-topic
python scripts/cli.py extend-range --years 5        # or --start / --end
python scripts/cli.py choose --title "Mercury (planet)"   # answer an ambiguity (or --qid Q...)
python scripts/cli.py report --to . --html          # copy the last report into a directory
python scripts/cli.py show                          # last summary (or the pending question)
python scripts/cli.py history                       # queries run so far
```

Every subcommand has `--help`. Exit codes: `0` ok, `1` error / not found / no data, `2` the topic
is ambiguous (candidates are listed; answer with `choose`).

`compare` and every follow-up copy `wikipedia-trends-<topics>-<editions>.pdf` and `.html` into the
directory the command was run from (unless that directory is the skill itself), so the report lands
next to the user. `--deliver DIR` and `--no-deliver` override this.

On Windows PowerShell, never chain scripts with `>` (it writes UTF-16); use each script's `--out`.

## Installing it as a skill

Claude Code discovers skills in `~/.claude/skills/<name>/SKILL.md`. Link (or copy) this repository
there under the name `wikipedia-trends` and create the virtual environment inside it, because the
agent calls the scripts through `<skill dir>/.venv`:

```bash
# macOS / Linux
ln -s /path/to/this/repo ~/.claude/skills/wikipedia-trends
```

```powershell
# Windows (PowerShell; symlinks need Developer Mode or an elevated prompt)
New-Item -ItemType SymbolicLink -Path "$HOME\.claude\skills\wikipedia-trends" -Target "D:\path\to\this\repo"
```

Then start Claude Code in any folder and ask a question in the founder's own words, or invoke it
explicitly with `/wikipedia-trends ...`. Cache (`data/`), outputs (`output/`) and `state.json` are
written inside the skill directory, so follow-ups work from anywhere.

## How it works

```
resolve_topic.py   search each edition (MediaWiki), confirm via Wikidata sitelinks, follow redirects,
                   detect disambiguation pages and cross-edition conflicts
fetch_pageviews.py Wikimedia Pageviews REST API (agent=user), merges history under former titles,
                   drops the last two (incomplete) days, caches raw responses in data/
analyze_trend.py   smoothing, endpoint change, linear regression, spike detection, seasonality,
                   confidence label with reasons, opportunity score
make_chart.py      indexed comparison + small multiples (matplotlib, Agg backend, dataviz palette)
make_report.py     PDF (reportlab) and self-contained HTML from the same content dictionary
cli.py             one entry point: compare + follow-ups, state.json, report delivery
```

Each module is a CLI of its own with `--out`; `cli.py` chains them and never computes a number
itself. Endpoints, parameters and the quirks discovered while probing the APIs are recorded in
[`references/api_notes.md`](references/api_notes.md).

## Limitations the skill always states

- Wikipedia pageviews measure curiosity, not willingness to pay or product-market fit.
- No article in an edition does not mean no interest; nobody may have written one yet.
- A spike is usually a news event or a viral post, not organic growth.
- Editions are compared on indexed series only; audience sizes differ by orders of magnitude.
- Data starts in July 2015 and bot filtering is reliable only from mid-2016.

## Repository layout

```
SKILL.md                 instructions and triggers for the agent
CLAUDE.md                working reference for development
scripts/                 the pipeline (see above)
references/
  api_notes.md           Wikimedia API endpoints, params, limits, gotchas
  methodology.md         how confidence and opportunity score are computed
  manual_gate.md         prompt sequences and golden cases for the end-to-end gate
assets/report_template.html   HTML report template
tests/                   pytest suite + recorded API fixtures (tests/fixtures/)
  record_fixtures.py     records fixtures missing from tests/fixtures/resolve/index.json
requirements.txt         pinned runtime + test dependencies
data/, output/, state.json    local cache, generated reports, session state (git-ignored)
```

## Testing

- `pytest tests/ -q` runs unit tests against recorded fixtures (parsing, redirects, date logic),
  synthetic series with known properties for the trend analyser, and offline CLI session tests.
  The fixture session refuses any request that is not recorded, so the suite never touches the
  network.
- `python tests/record_fixtures.py` records API responses for new scenarios (existing fixtures
  are never re-fetched).
- Before calling a feature done, run the manual gate in
  [`references/manual_gate.md`](references/manual_gate.md) on a cheap model from a folder outside
  the repo: check that the agent runs the CLI instead of computing, quotes the JSON numbers
  verbatim, surfaces the caveats, and stays within a reasonable token budget.
