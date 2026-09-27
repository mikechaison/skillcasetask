"""Single entry point for the agent: resolve -> fetch -> analyse -> chart -> report, one JSON summary.

Two comparison modes, both handled by ``compare``, plus follow-ups that re-run the saved query:

    python scripts/cli.py compare --topic "intermittent fasting" --langs pl,cs --years 2
    python scripts/cli.py compare --topics "World War I,World War II" --langs uk --years 2
    python scripts/cli.py add-language --lang sk          # also: remove-language, add-topic, remove-topic
    python scripts/cli.py extend-range --years 5          # or --start/--end
    python scripts/cli.py choose --title "Mercury (planet)"   # answer a pending ambiguity (or --qid Q…)
    python scripts/cli.py report --to . --html            # copy the last report into the user's directory
    python scripts/cli.py show | history

``state.json`` holds the last query, the pending clarification (if the last
compare was ambiguous) and a history of runs.

Every successful ``compare`` (and follow-up) also copies the PDF and HTML report
into the directory the command was run from, so the user finds it next to them
(``outputs.delivered``).  The copy is skipped when that directory is the skill
itself, and ``--deliver DIR`` / ``--no-deliver`` override it.

Result keys: language codes when there is one topic; topic slugs when there is
one language; ``slug:lang`` otherwise.  Every number in the summary comes from
the module JSON files written to ``--out-dir``; nothing is computed here.

Exit codes: 0 ok · 1 error · 2 a topic is ambiguous (candidates listed; re-run
with the exact title, ``--seed-lang`` or ``--qid``).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import analyze_trend as at  # noqa: E402
from scripts import fetch_pageviews as fp  # noqa: E402
from scripts import make_chart as mc  # noqa: E402
from scripts import make_report as mr  # noqa: E402
from scripts import resolve_topic as rt  # noqa: E402

SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_ROOT = SKILL_ROOT / "output"
_FILE_SUFFIXES = (".json", ".txt", ".pdf", ".png", ".html", ".csv")
DEFAULT_STATE = Path(__file__).resolve().parents[1] / "state.json"
LIMITATIONS = mr.LIMITATIONS


def slugify(text: str, limit: int = 24) -> str:
    """Filesystem- and key-safe slug.

    >>> slugify("World War I"), slugify("Přerušovaný půst!")
    ('world-war-i', 'prerusovany-pust')
    """
    folded = rt.fold_title(text)
    slug = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    return (slug or "topic")[:limit].rstrip("-")


def result_key(topic_slug: str, lang: str, n_topics: int, n_langs: int) -> str:
    """Key for one (topic, language) series in the merged outputs.

    >>> result_key("ww1", "uk", 1, 3), result_key("ww1", "uk", 2, 1), result_key("ww1", "uk", 2, 2)
    ('uk', 'ww1', 'ww1:uk')
    """
    if n_topics == 1:
        return lang
    if n_langs == 1:
        return topic_slug
    return f"{topic_slug}:{lang}"


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def _emit(payload: dict[str, Any], code: int) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def _deliver_dir(args: argparse.Namespace) -> Optional[Path]:
    """Where to put the user's copy of the report: ``--deliver``, else the working directory unless that is the skill itself.

    >>> _deliver_dir(argparse.Namespace(deliver="C:/reports", no_deliver=False))
    WindowsPath('C:/reports')
    """
    if getattr(args, "no_deliver", False):
        return None
    if getattr(args, "deliver", None):
        return Path(args.deliver)
    cwd = Path.cwd().resolve()
    return None if cwd == SKILL_ROOT or SKILL_ROOT in cwd.parents else cwd


def _copy_report(outputs: dict[str, Any], topics: list[str], langs: list[str], target_dir: Path, stem: Optional[str] = None,
                 html: bool = True) -> dict[str, str]:
    """Copy the report files named in ``outputs`` into ``target_dir`` as ``wikipedia-trends-<topics>-<langs>.pdf`` (and ``.html``).

    Topics and editions are both in the name, so a follow-up that changes either
    keeps the earlier file; one that only changes the period overwrites it.
    """
    if target_dir.suffix.lower() in _FILE_SUFFIXES:
        raise ValueError(f"the report target must be a directory, not a file: {str(target_dir)!r}")
    source = Path(outputs["report_pdf"])
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = stem or "wikipedia-trends-" + "-".join([slugify(t) for t in topics] + list(langs))
    copied = {"dir": str(target_dir.resolve()), "pdf": str((target_dir / f"{stem}.pdf").resolve())}
    shutil.copyfile(source, copied["pdf"])
    if html:
        html_source = Path(outputs.get("report_html") or source.with_name("report.html"))
        if not html_source.exists():
            raise ValueError(f"no HTML report next to {source}")
        copied["html"] = str((target_dir / f"{stem}.html").resolve())
        shutil.copyfile(html_source, copied["html"])
    return copied


# --- pipeline -------------------------------------------------------------------


def resolve_all(
    client: rt.WikiClient, topics: list[str], langs: list[str], seed_lang: Optional[str], qids: Optional[dict[str, str]] = None
) -> tuple[dict[str, rt.ResolvedTopic], Optional[dict[str, Any]]]:
    """Resolve every topic; return ``(resolved by slug, problem)`` where problem is set when a topic is ambiguous/not found.

    ``qids`` pins a Wikidata item per topic text (from ``--qid`` or a ``choose --qid`` follow-up).
    """
    resolved: dict[str, rt.ResolvedTopic] = {}
    qids = qids or {}
    for topic in topics:
        slug = slugify(topic)
        r = rt.resolve_topic(client, topic, langs, seed_lang=seed_lang, qid=qids.get(topic))
        if r.ambiguous:
            return resolved, {
                "status": "ambiguous", "topic": topic,
                "message": r.caveats[0] if r.caveats else "ambiguous topic",
                "candidates": [asdict(c) for c in r.candidates[:15]],
                "how_to_fix": "Re-run with the exact article title as the topic, or --seed-lang <lang>, or --qid Q…",
            }
        if not any(ref.found for ref in r.articles.values()):
            return resolved, {
                "status": "not_found", "topic": topic,
                "message": " ".join(r.caveats) or "no article in any requested edition",
                "articles": {k: asdict(v) for k, v in r.articles.items()},
            }
        resolved[slug] = r
    return resolved, None


def merge_resolved(resolved: dict[str, rt.ResolvedTopic], langs: list[str]) -> dict[str, Any]:
    """A resolve-shaped dict keyed by merged result keys (what chart and report expect)."""
    n_topics = len(resolved)
    articles: dict[str, Any] = {}
    caveats: list[str] = []
    for slug, r in resolved.items():
        for lang in langs:  # only the requested editions; keys never collide
            ref = r.articles.get(lang)
            if ref is None:
                continue
            key = result_key(slug, lang, n_topics, len(langs))
            entry = asdict(ref)
            entry["topic"] = r.topic
            entry["key"] = key
            if n_topics > 1 and ref.title:
                entry["title"] = f"{ref.title}" if len(langs) == 1 else f"{ref.title} ({lang})"
            articles[key] = entry
        prefix = f"{r.topic}: " if n_topics > 1 else ""
        caveats += [prefix + c for c in r.caveats]
    label = " vs ".join(r.label or r.topic for r in resolved.values())
    return {"topics": [r.topic for r in resolved.values()], "label": label, "articles": articles, "caveats": caveats,
            "qids": {slug: r.qid for slug, r in resolved.items()}}


def fetch_all(
    merged: dict[str, Any], start: date, end: date, granularity: str, cache_dir: Path, refresh: bool, today: Optional[date]
) -> dict[str, Any]:
    """Fetch every found article; returns the fetch-CLI-shaped dict keyed by merged keys."""
    client = fp.PageviewsClient()
    results: dict[str, Any] = {}
    skipped: list[str] = []
    for key, ref in merged["articles"].items():
        if not ref.get("found"):
            skipped.append(key)
            continue
        query = fp.PageviewsQuery(ref["project"], ref["pageviews_title"], granularity, start, end)
        results[key] = fp.fetch_with_redirects(client, query, list(ref.get("redirects", [])), cache_dir=cache_dir,
                                               refresh=refresh, today=today).to_dict()
    return {"period": {"start": start.isoformat(), "end": end.isoformat(), "granularity": granularity},
            "results": results, "skipped": skipped, "limitations": LIMITATIONS}


def analyze_all(fetched: dict[str, Any]) -> dict[str, Any]:
    analyses: dict[str, at.TrendAnalysis] = {}
    skipped: dict[str, str] = {}
    granularity = fetched["period"]["granularity"]
    for key, result in fetched["results"].items():
        if result.get("status") not in ("ok", "partial") or not result.get("csv_path"):
            skipped[key] = result.get("status", "unknown")
            continue
        analyses[key] = at.analyze_series(at._load_csv(result["csv_path"]), granularity, status=result["status"])
    return {"analyses": {k: a.to_dict() for k, a in analyses.items()},
            "comparison": at.compare(analyses) if len(analyses) > 1 else None,
            "skipped": skipped, "method": "references/methodology.md"}


def summarize(merged: dict[str, Any], fetched: dict[str, Any], analysis: dict[str, Any], outputs: dict[str, str]) -> dict[str, Any]:
    """Compact, self-contained summary for the agent (all values copied from the module outputs)."""
    per_key: dict[str, Any] = {}
    for key, a in analysis["analyses"].items():
        ref = merged["articles"].get(key, {})
        fetch = fetched["results"].get(key, {})
        biggest = max(a.get("spikes", []), key=lambda s: s.get("excess_views", 0), default=None)
        per_key[key] = {
            "topic": ref.get("topic"), "lang": ref.get("lang"), "article": ref.get("title"), "project": ref.get("project"),
            "direction": a["direction"], "headline_change": a["headline_change"], "headline_basis": a["headline_basis"],
            "endpoint_change": a["relative_change"], "indexed_last": a["indexed_last"], "seasonal": a["seasonal"],
            "mean_views_per_day": a["mean_views_per_day"], "total_views": a["total_views"],
            "confidence": a["confidence"]["label"], "confidence_score": a["confidence"]["score"], "confidence_reasons": a["confidence"]["reasons"],
            "spike_events": len(a.get("spikes", [])), "spike_share": a["spike_share"],
            "largest_spike": None if biggest is None else {"date": biggest["peak_date"], "views": biggest["peak_views"], "baseline": biggest["baseline"]},
            "opportunity_score": a["opportunity_score"],
            "data": {"status": fetch.get("status"), "from": fetch.get("data_from"), "through": fetch.get("data_through"),
                     "points": a["points"], "zero_filled_points": fetch.get("zero_filled_points"),
                     "merged_redirect_titles": fetch.get("merged_titles", []), "views_under_current_title": fetch.get("target_views")},
            "caveats": [c for c in a.get("caveats", []) if "willingness to pay" not in c] + fetch.get("caveats", []),
        }
    missing = {k: v.get("reason") for k, v in merged["articles"].items() if not v.get("found")}
    return {
        "status": "ok",
        "topics": merged["topics"], "label": merged["label"], "qids": merged["qids"],
        "period": fetched["period"],
        "results": per_key,
        "no_article": missing,
        "comparison": analysis.get("comparison"),
        "caveats": merged["caveats"],
        "limitations": LIMITATIONS,
        "outputs": outputs,
        "method": "references/methodology.md",
    }


# --- session state ---------------------------------------------------------------

COMPARE_FIELDS = ("topics", "langs", "years", "start", "end", "seed_lang", "qids", "granularity", "title",
                  "out_dir", "cache_dir", "today", "no_charts", "deliver", "no_deliver")
HISTORY_LIMIT = 20


def _read_state(path: str) -> dict[str, Any]:
    state_path = Path(path)
    return fp.read_json_file(state_path) if state_path.exists() else {}


def _write_state(path: str, state: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _period_from(args: argparse.Namespace) -> tuple[date, date, Optional[date]]:
    today = date.fromisoformat(args.today) if args.today else None
    start, end = fp.default_period(args.years, today=today)
    if args.end:
        end = date.fromisoformat(args.end)
        start = max(fp.shift_years(end, args.years), fp.EARLIEST_DATA)
    if args.start:
        start = date.fromisoformat(args.start)
    return start, end, today


def _query_fields(args: argparse.Namespace, topics: list[str], langs: list[str], out_dir: Path) -> dict[str, Any]:
    return {
        "topics": topics, "langs": langs, "years": args.years, "start": args.start, "end": args.end,
        "seed_lang": args.seed_lang, "qids": dict(getattr(args, "qids", None) or {}), "granularity": args.granularity,
        "title": args.title, "out_dir": str(out_dir), "cache_dir": args.cache_dir, "today": args.today,
        "no_charts": bool(args.no_charts), "deliver": getattr(args, "deliver", None), "no_deliver": bool(getattr(args, "no_deliver", False)),
    }


def _remember(state: dict[str, Any], entry: dict[str, Any]) -> None:
    history = list(state.get("history", []))
    history.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry})
    state["history"] = history[-HISTORY_LIMIT:]


def run_compare(args: argparse.Namespace, follow_up: Optional[dict[str, Any]] = None) -> tuple[dict[str, Any], int]:
    topics = [t.strip() for t in (args.topics.split(",") if args.topics else [args.topic]) if t and t.strip()]
    langs = [rt.normalize_lang(x) for x in args.langs.split(",") if x.strip()]
    if not topics or not langs:
        raise ValueError("at least one topic and one language are required")
    qids: dict[str, str] = dict(getattr(args, "qids", None) or {})
    if getattr(args, "qid", None):
        if len(topics) > 1:
            raise ValueError("--qid applies to a single --topic; in topics mode use `choose --qid` after an ambiguity")
        qids[topics[0]] = args.qid
    args.qids = qids
    start, end, today = _period_from(args)

    if args.out_dir and Path(args.out_dir).suffix.lower() in (".json", ".txt", ".pdf", ".png", ".html", ".csv"):
        raise ValueError(f"--out-dir must be a directory, not a file: {args.out_dir!r} (omit it to use output/<topic>/)")
    out_dir = Path(args.out_dir) if args.out_dir else DEFAULT_OUT_ROOT / "-".join(slugify(t, 16) for t in topics)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(args.cache_dir) if args.cache_dir else fp.DEFAULT_CACHE_DIR
    state = _read_state(args.state)
    query = _query_fields(args, topics, langs, out_dir)
    period = {"start": start.isoformat(), "end": end.isoformat(), "granularity": args.granularity}

    resolved, problem = resolve_all(rt.WikiClient(), topics, langs, args.seed_lang, qids)
    if problem:
        code = 2 if problem["status"] == "ambiguous" else 1
        if code == 2:  # keep the query so `choose` can finish it
            state.update(query)
            state["pending"] = {"topic": problem["topic"], "message": problem["message"], "candidates": problem["candidates"][:10],
                                "how_to_fix": "python scripts/cli.py choose --title \"<candidate title>\"   (or choose --qid Q…)"}
            _remember(state, {"command": (follow_up or {}).get("command", "compare"), "status": "ambiguous", "topic": problem["topic"],
                              "topics": topics, "langs": langs, "period": period})
            state["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _write_state(args.state, state)
            problem["how_to_fix"] = state["pending"]["how_to_fix"]
            problem["pending_saved"] = True
        return problem, code
    merged = merge_resolved(resolved, langs)
    (out_dir / "resolved.json").write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")

    fetched = fetch_all(merged, start, end, args.granularity, cache_dir, args.refresh, today)
    (out_dir / "fetched.json").write_text(json.dumps(fetched, ensure_ascii=False, indent=2), encoding="utf-8")
    if not fetched["results"]:
        return {"status": "no_data", "message": "no edition returned pageviews", "no_article": merged["articles"]}, 1

    analysis = analyze_all(fetched)
    (out_dir / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    if not analysis["analyses"]:
        return {"status": "no_data", "message": "no series had data to analyse", "skipped": analysis["skipped"]}, 1

    outputs = {"dir": str(out_dir), "resolved": str(out_dir / "resolved.json"), "fetched": str(out_dir / "fetched.json"),
               "analysis": str(out_dir / "analysis.json")}
    if not args.no_charts:
        series, skipped = mc.load_chart_series(fetched, analysis, merged)
        if series:
            title = args.title or merged["label"]
            mc.plt.close(mc.render_chart(series, title, out=out_dir / "chart.png"))
            mc.plt.close(mc.render_small_multiples(series, f"{title}: views per edition", out=out_dir / "multiples.png"))
            mc.plt.close(mc.render_seasonal_profiles(series, f"{title}: year over year", out=out_dir / "seasonal.png"))
            mc.plt.close(mc.render_ranking(series, f"{title}: where to look first", out=out_dir / "ranking.png"))
            extra = {k: out_dir / f"{k}.png" for k in ("multiples", "seasonal", "ranking")}
            content = mr.build_content(analysis, merged, fetched, title=title, today=today)
            pages = mr.render_pdf(content, out_dir / "chart.png", out_dir / "report.pdf", extra)
            (out_dir / "report.html").write_text(mr.render_html(content, out_dir / "chart.png", extra_charts=extra), encoding="utf-8")
            outputs.update({"chart": str(out_dir / "chart.png"), **{k: str(v) for k, v in extra.items()},
                            "report_pdf": str(out_dir / "report.pdf"), "report_html": str(out_dir / "report.html"), "report_pages": pages})
            if skipped:
                outputs["chart_skipped"] = skipped
            target = _deliver_dir(args)
            if target is not None:
                outputs["delivered"] = _copy_report(outputs, topics, langs, target)

    summary = summarize(merged, fetched, analysis, outputs)
    if follow_up:
        summary["follow_up"] = follow_up
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    state.update(query)
    state["command"] = "compare"
    state["pending"] = None
    state["outputs"] = outputs
    _remember(state, {"command": (follow_up or {}).get("command", "compare"), "status": "ok", "topics": topics, "langs": langs,
                      "period": period, "keys": list(summary["results"]), "out_dir": str(out_dir),
                      **({"changed": follow_up.get("changed")} if follow_up else {})})
    state["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write_state(args.state, state)
    return summary, 0


# --- follow-ups --------------------------------------------------------------------


def _load_state(path: str) -> dict[str, Any]:
    state = _read_state(path)
    if not state or "topics" not in state:
        raise ValueError(f"no saved query in {Path(path)}: run `compare` first")
    return state


def _rerun(state_path: str, state: dict[str, Any], command: str, changed: dict[str, Any], **overrides: Any) -> tuple[dict[str, Any], int]:
    fields = {k: state.get(k) for k in COMPARE_FIELDS}
    fields.update(overrides)
    ns = argparse.Namespace(
        topic=None, topics=",".join(fields["topics"]), langs=",".join(fields["langs"]), years=fields.get("years") or 2,
        start=fields.get("start"), end=fields.get("end"), seed_lang=fields.get("seed_lang"), qid=None, qids=dict(fields.get("qids") or {}),
        granularity=fields.get("granularity") or "daily", title=fields.get("title"), out_dir=fields.get("out_dir"),
        cache_dir=fields.get("cache_dir"), refresh=False, today=fields.get("today"), no_charts=bool(fields.get("no_charts")),
        deliver=fields.get("deliver"), no_deliver=bool(fields.get("no_deliver")), state=state_path,
    )
    return run_compare(ns, follow_up={"command": command, "changed": changed})


def _split(csv: str) -> list[str]:
    return [x.strip() for x in csv.split(",") if x.strip()]


def run_add_language(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    state = _load_state(args.state)
    new = [rt.normalize_lang(x) for x in _split(args.lang)]
    langs = list(dict.fromkeys(state["langs"] + new))
    added = [x for x in new if x not in state["langs"]]
    return _rerun(args.state, state, "add-language", {"added_languages": added, "langs": langs}, langs=langs)


def run_remove_language(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    state = _load_state(args.state)
    drop = {rt.normalize_lang(x) for x in _split(args.lang)}
    langs = [x for x in state["langs"] if x not in drop]
    if not langs:
        raise ValueError("cannot remove every language")
    return _rerun(args.state, state, "remove-language", {"removed_languages": sorted(drop & set(state["langs"])), "langs": langs}, langs=langs)


def run_add_topic(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    state = _load_state(args.state)
    new = _split(args.topic)
    topics = list(dict.fromkeys(state["topics"] + new))
    qids = dict(state.get("qids") or {})
    if args.qid:
        if len(new) != 1:
            raise ValueError("--qid pins exactly one added topic")
        qids[new[0]] = args.qid
    changed = {"added_topics": [t for t in new if t not in state["topics"]], "topics": topics}
    overrides: dict[str, Any] = {"topics": topics, "qids": qids}
    if args.seed_lang:
        overrides["seed_lang"] = args.seed_lang
        changed["seed_lang"] = args.seed_lang
    return _rerun(args.state, state, "add-topic", changed, **overrides)


def run_remove_topic(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    state = _load_state(args.state)
    wanted = {slugify(t) for t in _split(args.topic)}
    topics = [t for t in state["topics"] if slugify(t) not in wanted]
    if not topics:
        raise ValueError("cannot remove every topic")
    if len(topics) == len(state["topics"]):
        raise ValueError(f"no such topic in the saved query: {state['topics']}")
    qids = {t: q for t, q in (state.get("qids") or {}).items() if slugify(t) not in wanted}
    return _rerun(args.state, state, "remove-topic", {"removed_topics": [t for t in state["topics"] if t not in topics], "topics": topics},
                  topics=topics, qids=qids)


def run_extend_range(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    state = _load_state(args.state)
    if not (args.years or args.start or args.end):
        raise ValueError("give --years N, or --start/--end YYYY-MM-DD")
    overrides: dict[str, Any] = {}
    if args.years:
        overrides.update(years=args.years, start=None, end=None)
    if args.start or args.end:
        overrides.update(start=args.start or state.get("start"), end=args.end or state.get("end"))
        if args.years:
            overrides["years"] = args.years
    changed = {k: v for k, v in overrides.items() if v is not None}
    return _rerun(args.state, state, "extend-range", changed, **overrides)


def run_choose(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    state = _load_state(args.state) if Path(args.state).exists() else {}
    pending = state.get("pending")
    if not pending:
        raise ValueError("nothing to choose: the last compare was not ambiguous")
    if not (args.title or args.qid) or (args.title and args.qid):
        raise ValueError("give exactly one of --title \"<article title>\" or --qid Q…")
    old = pending["topic"]
    topics = list(state["topics"])
    qids = dict(state.get("qids") or {})
    if args.title:
        topics = [args.title if t == old else t for t in topics]
        qids.pop(old, None)
        changed = {"topic_replaced": {old: args.title}}
    else:
        qids[old] = args.qid
        changed = {"topic_pinned": {old: args.qid}}
    overrides: dict[str, Any] = {"topics": topics, "qids": qids}
    if args.seed_lang:
        overrides["seed_lang"] = args.seed_lang
    return _rerun(args.state, state, "choose", changed, **overrides)


def run_show(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    state = _load_state(args.state)
    if state.get("pending"):
        return {"status": "ambiguous", "pending": state["pending"], "query": {k: state.get(k) for k in ("topics", "langs")}}, 2
    summary_path = Path(state["out_dir"]) / "summary.json"
    if not summary_path.exists():
        raise ValueError(f"no summary at {summary_path}")
    return fp.read_json_file(summary_path), 0


def run_history(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    state = _read_state(args.state)
    return {"status": "ok", "history": state.get("history", []), "current": {k: state.get(k) for k in ("topics", "langs", "years", "start", "end")},
            "pending": state.get("pending")}, 0


def run_report(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """Copy the last query's report again: into another directory, under another name, or after the copy was lost.

    ``compare`` already delivers the report to the working directory; this is the
    explicit version.  Default target is the current working directory; the file is
    named ``wikipedia-trends-<topics>-<langs>.pdf`` (``--name`` overrides the stem, ``--html``
    also copies the self-contained HTML page).  Nothing is recomputed.

    >>> # python scripts/cli.py report --to . --html
    """
    state = _load_state(args.state)
    if state.get("pending"):
        return {"status": "ambiguous", "pending": state["pending"], "query": {k: state.get(k) for k in ("topics", "langs")}}, 2
    outputs = state.get("outputs") or {}
    source = Path(outputs["report_pdf"]) if outputs.get("report_pdf") else None
    if source is None or not source.exists():
        raise ValueError("the last query has no report (it ran with --no-charts, or its files were removed): "
                         "re-run `compare` without --no-charts")
    target_dir = Path(args.to) if args.to else Path.cwd()
    if target_dir.suffix.lower() in _FILE_SUFFIXES:
        raise ValueError(f"--to must be a directory, not a file: {args.to!r}")
    stem = re.sub(r"\.(pdf|html?)$", "", Path(args.name).name, flags=re.I) if args.name else None
    copied = _copy_report(outputs, state["topics"], state["langs"], target_dir, stem=stem, html=bool(args.html))
    last = (state.get("history") or [{}])[-1]
    return {"status": "ok", "report": {k: v for k, v in copied.items() if k != "dir"}, "pages": outputs.get("report_pages"),
            "topics": state["topics"], "langs": state["langs"], "period": last.get("period"), "source_dir": str(source.parent)}, 0


# --- CLI ----------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli.py", description="wikipedia-trends: compare Wikipedia interest across editions or topics (JSON output).")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_state(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument("--state", default=str(DEFAULT_STATE), help="state file for follow-ups (default: state.json)")
        return sp

    c = with_state(sub.add_parser("compare", help="resolve, fetch, analyse, chart and report in one go"))
    what = c.add_mutually_exclusive_group(required=True)
    what.add_argument("--topic", help="one topic, compared across --langs")
    what.add_argument("--topics", help="comma-separated topics, compared within --langs (usually one language)")
    c.add_argument("--langs", required=True, help="comma-separated Wikipedia language codes, e.g. pl,cs,uk")
    c.add_argument("--years", type=int, default=2, help="period length ending before the data lag (default 2)")
    c.add_argument("--start", help="YYYY-MM-DD (overrides --years)")
    c.add_argument("--end", help="YYYY-MM-DD (default: today minus the data lag)")
    c.add_argument("--seed-lang", help="edition to search first when the topic is written in that language")
    c.add_argument("--qid", help="Wikidata item id (single --topic only) to skip the search")
    c.add_argument("--granularity", choices=["daily", "monthly"], default="daily")
    c.add_argument("--title", help="chart/report title (default: the resolved label)")
    c.add_argument("--out-dir", help="where to write the JSON files, charts and report (default: output/<topic-slug>)")
    c.add_argument("--cache-dir", help="pageviews cache directory (default: data/pageviews)")
    c.add_argument("--refresh", action="store_true", help="ignore cached pageviews")
    c.add_argument("--no-charts", action="store_true", help="skip chart and report generation")
    c.add_argument("--deliver", metavar="DIR", help="directory to copy the PDF/HTML report into (default: the working directory, unless it is the skill itself)")
    c.add_argument("--no-deliver", action="store_true", help="do not copy the report anywhere")
    c.add_argument("--today", help="override today's date (YYYY-MM-DD) for reproducible runs")

    a = with_state(sub.add_parser("add-language", help="re-run the saved query with extra edition(s)"))
    a.add_argument("--lang", required=True, help="language code(s) to add, comma-separated")
    r = with_state(sub.add_parser("remove-language", help="re-run the saved query without edition(s)"))
    r.add_argument("--lang", required=True)
    t = with_state(sub.add_parser("add-topic", help="re-run the saved query with extra topic(s); switches to topics mode"))
    t.add_argument("--topic", required=True, help="topic(s) to add, comma-separated")
    t.add_argument("--seed-lang", help="edition to search first for the added topic")
    t.add_argument("--qid", help="Wikidata item id for the (single) added topic")
    rt_ = with_state(sub.add_parser("remove-topic", help="re-run the saved query without topic(s)"))
    rt_.add_argument("--topic", required=True)
    e = with_state(sub.add_parser("extend-range", help="re-run the saved query over a different period"))
    e.add_argument("--years", type=int)
    e.add_argument("--start", help="YYYY-MM-DD")
    e.add_argument("--end", help="YYYY-MM-DD")
    ch = with_state(sub.add_parser("choose", help="answer the pending clarification from an ambiguous compare"))
    ch.add_argument("--title", help="exact article title to use as the topic")
    ch.add_argument("--qid", help="Wikidata item id to pin for the ambiguous topic")
    ch.add_argument("--seed-lang")
    with_state(sub.add_parser("show", help="print the summary of the last compare (or the pending clarification)"))
    with_state(sub.add_parser("history", help="list the queries run in this session"))
    rp = with_state(sub.add_parser("report", help="copy the last query's PDF report into a directory the user can see"))
    rp.add_argument("--to", help="target directory (default: the current working directory)")
    rp.add_argument("--name", help="file name stem (default: wikipedia-trends-<topics>-<langs>)")
    rp.add_argument("--html", action="store_true", help="also copy the self-contained HTML page")
    return parser


COMMANDS = {
    "compare": run_compare, "add-language": run_add_language, "remove-language": run_remove_language,
    "add-topic": run_add_topic, "remove-topic": run_remove_topic, "extend-range": run_extend_range,
    "choose": run_choose, "show": run_show, "history": run_history, "report": run_report,
}


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point."""
    _force_utf8()
    args = build_parser().parse_args(argv)
    try:
        payload, code = COMMANDS[args.command](args)
    except (ValueError, OSError, KeyError, rt.WikiApiError, fp.PageviewsError) as exc:
        return _emit({"status": "error", "error": str(exc)}, 1)
    return _emit(payload, code)


if __name__ == "__main__":
    sys.exit(main())
