# Wikimedia API notes

## Common to all endpoints

- Send a descriptive `User-Agent` with contact info (`scripts/resolve_topic.py:USER_AGENT`).
  Anonymous or generic UAs get 403/429.
- Rate limiting is real: a burst of ~6 requests to one edition produced `HTTP 429` during
  fixture recording. `WikiClient` retries 429/5xx with exponential backoff and honours
  `Retry-After`. Keep calls sequential and cache what you can.
- Always request `format=json&formatversion=2` — v2 returns pages as a list (not a dict keyed by
  pageid) and uses `missing: true` instead of `"missing": ""`.

## Topic resolution (`scripts/resolve_topic.py`)

Base URL: `https://{lang}.wikipedia.org/w/api.php`

| Step | Params | Notes |
|---|---|---|
| Title-prefix search | `action=query&list=prefixsearch&pssearch=…&pslimit=5&psnamespace=0` | Matches titles only (incl. redirects). A hit means the edition has a page named like the query. Tried first. |
| Full-text search | `action=query&list=search&srsearch=…&srlimit=5&srnamespace=0` | Matches article bodies **including citations**. Searching an English phrase on pl.wikipedia returned "Stres oksydacyjny" (oxidative stress) as the top hit. Only accepted as seed when `title_similarity ≥ 0.75`. `snippet` contains `<span class="searchmatch">` HTML; strip it. |
| Redirects + QID | `action=query&titles=…&redirects=1&prop=pageprops&ppprop=wikibase_item` | `query.redirects[] = {from, to}`; final page in `query.pages[0]`; `pageprops.wikibase_item` is the QID. Missing pages have `missing: true` and no `pageid`. |

Wikidata: `https://www.wikidata.org/w/api.php`

| Step | Params | Notes |
|---|---|---|
| Sitelinks | `action=wbgetentities&ids=Q…&props=sitelinks\|labels&sitefilter=plwiki\|cswiki&languages=en` | `entities.Q….sitelinks.{lang}wiki.title` is the canonical article title. No key = no article in that edition (≠ no interest). A non-existent item has `"missing": ""`. |

Real example (recorded in `tests/fixtures/resolve/`): "intermittent fasting" → Q1666254 → `enwiki`,
`cswiki` ("Přerušovaný půst"), `ukwiki`; **no `plwiki` sitelink** at recording time (2026-09-26).

### Data findings from live probing (2026-09-26)

All of these are covered by recorded fixtures and tests in `tests/test_resolve_topic.py`.

1. **Disambiguation pages have Wikidata items and sitelinks.** "Mercury" (en) is a disambiguation
   page with QID Q48397 and sitelinks to `plwiki`/`dewiki` disambiguation pages. So is
   "Learning English" (en, Q16871974) — the seed for one of the skill's example prompts. Without
   a check the skill would compare pageviews of disambiguation pages. Detect with
   `ppprop=wikibase_item|disambiguation` (`pageprops.disambiguation` is present, value `""`).
   The resolver returns `ambiguous: true` with candidates and no articles; the agent must re-run
   with the exact title (e.g. "Mercury (planet)", "English as a second or foreign language") or `--qid`.
2. **Full-text search matches citations.** See the table above; "intermittent fasting" on pl →
   "Stres oksydacyjny". Prefix search first, then a title-similarity gate.
3. **Prefix search includes redirects**, which makes cross-language lookups work by accident:
   `pssearch=astronomy` on uk.wikipedia returns the redirect "Astronomy" → "Астрономія". Always
   pass the hit through `redirects=1`.
4. **Language code ≠ host ≠ sitelink id ≠ Pageviews project.**
   | user code | host | sitelink id | Pageviews project | note |
   |---|---|---|---|---|
   | `nb` | `no.wikipedia.org` (also `nb.` works) | `nowiki` | `no.wikipedia` | `nb.wikipedia` → 404 on Pageviews |
   | `zh-yue` | `zh-yue.wikipedia.org` | `zh_yuewiki` | `zh-yue.wikipedia` | dash in host, underscore in sitelink |
   | `be-tarask` | `be-tarask.wikipedia.org` | `be_x_oldwiki` | `be-tarask.wikipedia` | historical site id |
   | `simple` | `simple.wikipedia.org` | `simplewiki` | `simple.wikipedia` | not a language code but valid |
   Handled by `host_lang`, `sitelink_site`, `project_for`.
5. **Unknown language codes fail at DNS** (`xx.wikipedia.org` → `NameResolutionError`). The
   client fails fast on name-resolution errors instead of spending ~30 s retrying.
6. **Title normalisation.** `titles=intermittent_fasting` returns `query.normalized[]` and the
   canonical `Intermittent fasting`; this is not a redirect. Always use `pages[0].title`.
7. **Vague marketing phrases rarely map to one article.** "learning English" → disambiguation;
   the real target ("English as a second or foreign language") shows up only in full-text hits.
   The resolver adds full-text hits to `candidates` whenever the seed is ambiguous.
8. **Non-Latin titles are fine end-to-end** (ar RTL "علم الفلك", he, ja "天文学", zh-yue "天文學",
   uk). `pageviews_title` only swaps spaces for underscores; percent-encoding is done at request time.
9. **Renamed pages split their pageview history.** Pageviews are per title. If an article was
   moved, views before the move sit under the old title (now a redirect), and readers who land on
   a redirect are counted on the redirect page, not the target. The resolver flags every
   `redirected_from`; `fetch_pageviews` should consider summing the redirect's views (TODO).
10. **Rate limiting is per-edition and aggressive**: 6 rapid requests to one edition produced 429.
11. **Prefix search is not a topic match either.** `pssearch=astronomy` on no.wikipedia ranks the
    Pink Floyd song "Astronomy Domine" above "Astronomi"; `pssearch=learning English` on pl returns
    only the album "Learning English, Lesson One". Full-text search on no.wikipedia ranks the song
    first as well. The resolver therefore accepts a prefix hit only on an exact (case-insensitive)
    title match, and a full-text hit only if the *best* of the top hits scores ≥ 0.8 on a plain
    difflib ratio (no prefix bonus: "Astronomy Domine" scores 0.72, "Astronomi" 0.89). When nothing
    matches in the requested editions the English edition is the fallback seed and the local
    article is recovered through the Wikidata sitelink — which is how "astronomy" with `nb` first
    still ends up at "Astronomi".
12. **An English word can be an exact title in a foreign edition — for something else.** "Running"
    on pl.wikipedia is a 2014 Hungarian Eurovision song (Q15796039); "Sleep" on cs.wikipedia is an
    album; "Strava" on cs.wikipedia means *food* (a disambiguation page); "Tinder" on en.wikipedia
    is the fire-starting material while pl/cs "Tinder" is the app. The resolver therefore runs a
    cross-check: when the seed came from a non-English edition, it also looks for an exact English
    title; if that resolves to a *different* Wikidata item the result is `ambiguous` and both items
    are described (label + description from Wikidata) so the agent can pick `--seed-lang`.
13. **Full-text hits that *contain* the query are usually a different topic.** "meditation" on
    pl.wikipedia: full-text ranks "Medytacja" first but the similarity ratio is 0.74; "Guru
    Meditation" (the Amiga error screen) scored 0.8 on a plain ratio and won. Titles with extra
    words the query lacks are now scaled down (`title_similarity`), so the search moves on to
    English and comes back to "Medytacja" through the sitelink.
14. **Prefix search is accent-insensitive; our comparison must be too.** `pssearch=prerusovany pust`
    on cs.wikipedia returns "Přerušovaný půst", but an exact-title check without accent folding
    rejected it. `fold_title` strips diacritics (NFKD) before comparing.
15. **Redirects silently change the concept.** "Cold plunge" → "Ice bath"; "Period tracker" →
    "Fertility awareness"; "Budgeting" → "Budget"; "Intervallfasten" → "Intermittierendes Fasten";
    "Bieganie" → "Bieg" (a disambiguation page). Pageviews will measure the *target*. The resolver
    reports `seed_redirected_from` and a caveat naming both titles.
16. **Disambiguation pages differ per edition.** "Sleep" and "Mindfulness" are disambiguation pages
    on pl.wikipedia but articles on en.wikipedia; "Keto diet" is a disambiguation page on en while
    "Ketogenic diet" is the article. The resolver skips disambiguation pages and continues with the
    next edition, adding a caveat; only when *every* edition yields a disambiguation page is the
    topic reported as ambiguous.
17. **Wikidata items may have no English label** ("Wordle", Q110367996, only has a `mul` label).
    Request `languages=en|mul` and fall back to the seed title.
18. **Sitelinked titles can be in the "wrong" language or a broader concept.** The Czech article
    linked from "Ice bath" is titled "Ice bathing"; Polish "Zakwas" (linked from "Sourdough") covers
    every kind of leaven. Wikidata sitelinks match concepts approximately — report titles verbatim.
19. **Culturally specific concepts have no English article.** Czech "otužování" (cold hardening)
    is Q2714298 with no enwiki/plwiki sitelink; the question "is cold exposure trending in CZ" can
    only be answered on cs.wikipedia. Polish Wikipedia has no keto-diet or pickleball article.
20. **Pageviews titles need percent-encoding beyond underscores.** "AC/DC" and "Who Wants to Be a
    Millionaire?" are valid titles; the per-article endpoint needs `AC%2FDC` and `%3F`.
21. **One concept can be split across several Wikidata items.** The general "running" item
    (Q105674) has no plwiki sitelink even though pl.wikipedia covers running — its article hangs
    off a different item (running as an athletics discipline), and "Bieganie" redirects to the
    disambiguation page "Bieg". `found: false` therefore sometimes means "linked elsewhere", not
    "no article"; the caveat text says so and the agent should try the local wording as the topic.
22. **Disambiguation pages know their meanings; search does not.** Full-text search for "keto diet"
    returns "Keto", "Low-carbohydrate diet", two TV personalities — never "Ketogenic diet". The
    disambiguation page links to it. `prop=links` returns links *alphabetically* (113 for
    "Mercury", the planet far down), so the resolver reads the page wikitext
    (`prop=revisions&rvprop=content&rvslots=main`) and extracts `[[links]]` in document order,
    where editors put the main meanings first ("Mercury (planet)", "Mercury (element)",
    "Mercury (mythology)"). Wikitext links keep the author's first-letter case
    (`[[ketogenic diet]]`); MediaWiki titles are upper-cased on the first letter.
23. **Example-prompt reality check.** "learning English" resolves to a disambiguation page, and the
    encyclopaedic article behind it ("English as a second or foreign language", Q130192) has no
    pl/cs/uk sitelink. The skill must say so rather than quietly substituting e.g. the article on
    the English language; the agent should offer nearby concrete topics as candidates instead.

## Running the CLIs on Windows

- Every CLI forces UTF-8 on stdout/stderr (`_force_utf8_stdout`): the default console code page
  (cp1251/cp1252) cannot encode Polish/Czech/Ukrainian titles and crashed `json.dump`.
- Windows PowerShell 5.1 `>` redirection writes **UTF-16 with a BOM** (`FF FE`). A downstream
  script reading that as UTF-8 fails with `'utf-8' codec can't decode byte 0xff in position 0`.
  `read_json_file` detects UTF-16/BOMs; the `--out` option writes plain UTF-8 and is the
  recommended way to chain scripts.

## Pageviews (`scripts/fetch_pageviews.py`)

- `https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/{project}/all-access/user/{article}/{granularity}/{start}/{end}`
  - `project` = `pl.wikipedia`; `article` = title with underscores (`pageviews_title` from the
    resolver), percent-encoded with `quote(safe="")` so `AC/DC` → `AC%2FDC` and `…?` → `%3F`.
  - `agent=user` (path segment) is mandatory — filters bots/spiders. Verified live for every
    scenario in `tests/record_fixtures.py`.
  - Dates are `YYYYMMDD` for both granularities; monthly returns the whole months containing them
    (`20250115`–`20250615` → 6 items, timestamps `2025010100`…).
  - Timestamps are `YYYYMMDDHH`; only the first 8 characters matter.

### Data findings (2026-09-27)

1. **The API silently stops ~2 days before today.** An end date of today returns items through
   `today - 2` with status 200. `default_period` ends there (`DATA_LAG_DAYS`), and a series whose
   last item is before the requested end gets a caveat.
2. **404 = no data anywhere in the range — not "article missing".** cs "Ice bathing" exists
   (created March 2026) but `daily/20250101/20250331` is 404. Requesting a range that *contains*
   data (`20250101`–`20260901`) returns 200 with items starting at creation. So: 404 →
   `status: no_data`; items starting after `start` → `status: partial` and the series starts
   there. Never zero-fill a long leading gap: 440 zero days followed by real views would look like
   explosive growth. A *short* leading gap (≤ 14 days, `LEADING_GAP_DAYS`) is different: bg
   "Периодично гладуване" simply had zero views on the first requested day, and treating that as
   "article younger than the period" was wrong — short gaps are zero-filled like internal ones.
3. **Days with zero views are sometimes omitted and sometimes returned as 0.** The two-year daily
   series for cs "Přerušovaný půst" has 731 days, 729 items, 8 explicit zeros and 2 omitted days.
   Internal gaps are zero-filled and counted (`zero_filled_points`); a small wiki article
   (zh-yue 天文學) still had 90/90 days.
4. **Before 2015-07-01 there is nothing** (a request from 2015-06-01 returns items from 07-01,
   status 200), and bot filtering is only reliable from ~mid-2016 — both are caveats.
5. **`nb.wikipedia` → 404; `no.wikipedia` works** (`normalize_project` applies the host alias).
6. **Raw responses are cached** in `data/pageviews/<sha256 of params>.json` (404s too, so a
   missing article is not re-requested) and the zero-filled series in `<hash>.csv`. The default
   period moves daily, so the hash changes daily — a repeated query on the same day is free.
7. **Redirects have their own pageviews — and a renamed article's history lives under its old
   title.** Small case: January 2025 `5:2_diet` 409 vs `Intermittent_fasting` 22 461 (~2 %).
   Big case (found by a user): uk "Альберт Ейнштейн" was moved to "Альберт Айнштайн" on
   2026-02-10 (spelling reform). The old title carried ~7 000 views/month until the move, the new
   one ~15/month before and ~3 000-4 500 after; the old title still gets 200-700/month as a
   redirect. Fetching only the current title showed 0-3 views/day rising to 90 — a fake "growing"
   trend with 162 zero days. Fix: `resolve_topic` lists each article's redirects
   (`prop=redirects&rdnamespace=0`), `fetch_with_redirects` probes each one monthly and merges
   the daily views of any redirect with ≥ 2 % of the target's views or ≥ 200 views; the result
   carries `merged_titles`, `target_views` and a caveat. The move log
   (`list=logevents&letype=move&letitle=<old title>`) confirms such moves.
8. **Titles must match exactly** — the endpoint does no normalisation. All 404 for an existing
   article: `intermittent_fasting` (lower-case first letter), `Intermittent__fasting`,
   `Intermittent_fasting␠` (trailing space), and the NFD form of `Přerušovaný_půst`. A space
   encoded as `%20` works like an underscore. `C++`, `Barnes_&_Noble`, `Grey's_Anatomy`,
   `Star_Trek:_The_Next_Generation`, `100%_(song)` all work once percent-encoded.
   `normalize_title` applies NFC, collapses whitespace/underscores and upper-cases the first letter.
9. **Monthly granularity: whole months only, and the current month is partial.** Dates that do
   not span a full month → HTTP 400 `no full months between dates`; the API otherwise returns the
   whole months containing the dates, *including the current month with only the days published
   so far* (`20250101`–`20260925` → 21 items). `PageviewsQuery` snaps monthly ranges to month
   bounds and `fetch_pageviews` drops any month not complete as of `today - 2 days`.
10. **Ranges: no length limit, future end is fine.** One daily call from 2015-07-01 to today
    returned 4 105 items. An end date in the future returns data through the lag; a single day
    works; a range entirely before 2015-07-01 is 404; `end < start` or ISO dates are HTTP 400.
11. **`en.m.wikipedia` is 404 on per-article**; mobile views are inside `all-access` of
    `en.wikipedia`. `normalize_project` drops the `.m` segment. A trailing `.org` is accepted.
12. **How much `agent=user` removes** (January 2025, `Intermittent_fasting`, en): all-agents
    29 176 = user 22 461 + spider 5 077 + automated 1 638 → 23 % non-human. On cs
    `Přerušovaný_půst`: 609 all-agents vs 489 user (20 %). This is why the skill never uses
    `all-agents`.
