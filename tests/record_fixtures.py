"""Record (or top up) the resolver fixtures by running real scenarios once.

    python tests/record_fixtures.py            # records only routes missing from index.json

Every scenario below is also exercised by tests/test_resolve_topic.py.  Adding a
scenario here and re-running records exactly the API responses it needs; existing
fixtures are never re-fetched, so recorded data stays stable.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tempfile  # noqa: E402
from datetime import date  # noqa: E402

from scripts.fetch_pageviews import PageviewsClient, PageviewsQuery, fetch_pageviews  # noqa: E402
from scripts.resolve_topic import USER_AGENT, WikiClient, describe_items, get_page_info, resolve_topic  # noqa: E402
from tests.fixture_session import RecordingSession  # noqa: E402

# (topic, langs, kwargs) — keep in sync with the test module's scenario tables.
SCENARIOS: list[tuple[str, list[str], dict]] = [
    # education / earlier cases
    ("intermittent fasting", ["en", "cs", "pl"], {}),
    ("intermittent fasting", ["pl", "en"], {}),
    ("přerušovaný půst", ["cs", "en"], {}),
    ("prerusovany pust", ["cs", "pl"], {}),  # typed without diacritics
    ("astronomy", ["uk", "ja", "ar", "zh-yue", "nb", "en"], {}),
    ("astronomy", ["nb", "en"], {}),
    ("astronomy", ["uk", "pl", "cs", "nb"], {}),
    ("astronomy", ["ja", "uk"], {"seed_lang": "uk"}),
    ("mercury", ["en", "pl"], {}),
    ("Mercury (planet)", ["en", "pl", "cs"], {}),
    ("mercury", ["en", "pl"], {"qid": "Q48397"}),
    ("learning English", ["pl", "en"], {}),
    ("English as a second or foreign language", ["en", "de", "pl", "cs"], {}),
    # B2C: diet / fitness / wellness
    ("keto diet", ["en", "pl"], {}),  # disambiguation on en
    ("Ketogenic diet", ["en", "pl", "cs", "uk"], {}),
    ("meditation", ["pl", "cs", "uk"], {}),  # "Guru Meditation" trap on pl
    ("cold plunge", ["en", "pl", "cs"], {}),  # redirect to "Ice bath"
    ("otužování", ["cs", "pl", "en"], {}),  # Czech concept without an English article
    ("pilates", ["pl", "cs", "uk"], {}),
    ("pickleball", ["pl", "cs", "de"], {}),
    ("running", ["pl", "en"], {}),  # pl "Running" is a song -> conflict with en
    ("running", ["pl", "cs"], {"seed_lang": "en"}),
    ("sleep", ["pl", "cs", "en"], {}),  # pl disambiguation, cs song, en article -> conflict
    ("sleep", ["pl", "cs"], {"seed_lang": "en"}),
    # B2C: health / finance / apps / games / brands
    ("ADHD", ["pl", "uk", "en"], {}),  # acronym title on pl, redirect on en, same item
    ("budgeting", ["pl", "cs", "en"], {}),  # en redirect "Budgeting" -> "Budget"
    ("Tinder", ["pl", "en"], {}),  # pl app vs en fire-starting material -> conflict
    ("Tinder", ["pl", "cs", "en"], {"qid": "Q15078152"}),
    ("Strava", ["cs", "pl", "en"], {}),  # cs "Strava" (food) is a disambiguation page
    ("Wordle", ["pl", "cs", "uk"], {}),  # Wikidata item without an English label
    ("AC/DC", ["pl", "en"], {}),  # slash in the title
    # cli.py offline scenarios
    ("astronomy", ["uk"], {}),
    ("astronomy", ["uk", "pl"], {}),
    ("intermittent fasting", ["uk"], {}),
    ("World War I", ["uk", "en"], {"seed_lang": "en"}),
    ("World War II", ["uk", "en"], {"seed_lang": "en"}),
    ("mercury", ["uk"], {}),  # second topic ambiguous after a good first one
]


# Fixed date ranges so the cache keys (and therefore the fixtures) are deterministic.
PAGEVIEW_SCENARIOS: list[tuple[str, str, str, date, date]] = [
    ("cs.wikipedia", "Přerušovaný_půst", "daily", date(2024, 9, 1), date(2026, 9, 1)),  # example prompt 1
    ("pl.wikipedia", "Medytacja", "daily", date(2024, 9, 1), date(2026, 9, 1)),
    ("uk.wikipedia", "Астрономія", "daily", date(2024, 9, 1), date(2026, 9, 1)),  # example prompt 2
    ("en.wikipedia", "Intermittent_fasting", "monthly", date(2024, 9, 1), date(2026, 9, 1)),
    ("en.wikipedia", "AC/DC", "monthly", date(2025, 1, 1), date(2025, 6, 30)),  # slash in the title
    ("cs.wikipedia", "Ice_bathing", "daily", date(2025, 1, 1), date(2026, 9, 1)),  # article younger than the range
    ("cs.wikipedia", "Xqzvv_qwrtyplk_zzz", "daily", date(2025, 1, 1), date(2025, 1, 31)),  # no such article
    ("nb.wikipedia", "Astronomi", "daily", date(2026, 8, 1), date(2026, 8, 31)),  # alias -> no.wikipedia
    ("en.wikipedia", "Intermittent_fasting", "daily", date(2015, 6, 1), date(2015, 7, 31)),  # before data starts
    ("en.wikipedia", "Astronomy", "daily", date(2015, 1, 1), date(2015, 6, 30)),  # entirely before data starts -> 404
    ("en.wikipedia", "5:2_diet", "monthly", date(2025, 1, 1), date(2025, 1, 31)),  # a redirect page has its own views
    ("en.wikipedia", "Budget", "monthly", date(2025, 1, 1), date(2025, 1, 31)),
    ("en.wikipedia", "Budgeting", "monthly", date(2025, 1, 1), date(2025, 1, 31)),  # redirect to Budget
    ("en.wikipedia", "Astronomy", "monthly", date(2025, 1, 1), date(2026, 9, 25)),  # includes the current partial month
    ("en.wikipedia", "Astronomy", "daily", date(2026, 9, 1), date(2026, 9, 1)),  # single day
    ("en.wikipedia", "C++", "monthly", date(2025, 1, 1), date(2025, 1, 31)),  # plus signs
    ("en.wikipedia", "100% (song)", "monthly", date(2025, 1, 1), date(2025, 1, 31)),  # percent sign
    ("uk.wikipedia", "Інтервальне_голодування", "daily", date(2024, 9, 1), date(2026, 9, 1)),  # cli.py --topics test
    ("pl.wikipedia", "Astronomia", "daily", date(2024, 9, 1), date(2026, 9, 1)),  # cli.py add-language test
    ("uk.wikipedia", "Перша_світова_війна", "daily", date(2024, 9, 1), date(2026, 9, 1)),  # cli.py topics x langs
    ("uk.wikipedia", "Друга_світова_війна", "daily", date(2024, 9, 1), date(2026, 9, 1)),
    ("en.wikipedia", "World_War_I", "daily", date(2024, 9, 1), date(2026, 9, 1)),
    ("en.wikipedia", "World_War_II", "daily", date(2024, 9, 1), date(2026, 9, 1)),
    ("en.wikipedia", "Intermittent_fasting", "daily", date(2024, 9, 1), date(2026, 9, 1)),  # cli.py en,cs,pl test
]


# Whole-pipeline scenarios (mirror tests/test_cli.py): recorded through cli.run_compare so that every
# route the CLI needs (redirect lists, monthly redirect probes, merged daily series) is captured.
CLI_SCENARIOS: list[dict] = [
    {"topic": "astronomy", "langs": "uk"},
    {"topic": "astronomy", "langs": "uk,pl"},
    {"topic": "intermittent fasting", "langs": "en,cs,pl"},
    {"topics": "astronomy,intermittent fasting", "langs": "uk"},
    {"topics": "World War I,World War II", "langs": "uk,en", "seed_lang": "en"},
    {"topic": "Albert Einstein", "langs": "uk"},  # renamed article: history under the old title is merged
    {"topic": "whatever", "langs": "uk", "qid": "Q333"},
    # session follow-ups (tests/test_cli_session.py)
    {"topic": "astronomy", "langs": "uk", "start": "2025-09-01"},  # extend-range starts from one year
    {"topic": "Mercury (planet)", "langs": "en,pl"},  # choose --title after an ambiguous "mercury"
    {"topic": "mercury", "langs": "en,pl", "qid": "Q308"},  # choose --qid
    {"topics": "astronomy,mercury", "langs": "en", "qids": {"mercury": "Q308"}},  # choose --qid in topics mode
    {"topic": "intermittent fasting", "langs": "uk"},
    {"topic": "astronomy", "langs": "pl"},  # remove-language leaves pl alone
]


def record_cli(session: RecordingSession) -> None:
    import argparse

    from scripts import cli
    from scripts import resolve_topic as rt

    import scripts.fetch_pageviews as fp_mod

    rt.WikiClient = lambda: WikiClient(session=session, backoff_seconds=0)  # type: ignore[assignment]
    fp_mod.PageviewsClient = lambda: PageviewsClient(session=session, backoff_seconds=0)  # type: ignore[assignment]
    with tempfile.TemporaryDirectory() as tmp:
        for sc in CLI_SCENARIOS:
            ns = argparse.Namespace(
                topic=sc.get("topic"), topics=sc.get("topics"), langs=sc["langs"], years=2, start=sc.get("start", "2024-09-01"),
                end="2026-09-01", seed_lang=sc.get("seed_lang"), qid=sc.get("qid"), qids=sc.get("qids"), granularity="daily", title=None,
                out_dir=str(Path(tmp) / "run"), cache_dir=str(Path(tmp) / "cache"), refresh=False, today="2026-09-27",
                no_charts=True, state=str(Path(tmp) / "state.json"),
            )
            payload, code = cli.run_compare(ns)
            keys = list(payload.get("results", {})) if code == 0 else payload.get("status")
            print(f"cli {sc} -> {keys}")


def record_pageviews(session: RecordingSession) -> None:
    client = PageviewsClient(session=session, backoff_seconds=0)
    with tempfile.TemporaryDirectory() as tmp:
        for project, article, granularity, start, end in PAGEVIEW_SCENARIOS:
            result = fetch_pageviews(client, PageviewsQuery(project, article, granularity, start, end), cache_dir=Path(tmp))
            print(f"pageviews {project} {article} {granularity} {start}..{end} -> {result.status} "
                  f"points={result.points_requested} data={result.data_from}..{result.data_through} total={result.total_views}")


def main() -> int:
    session = RecordingSession(user_agent=USER_AGENT + " fixture-recorder")
    client = WikiClient(session=session, backoff_seconds=0)
    for topic, langs, kwargs in SCENARIOS:
        result = resolve_topic(client, topic, langs, **kwargs)
        found = {lang: ref.found for lang, ref in result.articles.items()}
        print(f"{topic!r} {langs} {kwargs or ''} -> qid={result.qid} ambiguous={result.ambiguous} {found}")
    # standalone calls used by unit tests
    get_page_info(client, "en", "intermittent_fasting")
    get_page_info(client, "en", "5:2 diet")
    get_page_info(client, "pl", "Xqzvv qwrtyplk zzz")
    describe_items(client, ["Q308"])
    record_pageviews(session)
    record_cli(session)
    print(f"\nrecorded {len(session.recorded)} new fixture(s); index has {len(session.index)} routes")
    for key in session.recorded:
        print("  +", key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
