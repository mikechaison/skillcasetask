"""Offline tests for scripts/cli.py: the whole pipeline on recorded fixtures."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import cli
from scripts import fetch_pageviews as fp
from scripts import make_chart as mc
from scripts import resolve_topic as rt
from tests.fixture_session import FixtureSession

PERIOD = ["--start", "2024-09-01", "--end", "2026-09-01", "--today", "2026-09-27"]
WW = ["compare", "--topics", "World War I,World War II", "--langs", "uk,en", "--seed-lang", "en", *PERIOD]


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    real_wiki, real_pv = rt.WikiClient, fp.PageviewsClient
    monkeypatch.setattr(rt, "WikiClient", lambda: real_wiki(session=FixtureSession(), backoff_seconds=0))
    monkeypatch.setattr(fp, "PageviewsClient", lambda: real_pv(session=FixtureSession(), backoff_seconds=0))


def run(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[dict, int]:
    code = cli.main(args)
    return json.loads(capsys.readouterr().out), code


def paths(tmp_path: Path) -> list[str]:
    return ["--out-dir", str(tmp_path / "run"), "--cache-dir", str(tmp_path / "cache"), "--state", str(tmp_path / "state.json")]


# --- helpers -------------------------------------------------------------------------


def test_helpers() -> None:
    assert cli.slugify("World War I") == "world-war-i" and cli.slugify("Přerušovaný půst!") == "prerusovany-pust"
    assert cli.slugify("   ") == "topic" and len(cli.slugify("x" * 50)) == 24
    assert cli.result_key("a", "uk", 1, 3) == "uk" and cli.result_key("a", "uk", 2, 1) == "a" and cli.result_key("a", "uk", 2, 2) == "a:uk"


def test_merge_resolved_keys_and_labels() -> None:
    ref_uk = rt.ArticleRef("uk", "uk.wikipedia", True, title="Перша світова війна", pageviews_title="Перша_світова_війна", qid="Q361")
    ref_en = rt.ArticleRef("en", "en.wikipedia", True, title="World War I", pageviews_title="World_War_I", qid="Q361")
    missing = rt.ArticleRef("pl", "pl.wikipedia", False, reason="no sitelink")
    one = rt.ResolvedTopic("World War I", "Q361", "World War I", "en", "World War I", articles={"uk": ref_uk, "pl": missing}, caveats=["c1"])
    two = rt.ResolvedTopic("World War II", "Q362", "World War II", "en", "World War II", articles={"uk": replace(ref_uk, title="Друга"), "pl": missing})

    single = cli.merge_resolved({"world-war-i": one}, ["uk", "pl"])
    assert list(single["articles"]) == ["uk", "pl"] and single["articles"]["uk"]["title"] == "Перша світова війна"
    assert single["caveats"] == ["c1"] and single["label"] == "World War I"

    matrix = cli.merge_resolved({"world-war-i": one, "world-war-ii": two}, ["uk", "en", "pl"])
    assert matrix["articles"]["world-war-i:uk"]["title"] == "Перша світова війна (uk)"
    assert matrix["articles"]["world-war-i:pl"]["found"] is False and matrix["caveats"] == ["World War I: c1"]
    assert matrix["label"] == "World War I vs World War II" and matrix["qids"] == {"world-war-i": "Q361", "world-war-ii": "Q362"}

    one_lang = cli.merge_resolved({"world-war-i": one, "world-war-ii": two}, ["uk"])
    assert one_lang["articles"]["world-war-i"]["title"] == "Перша світова війна"  # no language suffix needed
    assert cli.merge_resolved({"a": replace(one, articles={"en": ref_en})}, ["en"])["articles"]["en"]["title"] == "World War I"


def test_help_for_every_subcommand() -> None:
    parser = cli.build_parser()
    for command in ("compare", "add-language", "show"):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args([command, "--help"])
        assert exc.value.code == 0


# --- one topic across editions --------------------------------------------------------


def test_compare_single_topic(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out, code = run(["compare", "--topic", "astronomy", "--langs", "uk", *PERIOD, *paths(tmp_path)], capsys)
    assert code == 0 and out["status"] == "ok"
    assert out["topics"] == ["astronomy"] and out["qids"] == {"astronomy": "Q333"}
    assert out["period"] == {"start": "2024-09-01", "end": "2026-09-01", "granularity": "daily"}
    uk = out["results"]["uk"]
    assert uk["article"] == "Астрономія" and uk["direction"] == "declining" and uk["seasonal"] is True
    assert uk["confidence"] == "High" and uk["confidence_reasons"] and uk["headline_basis"].startswith("last 12 months")
    assert uk["data"]["points"] == 731 and uk["largest_spike"]["date"] == "2026-07-16"
    assert out["comparison"] is None and out["no_article"] == {}
    assert any("willingness to pay" in line for line in out["limitations"])
    for name in ("resolved", "fetched", "analysis", "chart", "multiples", "seasonal", "ranking", "report_pdf", "report_html"):
        assert Path(out["outputs"][name]).exists(), name
    assert out["outputs"]["report_pages"] >= 2
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["topics"] == ["astronomy"] and state["langs"] == ["uk"] and state["out_dir"] == str(tmp_path / "run")


def test_summary_no_article_and_skipped_keys(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out, code = run(["compare", "--topic", "intermittent fasting", "--langs", "en,cs,pl", *PERIOD, "--no-charts", *paths(tmp_path)], capsys)
    assert code == 0 and list(out["results"]) == ["en", "cs"] and list(out["no_article"]) == ["pl"]
    assert "Wikidata" in out["no_article"]["pl"]
    assert fp.read_json_file(tmp_path / "run" / "fetched.json")["skipped"] == ["pl"]
    assert [r["key"] for r in out["comparison"]["ranking"]] and "chart" not in out["outputs"]


def test_qid_pins_the_item(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out, code = run(["compare", "--topic", "whatever", "--langs", "uk", "--qid", "Q333", *PERIOD, "--no-charts", *paths(tmp_path)], capsys)
    assert code == 0 and out["qids"] == {"whatever": "Q333"} and out["results"]["uk"]["article"] == "Астрономія"


def test_add_language_reruns_with_state(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "state.json"
    run(["compare", "--topic", "astronomy", "--langs", "uk", *PERIOD, "--no-charts", *paths(tmp_path)], capsys)
    out, code = run(["add-language", "--lang", "pl", "--state", str(state)], capsys)
    assert code == 0 and out["follow_up"]["changed"]["added_languages"] == ["pl"]
    assert list(out["results"]) == ["uk", "pl"] and out["results"]["pl"]["article"] == "Astronomia"
    assert len(out["comparison"]["ranking"]) == 2
    assert json.loads(state.read_text(encoding="utf-8"))["langs"] == ["uk", "pl"]
    shown, code = run(["show", "--state", str(state)], capsys)
    assert code == 0 and list(shown["results"]) == ["uk", "pl"]


# --- topics within one edition, and topics x editions --------------------------------


def test_compare_topics_mode_in_one_language(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out, code = run(["compare", "--topics", "astronomy, intermittent fasting", "--langs", "uk", *PERIOD, "--no-charts", *paths(tmp_path)], capsys)
    assert code == 0 and out["status"] == "ok"
    assert list(out["results"]) == ["astronomy", "intermittent-fasting"]  # topic slugs as keys
    assert out["label"] == "astronomy vs intermittent fasting"
    assert out["results"]["intermittent-fasting"]["article"] == "Інтервальне голодування"
    assert [r["key"] for r in out["comparison"]["ranking"]] == ["astronomy", "intermittent-fasting"]
    merged = json.loads((tmp_path / "run" / "resolved.json").read_text(encoding="utf-8"))
    assert merged["articles"]["astronomy"]["topic"] == "astronomy" and merged["qids"]["intermittent-fasting"] == "Q1666254"


def test_topics_times_languages_matrix(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out, code = run([*WW, *paths(tmp_path)], capsys)
    assert code == 0 and out["status"] == "ok"
    assert list(out["results"]) == ["world-war-i:uk", "world-war-i:en", "world-war-ii:uk", "world-war-ii:en"]
    assert out["qids"] == {"world-war-i": "Q361", "world-war-ii": "Q362"}
    r = out["results"]
    assert r["world-war-i:uk"]["article"] == "Перша світова війна (uk)" and r["world-war-i:en"]["article"] == "World War I (en)"
    assert r["world-war-ii:uk"]["topic"] == "World War II" and r["world-war-ii:uk"]["lang"] == "uk"
    assert all(v["direction"] in ("growing", "declining", "flat") for v in r.values())
    assert any(v["seasonal"] for v in r.values())  # anniversaries drive a yearly pattern in at least one series
    assert r["world-war-ii:en"]["mean_views_per_day"] > r["world-war-ii:uk"]["mean_views_per_day"] * 10
    assert len(out["comparison"]["ranking"]) == 4 and {x["key"] for x in out["comparison"]["ranking"]} == set(r)
    assert out["label"] == "World War I vs World War II"
    for name in ("chart", "multiples", "seasonal", "ranking", "report_pdf", "report_html"):
        assert Path(out["outputs"][name]).exists()
    html = Path(out["outputs"]["report_html"]).read_text(encoding="utf-8")
    assert html.count('class="tile"') == 4 and "Перша світова війна (uk)" in html


def test_topics_mode_chart_labels_carry_language(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out, _ = run([*WW, "--no-charts", *paths(tmp_path)], capsys)
    fetched = fp.read_json_file(out["outputs"]["fetched"])
    analysis = fp.read_json_file(out["outputs"]["analysis"])
    merged = fp.read_json_file(out["outputs"]["resolved"])
    series, skipped = mc.load_chart_series(fetched, analysis, merged)
    assert skipped == {} and [s.label for s in series] == [
        "world-war-i:uk · Перша світова війна (uk)", "world-war-i:en · World War I (en)",
        "world-war-ii:uk · Друга світова війна (uk)", "world-war-ii:en · World War II (en)",
    ]


def test_add_language_in_topics_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    state = tmp_path / "state.json"
    first, _ = run(["compare", "--topics", "World War I,World War II", "--langs", "uk", "--seed-lang", "en", *PERIOD, "--no-charts",
                    *paths(tmp_path)], capsys)
    assert list(first["results"]) == ["world-war-i", "world-war-ii"]  # one language: slug keys
    out, code = run(["add-language", "--lang", "en", "--state", str(state)], capsys)
    assert code == 0 and out["follow_up"]["changed"]["added_languages"] == ["en"]
    assert list(out["results"]) == ["world-war-i:uk", "world-war-i:en", "world-war-ii:uk", "world-war-ii:en"]  # keys switch to slug:lang
    assert json.loads(state.read_text(encoding="utf-8"))["langs"] == ["uk", "en"]
    again, code = run(["add-language", "--lang", "en", "--state", str(state)], capsys)  # idempotent
    assert code == 0 and list(again["results"]) == list(out["results"])


def test_default_out_dir_and_title(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "DEFAULT_OUT_ROOT", tmp_path / "output")
    out, code = run(["compare", "--topics", "World War I,World War II", "--langs", "uk", "--seed-lang", "en", *PERIOD, "--no-charts",
                     "--title", "Wars on ukwiki", "--cache-dir", str(tmp_path / "cache"), "--state", str(tmp_path / "state.json")], capsys)
    assert code == 0 and out["outputs"]["dir"] == str(tmp_path / "output" / "world-war-i-world-war-ii")
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["title"] == "Wars on ukwiki"


# --- failures and bookkeeping -----------------------------------------------------------


def test_ambiguous_topic_exits_2_with_ranked_candidates(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out, code = run(["compare", "--topic", "mercury", "--langs", "en,pl", *PERIOD, *paths(tmp_path)], capsys)
    assert code == 2 and out["status"] == "ambiguous" and out["topic"] == "mercury"
    assert "disambiguation page" in out["message"]
    assert out["candidates"][0]["title"] == "Mercury (planet)"  # ranked like the caveat: meanings first
    assert "choose" in out["how_to_fix"] and out["pending_saved"] is True
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert saved["pending"]["topic"] == "mercury" and saved["langs"] == ["en", "pl"]  # the question is kept for `choose`


def test_second_topic_ambiguous_stops_before_writing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out, code = run(["compare", "--topics", "astronomy,mercury", "--langs", "en", *PERIOD, *paths(tmp_path)], capsys)
    assert code == 2 and out["status"] == "ambiguous" and out["topic"] == "mercury"
    assert not (tmp_path / "run" / "resolved.json").exists()  # nothing analysed
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["pending"]["topic"] == "mercury"


def test_topic_without_any_article_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out, code = run(["compare", "--topic", "intermittent fasting", "--langs", "pl", *PERIOD, *paths(tmp_path)], capsys)
    assert code == 1 and out["status"] == "not_found"
    assert "does not mean no interest" in out["message"]


def test_errors_are_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out, code = run(["compare", "--topic", "astronomy", "--langs", "en_US", "--state", str(tmp_path / "state.json")], capsys)
    assert code == 1 and out["status"] == "error" and "invalid language code" in out["error"]
    out, code = run(["show", "--state", str(tmp_path / "missing.json")], capsys)
    assert code == 1 and "run `compare` first" in out["error"]


def test_summary_file_matches_stdout_and_show(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out, _ = run(["compare", "--topic", "astronomy", "--langs", "uk", *PERIOD, "--no-charts", *paths(tmp_path)], capsys)
    assert json.loads((tmp_path / "run" / "summary.json").read_text(encoding="utf-8")) == out
    shown, _ = run(["show", "--state", str(tmp_path / "state.json")], capsys)
    assert shown == out


def test_second_run_is_served_from_cache(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["compare", "--topic", "astronomy", "--langs", "uk", *PERIOD, "--no-charts", *paths(tmp_path)]
    run(args, capsys)
    assert fp.read_json_file(tmp_path / "run" / "fetched.json")["results"]["uk"]["from_cache"] is False
    run(args, capsys)
    assert fp.read_json_file(tmp_path / "run" / "fetched.json")["results"]["uk"]["from_cache"] is True
    run([*args, "--refresh"], capsys)
    assert fp.read_json_file(tmp_path / "run" / "fetched.json")["results"]["uk"]["from_cache"] is False


# --- delivering the report to the working directory --------------------------------------


def test_deliver_dir_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ns = lambda **kw: argparse.Namespace(**{"deliver": None, "no_deliver": False, **kw})  # noqa: E731
    monkeypatch.chdir(tmp_path)
    assert cli._deliver_dir(ns()) == tmp_path.resolve()
    assert cli._deliver_dir(ns(no_deliver=True)) is None
    assert cli._deliver_dir(ns(deliver=str(tmp_path / "x"))) == tmp_path / "x"
    monkeypatch.chdir(cli.SKILL_ROOT / "tests")
    assert cli._deliver_dir(ns()) is None, "inside the skill directory the report stays in output/"


def test_compare_delivers_report_to_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    here = tmp_path / "founder"
    out, code = run(["compare", "--topic", "astronomy", "--langs", "uk", *PERIOD, "--deliver", str(here), *paths(tmp_path)], capsys)
    assert code == 0 and out["outputs"]["delivered"]["dir"] == str(here.resolve())
    pdf, html = Path(out["outputs"]["delivered"]["pdf"]), Path(out["outputs"]["delivered"]["html"])
    assert pdf.name == "wikipedia-trends-astronomy-uk.pdf" and pdf.read_bytes()[:5] == b"%PDF-"
    assert html.name == "wikipedia-trends-astronomy-uk.html" and "data:image/png;base64" in html.read_text(encoding="utf-8")
    assert pdf.stat().st_size == Path(out["outputs"]["report_pdf"]).stat().st_size
    out, code = run(["compare", "--topic", "astronomy", "--langs", "uk", *PERIOD, "--no-deliver", *paths(tmp_path)], capsys)
    assert code == 0 and "delivered" not in out["outputs"]
    out, code = run(["compare", "--topic", "astronomy", "--langs", "uk", *PERIOD, "--deliver", str(here / "r.pdf"), *paths(tmp_path)], capsys)
    assert code == 1 and "must be a directory" in out["error"]
