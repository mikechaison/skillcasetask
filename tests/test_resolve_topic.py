"""Offline tests for scripts/resolve_topic.py against recorded API fixtures.

Fixtures live in tests/fixtures/resolve/ (see index.json) and were recorded
from the live MediaWiki / Wikidata APIs on 2026-09-26 with
tests/record_fixtures.py.  Scenarios cover:

- education topics: "intermittent fasting" (no pl article, a redirect, a
  full-text false positive on pl), "astronomy" (many scripts, nb/no alias, a
  redirect on uk, the song "Astronomy Domine" on no), "learning English"
  (disambiguation; the real article exists only in en/de).
- B2C topics: diet ("keto diet" disambiguation, "Ketogenic diet"), wellness
  ("meditation" with the "Guru Meditation" trap, "cold plunge" redirect,
  "otužování" with no English article, "pilates", "pickleball", "sleep"),
  health ("ADHD"), finance ("budgeting" redirect), apps and brands ("Tinder",
  "Strava", "Wordle", "AC/DC"), and the English-word-in-foreign-edition
  conflicts ("running", "sleep", "Tinder").
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import requests

from scripts import resolve_topic as rt
from tests.fixture_session import FakeResponse, FixtureSession, load_fixture

ESL = "English as a second or foreign language"


@pytest.fixture
def session() -> FixtureSession:
    return FixtureSession()


@pytest.fixture
def client(session: FixtureSession) -> rt.WikiClient:
    return rt.WikiClient(session=session, backoff_seconds=0)


def found_map(result: rt.ResolvedTopic) -> dict[str, bool]:
    return {lang: ref.found for lang, ref in result.articles.items()}


def titles(result: rt.ResolvedTopic) -> dict[str, str | None]:
    return {lang: ref.pageviews_title for lang, ref in result.articles.items()}


# --- language codes and titles -------------------------------------------------


@pytest.mark.parametrize(
    ("lang", "host", "project", "site"),
    [
        ("pl", "pl", "pl.wikipedia", "plwiki"),
        ("uk", "uk", "uk.wikipedia", "ukwiki"),
        ("nb", "no", "no.wikipedia", "nowiki"),  # Bokmål: host and Pageviews use "no"
        ("no", "no", "no.wikipedia", "nowiki"),
        ("zh-yue", "zh-yue", "zh-yue.wikipedia", "zh_yuewiki"),  # dash in host, underscore in sitelink
        ("be-tarask", "be-tarask", "be-tarask.wikipedia", "be_x_oldwiki"),  # historical site id
        ("simple", "simple", "simple.wikipedia", "simplewiki"),
    ],
)
def test_language_code_mappings(lang: str, host: str, project: str, site: str) -> None:
    assert rt.host_lang(lang) == host
    assert rt.project_for(lang) == project
    assert rt.sitelink_site(lang) == site


@pytest.mark.parametrize("raw, expected", [(" PL ", "pl"), ("Zh-Yue", "zh-yue"), ("simple", "simple")])
def test_normalize_lang_accepts_valid_codes(raw: str, expected: str) -> None:
    assert rt.normalize_lang(raw) == expected


@pytest.mark.parametrize("raw", ["", "en_US", "123", "e", "pl,cs", "en wiki"])
def test_normalize_lang_rejects_invalid_codes(raw: str) -> None:
    with pytest.raises(ValueError):
        rt.normalize_lang(raw)


def test_to_pageviews_title_only_swaps_spaces() -> None:
    assert rt.to_pageviews_title(" Intermittent fasting ") == "Intermittent_fasting"
    assert rt.to_pageviews_title("Přerušovaný půst") == "Přerušovaný_půst"
    assert rt.to_pageviews_title("علم الفلك") == "علم_الفلك"
    assert rt.to_pageviews_title("AC/DC") == "AC/DC"  # percent-encoding is the fetcher's job


def test_fold_title_ignores_case_and_accents() -> None:
    assert rt.fold_title("Přerušovaný půst") == rt.fold_title("prerusovany PUST")
    assert rt.fold_title("Otužování") == "otuzovani"
    assert rt.fold_title("Медитація") == "медитація"  # Cyrillic keeps its letters


@pytest.mark.parametrize(
    ("query", "title", "expected_high"),
    [
        ("intermittent fasting", "Intermittent fasting", True),
        ("Intermittent Fasting", "intermittent fasting", True),
        ("prerusovany pust", "Přerušovaný půst", True),  # accent-insensitive
        ("Mercury (planet)", "Mercury (planet)", True),
        ("astronomy", "Astronomi", True),  # cross-language near-miss is accepted
        ("dieta ketogeniczna", "Dieta ketogenna", True),
        ("budgeting", "Budget", True),
        ("astronomy", "Astronomy Domine", False),  # extra word: a song
        ("meditation", "Guru Meditation", False),  # extra word: an Amiga error screen
        ("meditation", "Medytacja", False),  # too different; found via sitelinks instead
        ("astronomy", "Astronomy (journal)", False),
        ("learning English", "Learning English, Lesson One", False),
        ("learning English", ESL, False),
        ("intermittent fasting", "Stres oksydacyjny", False),
        ("intermittent fasting", "Fasting", False),
        ("Tinder", "Tinder (app)", False),  # only reachable via exact match / sitelink
    ],
)
def test_title_similarity(query: str, title: str, expected_high: bool) -> None:
    score = rt.title_similarity(query, title)
    assert 0.0 <= score <= 1.0
    assert (score >= rt.MIN_TITLE_SIMILARITY) is expected_high


def test_client_sets_user_agent(session: FixtureSession) -> None:
    rt.WikiClient(session=session)
    assert session.headers["User-Agent"].startswith("wikipedia-trends-skill/")


# --- search --------------------------------------------------------------------


def test_search_parses_hits_and_strips_html(client: rt.WikiClient) -> None:
    hits = rt.search_articles(client, "en", "intermittent fasting")
    assert [h.title for h in hits][:2] == ["Intermittent fasting", "Fasting"]
    assert hits[0].pageid == 20104879 and hits[0].lang == "en"
    assert "<" not in hits[0].snippet and "Intermittent" in hits[0].snippet


def test_search_requests_main_namespace_only(client: rt.WikiClient, session: FixtureSession) -> None:
    rt.search_articles(client, "en", "intermittent fasting", limit=3)
    _, params = session.calls[-1]
    assert params["srnamespace"] == "0" and params["srlimit"] == "3"
    assert params["format"] == "json" and params["formatversion"] == "2"


def test_search_no_results() -> None:
    client = rt.WikiClient(session=FixtureSession({"pl:search:anything": "search_pl_no_results"}))
    assert rt.search_articles(client, "pl", "anything") == []


@pytest.mark.parametrize(
    ("lang", "query", "top_title"),
    [
        ("pl", "intermittent fasting", "Stres oksydacyjny"),  # citation match
        ("pl", "meditation", "Medytacja"),  # right article, but "Guru Meditation" is 3rd
        ("no", "astronomy", "Astronomy Domine"),  # a song outranks "Astronomi"
    ],
)
def test_search_full_text_traps_are_visible(client: rt.WikiClient, lang: str, query: str, top_title: str) -> None:
    hits = rt.search_articles(client, lang, query)
    assert hits[0].title == top_title and hits[0].lang == lang


def test_search_full_text_finds_real_target_behind_vague_phrase(client: rt.WikiClient) -> None:
    hits = rt.search_articles(client, "en", "learning English")
    assert ESL in [h.title for h in hits]


def test_prefix_search_uses_alias_host_for_nb(client: rt.WikiClient, session: FixtureSession) -> None:
    rt.prefix_search(client, "nb", "astronomi")
    url, _ = session.calls[-1]
    assert url.startswith("https://no.wikipedia.org/")


def test_prefix_search_parses_title_hits(client: rt.WikiClient, session: FixtureSession) -> None:
    hits = rt.prefix_search(client, "cs", "přerušovaný půst")
    assert [h.title for h in hits] == ["Přerušovaný půst"]
    assert hits[0].pageid == 1632302 and hits[0].lang == "cs"
    _, params = session.calls[-1]
    assert params["list"] == "prefixsearch" and params["psnamespace"] == "0"


def test_prefix_search_empty_when_title_absent(client: rt.WikiClient) -> None:
    assert rt.prefix_search(client, "pl", "intermittent fasting") == []
    assert rt.prefix_search(client, "pl", "post przerywany") == []


def test_prefix_search_is_accent_insensitive(client: rt.WikiClient) -> None:
    assert rt.prefix_search(client, "cs", "prerusovany pust")[0].title == "Přerušovaný půst"


def test_prefix_search_includes_redirects(client: rt.WikiClient) -> None:
    # "Astronomy" on uk.wikipedia is a redirect to "Астрономія"; prefix search still lists it.
    assert rt.prefix_search(client, "uk", "astronomy")[0].title == "Astronomy"


@pytest.mark.parametrize(
    ("lang", "query", "first_two"),
    [
        ("nb", "astronomy", ["Astronomy Domine", "Astronomi"]),
        ("pl", "running", ["Running", "Running Wild"]),
        ("cs", "Strava", ["Strava", "Strava (aplikace)"]),
    ],
)
def test_prefix_search_orders_exact_or_longer_titles_first(
    client: rt.WikiClient, lang: str, query: str, first_two: list[str]
) -> None:
    assert [h.title for h in rt.prefix_search(client, lang, query)][:2] == first_two


# --- page info / redirects / disambiguation -----------------------------------


def test_page_info_resolves_redirect(client: rt.WikiClient) -> None:
    info = rt.get_page_info(client, "en", "5:2 diet")
    assert info.title == "Intermittent fasting" and info.redirected_from == "5:2 diet"
    assert info.qid == "Q1666254" and info.pageid == 20104879
    assert info.missing is False and info.disambiguation is False


@pytest.mark.parametrize(
    ("lang", "title", "target", "qid"),
    [
        ("uk", "Astronomy", "Астрономія", "Q333"),  # cross-script redirect
        ("en", "Cold plunge", "Ice bath", "Q5985390"),  # concept-changing redirect
        ("en", "Budgeting", "Budget", "Q41263"),
        ("en", "ADHD", "Attention deficit hyperactivity disorder", "Q181923"),  # acronym redirect
    ],
)
def test_page_info_redirect_targets(client: rt.WikiClient, lang: str, title: str, target: str, qid: str) -> None:
    info = rt.get_page_info(client, lang, title)
    assert info.title == target and info.redirected_from == title and info.qid == qid


def test_page_info_direct_has_no_redirect(client: rt.WikiClient) -> None:
    info = rt.get_page_info(client, "cs", "Přerušovaný půst")
    assert info.redirected_from is None and info.qid == "Q1666254"


def test_page_info_normalises_title_without_redirect(client: rt.WikiClient) -> None:
    info = rt.get_page_info(client, "en", "intermittent_fasting")
    assert info.title == "Intermittent fasting" and info.redirected_from is None
    assert info.qid == "Q1666254"


def test_page_info_missing_page(client: rt.WikiClient) -> None:
    info = rt.get_page_info(client, "pl", "Xqzvv qwrtyplk zzz")
    assert info.missing is True and info.qid is None and info.pageid is None


@pytest.mark.parametrize(
    "lang, title",
    [("en", "Mercury"), ("pl", "Mercury"), ("en", "Learning English"), ("en", "Keto diet"), ("pl", "Sleep"), ("cs", "Strava")],
)
def test_page_info_flags_disambiguation(client: rt.WikiClient, lang: str, title: str) -> None:
    info = rt.get_page_info(client, lang, title)
    assert info.disambiguation is True
    assert info.qid is not None  # disambiguation pages have Wikidata items too


@pytest.mark.parametrize(
    "lang, title, pageid",
    [("ja", "天文学", 988027), ("ar", "علم الفلك", 1581), ("zh-yue", "天文學", 33210), ("no", "Astronomi", 494109)],
)
def test_page_info_non_latin_and_alias_hosts(client: rt.WikiClient, lang: str, title: str, pageid: int) -> None:
    info = rt.get_page_info(client, lang, title)
    assert info.title == title and info.pageid == pageid and info.qid == "Q333"


def test_page_info_requests_disambiguation_prop(client: rt.WikiClient, session: FixtureSession) -> None:
    rt.get_page_info(client, "en", "5:2 diet")
    _, params = session.calls[-1]
    assert params["redirects"] == "1"
    assert set(params["ppprop"].split("|")) == {"wikibase_item", "disambiguation"}


# --- Wikidata ------------------------------------------------------------------


def test_sitelinks_map_lang_to_title(client: rt.WikiClient) -> None:
    links, label = rt.get_sitelinks(client, "Q1666254", ["pl", "cs", "en", "uk"])
    assert links == {"cs": "Přerušovaný půst", "en": "Intermittent fasting", "uk": "Інтервальне голодування"}
    assert "pl" not in links  # no Polish article at recording time
    assert label == "intermittent fasting"


def test_sitelinks_many_scripts_and_aliases(client: rt.WikiClient) -> None:
    links, label = rt.get_sitelinks(client, "Q333", ["ar", "he", "ja", "zh-yue", "be-tarask", "nb", "simple", "uk"])
    assert links == {
        "ar": "علم الفلك",
        "he": "אסטרונומיה",
        "ja": "天文学",
        "zh-yue": "天文學",
        "be-tarask": "Астраномія",
        "nb": "Astronomi",  # keyed by the user's code, read from "nowiki"
        "simple": "Astronomy",
        "uk": "Астрономія",
    }
    assert label == "astronomy"


def test_sitelinks_use_sitefilter_with_aliases(client: rt.WikiClient, session: FixtureSession) -> None:
    rt.get_sitelinks(client, "Q333", ["pl", "nb", "zh-yue", "be-tarask"])
    _, params = session.calls[-1]
    assert params["sitefilter"] == "plwiki|nowiki|zh_yuewiki|be_x_oldwiki"
    assert params["languages"] == "en|mul"


def test_sitelinks_label_falls_back_to_mul(client: rt.WikiClient) -> None:
    _, label = rt.get_sitelinks(client, "Q110367996", ["pl"])  # Wordle has no English label
    assert label == "Wordle"


def test_sitelinks_missing_item_raises() -> None:
    class MissingSession:
        headers: dict[str, str] = {}

        def get(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
            return FakeResponse({"entities": {"Q1": {"id": "Q1", "missing": ""}}})

    with pytest.raises(rt.WikiApiError):
        rt.get_sitelinks(rt.WikiClient(session=MissingSession()), "Q1", ["en"])


def test_describe_items(client: rt.WikiClient) -> None:
    assert rt.describe_items(client, ["Q308"])["Q308"].startswith("Mercury — first planet")
    described = rt.describe_items(client, ["Q15796039", "Q105674"])
    assert "song" in described["Q15796039"] and described["Q105674"].startswith("running")


@pytest.mark.parametrize(
    ("lang", "title", "expected_link"),
    [
        ("en", "Keto diet", "Ketogenic diet"),
        ("en", "Mercury", "Mercury (planet)"),  # 113 links; wikitext order puts the planet first
        ("en", "Learning English", "BBC Learning English"),  # does NOT link to the ESL article
    ],
)
def test_disambiguation_links(client: rt.WikiClient, lang: str, title: str, expected_link: str) -> None:
    hits = rt.get_disambiguation_links(client, lang, title)
    assert expected_link in [h.title for h in hits]
    assert len(hits) <= 15
    assert all(h.lang == lang and "disambiguation" in h.snippet for h in hits)


def test_disambiguation_links_follow_page_order(client: rt.WikiClient) -> None:
    hits = [h.title for h in rt.get_disambiguation_links(client, "en", "Mercury")]
    assert hits[:2] == ["Mercury (planet)", "Mercury (element)"]  # editors list the main meanings first
    assert not any(t.startswith(("File:", "Category:", "Wikipedia:")) for t in hits)


# --- resolve_topic: scenario table ---------------------------------------------


@pytest.mark.parametrize(
    ("topic", "langs", "kwargs", "qid", "seed", "expected_found"),
    [
        # education
        ("intermittent fasting", ["en", "cs", "pl"], {}, "Q1666254", ("en", "Intermittent fasting"), {"en": True, "cs": True, "pl": False}),
        ("intermittent fasting", ["pl", "en"], {}, "Q1666254", ("en", "Intermittent fasting"), {"pl": False, "en": True}),
        ("přerušovaný půst", ["cs", "en"], {}, "Q1666254", ("cs", "Přerušovaný půst"), {"cs": True, "en": True}),
        ("prerusovany pust", ["cs", "pl"], {}, "Q1666254", ("cs", "Přerušovaný půst"), {"cs": True, "pl": False}),
        ("astronomy", ["uk", "pl", "cs", "nb"], {}, "Q333", ("uk", "Астрономія"), {"uk": True, "pl": True, "cs": True, "nb": True}),
        ("astronomy", ["nb", "en"], {}, "Q333", ("en", "Astronomy"), {"nb": True, "en": True}),
        ("astronomy", ["ja", "uk"], {"seed_lang": "uk"}, "Q333", ("uk", "Астрономія"), {"ja": True, "uk": True}),
        ("Mercury (planet)", ["en", "pl", "cs"], {}, "Q308", ("en", "Mercury (planet)"), {"en": True, "pl": True, "cs": True}),
        (ESL, ["en", "de", "pl", "cs"], {}, "Q130192", ("en", ESL), {"en": True, "de": True, "pl": False, "cs": False}),
        # B2C: diet / fitness / wellness
        ("Ketogenic diet", ["en", "pl", "cs", "uk"], {}, "Q1070684", ("en", "Ketogenic diet"), {"en": True, "pl": False, "cs": True, "uk": True}),
        ("meditation", ["pl", "cs", "uk"], {}, "Q108458", ("en", "Meditation"), {"pl": True, "cs": True, "uk": True}),
        ("cold plunge", ["en", "pl", "cs"], {}, "Q5985390", ("en", "Ice bath"), {"en": True, "pl": False, "cs": True}),
        ("otužování", ["cs", "pl", "en"], {}, "Q2714298", ("cs", "Otužování"), {"cs": True, "pl": False, "en": False}),
        ("pilates", ["pl", "cs", "uk"], {}, "Q272460", ("pl", "Pilates"), {"pl": True, "cs": True, "uk": True}),
        ("pickleball", ["pl", "cs", "de"], {}, "Q866224", ("cs", "Pickleball"), {"pl": False, "cs": True, "de": True}),
        ("running", ["pl", "cs"], {"seed_lang": "en"}, "Q105674", ("en", "Running"), {"pl": False, "cs": True}),
        ("sleep", ["pl", "cs"], {"seed_lang": "en"}, "Q35831", ("en", "Sleep"), {"pl": True, "cs": True}),
        # B2C: health / finance / apps / brands
        ("ADHD", ["pl", "uk", "en"], {}, "Q181923", ("pl", "ADHD"), {"pl": True, "uk": True, "en": True}),
        ("budgeting", ["pl", "cs", "en"], {}, "Q41263", ("en", "Budget"), {"pl": True, "cs": True, "en": True}),
        ("Tinder", ["pl", "cs", "en"], {"qid": "Q15078152"}, "Q15078152", (None, None), {"pl": True, "cs": True, "en": True}),
        ("Strava", ["cs", "pl", "en"], {}, "Q13884156", ("pl", "Strava"), {"cs": True, "pl": True, "en": True}),
        ("Wordle", ["pl", "cs", "uk"], {}, "Q110367996", ("pl", "Wordle"), {"pl": True, "cs": True, "uk": True}),
        ("AC/DC", ["pl", "en"], {}, "Q27593", ("pl", "AC/DC"), {"pl": True, "en": True}),
    ],
)
def test_resolve_topic_scenarios(
    client: rt.WikiClient,
    topic: str,
    langs: list[str],
    kwargs: dict[str, str],
    qid: str,
    seed: tuple[str | None, str | None],
    expected_found: dict[str, bool],
) -> None:
    result = rt.resolve_topic(client, topic, langs, **kwargs)
    assert result.ambiguous is False and result.qid == qid
    assert (result.seed_lang, result.seed_title) == seed
    assert found_map(result) == expected_found
    assert list(result.articles) == langs  # output order follows the request
    for ref in result.articles.values():
        assert ref.project == rt.project_for(ref.lang)
        if ref.found:
            assert ref.pageviews_title and " " not in ref.pageviews_title and ref.pageid and ref.qid
        else:
            assert ref.reason and ref.pageviews_title is None
    missing = [lang for lang, ok in expected_found.items() if not ok]
    if missing:
        assert any(f"No article in: {', '.join(missing)}" in c for c in result.caveats)
    # every result is JSON-serialisable for the agent
    data = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))
    assert data["qid"] == qid and isinstance(data["caveats"], list)


@pytest.mark.parametrize(
    ("topic", "langs", "kwargs", "expected_titles"),
    [
        ("astronomy", ["uk", "ja", "ar", "zh-yue", "nb", "en"], {}, {
            "uk": "Астрономія", "ja": "天文学", "ar": "علم_الفلك", "zh-yue": "天文學", "nb": "Astronomi", "en": "Astronomy",
        }),
        ("Mercury (planet)", ["en", "pl", "cs"], {}, {"en": "Mercury_(planet)", "pl": "Merkury", "cs": "Merkur_(planeta)"}),
        ("Ketogenic diet", ["en", "cs", "uk"], {}, {"en": "Ketogenic_diet", "cs": "Ketogenní_dieta", "uk": "Кетогенна_дієта"}),
        ("meditation", ["pl", "cs", "uk"], {}, {"pl": "Medytacja", "cs": "Meditace", "uk": "Медитація"}),
        ("cold plunge", ["en", "cs"], {}, {"en": "Ice_bath", "cs": "Ice_bathing"}),  # cs article has an English title
        ("sleep", ["pl", "cs"], {"seed_lang": "en"}, {"pl": "Sen", "cs": "Spánek"}),
        ("ADHD", ["pl", "uk", "en"], {}, {
            "pl": "ADHD", "uk": "Розлад_дефіциту_уваги_та_гіперактивності", "en": "Attention_deficit_hyperactivity_disorder",
        }),
        ("budgeting", ["pl", "cs", "en"], {}, {"pl": "Budżet", "cs": "Rozpočet", "en": "Budget"}),
        ("Tinder", ["pl", "cs", "en"], {"qid": "Q15078152"}, {"pl": "Tinder", "cs": "Tinder", "en": "Tinder_(app)"}),
        ("Strava", ["cs", "pl", "en"], {}, {"cs": "Strava_(aplikace)", "pl": "Strava", "en": "Strava"}),
        ("AC/DC", ["pl", "en"], {}, {"pl": "AC/DC", "en": "AC/DC"}),
    ],
)
def test_resolve_topic_pageviews_titles(
    client: rt.WikiClient, topic: str, langs: list[str], kwargs: dict[str, str], expected_titles: dict[str, str]
) -> None:
    result = rt.resolve_topic(client, topic, langs, **kwargs)
    assert titles(result) == expected_titles


def test_resolve_topic_many_languages_and_scripts(client: rt.WikiClient) -> None:
    result = rt.resolve_topic(client, "astronomy", ["uk", "ja", "ar", "zh-yue", "nb", "en"])
    assert result.qid == "Q333" and result.label == "astronomy"
    assert result.seed_lang == "uk" and result.seed_title == "Астрономія"
    assert result.seed_redirected_from == "Astronomy"
    assert result.articles["nb"].project == "no.wikipedia"
    assert result.articles["zh-yue"].project == "zh-yue.wikipedia"
    assert not any("No article in" in c for c in result.caveats)


def test_resolve_topic_labels(client: rt.WikiClient) -> None:
    assert rt.resolve_topic(client, "Wordle", ["pl"]).label == "Wordle"  # mul label
    assert rt.resolve_topic(client, "otužování", ["cs"]).label == "dousing"  # Wikidata's English label
    assert rt.resolve_topic(client, "ADHD", ["pl"]).label == "attention deficit hyperactivity disorder"


# --- resolve_topic: seed selection ---------------------------------------------


def test_resolve_topic_falls_back_to_english_seed(client: rt.WikiClient, session: FixtureSession) -> None:
    result = rt.resolve_topic(client, "intermittent fasting", ["pl", "en"])
    assert result.seed_lang == "en" and result.articles["en"].found
    assert session.search_keys() == [
        "pl:prefix:intermittent fasting",
        "pl:search:intermittent fasting",
        "en:prefix:intermittent fasting",
    ]


def test_resolve_topic_rejects_full_text_citation_match(client: rt.WikiClient) -> None:
    # "intermittent fasting" full-text-searched on pl.wikipedia returned "Stres oksydacyjny"
    # (oxidative stress) because a citation mentioned the phrase.
    result = rt.resolve_topic(client, "intermittent fasting", ["pl", "en"])
    assert result.seed_title == "Intermittent fasting" and not result.articles["pl"].found
    rejected = [c for c in result.candidates if c.title == "Stres oksydacyjny"]
    assert rejected and rejected[0].lang == "pl"
    assert any("Stres oksydacyjny (pl)" in c for c in result.caveats)


def test_resolve_topic_rejects_prefix_false_positive_song(client: rt.WikiClient, session: FixtureSession) -> None:
    # "astronomy" on no.wikipedia prefix-matches "Astronomy Domine" (a song), which also tops
    # full-text search. English wins; the Norwegian article comes back through the sitelink.
    result = rt.resolve_topic(client, "astronomy", ["nb", "en"])
    assert result.seed_lang == "en" and result.articles["nb"].title == "Astronomi"
    assert session.search_keys() == ["no:prefix:astronomy", "no:search:astronomy", "en:prefix:astronomy"]
    assert "Astronomy Domine" in [c.title for c in result.candidates]


def test_resolve_topic_rejects_full_text_containing_match(client: rt.WikiClient, session: FixtureSession) -> None:
    # Regression: "meditation" on pl.wikipedia used to resolve to "Guru Meditation" (the Amiga
    # error screen) because its title contains the query. Now all three editions are skipped
    # and the English article maps back to "Medytacja" via Wikidata.
    result = rt.resolve_topic(client, "meditation", ["pl", "cs", "uk"])
    assert result.seed_lang == "en" and result.qid == "Q108458"
    assert result.articles["pl"].title == "Medytacja"
    assert session.search_keys()[-1] == "en:prefix:meditation"
    assert "Guru Meditation" in [c.title for c in result.candidates if c.lang == "pl"]


def test_resolve_topic_accent_insensitive_exact_match(client: rt.WikiClient, session: FixtureSession) -> None:
    result = rt.resolve_topic(client, "prerusovany pust", ["cs", "pl"])
    assert result.seed_lang == "cs" and result.seed_title == "Přerušovaný půst"
    assert "cs:search:prerusovany pust" not in session.keys()  # exact prefix hit, no full-text needed


def test_resolve_topic_prefers_prefix_hit_over_full_text(client: rt.WikiClient, session: FixtureSession) -> None:
    rt.resolve_topic(client, "intermittent fasting", ["en"])
    assert not any(":search:" in k for k in session.keys())


def test_resolve_topic_uses_first_requested_language_as_seed(client: rt.WikiClient, session: FixtureSession) -> None:
    result = rt.resolve_topic(client, "přerušovaný půst", ["cs", "en"])
    assert result.seed_lang == "cs" and result.articles["en"].title == "Intermittent fasting"
    # English cross-check ran (one prefix call) and found nothing
    assert session.search_keys() == ["cs:prefix:přerušovaný půst", "en:prefix:přerušovaný půst"]


def test_resolve_topic_explicit_seed_lang_skips_cross_check(client: rt.WikiClient, session: FixtureSession) -> None:
    result = rt.resolve_topic(client, "astronomy", ["ja", "uk"], seed_lang="uk")
    assert result.seed_lang == "uk"
    assert session.search_keys() == ["uk:prefix:astronomy"]


def test_resolve_topic_with_explicit_qid_skips_search(client: rt.WikiClient, session: FixtureSession) -> None:
    result = rt.resolve_topic(client, "whatever", ["cs"], qid="Q1666254")
    assert result.candidates == [] and result.articles["cs"].found
    assert session.search_keys() == []


def test_resolve_topic_reports_not_found() -> None:
    routes = {
        "pl:prefix:intermittent fasting": "prefix_pl_intermittent_fasting",
        "pl:search:intermittent fasting": "search_pl_intermittent_fasting",  # only non-matching hits
        "en:prefix:intermittent fasting": "prefix_pl_intermittent_fasting",  # reuse an empty payload
        "en:search:intermittent fasting": "search_pl_no_results",
    }
    result = rt.resolve_topic(rt.WikiClient(session=FixtureSession(routes)), "intermittent fasting", ["pl"])
    assert result.qid is None and not result.articles["pl"].found
    assert "No article whose title matches" in result.caveats[0]
    assert result.candidates[0].title == "Stres oksydacyjny"


# --- resolve_topic: redirects --------------------------------------------------


@pytest.mark.parametrize(
    ("topic", "langs", "redirected_from", "seed_title"),
    [
        ("cold plunge", ["en", "cs"], "Cold plunge", "Ice bath"),
        ("budgeting", ["pl", "cs", "en"], "Budgeting", "Budget"),
        ("astronomy", ["uk", "en"], "Astronomy", "Астрономія"),
    ],
)
def test_resolve_topic_reports_seed_redirect(
    client: rt.WikiClient, topic: str, langs: list[str], redirected_from: str, seed_title: str
) -> None:
    result = rt.resolve_topic(client, topic, langs)
    assert result.seed_redirected_from == redirected_from and result.seed_title == seed_title
    assert any(f"'{redirected_from}' is a redirect to '{seed_title}'" in c for c in result.caveats)


def test_resolve_topic_acronym_redirect_agrees_across_editions(client: rt.WikiClient, session: FixtureSession) -> None:
    # pl "ADHD" is the article title; en "ADHD" redirects to the long title of the same item,
    # so the cross-check finds no conflict.
    result = rt.resolve_topic(client, "ADHD", ["pl", "uk", "en"])
    assert result.ambiguous is False and result.seed_lang == "pl"
    assert session.search_keys() == ["pl:prefix:ADHD", "en:prefix:ADHD"]
    assert result.articles["en"].pageviews_title == "Attention_deficit_hyperactivity_disorder"


# --- resolve_topic: ambiguity -------------------------------------------------


def test_resolve_topic_disambiguation_page_is_ambiguous(client: rt.WikiClient, session: FixtureSession) -> None:
    result = rt.resolve_topic(client, "mercury", ["en", "pl"])
    assert result.ambiguous is True and result.qid is None
    assert result.seed_title == "Mercury"
    assert all(not ref.found and ref.reason == "ambiguous topic" for ref in result.articles.values())
    candidate_titles = [c.title for c in result.candidates]
    assert "Mercury (planet)" in candidate_titles and "Mercury (element)" in candidate_titles
    assert not any(k.startswith("wikidata:") for k in session.keys())
    caveat = result.caveats[0]
    assert "disambiguation page" in caveat and "Mercury (planet) (en)" in caveat and "--qid" in caveat
    assert "'Mercury' on pl.wikipedia" in caveat  # both editions were disambiguation pages


@pytest.mark.parametrize(
    ("topic", "langs", "real_target"),
    [
        ("learning English", ["pl", "en"], ESL),
        ("keto diet", ["en", "pl"], "Ketogenic diet"),
    ],
)
def test_resolve_topic_ambiguous_lists_disambiguation_targets(
    client: rt.WikiClient, session: FixtureSession, topic: str, langs: list[str], real_target: str
) -> None:
    # Full-text search for "keto diet" never returns "Ketogenic diet"; the disambiguation
    # page's own links do. Both sources feed `candidates`.
    result = rt.resolve_topic(client, topic, langs)
    assert result.ambiguous is True
    assert real_target in [c.title for c in result.candidates]
    assert f"en:search:{topic}" in session.keys()
    assert any(k.startswith("en:content:") for k in session.keys())
    assert any(real_target in c for c in result.caveats) or len(result.candidates) > rt.MAX_CAVEAT_CANDIDATES


def test_resolve_topic_skips_disambiguation_and_continues(client: rt.WikiClient) -> None:
    # cs "Strava" means food (disambiguation); pl "Strava" is the app and en agrees.
    result = rt.resolve_topic(client, "Strava", ["cs", "pl", "en"])
    assert result.ambiguous is False and result.qid == "Q13884156" and result.seed_lang == "pl"
    assert result.articles["cs"].pageviews_title == "Strava_(aplikace)"
    assert any("'Strava' on cs.wikipedia is a disambiguation page; used 'Strava' on pl.wikipedia" in c for c in result.caveats)


@pytest.mark.parametrize(
    ("topic", "langs", "seed_qid", "en_qid", "seed_lang", "hint"),
    [
        ("running", ["pl", "en"], "Q15796039", "Q105674", "pl", "song"),
        ("Tinder", ["pl", "en"], "Q15078152", "Q143518", "pl", "dating"),
        ("sleep", ["pl", "cs", "en"], "Q693387", "Q35831", "cs", "shell command"),
    ],
)
def test_resolve_topic_cross_edition_conflict_is_ambiguous(
    client: rt.WikiClient,
    session: FixtureSession,
    topic: str,
    langs: list[str],
    seed_qid: str,
    en_qid: str,
    seed_lang: str,
    hint: str,
) -> None:
    result = rt.resolve_topic(client, topic, langs)
    assert result.ambiguous is True and result.qid is None
    assert result.seed_lang == seed_lang
    assert all(ref.reason == "ambiguous topic" for ref in result.articles.values())
    caveat = result.caveats[0]
    assert seed_qid in caveat and en_qid in caveat and hint in caveat
    assert f"--seed-lang {seed_lang}" in caveat and "--seed-lang en" in caveat
    assert f"wikidata:describe{seed_qid}|{en_qid}" in session.keys()
    assert not any(k == f"wikidata:{seed_qid}" for k in session.keys())  # no sitelinks fetched


def test_resolve_topic_conflict_recovery_with_seed_lang(client: rt.WikiClient) -> None:
    result = rt.resolve_topic(client, "running", ["pl", "cs"], seed_lang="en")
    assert result.ambiguous is False and result.qid == "Q105674"
    assert result.articles["cs"].pageviews_title == "Běh"
    assert not result.articles["pl"].found  # pl's running article hangs off a different item


def test_resolve_topic_conflict_recovery_with_qid(client: rt.WikiClient) -> None:
    result = rt.resolve_topic(client, "Tinder", ["pl", "cs", "en"], qid="Q15078152")
    assert result.ambiguous is False and result.articles["en"].pageviews_title == "Tinder_(app)"


def test_resolve_topic_ambiguous_recovery_with_exact_title(client: rt.WikiClient) -> None:
    result = rt.resolve_topic(client, "Mercury (planet)", ["en", "pl"])
    assert result.ambiguous is False and result.qid == "Q308"
    assert result.articles["pl"].pageviews_title == "Merkury"


def test_resolve_topic_qid_of_disambiguation_item_marks_every_language(client: rt.WikiClient) -> None:
    result = rt.resolve_topic(client, "mercury", ["en", "pl"], qid="Q48397")
    for ref in result.articles.values():
        assert not ref.found and "disambiguation" in (ref.reason or "")
    assert any("No article in: en, pl" in c for c in result.caveats)


# --- validation and serialisation ---------------------------------------------


def test_resolve_topic_rejects_empty_langs(client: rt.WikiClient) -> None:
    with pytest.raises(ValueError):
        rt.resolve_topic(client, "x", [" "])


def test_resolve_topic_rejects_invalid_and_duplicate_langs(client: rt.WikiClient) -> None:
    with pytest.raises(ValueError, match="invalid language code"):
        rt.resolve_topic(client, "x", ["en_US"])
    with pytest.raises(ValueError, match="duplicate"):
        rt.resolve_topic(client, "x", ["en", "EN"])


def test_result_serialises_to_json(client: rt.WikiClient) -> None:
    result = rt.resolve_topic(client, "přerušovaný půst", ["cs", "pl"])
    data = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))
    assert data["articles"]["cs"]["pageviews_title"] == "Přerušovaný_půst"
    assert data["articles"]["pl"]["found"] is False
    assert data["ambiguous"] is False and data["seed_redirected_from"] is None


# --- client retry / failure behaviour -----------------------------------------


def test_client_retries_on_429_and_honours_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(rt.time, "sleep", sleeps.append)

    class FlakySession:
        headers: dict[str, str] = {}
        calls = 0

        def get(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
            self.calls += 1
            if self.calls == 1:
                return FakeResponse({}, status_code=429, headers={"Retry-After": "7"})
            return FakeResponse(load_fixture("pageinfo_en_direct"))

    session = FlakySession()
    info = rt.get_page_info(rt.WikiClient(session=session, backoff_seconds=1), "en", "Intermittent fasting")
    assert info.qid == "Q1666254" and session.calls == 2 and sleeps == [7.0]


def test_client_gives_up_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rt.time, "sleep", lambda _s: None)

    class DeadSession:
        headers: dict[str, str] = {}

        def get(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
            return FakeResponse({}, status_code=503)

    with pytest.raises(rt.WikiApiError, match="3 attempts"):
        rt.WikiClient(session=DeadSession(), max_retries=2).get("https://en.wikipedia.org/w/api.php", {})


def test_client_fails_fast_on_unknown_host(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(rt.time, "sleep", sleeps.append)

    class NoDnsSession:
        headers: dict[str, str] = {}
        calls = 0

        def get(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
            self.calls += 1
            raise requests.ConnectionError(
                "HTTPSConnectionPool(host='xx.wikipedia.org', port=443): Max retries exceeded "
                "(Caused by NameResolutionError(...))"
            )

    session = NoDnsSession()
    with pytest.raises(rt.WikiApiError, match="xx.wikipedia.org"):
        rt.prefix_search(rt.WikiClient(session=session), "xx", "astronomy")
    assert session.calls == 1 and sleeps == []


def test_client_raises_on_api_error_payload() -> None:
    class ErrorSession:
        headers: dict[str, str] = {}

        def get(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
            return FakeResponse({"error": {"code": "badvalue", "info": "Unrecognized value"}})

    with pytest.raises(rt.WikiApiError, match="badvalue"):
        rt.WikiClient(session=ErrorSession()).get("https://en.wikipedia.org/w/api.php", {})


# --- CLI -----------------------------------------------------------------------


def test_cli_prints_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    real_client = rt.WikiClient
    monkeypatch.setattr(rt, "WikiClient", lambda: real_client(session=FixtureSession()))
    code = rt.main(["intermittent fasting", "--langs", "en,cs,pl"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["qid"] == "Q1666254"
    assert out["articles"]["cs"]["title"] == "Přerušovaný půst"


def test_cli_seed_lang_and_qid_flags(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    real_client = rt.WikiClient
    monkeypatch.setattr(rt, "WikiClient", lambda: real_client(session=FixtureSession()))
    assert rt.main(["running", "--langs", "pl,cs", "--seed-lang", "en"]) == 0
    assert json.loads(capsys.readouterr().out)["qid"] == "Q105674"
    assert rt.main(["Tinder", "--langs", "pl,cs,en", "--qid", "Q15078152"]) == 0
    assert json.loads(capsys.readouterr().out)["articles"]["en"]["pageviews_title"] == "Tinder_(app)"


def test_cli_reports_errors_as_json(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    real_client = rt.WikiClient
    monkeypatch.setattr(rt, "WikiClient", lambda: real_client(session=FixtureSession()))
    code = rt.main(["x", "--langs", "en_US"])
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and "invalid language code" in out["error"]


def test_cli_out_writes_utf8_file(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path) -> None:
    real_client = rt.WikiClient
    monkeypatch.setattr(rt, "WikiClient", lambda: real_client(session=FixtureSession()))
    out = tmp_path / "resolved.json"
    assert rt.main(["intermittent fasting", "--langs", "en,cs,pl", "--out", str(out)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["written"] == str(out) and summary["found"] == {"en": True, "cs": True, "pl": False}
    raw = out.read_bytes()
    assert not raw.startswith((b"\xff\xfe", b"\xef\xbb\xbf"))
    assert json.loads(raw.decode("utf-8"))["articles"]["cs"]["title"] == "Přerušovaný půst"
