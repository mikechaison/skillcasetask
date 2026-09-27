"""Resolve a topic to concrete Wikipedia article titles across language editions.

Pipeline:
1. Search the topic in a *seed* language edition: title-prefix search first
   (accepted only on an exact, accent- and case-insensitive title match), then
   full-text search whose best hit must have a similar title.
2. Take the hit, resolve redirects, and read its Wikidata item id (QID).
   Disambiguation pages are skipped; if nothing else matches, the topic is
   reported as *ambiguous* with candidates instead of a guess.
3. If the seed came from a non-English edition, check whether English has an
   exact-title article for a *different* item (an English word matched a
   foreign-titled song/brand): if so, report both and let the agent decide.
4. Ask Wikidata for the item's sitelinks -> the canonical article title in every
   requested language edition.  Editions with no sitelink are reported as
   ``found: false`` (no article != no interest).
5. Resolve redirects for every sitelinked title and return the final titles in
   both display form and Pageviews-API form (underscores).

Article titles are never translated literally; the cross-language mapping comes
only from Wikidata sitelinks.

Usage (CLI):

    python scripts/resolve_topic.py "intermittent fasting" --langs pl,cs,en

Usage (library):

    >>> from scripts.resolve_topic import WikiClient, resolve_topic
    >>> result = resolve_topic(WikiClient(), "intermittent fasting", ["pl", "cs"])
    >>> result.articles["cs"].title
    'Přerušovaný půst'
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
import unicodedata
from pathlib import Path
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

import requests

WIKIPEDIA_API = "https://{lang}.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
# Wikimedia asks for a descriptive User-Agent with contact info; anonymous UAs get 403/429.
USER_AGENT = "wikipedia-trends-skill/0.1 (https://github.com/wikipedia-trends; contact: projectforsmth@gmail.com)"
FALLBACK_SEED_LANG = "en"
# A full-text hit is accepted as the seed only if its title is at least this similar to the query.
# 0.8 keeps "astronomi" (0.89) and rejects "Astronomy Domine" (0.36 after the extra-word penalty).
MIN_TITLE_SIMILARITY = 0.8
# How many alternative search hits to name in the caveat text (the full list is always in `candidates`).
MAX_CAVEAT_CANDIDATES = 6

# Language codes whose Wikipedia host / Pageviews project differ from the code users type.
# Verified live: pageviews accepts no.wikipedia, zh-yue.wikipedia, be-tarask.wikipedia; nb.wikipedia -> 404.
HOST_ALIASES = {"nb": "no"}
# Wikidata sitelink site ids that are not simply "<host lang with - -> _>wiki".
SITELINK_ALIASES = {"nb": "nowiki", "no": "nowiki", "be-tarask": "be_x_oldwiki"}
# Wikipedia subdomains: "pl", "simple", "zh-yue", "zh-min-nan", "be-tarask", "roa-rup", ...
_LANG_CODE_RE = re.compile(r"^[a-z]{2,10}(?:-[a-z]{2,10})*$")
_COMMON_PARAMS = {"format": "json", "formatversion": "2"}
_TAG_RE = re.compile(r"<[^>]+>")
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_DNS_FAILURE_MARKERS = ("NameResolutionError", "getaddrinfo", "Name or service not known", "nodename nor servname")


class WikiApiError(RuntimeError):
    """Raised when the MediaWiki/Wikidata API returns an error payload or keeps failing."""


class WikiClient:
    """Thin HTTP client for MediaWiki-style APIs with retry on 429/5xx.

    Inject a fake ``session`` (anything with ``.get(url, params=, timeout=)``)
    in tests to serve recorded fixtures instead of hitting the network.
    DNS failures (an unknown language code) fail fast instead of retrying.

    >>> client = WikiClient()
    >>> payload = client.get("https://en.wikipedia.org/w/api.php", {"action": "query", "meta": "siteinfo"})
    >>> payload["query"]["general"]["sitename"]
    'Wikipedia'
    """

    def __init__(
        self,
        session: Optional[Any] = None,
        user_agent: str = USER_AGENT,
        max_retries: int = 3,
        backoff_seconds: float = 2.0,
        timeout: float = 30.0,
    ) -> None:
        self.session = session or requests.Session()
        if hasattr(self.session, "headers"):
            self.session.headers["User-Agent"] = user_agent
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.timeout = timeout

    def get(self, url: str, params: dict[str, str]) -> dict[str, Any]:
        """GET ``url`` with ``params`` (plus format=json) and return the parsed JSON."""
        full_params = {**_COMMON_PARAMS, **params}
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(url, params=full_params, timeout=self.timeout)
            except requests.ConnectionError as exc:
                if any(marker in str(exc) for marker in _DNS_FAILURE_MARKERS):
                    host = url.split("/")[2]
                    raise WikiApiError(
                        f"Cannot resolve host {host}. Is the language code a real Wikipedia edition?"
                    ) from exc
                last_error = exc
                self._sleep(attempt, None)
                continue
            except requests.RequestException as exc:  # timeout etc.: retry
                last_error = exc
                self._sleep(attempt, None)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last_error = WikiApiError(f"HTTP {response.status_code} from {url}")
                self._sleep(attempt, response.headers.get("Retry-After"))
                continue
            response.raise_for_status()
            payload = response.json()
            if "error" in payload:
                raise WikiApiError(f"{payload['error'].get('code')}: {payload['error'].get('info')}")
            return payload
        raise WikiApiError(f"Giving up after {self.max_retries + 1} attempts: {last_error}")

    def _sleep(self, attempt: int, retry_after: Optional[str]) -> None:
        delay = self.backoff_seconds * (2**attempt)
        if retry_after and retry_after.isdigit():
            delay = max(delay, float(retry_after))
        time.sleep(delay)


@dataclass
class SearchHit:
    """One result of the MediaWiki search APIs."""

    title: str
    pageid: int
    snippet: str
    lang: str


@dataclass
class PageInfo:
    """A page after redirect resolution, with its Wikidata item id if any."""

    lang: str
    title: str
    pageid: Optional[int]
    qid: Optional[str]
    redirected_from: Optional[str]
    missing: bool
    disambiguation: bool = False


@dataclass
class ArticleRef:
    """Final per-language article reference ready for the Pageviews API."""

    lang: str
    project: str
    found: bool
    title: Optional[str] = None
    pageviews_title: Optional[str] = None
    pageid: Optional[int] = None
    qid: Optional[str] = None
    redirected_from: Optional[str] = None
    reason: Optional[str] = None
    redirects: list[str] = field(default_factory=list)  # titles redirecting here (former names, aliases)


@dataclass
class ResolvedTopic:
    """Output of :func:`resolve_topic` (serialised to JSON for the agent)."""

    topic: str
    qid: Optional[str]
    label: Optional[str]
    seed_lang: Optional[str]
    seed_title: Optional[str]
    seed_redirected_from: Optional[str] = None
    ambiguous: bool = False
    candidates: list[SearchHit] = field(default_factory=list)
    articles: dict[str, ArticleRef] = field(default_factory=dict)
    caveats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict form suitable for ``json.dump``."""
        return asdict(self)


# --- language codes and titles ------------------------------------------------


def normalize_lang(lang: str) -> str:
    """Lower-case and validate a Wikipedia language code.

    >>> normalize_lang(" Zh-Yue ")
    'zh-yue'
    >>> normalize_lang("en_US")
    Traceback (most recent call last):
    ValueError: invalid language code 'en_US' (expected e.g. 'pl', 'zh-yue')
    """
    code = lang.strip().lower()
    if not _LANG_CODE_RE.match(code):
        raise ValueError(f"invalid language code {lang.strip()!r} (expected e.g. 'pl', 'zh-yue')")
    return code


def host_lang(lang: str) -> str:
    """Subdomain of the Wikipedia edition for ``lang`` (``nb`` lives at ``no.wikipedia.org``).

    >>> host_lang("nb"), host_lang("uk")
    ('no', 'uk')
    """
    return HOST_ALIASES.get(lang, lang)


def project_for(lang: str) -> str:
    """Pageviews project name for a language edition.

    >>> project_for("pl"), project_for("nb"), project_for("zh-yue")
    ('pl.wikipedia', 'no.wikipedia', 'zh-yue.wikipedia')
    """
    return f"{host_lang(lang)}.wikipedia"


def sitelink_site(lang: str) -> str:
    """Wikidata sitelink id for a language edition.

    >>> sitelink_site("pl"), sitelink_site("nb"), sitelink_site("zh-yue"), sitelink_site("be-tarask")
    ('plwiki', 'nowiki', 'zh_yuewiki', 'be_x_oldwiki')
    """
    return SITELINK_ALIASES.get(lang) or host_lang(lang).replace("-", "_") + "wiki"


def to_pageviews_title(title: str) -> str:
    """Convert a display title to the form the Pageviews REST API expects.

    Only spaces become underscores; URL-encoding of ``/``, ``?`` etc. is the
    HTTP layer's job (``fetch_pageviews``).

    >>> to_pageviews_title("Intermittent fasting")
    'Intermittent_fasting'
    >>> to_pageviews_title("AC/DC")
    'AC/DC'
    """
    return title.strip().replace(" ", "_")


def fold_title(text: str) -> str:
    """Case- and accent-insensitive comparison key for titles.

    >>> fold_title("Přerušovaný půst") == fold_title("prerusovany PUST")
    True
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold().strip()


def title_similarity(query: str, title: str) -> float:
    """Similarity in [0, 1] between a query and a page title.

    Case- and accent-insensitive difflib ratio, scaled down when the title has
    extra words the query lacks: "Guru Meditation" is not "meditation" and
    "Astronomy Domine" (a song) is not "astronomy", even though both contain
    the query.  Near-misses in spelling ("astronomi") still score high.

    >>> title_similarity("intermittent fasting", "Intermittent fasting")
    1.0
    >>> title_similarity("prerusovany pust", "Přerušovaný půst")
    1.0
    >>> round(title_similarity("astronomy", "Astronomi"), 2)
    0.89
    >>> title_similarity("meditation", "Guru Meditation") < 0.5
    True
    """
    q, t = fold_title(query), fold_title(title)
    if not q or not t:
        return 0.0
    ratio = difflib.SequenceMatcher(None, q, t).ratio()
    q_words, t_words = set(_WORD_RE.findall(q)), _WORD_RE.findall(t)
    extra = [w for w in t_words if w not in q_words]
    if extra and len(t_words) > len(q_words):
        ratio *= (len(t_words) - len(extra)) / len(t_words)
    return ratio


def _api_url(lang: str) -> str:
    return WIKIPEDIA_API.format(lang=host_lang(lang))


# --- search -------------------------------------------------------------------


def search_articles(client: WikiClient, lang: str, query: str, limit: int = 5) -> list[SearchHit]:
    """Full-text search in one language edition (main namespace only).

    Full-text search matches article *bodies*, including citations, so the top
    hit is not necessarily about the query (an English phrase searched on
    pl.wikipedia can return an unrelated article that cites an English paper).
    Use :func:`prefix_search` or :func:`title_similarity` before trusting a hit.

    >>> search_articles(WikiClient(), "en", "intermittent fasting")[0].title
    'Intermittent fasting'
    """
    payload = client.get(
        _api_url(lang),
        {"action": "query", "list": "search", "srsearch": query, "srlimit": str(limit), "srnamespace": "0"},
    )
    hits = payload.get("query", {}).get("search", [])
    return [SearchHit(h["title"], int(h["pageid"]), _TAG_RE.sub("", h.get("snippet", "")), lang) for h in hits]


def prefix_search(client: WikiClient, lang: str, query: str, limit: int = 5) -> list[SearchHit]:
    """Title-prefix search in one language edition (main namespace only).

    Matches only page titles (case-insensitive, redirects included), so a hit
    means the edition really has a page named like the query.

    >>> prefix_search(WikiClient(), "cs", "přerušovaný půst")[0].title
    'Přerušovaný půst'
    """
    payload = client.get(
        _api_url(lang),
        {"action": "query", "list": "prefixsearch", "pssearch": query, "pslimit": str(limit), "psnamespace": "0"},
    )
    hits = payload.get("query", {}).get("prefixsearch", [])
    return [SearchHit(h["title"], int(h["pageid"]), "", lang) for h in hits]


# --- page info / Wikidata -----------------------------------------------------


def get_page_info(client: WikiClient, lang: str, title: str) -> PageInfo:
    """Resolve redirects for ``title`` and read its Wikidata item id.

    Also reports whether the page is a disambiguation page and normalises the
    title (``intermittent_fasting`` -> ``Intermittent fasting``).

    >>> info = get_page_info(WikiClient(), "en", "5:2 diet")
    >>> (info.title, info.redirected_from, info.qid)
    ('Intermittent fasting', '5:2 diet', 'Q1666254')
    """
    payload = client.get(
        _api_url(lang),
        {
            "action": "query",
            "titles": title,
            "redirects": "1",
            "prop": "pageprops",
            "ppprop": "wikibase_item|disambiguation",
        },
    )
    query = payload.get("query", {})
    pages = query.get("pages", [])
    if not pages:
        return PageInfo(lang, title, None, None, None, True)
    page = pages[0]
    redirected_from = None
    for redirect in query.get("redirects", []):
        if redirect.get("to") == page.get("title"):
            redirected_from = redirect.get("from")
            break
    props = page.get("pageprops", {})
    return PageInfo(
        lang=lang,
        title=page.get("title", title),
        pageid=page.get("pageid"),
        qid=props.get("wikibase_item"),
        redirected_from=redirected_from,
        missing=bool(page.get("missing", False)),
        disambiguation="disambiguation" in props,
    )


_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+)(?:#[^\[\]|]*)?(?:\|[^\[\]]*)?\]\]")
_NON_ARTICLE_PREFIXES = ("file:", "image:", "category:", "wikipedia:", "wp:", "template:", "help:", "wikt:", "wiktionary:", "special:")


def get_disambiguation_links(client: WikiClient, lang: str, title: str, limit: int = 15) -> list[SearchHit]:
    """Articles linked from a disambiguation page, in the order editors listed them.

    Full-text search for "keto diet" never returns "Ketogenic diet", but the
    disambiguation page "Keto diet" links to it.  ``prop=links`` would return
    the links alphabetically (113 for "Mercury", with the planet far down), so
    the page *wikitext* is read instead: the main meanings come first.

    >>> [h.title for h in get_disambiguation_links(WikiClient(), "en", "Keto diet")][:1]
    ['Ketogenic diet']
    """
    payload = client.get(
        _api_url(lang),
        {"action": "query", "titles": title, "prop": "revisions", "rvprop": "content", "rvslots": "main"},
    )
    pages = payload.get("query", {}).get("pages", [])
    try:
        content = pages[0]["revisions"][0]["slots"]["main"]["content"]
    except (IndexError, KeyError, TypeError):
        return []
    seen: set[str] = set()
    hits: list[SearchHit] = []
    for match in _WIKILINK_RE.finditer(content):
        target = match.group(1).strip().replace("_", " ")
        target = target[:1].upper() + target[1:]  # MediaWiki titles start upper-case: [[ketogenic diet]]
        if not target or target.lower().startswith(_NON_ARTICLE_PREFIXES) or target in seen or target == title:
            continue
        seen.add(target)
        hits.append(SearchHit(target, 0, f"linked from disambiguation page '{title}'", lang))
        if len(hits) >= limit:
            break
    return hits


MAX_REDIRECTS = 50


def get_redirects(client: WikiClient, lang: str, title: str, limit: int = MAX_REDIRECTS) -> list[str]:
    """Main-namespace titles that redirect to ``title`` (former names, spellings, aliases).

    Pageviews are counted per title, so a renamed article's history before the
    move lives under the old title; the fetcher merges such redirects.

    >>> "Альберт Ейнштейн" in get_redirects(WikiClient(), "uk", "Альберт Айнштайн")
    True
    """
    payload = client.get(
        _api_url(lang),
        {"action": "query", "titles": title, "prop": "redirects", "rdnamespace": "0", "rdlimit": str(limit)},
    )
    pages = payload.get("query", {}).get("pages", [])
    return [r["title"] for r in (pages[0].get("redirects", []) if pages else [])]


def _label(entity: dict[str, Any], key: str) -> Optional[str]:
    values = entity.get(key, {})
    for language in ("en", "mul"):
        if language in values:
            return values[language].get("value")
    return None


def get_sitelinks(client: WikiClient, qid: str, langs: Iterable[str]) -> tuple[dict[str, str], Optional[str]]:
    """Return ``{lang: title}`` for the Wikipedia sitelinks of ``qid`` plus its English label.

    Keys are the language codes as passed in (``nb`` stays ``nb`` even though
    the sitelink id is ``nowiki``).  The label falls back to the multilingual
    (``mul``) label; items like "Wordle" have no English label at all.

    >>> links, label = get_sitelinks(WikiClient(), "Q1666254", ["cs", "pl"])
    >>> links
    {'cs': 'Přerušovaný půst'}
    """
    langs = list(langs)
    payload = client.get(
        WIKIDATA_API,
        {
            "action": "wbgetentities",
            "ids": qid,
            "props": "sitelinks|labels",
            "sitefilter": "|".join(sitelink_site(lang) for lang in langs),
            "languages": "en|mul",
        },
    )
    entity = payload.get("entities", {}).get(qid, {})
    if "missing" in entity:
        raise WikiApiError(f"Wikidata item {qid} does not exist")
    sitelinks = entity.get("sitelinks", {})
    result: dict[str, str] = {}
    for lang in langs:
        link = sitelinks.get(sitelink_site(lang))
        if link:
            result[lang] = link["title"]
    return result, _label(entity, "labels")


def describe_items(client: WikiClient, qids: Iterable[str]) -> dict[str, str]:
    """Return ``{qid: "label — description"}`` for a few items, for ambiguity messages.

    >>> describe_items(WikiClient(), ["Q308"])["Q308"]
    'Mercury — first planet from the Sun'
    """
    qids = list(dict.fromkeys(qids))
    payload = client.get(
        WIKIDATA_API,
        {"action": "wbgetentities", "ids": "|".join(qids), "props": "labels|descriptions", "languages": "en|mul"},
    )
    out: dict[str, str] = {}
    for qid in qids:
        entity = payload.get("entities", {}).get(qid, {})
        label, description = _label(entity, "labels") or qid, _label(entity, "descriptions")
        out[qid] = f"{label} — {description}" if description else label
    return out


# --- resolution ---------------------------------------------------------------


@dataclass
class _SeedSearch:
    """Internal result of the seed search across editions."""

    lang: Optional[str] = None
    info: Optional[PageInfo] = None
    exact: bool = False
    candidates: list[SearchHit] = field(default_factory=list)
    disambiguations: list[PageInfo] = field(default_factory=list)
    tried: list[str] = field(default_factory=list)


def _best_hit(
    client: WikiClient, lang: str, topic: str, limit: int, candidates: list[SearchHit]
) -> tuple[Optional[SearchHit], bool]:
    """Best title match for ``topic`` in one edition; returns ``(hit, is_exact)``.

    Prefix hits only count on an exact (accent-/case-insensitive) title match:
    "astronomy" on no.wikipedia prefix-matches "Astronomy Domine" (a song).
    Otherwise the best-scoring full-text hit above the similarity threshold.
    """
    wanted = fold_title(topic)
    hits = prefix_search(client, lang, topic, limit=limit)
    candidates.extend(hits)
    exact = [h for h in hits if fold_title(h.title) == wanted]
    if exact:
        return exact[0], True
    hits = search_articles(client, lang, topic, limit=limit)
    candidates.extend(hits)
    if not hits:
        return None, False
    chosen = max(hits, key=lambda h: title_similarity(topic, h.title))
    if title_similarity(topic, chosen.title) < MIN_TITLE_SIMILARITY:
        return None, False
    return chosen, False


def _find_seed(client: WikiClient, topic: str, seed_order: list[str], limit: int) -> _SeedSearch:
    """Search ``topic`` in each seed language until a title-matching, non-disambiguation page appears.

    Disambiguation pages are remembered (with the pages they link to, plus
    full-text hits, added to the candidates) and the search moves on to the
    next edition.
    """
    seed = _SeedSearch()
    for lang in seed_order:
        seed.tried.append(lang)
        chosen, exact = _best_hit(client, lang, topic, limit, seed.candidates)
        if chosen is None:
            continue
        info = get_page_info(client, lang, chosen.title)
        if info.disambiguation:
            # The page's own links are authoritative meanings: they replace duplicate search hits
            # so that they keep their editorial order in the candidate list.
            links = get_disambiguation_links(client, lang, info.title)
            linked = {(h.title, h.lang) for h in links}
            seed.candidates = [h for h in seed.candidates if (h.title, h.lang) not in linked]
            extra = links + (search_articles(client, lang, topic, limit=limit) if exact else [])
            seed.candidates.extend(h for h in extra if h.title != info.title)
            seed.disambiguations.append(info)
            continue
        if info.qid and not info.missing:
            seed.lang, seed.info, seed.exact = lang, info, exact
            return seed
    return seed


def _english_conflict(client: WikiClient, topic: str, seed: _SeedSearch, limit: int) -> Optional[PageInfo]:
    """If English has an exact-title article for a *different* item than the seed, return it.

    An English word searched in a non-English edition can exactly match a
    foreign-titled song or brand ("Running" on pl.wikipedia is a 2014 song,
    "Strava" on cs.wikipedia means food).  Only runs when English was not
    already tried as a seed.
    """
    assert seed.info is not None
    if seed.lang == FALLBACK_SEED_LANG or FALLBACK_SEED_LANG in seed.tried:
        return None
    hits = prefix_search(client, FALLBACK_SEED_LANG, topic, limit=limit)
    wanted = fold_title(topic)
    exact = [h for h in hits if fold_title(h.title) == wanted]
    if not exact:
        return None
    seed.candidates.extend(hits)
    info = get_page_info(client, FALLBACK_SEED_LANG, exact[0].title)
    if info.missing or info.disambiguation or not info.qid or info.qid == seed.info.qid:
        return None
    return info


def _dedupe(hits: Iterable[SearchHit]) -> list[SearchHit]:
    seen: set[tuple[str, str]] = set()
    out: list[SearchHit] = []
    for h in hits:
        if (h.title, h.lang) not in seen:
            seen.add((h.title, h.lang))
            out.append(h)
    return out


def _is_disambiguation_link(hit: SearchHit) -> bool:
    return hit.snippet.startswith("linked from disambiguation page")


def rank_candidates(hits: Iterable[SearchHit], query: str, exclude: Optional[str] = None) -> list[SearchHit]:
    """Candidates in the order an agent should consider them: disambiguation-page meanings in page order, then by title similarity.

    >>> hits = [SearchHit("Zebra", 1, "", "en"), SearchHit("Mercury (planet)", 2, "linked from disambiguation page 'Mercury'", "en")]
    >>> [h.title for h in rank_candidates(hits, "mercury")]
    ['Mercury (planet)', 'Zebra']
    """
    return sorted(
        _dedupe(h for h in hits if h.title != exclude),
        key=lambda h: (0, 0.0) if _is_disambiguation_link(h) else (1, -title_similarity(query, h.title)),
    )


def _format_candidates(hits: Iterable[SearchHit], query: str, exclude: Optional[str] = None) -> str:
    """Candidates for a caveat, in ``rank_candidates`` order."""
    ranked = rank_candidates(hits, query, exclude)
    seen = [f"{h.title} ({h.lang})" for h in ranked]
    shown = ", ".join(seen[:MAX_CAVEAT_CANDIDATES])
    if len(seen) > MAX_CAVEAT_CANDIDATES:
        shown += f" and {len(seen) - MAX_CAVEAT_CANDIDATES} more (see candidates)"
    return shown


def _not_found(result: ResolvedTopic, langs: list[str], reason: str) -> ResolvedTopic:
    for lang in langs:
        result.articles[lang] = ArticleRef(lang, project_for(lang), found=False, reason=reason)
    return result


def resolve_topic(
    client: WikiClient,
    topic: str,
    langs: list[str],
    seed_lang: Optional[str] = None,
    qid: Optional[str] = None,
    candidates_limit: int = 5,
) -> ResolvedTopic:
    """Map ``topic`` to one article per language edition via Wikidata sitelinks.

    ``seed_lang`` forces the edition to search first and disables the
    English cross-check; otherwise the requested ``langs`` are tried in order,
    then English.  Pass ``qid`` to skip the search (e.g. when the agent already
    picked a candidate from ``candidates``).  When the best match is a
    disambiguation page, or the same title means different things in the seed
    edition and in English, the result has ``ambiguous=True`` and no articles:
    re-run with the exact title, ``seed_lang`` or a QID.

    >>> r = resolve_topic(WikiClient(), "intermittent fasting", ["pl", "cs"])
    >>> r.qid, r.articles["pl"].found, r.articles["cs"].title
    ('Q1666254', False, 'Přerušovaný půst')
    """
    langs = [normalize_lang(lang) for lang in langs if lang.strip()]
    if not langs:
        raise ValueError("at least one language code is required")
    if len(set(langs)) != len(langs):
        raise ValueError(f"duplicate language codes: {langs}")

    result = ResolvedTopic(topic=topic, qid=qid, label=None, seed_lang=None, seed_title=None)

    if qid is None:
        seed_order = [normalize_lang(seed_lang)] if seed_lang else list(langs)
        if FALLBACK_SEED_LANG not in seed_order:
            seed_order.append(FALLBACK_SEED_LANG)
        seed = _find_seed(client, topic, seed_order, candidates_limit)
        seed.candidates = _dedupe(seed.candidates)
        exclude = seed.info.title if seed.info else (seed.disambiguations[0].title if seed.disambiguations else None)
        result.candidates = rank_candidates(seed.candidates, topic, exclude)

        if seed.info is None:
            if seed.disambiguations:
                first = seed.disambiguations[0]
                result.ambiguous = True
                result.seed_lang, result.seed_title = first.lang, first.title
                pages = "; ".join(f"'{d.title}' on {project_for(d.lang)}" for d in seed.disambiguations)
                result.caveats.append(
                    f"{pages}: disambiguation page(s), so the phrase has several meanings and its pageviews "
                    f"are not about any one topic. Candidates: {_format_candidates(seed.candidates, topic, exclude=first.title)}. "
                    "Re-run with the exact article title as the topic, or pass --qid."
                )
                return _not_found(result, langs, "ambiguous topic")
            result.caveats.append(
                f"No article whose title matches '{topic}' was found in any of: {', '.join(seed_order)}. "
                "The candidates listed are full-text hits that mention the phrase, not necessarily "
                "about it. Try the topic name as written in one of the editions, or pass --qid."
            )
            return _not_found(result, langs, "topic not found")

        info = seed.info
        result.seed_lang, result.seed_title, result.qid = seed.lang, info.title, info.qid
        result.seed_redirected_from = info.redirected_from

        if seed_lang is None:
            conflict = _english_conflict(client, topic, seed, candidates_limit)
            if conflict is not None:
                assert info.qid and conflict.qid
                described = describe_items(client, [info.qid, conflict.qid])
                result.ambiguous = True
                result.qid = None
                result.caveats.append(
                    f"'{topic}' is an article title in two editions but means different things: "
                    f"'{info.title}' on {project_for(info.lang)} is {info.qid} ({described[info.qid]}), while "
                    f"'{conflict.title}' on {project_for(conflict.lang)} is {conflict.qid} ({described[conflict.qid]}). "
                    f"Re-run with --seed-lang {info.lang} or --seed-lang en, or pass --qid."
                )
                return _not_found(result, langs, "ambiguous topic")

        for d in seed.disambiguations:
            result.caveats.append(
                f"'{d.title}' on {project_for(d.lang)} is a disambiguation page; used '{info.title}' "
                f"on {project_for(info.lang)} instead. Check that this is the intended meaning."
            )
        if info.redirected_from:
            result.caveats.append(
                f"'{info.redirected_from}' is a redirect to '{info.title}' on {project_for(info.lang)}; "
                f"the analysis will measure interest in '{info.title}'. Check that this is the concept you mean."
            )
        others = _format_candidates(seed.candidates, topic, exclude=info.title)
        if others:
            result.caveats.append(
                f"Matched '{info.title}' on {project_for(info.lang)}; other candidates seen: {others}. "
                "Re-run with --qid if a different candidate is the intended topic."
            )

    assert result.qid is not None
    sitelinks, label = get_sitelinks(client, result.qid, langs)
    result.label = label or result.seed_title

    for lang in langs:
        project = project_for(lang)
        title = sitelinks.get(lang)
        if title is None:
            result.articles[lang] = ArticleRef(
                lang, project, found=False, qid=result.qid,
                reason=f"No {project} article is linked to {result.qid} on Wikidata.",
            )
            continue
        info = get_page_info(client, lang, title)
        if info.missing:
            result.articles[lang] = ArticleRef(
                lang, project, found=False, qid=result.qid,
                reason=f"Wikidata links '{title}' but the page does not exist on {project} (stale sitelink).",
            )
            continue
        if info.disambiguation:
            result.articles[lang] = ArticleRef(
                lang, project, found=False, qid=result.qid, title=info.title,
                reason=f"'{info.title}' on {project} is a disambiguation page; its pageviews are not about one topic.",
            )
            continue
        result.articles[lang] = ArticleRef(
            lang, project, found=True,
            title=info.title, pageviews_title=to_pageviews_title(info.title),
            pageid=info.pageid, qid=info.qid or result.qid, redirected_from=info.redirected_from,
            redirects=get_redirects(client, lang, info.title),
        )

    missing = [lang for lang, ref in result.articles.items() if not ref.found]
    if missing:
        result.caveats.append(
            f"No article in: {', '.join(missing)}. No article does not mean no interest; "
            "nobody may have written one yet."
        )
    renamed = [f"{ref.lang}: '{ref.redirected_from}' -> '{ref.title}'" for ref in result.articles.values() if ref.redirected_from]
    if renamed:
        result.caveats.append(
            "Resolved through redirects: " + "; ".join(renamed) + ". If the page was renamed recently, "
            "pageviews before the rename are recorded under the old title."
        )
    return result


# --- CLI ----------------------------------------------------------------------


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve a topic to Wikipedia article titles across language editions (JSON output).",
    )
    parser.add_argument("topic", help="topic to look up, in any language")
    parser.add_argument("--langs", required=True, help="comma-separated language codes, e.g. pl,cs,uk")
    parser.add_argument(
        "--seed-lang",
        help="edition to search first (default: the first of --langs, then en); also disables the English cross-check",
    )
    parser.add_argument("--qid", help="Wikidata item id (e.g. Q1666254) to skip the search step")
    parser.add_argument("--candidates", type=int, default=5, help="number of search candidates to report")
    parser.add_argument("--out", help="write the JSON to this file (UTF-8) instead of stdout")
    return parser.parse_args(argv)


def _force_utf8_stdout() -> None:
    """Emit UTF-8 regardless of the console code page (Windows defaults to cp1251/cp1252)."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point: print the resolution result as JSON to stdout."""
    _force_utf8_stdout()
    args = _parse_args(argv)
    try:
        result = resolve_topic(
            WikiClient(), args.topic, args.langs.split(","), seed_lang=args.seed_lang, qid=args.qid,
            candidates_limit=args.candidates,
        )
    except (WikiApiError, ValueError, requests.RequestException) as exc:
        json.dump({"error": str(exc)}, sys.stdout, ensure_ascii=False)
        print()
        return 1
    payload = result.to_dict()
    if args.out:
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"written": str(Path(args.out)), "qid": payload["qid"], "ambiguous": payload["ambiguous"],
                          "found": {k: v["found"] for k, v in payload["articles"].items()}}))
    else:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
