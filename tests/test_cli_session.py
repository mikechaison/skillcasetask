"""Session state and follow-up commands of scripts/cli.py (offline, recorded fixtures)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import cli
from scripts import fetch_pageviews as fp
from scripts import resolve_topic as rt
from tests.fixture_session import FixtureSession

PERIOD = ["--start", "2024-09-01", "--end", "2026-09-01", "--today", "2026-09-27"]
ONE_YEAR = ["--start", "2025-09-01", "--end", "2026-09-01", "--today", "2026-09-27"]


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    real_wiki, real_pv = rt.WikiClient, fp.PageviewsClient
    monkeypatch.setattr(rt, "WikiClient", lambda: real_wiki(session=FixtureSession(), backoff_seconds=0))
    monkeypatch.setattr(fp, "PageviewsClient", lambda: real_pv(session=FixtureSession(), backoff_seconds=0))


def run(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[dict, int]:
    code = cli.main(args)
    return json.loads(capsys.readouterr().out), code


@pytest.fixture
def ws(tmp_path: Path) -> dict[str, str]:
    return {"state": str(tmp_path / "state.json"), "out": str(tmp_path / "run"), "cache": str(tmp_path / "cache")}


def common(ws: dict[str, str]) -> list[str]:
    return ["--no-charts", "--out-dir", ws["out"], "--cache-dir", ws["cache"], "--state", ws["state"]]


def state_of(ws: dict[str, str]) -> dict:
    return json.loads(Path(ws["state"]).read_text(encoding="utf-8"))


# --- state.json contents ---------------------------------------------------------------


def test_state_holds_query_outputs_and_history(ws: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    run(["compare", "--topic", "astronomy", "--langs", "uk", *PERIOD, *common(ws)], capsys)
    st = state_of(ws)
    assert st["topics"] == ["astronomy"] and st["langs"] == ["uk"] and st["start"] == "2024-09-01" and st["pending"] is None
    assert st["qids"] == {} and st["outputs"]["analysis"].endswith("analysis.json")
    assert len(st["history"]) == 1 and st["history"][0]["command"] == "compare" and st["history"][0]["status"] == "ok"
    assert st["history"][0]["keys"] == ["uk"] and "at" in st["history"][0]


def test_history_command_lists_runs(ws: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    run(["compare", "--topic", "astronomy", "--langs", "uk", *PERIOD, *common(ws)], capsys)
    run(["add-language", "--lang", "pl", "--state", ws["state"]], capsys)
    out, code = run(["history", "--state", ws["state"]], capsys)
    assert code == 0 and [h["command"] for h in out["history"]] == ["compare", "add-language"]
    assert out["history"][1]["changed"] == {"added_languages": ["pl"], "langs": ["uk", "pl"]}
    assert out["current"]["langs"] == ["uk", "pl"] and out["pending"] is None
    empty, code = run(["history", "--state", str(Path(ws["state"]).with_name("none.json"))], capsys)
    assert code == 0 and empty["history"] == []


# --- language follow-ups ------------------------------------------------------------------


def test_add_and_remove_language(ws: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    run(["compare", "--topic", "astronomy", "--langs", "uk", *PERIOD, *common(ws)], capsys)
    out, code = run(["add-language", "--lang", "pl", "--state", ws["state"]], capsys)
    assert code == 0 and list(out["results"]) == ["uk", "pl"]
    assert out["follow_up"] == {"command": "add-language", "changed": {"added_languages": ["pl"], "langs": ["uk", "pl"]}}
    out, code = run(["remove-language", "--lang", "pl", "--state", ws["state"]], capsys)
    assert code == 0 and list(out["results"]) == ["uk"] and out["follow_up"]["changed"]["removed_languages"] == ["pl"]
    out, code = run(["remove-language", "--lang", "uk", "--state", ws["state"]], capsys)
    assert code == 1 and "cannot remove every language" in out["error"]


def test_follow_up_itself_can_become_ambiguous(ws: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    # "astronomy" searched on pl.wikipedia alone exact-matches a different item than on en.wikipedia,
    # so dropping uk turns the saved query ambiguous; the pending question is saved like any other.
    run(["compare", "--topic", "astronomy", "--langs", "uk,pl", *PERIOD, *common(ws)], capsys)
    out, code = run(["remove-language", "--lang", "uk", "--state", ws["state"]], capsys)
    assert code == 2 and out["status"] == "ambiguous" and out["pending_saved"] is True
    st = state_of(ws)
    assert st["pending"]["topic"] == "astronomy" and st["langs"] == ["pl"]
    assert st["history"][-1]["command"] == "remove-language" and st["history"][-1]["status"] == "ambiguous"
    out, code = run(["choose", "--qid", "Q333", "--state", ws["state"]], capsys)
    assert code == 0 and list(out["results"]) == ["pl"] and out["results"]["pl"]["article"] == "Astronomia"


# --- topic follow-ups ----------------------------------------------------------------------


def test_add_topic_switches_to_topics_mode_and_remove_topic_switches_back(ws: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    first, _ = run(["compare", "--topic", "astronomy", "--langs", "uk", *PERIOD, *common(ws)], capsys)
    assert list(first["results"]) == ["uk"]
    out, code = run(["add-topic", "--topic", "intermittent fasting", "--state", ws["state"]], capsys)
    assert code == 0 and list(out["results"]) == ["astronomy", "intermittent-fasting"]
    assert out["follow_up"]["changed"]["added_topics"] == ["intermittent fasting"] and state_of(ws)["topics"] == ["astronomy", "intermittent fasting"]
    out, code = run(["remove-topic", "--topic", "astronomy", "--state", ws["state"]], capsys)
    assert code == 0 and list(out["results"]) == ["uk"] and out["topics"] == ["intermittent fasting"]
    out, code = run(["remove-topic", "--topic", "nope", "--state", ws["state"]], capsys)
    assert code == 1 and "no such topic" in out["error"]


def test_add_topic_with_qid_pins_only_that_topic(ws: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    run(["compare", "--topic", "intermittent fasting", "--langs", "uk", *PERIOD, *common(ws)], capsys)
    out, code = run(["add-topic", "--topic", "whatever", "--qid", "Q333", "--state", ws["state"]], capsys)
    assert code == 0 and out["qids"] == {"intermittent-fasting": "Q1666254", "whatever": "Q333"}
    assert out["results"]["whatever"]["article"] == "Астрономія" and state_of(ws)["qids"] == {"whatever": "Q333"}


# --- period follow-up -------------------------------------------------------------------------


def test_extend_range_with_dates(ws: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    first, _ = run(["compare", "--topic", "astronomy", "--langs", "uk", *ONE_YEAR, *common(ws)], capsys)
    assert first["period"]["start"] == "2025-09-01" and first["results"]["uk"]["data"]["points"] == 366
    out, code = run(["extend-range", "--start", "2024-09-01", "--state", ws["state"]], capsys)
    assert code == 0 and out["period"] == {"start": "2024-09-01", "end": "2026-09-01", "granularity": "daily"}
    assert out["results"]["uk"]["data"]["points"] == 731 and out["follow_up"]["changed"] == {"start": "2024-09-01", "end": "2026-09-01"}
    assert out["results"]["uk"]["seasonal"] is True  # two years now allow the seasonal check
    assert state_of(ws)["start"] == "2024-09-01"


def test_extend_range_requires_an_argument(ws: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    run(["compare", "--topic", "astronomy", "--langs", "uk", *PERIOD, *common(ws)], capsys)
    out, code = run(["extend-range", "--state", ws["state"]], capsys)
    assert code == 1 and "--years" in out["error"]


def test_extend_range_years_replaces_explicit_dates(ws: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    # `--years` clears --start/--end; with today pinned to 2026-09-27 the two-year default is 2024-09-25..2026-09-25,
    # which is not recorded, so the follow-up must fail loudly rather than reuse the old dates.
    run(["compare", "--topic", "astronomy", "--langs", "uk", *PERIOD, *common(ws)], capsys)
    with pytest.raises(AssertionError, match="unexpected request"):
        cli.main(["extend-range", "--years", "2", "--state", ws["state"]])


# --- clarification flow -------------------------------------------------------------------------


def test_ambiguous_compare_saves_pending_and_choose_by_title_finishes(ws: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    out, code = run(["compare", "--topic", "mercury", "--langs", "en,pl", *PERIOD, *common(ws)], capsys)
    assert code == 2 and out["pending_saved"] is True and "choose --title" in out["how_to_fix"]
    st = state_of(ws)
    assert st["pending"]["topic"] == "mercury" and st["pending"]["candidates"][0]["title"] == "Mercury (planet)"
    assert st["topics"] == ["mercury"] and st["langs"] == ["en", "pl"] and st["history"][-1]["status"] == "ambiguous"
    shown, code = run(["show", "--state", ws["state"]], capsys)
    assert code == 2 and shown["status"] == "ambiguous" and shown["pending"]["topic"] == "mercury"

    out, code = run(["choose", "--title", "Mercury (planet)", "--state", ws["state"]], capsys)
    assert code == 0 and out["topics"] == ["Mercury (planet)"] and out["qids"] == {"mercury-planet": "Q308"}
    assert out["results"]["pl"]["article"] == "Merkury" and out["follow_up"]["changed"] == {"topic_replaced": {"mercury": "Mercury (planet)"}}
    assert state_of(ws)["pending"] is None and state_of(ws)["topics"] == ["Mercury (planet)"]


def test_choose_by_qid(ws: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    run(["compare", "--topic", "mercury", "--langs", "en,pl", *PERIOD, *common(ws)], capsys)
    out, code = run(["choose", "--qid", "Q308", "--state", ws["state"]], capsys)
    assert code == 0 and out["topics"] == ["mercury"] and out["qids"] == {"mercury": "Q308"}
    assert out["results"]["en"]["article"] == "Mercury (planet)" and state_of(ws)["qids"] == {"mercury": "Q308"}
    assert state_of(ws)["pending"] is None


def test_choose_in_topics_mode_replaces_only_the_ambiguous_topic(ws: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    out, code = run(["compare", "--topics", "astronomy,mercury", "--langs", "en", *PERIOD, *common(ws)], capsys)
    assert code == 2 and state_of(ws)["pending"]["topic"] == "mercury"
    out, code = run(["choose", "--qid", "Q308", "--state", ws["state"]], capsys)
    assert code == 0 and out["topics"] == ["astronomy", "mercury"] and list(out["results"]) == ["astronomy", "mercury"]
    assert out["results"]["mercury"]["article"] == "Mercury (planet)"


def test_choose_errors(ws: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    run(["compare", "--topic", "astronomy", "--langs", "uk", *PERIOD, *common(ws)], capsys)
    out, code = run(["choose", "--qid", "Q308", "--state", ws["state"]], capsys)
    assert code == 1 and "nothing to choose" in out["error"]
    run(["compare", "--topic", "mercury", "--langs", "en,pl", *PERIOD, *common(ws)], capsys)
    out, code = run(["choose", "--state", ws["state"]], capsys)
    assert code == 1 and "exactly one" in out["error"]
    out, code = run(["choose", "--title", "x", "--qid", "Q1", "--state", ws["state"]], capsys)
    assert code == 1


def test_qid_flag_rejected_in_topics_mode(ws: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    out, code = run(["compare", "--topics", "astronomy,mercury", "--langs", "en", "--qid", "Q308", *PERIOD, *common(ws)], capsys)
    assert code == 1 and "choose --qid" in out["error"]


def test_follow_ups_require_a_saved_query(ws: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    for cmd in (["add-language", "--lang", "pl"], ["add-topic", "--topic", "x"], ["extend-range", "--years", "3"], ["remove-topic", "--topic", "x"]):
        out, code = run([*cmd, "--state", ws["state"]], capsys)
        assert code == 1 and "run `compare` first" in out["error"], cmd


def test_help_for_every_subcommand() -> None:
    parser = cli.build_parser()
    for command in cli.COMMANDS:
        with pytest.raises(SystemExit) as exc:
            parser.parse_args([command, "--help"])
        assert exc.value.code == 0


def test_out_dir_that_looks_like_a_file_is_rejected(ws: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    # an agent once passed --out-dir output.json (confusing it with the modules' --out) and got a folder named output.json
    out, code = run(["compare", "--topic", "astronomy", "--langs", "uk", *PERIOD, "--out-dir", str(Path(ws["out"]) / "output.json"),
                     "--cache-dir", ws["cache"], "--state", ws["state"]], capsys)
    assert code == 1 and "--out-dir must be a directory" in out["error"]
    assert not (Path(ws["out"]) / "output.json").exists()


# --- report export -------------------------------------------------------------------------


def test_report_copies_last_report_into_a_directory(ws: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out, code = run(["report", "--state", ws["state"]], capsys)
    assert code == 1 and "run `compare` first" in out["error"]
    run(["compare", "--topic", "astronomy", "--langs", "uk", *PERIOD, *common(ws)], capsys)
    out, code = run(["report", "--to", str(tmp_path / "deliver"), "--state", ws["state"]], capsys)
    assert code == 1 and "no report" in out["error"]
    st = state_of(ws)
    pdf, html = Path(ws["out"]) / "report.pdf", Path(ws["out"]) / "report.html"
    pdf.write_bytes(b"%PDF-1.4 fake")
    html.write_text("<html></html>", encoding="utf-8")
    st["outputs"].update({"report_pdf": str(pdf), "report_html": str(html), "report_pages": 2})
    Path(ws["state"]).write_text(json.dumps(st), encoding="utf-8")
    out, code = run(["report", "--to", str(tmp_path / "deliver"), "--html", "--state", ws["state"]], capsys)
    assert code == 0 and out["status"] == "ok" and out["pages"] == 2 and out["topics"] == ["astronomy"]
    assert out["period"]["start"] == "2024-09-01" and out["source_dir"] == str(pdf.parent)
    assert Path(out["report"]["pdf"]).name == "wikipedia-trends-astronomy-uk.pdf" and Path(out["report"]["pdf"]).read_bytes() == b"%PDF-1.4 fake"
    assert Path(out["report"]["html"]).read_text(encoding="utf-8") == "<html></html>"
    out, code = run(["report", "--to", str(tmp_path / "deliver" / "x.pdf"), "--state", ws["state"]], capsys)
    assert code == 1 and "must be a directory" in out["error"]
    out, code = run(["report", "--name", "fasting report.pdf", "--to", str(tmp_path / "deliver"), "--state", ws["state"]], capsys)
    assert code == 0 and Path(out["report"]["pdf"]).name == "fasting report.pdf"


def test_report_while_clarification_pending(ws: dict[str, str], capsys: pytest.CaptureFixture[str]) -> None:
    run(["compare", "--topic", "mercury", "--langs", "en,pl", *PERIOD, *common(ws)], capsys)
    out, code = run(["report", "--state", ws["state"]], capsys)
    assert code == 2 and out["status"] == "ambiguous" and out["pending"]["topic"] == "mercury"
