# Manual gate: prompt sequences and golden cases

Run these in a Claude Code session with a cheap model (Claude Haiku 4.5 or similar), **started in a
folder outside this repository** (an empty directory is ideal). Inside the repo Claude Code loads
CLAUDE.md and the agent behaves as the project's developer; outside it, the only instructions are the
installed `~/.claude/skills/wikipedia-trends/SKILL.md`, which is what the gate must test. Type the prompts
in the user's own words and ignore the auto-suggested follow-ups.

**The topics below are fixtures, not a catalogue.** They were picked because each exercises one
mechanism (disambiguation, redirects, tiny audiences, renamed articles, seasonality). Nothing in this
file or in `SKILL.md` is a suggestion of what to analyse; section G checks that the agent has not
learned it as one. Each gate run should also include one or two topics of the tester's own choosing
that appear nowhere in this repository.

**Phrase prompts as a founder would: about interest, popularity or trends.** "Compare intermittent
fasting on Polish and Czech Wikipedia" is a request to compare *article content*, and a browser is the
correct tool for that; the skill only has a claim when the prompt is about how much people read or care
("growth of interest", "how popular", "is it trending", "pageviews"). A wrong-tool result on a
content-shaped prompt is not a trigger miss.

**Trigger results so far (2026-09-27).** With the full B1 prompt in a fresh session outside the repo:
Sonnet loads the skill immediately; Haiku 4.5 does not — with the Chrome extension connected it opens
the browser, and with `claude --no-chrome` it fetches the Wikipedia pages with WebFetch instead. The
description was then shortened to lead with the user phrasing (retest pending). Until Haiku triggers on
its own, cheap-model gate runs invoke the skill by name; the other gate criteria are unaffected.

**Trigger check first.** On Haiku with the Chrome extension connected, the plain prompt may be routed to
the browser instead of this skill (observed 2026-09-27). Score that as a *trigger miss* and then run the
gate with the skill invoked by name, which tests everything else:

```
/wikipedia-trends Compare the growth of interest in intermittent fasting on the Polish and Czech Wikipedias over the last two years.
```

Auto-triggering is decided by the model from the skill description alone; do not add global
instructions to steer it, as that would not reflect a real user's setup. If the browser keeps winning,
disconnect the Chrome extension for the session (a user without it is the typical case) and note the
result; the description's wording is the only legitimate fix on the skill's side. Seeing the agent run `cli.py` in a
shell is the skill working — the skill is the instructions plus that command, not a separate app. For every answer check four things: the agent **ran the CLI** (no arithmetic of its own),
every number appears verbatim in `output/<topic>/summary.json`, the **caveats** are present, and the
token count per query is reasonable. Expected values below were recorded on 2026-09-27; real data
drifts, so treat labels and directions as the check and numbers as a sanity range.

Legend: **U** = user prompt · **→** = command the agent should run · **✓** = what to verify.

---

## A. The three example prompts (baseline)

### A1. One topic, two editions — a tiny, spiky article
- **U** "Compare the growth of interest in intermittent fasting on the Polish and Czech Wikipedias over the last two years."
- → `compare --topic "intermittent fasting" --langs pl,cs --years 2`
- ✓ `no_article: pl` and the sentence "no article does not mean no interest".
- ✓ cs: **declining**, confidence **Low** with reasons *very low volume (~9 views/day)*, *noisy slope*,
  *~14 % of views from spikes*; largest spike **2025-04-14, 508 views** against a level of ~14.
- ✓ The agent does not present the Czech decline as a finding; it calls it a weak signal.
- ✓ `wikipedia-trends-intermittent-fasting-pl-cs.pdf` and `.html` appear in the **tester's working
  directory** (`outputs.delivered`), and the answer's "Report:" line gives that path, not one
  inside `~/.claude/skills`.

### A2. One edition, trust question — seasonal article
- **U** "Is interest in astronomy growing on Ukrainian Wikipedia, and how much can that growth be trusted?"
- → `compare --topic "astronomy" --langs uk --years 2`
- ✓ **declining**, `seasonal: true`, headline is **year-over-year (≈ −55 %)**, and the agent says the
  first-vs-last figure (≈ −70 %) mixes seasons (school year).
- ✓ Confidence **High**; reasons quoted (modest volume ~27 views/day; annual change and last window agree).
- ✓ The answer to "can it be trusted" is the reasons list, not a number.

### A3. Vague phrase — must ask before analysing
- **U** "Compare interest in learning English across Polish, Czech, Ukrainian and German Wikipedia and recommend which audiences to investigate next."
- → `compare --topic "learning English" --langs pl,cs,uk,de --years 2` → **exit 2, ambiguous**
- ✓ The agent shows candidates and asks (or picks the obvious "English as a second or foreign language").
- **U** "Use the article about English as a second language."
- → `choose --title "English as a second or foreign language"`
- ✓ Only **de** has an article (en too, if requested); pl/cs/uk are listed under `no_article`.
  The recommendation must say the question cannot be answered by article for those three markets,
  and must not substitute the article on the English language.

---

## B. Iterative session (state and follow-ups)

| # | **U** | → | ✓ |
|---|---|---|---|
| B1 | "Compare the growth of interest in intermittent fasting on the Polish and Czech Wikipedias over the last two years." | `compare --topic "intermittent fasting" --langs pl,cs` | as A1 |
| B2 | "Add Ukrainian to that." | `add-language --lang uk` | uk appears (**declining ≈ −84 %, High**); `follow_up.changed.added_languages == ["uk"]`; the agent restates all series; a second file `wikipedia-trends-intermittent-fasting-pl-cs-uk.pdf` appears next to the B1 one |
| B3 | "And how does the keto diet compare?" | `add-topic --topic "keto diet"` | **ambiguous**, saved as pending; candidates include *Ketogenic diet* |
| B4 | "What were the options again?" | `show` | reprints the pending candidates (exit 2) |
| B5 | "I mean the ketogenic diet article." | `choose --title "Ketogenic diet"` | four keys `intermittent-fasting:cs/uk`, `ketogenic-diet:cs/uk`; ranking recomputed |
| B6 | "Show me five years instead of two." | `extend-range --years 5` | period starts 2021; uk intermittent fasting softens from ≈ −84 % to ≈ −22 % — the agent must mention the change |
| B7 | "Drop Polish, there is no article anyway." | `remove-language --lang pl` | same four series, `no_article` empty |
| B8 | "What have we looked at so far?" | `history` | eight entries incl. the ambiguous one, with commands and statuses |
| B9 | "Can you give me a report on the last research? I want to see it in my directory." | `report --html` | The agent **runs the command** and quotes the path it prints (`wikipedia-trends-intermittent-fasting-ketogenic-diet-cs-uk.pdf` in the tester's working directory — the B7 edition set). **Fail** if it answers from memory, gives a path inside `~/.claude/skills` or `output/`, re-runs `compare`, or offers an artifact / written breakdown instead of the file (observed 2026-09-27 on Haiku 4.5: quoted `output/intermittent-fas/report.pdf` and offered an HTML artifact). "Put it on my desktop" → `report --to <dir>` |

---

## C. Clarification situations

### C1. Disambiguation page
- **U** "Is interest in mercury growing on English and Polish Wikipedia?"
- → `compare --topic "mercury" --langs en,pl` → **ambiguous**; candidates start with *Mercury (planet)*, *Mercury (element)*, *Mercury (mythology)*.
- **U** "The planet." → `choose --title "Mercury (planet)"` (or `--qid Q308`)
- ✓ pl article **Merkury**; no analysis of the disambiguation page ever appears.

### C2. Same word, different things in two editions
- **U** "How is interest in running developing on Polish Wikipedia?"
- → `compare --topic "running" --langs pl` → **ambiguous**: pl "Running" is a 2014 song (Q15796039), en "Running" is the activity (Q105674); both described.
- **U** "I mean running as in jogging." → `choose --qid Q105674` (or re-run with `--seed-lang en`)
- ✓ `no_article: pl` — the Polish running article hangs off another item; the agent says so and
  suggests trying the Polish wording ("bieganie") as a follow-up topic.

### C3. Topic typed in the user's language
- **U** "Czy zainteresowanie otužováním rośnie w Czechach?" (or: "Is 'otužování' trending on Czech Wikipedia?")
- → `compare --topic "otužování" --langs cs,pl,en --seed-lang cs`
- ✓ cs found (Q2714298, label *dousing*), pl/en `no_article`; the agent explains the concept has no
  English article rather than translating it.

### C4. Follow-up that becomes ambiguous
- After C2-style `compare --topic "astronomy" --langs uk,pl`: **U** "Just Polish." → `remove-language --lang uk`
- ✓ **ambiguous** (pl "Astronomy" is a different item), pending saved, `history` shows the
  follow-up with status ambiguous; `choose --qid Q333` finishes with **Astronomia**.

---

## D. Data-quality situations

### D1. Renamed article (history under the old title)
- **U** "Compare Albert Einstein on English, German and Ukrainian Wikipedia."
- → `compare --topic "Albert Einstein" --langs en,de,uk`
- ✓ uk `merged_redirect_titles` contains **Альберт_Ейнштейн** with more views than the current title;
  uk is **declining** in line with en/de, not "growing" from zero; the caveat "probably renamed" is quoted.

### D2. Article younger than the period
- **U** "Is cold plunging catching on in Czechia?"
- → `compare --topic "cold plunge" --langs en,cs` (resolves via the redirect to *Ice bath*)
- ✓ `seed_redirected_from`/caveat: "cold plunge" is a redirect to "Ice bath" — the agent says what is measured.
- ✓ cs *Ice bathing* is `data.status: partial` (created March 2026); the agent gives the shorter window.

### D3. Tiny audiences
- **U** "Compare intermittent fasting on Romanian, Bulgarian and Ukrainian Wikipedia."
- → `compare --topic "intermittent fasting" --langs ro,bg,uk`
- ✓ ro (~3/day) and bg (~1/day) flagged *very low volume*, confidence Low/Medium; bg is **not** reported as
  partial (its first day just had zero views). The agent warns that indexed lines for such articles are noise.

### D4. Event-driven interest and indexed vs absolute
- **U** "Compare World War II on Ukrainian, English and Romanian Wikipedia over five years."
- → `compare --topic "World War II" --langs uk,en,ro --years 5`
- ✓ All three spike on **2022-02-24**; en's spike is the largest in views (~140k), uk's the largest
  relative surge (≈ 11× its baseline, sustained). The agent must not compare absolute views across
  editions, and should say interest is event-driven (`spike_share`, dates).

### D5. Non-Latin titles and many editions
- **U** "Compare intermittent fasting on Romanian, Bulgarian, Ukrainian, Hungarian, German, Japanese and Danish Wikipedia."
- → `compare --topic "intermittent fasting" --langs ro,bg,uk,hu,de,ja,da`
- ✓ Seven tiles in the report, Japanese title 断続的断食 rendered (no boxes), all seven editions in the
  extra charts and the ranking, de (~70/day) ranked first.

### D6. Two topics in one edition
- **U** "Compare WW1 and WW2 on Ukrainian Wikipedia."
- → `compare --topics "World War I,World War II" --langs uk --seed-lang en`
- ✓ Keys are topic slugs; absolute `mean_views_per_day` is comparable here (same edition) and the
  agent may say so; both seasonal (May anniversaries), spikes on 8–9 May.

---

## E. Golden cases (real data with known expected outcome)

Re-run these periodically; the label is the assertion.

| Case | Command | Expected |
|---|---|---|
| Tiny article with a news spike | `compare --topic "intermittent fasting" --langs cs` | **Low** confidence; 4 spike events; largest 2025-04-14 |
| Moderate steady article | `compare --topic "meditation" --langs pl` (resolves to *Medytacja* via en) | **declining**, **Medium**; `spike_share` < 1 %; "Guru Meditation" never chosen |
| Seasonal article | `compare --topic "astronomy" --langs uk` | `seasonal: true`, headline year-over-year, **High** |
| Renamed article | `compare --topic "Albert Einstein" --langs uk` | merged former title, **declining**, not growing |
| Disambiguation | `compare --topic "mercury" --langs en,pl` | exit 2, *Mercury (planet)* first candidate |
| Cross-edition conflict | `compare --topic "Tinder" --langs pl,en` | exit 2: pl app (Q15078152) vs en fire-starting material (Q143518) |
| Disambiguation skipped, next edition used | `compare --topic "Strava" --langs cs,pl,en` | resolves to the app via pl; caveat that cs "Strava" is a disambiguation page |
| No article anywhere | `compare --topic "intermittent fasting" --langs pl` | exit 1, `not_found`, "does not mean no interest" |
| Bad language code | `compare --topic "astronomy" --langs xx` | exit 1 within a second: "Cannot resolve host xx.wikipedia.org" |
| Redirect to a narrower item | `compare --topics "Carnivore diet,Vegan diet" --langs en,de,fr` | 4 of 6 series; `no_article` = carnivore-diet:fr, vegan-diet:de; redirect caveat "Vegan diet → Vegan nutrition" |
| File-like output path | `compare --topic "astronomy" --langs uk --out-dir output.json` | exit 1: "--out-dir must be a directory" |
| Report before any query | `report` (fresh `--state`) | exit 1: "run `compare` first" — the agent asks what to analyse, it does not pick a topic |
| Delivery skipped inside the skill | `compare …` run from the repo root | no `outputs.delivered`; nothing written to the repo root. From any other directory: `delivered.pdf`/`.html` there |
| Report after `--no-charts` | `compare … --no-charts` then `report` | exit 1: "no report … re-run `compare` without --no-charts" |

Synthetic counterparts (offline, `pytest tests/test_analyze_trend.py`): linear growth → growing/High;
flat + one spike → flat/High with the spike listed; spike inside the last window → Medium;
2→6 views/day growth → Medium (volume); 100 days → insufficient.

---

## F. Things the agent must never do (check in every transcript)

- Compute or estimate a percentage, average or index itself, or "round up" the story beyond the JSON.
- Analyse a disambiguation page, or silently swap in a related article.
- Compare absolute views across editions, or call a tiny-article swing a trend.
- Quote a confidence label without its reasons, or drop the "not willingness to pay" caveat.
- Describe a chart it did not open, or invent a cause for a spike without a search result.
- Choose a topic, edition or period for the user — from this document, from `SKILL.md`'s examples,
  from `history`/`state.json` or from its own earlier turns. An open-ended prompt gets a question back.

---

## G. Anchoring check (test-selection bias)

Run once per gate, right after A1 or B1, so that `state.json` holds a real query and the model has
just seen the skill's example topics.

- **U** "Try another example query with different languages/topics?"
- ✓ The agent runs **nothing** and asks which topic, editions and period to use (and whether to
  build on the current query or start fresh).
- ✗ Fail if it runs `compare` or `add-topic` on its own, or proposes any topic that appears in
  this file, in `SKILL.md` or in `history` (keto/ketogenic, paleo, carnivore, vegan, astronomy,
  WW1/WW2, meditation, Einstein, cold plunge, mercury, running, Tinder, Strava, learning English…).
  Reaching for the diet cluster is the observed failure (2026-09-27, Haiku 4.5: it added keto and
  paleo diets unprompted).
- **U** "You pick." → the topic must be one named nowhere in this repository or the session; the
  agent says the choice is arbitrary and offers to switch. Still a fail if it picks from the list
  above.
- **U** "Is interest in <tester's own topic> growing on <edition>?" → a fresh `compare`, not an
  `add-topic` onto the saved query.
