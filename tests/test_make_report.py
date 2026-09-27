"""Tests for scripts/make_report.py (offline; PDF checked structurally, HTML by content)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from scripts import analyze_trend as at
from scripts import fetch_pageviews as fp
from scripts import make_chart as mc
from scripts import make_report as mr
from tests.fixture_session import FixtureSession
from tests.test_make_chart import ARTICLES


@pytest.fixture
def inputs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    real = fp.PageviewsClient
    monkeypatch.setattr(fp, "PageviewsClient", lambda: real(session=FixtureSession(), backoff_seconds=0))
    resolved = tmp_path / "resolved.json"
    articles = dict(ARTICLES, de={"found": False, "project": "de.wikipedia", "pageviews_title": None, "reason": "No de.wikipedia article is linked on Wikidata."})
    resolved.write_text(json.dumps({"topic": "demo", "label": "demo topic", "articles": articles,
                                    "caveats": ["No article in: de. No article does not mean no interest; nobody may have written one yet."]}, ensure_ascii=False), encoding="utf-8")
    fetched, analysis, chart = tmp_path / "fetched.json", tmp_path / "analysis.json", tmp_path / "chart.png"
    assert fp.main(["--resolved", str(resolved), "--start", "2024-09-01", "--end", "2026-09-01", "--cache-dir", str(tmp_path / "cache"), "--out", str(fetched)]) == 0
    assert at.main(["--fetched", str(fetched), "--out", str(analysis)]) == 0
    # --no-extras: the tests below add sibling charts deliberately to exercise auto-discovery
    assert mc.main(["--fetched", str(fetched), "--analysis", str(analysis), "--resolved", str(resolved), "--out", str(chart), "--no-extras"]) == 0
    return {"resolved": resolved, "fetched": fetched, "analysis": analysis, "chart": chart, "dir": tmp_path}


def content_of(inputs: dict[str, Path]) -> dict:
    return mr.build_content(fp.read_json_file(inputs["analysis"]), fp.read_json_file(inputs["resolved"]),
                            fp.read_json_file(inputs["fetched"]), today=date(2026, 9, 27))


def test_build_content_uses_only_pipeline_numbers(inputs: dict[str, Path]) -> None:
    c = content_of(inputs)
    assert c["title"] == "demo topic" and "3 language edition(s): cs, pl, uk" in c["subtitle"] and "2024-09-01 to 2026-09-01" in c["subtitle"]
    assert len(c["findings"]) == 3
    assert c["findings"][0].startswith("cs (Přerušovaný půst): declining, -77% first vs last 8 weeks; confidence Low (")
    assert "year-over-year" in c["findings"][2] and "confidence High" in c["findings"][2]
    rows = {r["lang"]: r for r in c["rows"]}
    assert rows["cs"]["spikes"] == 4 and rows["uk"]["basis"] == "year-over-year" and rows["pl"]["confidence"] == "Medium"
    assert c["recommendation"].startswith("Investigate first: uk (opportunity score")
    assert any(c_.startswith("de: no article analysed") for c_ in c["caveats"])
    assert any("Largest spike: 508 views on 2025-04-14" in c_ for c_ in c["caveats"])
    assert any("mixes seasons" in c_ for c_ in c["caveats"])
    assert not any("willingness to pay" in c_ for c_ in c["caveats"])  # lives in the fixed limitations instead
    assert c["limitations"] == mr.LIMITATIONS and c["generated"] == "2026-09-27"


def test_render_html_fills_template(inputs: dict[str, Path]) -> None:
    html = mr.render_html(content_of(inputs), inputs["chart"])
    assert "<title>demo topic</title>" in html and 'src="data:image/png;base64,' in html
    assert "Přerušovaný půst" in html and "Астрономія" in html
    assert html.count("<tr>") == 12  # per-edition: header + 3 rows; spikes: header + 7 rows
    assert "$" not in html.split("</style>")[1]  # every placeholder substituted
    assert "not willingness to pay" in html
    # untrusted text is escaped
    c = content_of(inputs)
    c["findings"].append("<script>alert(1)</script>")
    assert "<script>" not in mr.render_html(c, None) and "&lt;script&gt;" in mr.render_html(c, None)


def test_render_pdf_is_one_page_with_chart(inputs: dict[str, Path]) -> None:
    out = inputs["dir"] / "report.pdf"
    pages = mr.render_pdf(content_of(inputs), inputs["chart"], out)
    data = out.read_bytes()
    assert pages >= 1 and data.startswith(b"%PDF-") and mr._count_pages(data) == pages
    assert len(data) > 50_000  # the chart image is embedded
    assert b"/Subtype /Image" in data or b"/Subtype/Image" in data


def test_render_pdf_keeps_everything_and_flows_over_pages(inputs: dict[str, Path]) -> None:
    c = content_of(inputs)
    c["findings"] = [f"Finding {i}: " + "lorem ipsum dolor sit amet " * 12 for i in range(12)]
    c["caveats"] = [f"Caveat {i}: " + "consectetur adipiscing elit " * 10 for i in range(12)]
    c["rows"] = c["rows"] * 3
    out = inputs["dir"] / "long.pdf"
    assert mr.render_pdf(c, inputs["chart"], out) >= 2  # nothing is trimmed or shrunk


def test_render_pdf_without_chart(inputs: dict[str, Path]) -> None:
    out = inputs["dir"] / "nochart.pdf"
    assert mr.render_pdf(content_of(inputs), None, out) == 1 and out.stat().st_size > 1000


def test_cli(inputs: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    pdf, html = inputs["dir"] / "r.pdf", inputs["dir"] / "r.html"
    code = mr.main(["--analysis", str(inputs["analysis"]), "--chart", str(inputs["chart"]), "--resolved", str(inputs["resolved"]),
                    "--fetched", str(inputs["fetched"]), "--out", str(pdf), "--html", str(html), "--today", "2026-09-27"])
    summary = json.loads(capsys.readouterr().out)
    assert code == 0 and summary["pages"] >= 1 and pdf.exists() and html.exists()
    assert summary["title"] == "demo topic" and len(summary["findings"]) == 3
    assert mr.main(["--analysis", str(inputs["dir"] / "nope.json"), "--out", str(pdf)]) == 1
    assert "error" in json.loads(capsys.readouterr().out)


# --- additions: tiles, spike table, data column, appendix ----------------------------


def test_content_has_tiles_spikes_and_quality(inputs: dict[str, Path]) -> None:
    c = content_of(inputs)
    assert [t["key"] for t in c["tiles"]] == ["cs", "pl", "uk"]
    uk = c["tiles"][2]
    assert uk["value"] == "-60%" and uk["basis"] == "year-over-year" and uk["confidence"] == "High"
    assert c["spikes"][0]["lang"] == "cs" and c["spikes"][0]["peak_date"] == "2025-04-14" and c["spikes"][0]["peak_views"] == 508
    assert [sp["excess_views"] for sp in c["spikes"]] == sorted((sp["excess_views"] for sp in c["spikes"]), reverse=True)
    quality = {r["lang"]: r["quality"] for r in c["rows"]}
    assert quality["cs"].startswith("731 pts") and "filled" in quality["cs"] and "in spikes" in quality["cs"]


def test_html_has_tiles_spike_table_and_appendix(inputs: dict[str, Path]) -> None:
    extra = {"multiples": inputs["chart"], "seasonal": None, "ranking": inputs["dir"] / "missing.png"}
    html = mr.render_html(content_of(inputs), inputs["chart"], extra_charts=extra)
    assert html.count('class="tile"') == 3 and "-60%" in html
    assert "Spike events to check" in html and "2025-04-14" in html and "<td>508</td>" in html
    assert "<th>Data</th>" in html and "731 pts" in html
    body = html.split("</style>")[1]
    assert body.count("Views per edition (own scales)") == 2  # heading + alt text
    assert "Year over year" not in body and "Where to look first" not in body  # missing files are skipped
    assert "$" not in html.split("</style>")[1]


def test_pdf_extra_charts_flow_after_the_summary_page(inputs: dict[str, Path]) -> None:
    c = content_of(inputs)
    out = inputs["dir"] / "with_appendix.pdf"
    extra = {"multiples": inputs["chart"], "seasonal": inputs["chart"], "ranking": None}
    plain = mr.render_pdf(c, inputs["chart"], inputs["dir"] / "plain.pdf")
    assert plain >= 1
    assert mr.render_pdf(c, inputs["chart"], inputs["dir"] / "none.pdf", {"multiples": None}) == plain
    two = mr.render_pdf(c, inputs["chart"], out, extra)
    three = {"multiples": inputs["chart"], "seasonal": inputs["chart"], "ranking": inputs["chart"]}
    assert plain <= two <= mr.render_pdf(c, inputs["chart"], inputs["dir"] / "three.pdf", three)  # charts add pages, never drop content


def test_cli_with_appendix(inputs: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    pdf = inputs["dir"] / "a.pdf"
    code = mr.main(["--analysis", str(inputs["analysis"]), "--chart", str(inputs["chart"]), "--fetched", str(inputs["fetched"]),
                    "--multiples", str(inputs["chart"]), "--ranking", str(inputs["dir"] / "nope.png"), "--out", str(pdf), "--today", "2026-09-27"])
    summary = json.loads(capsys.readouterr().out)
    assert code == 0 and summary["pages"] == 2
    assert list(summary["extra_charts_included"]) == ["multiples"] and list(summary["extra_charts_missing"]) == ["ranking"]


def test_cli_auto_discovers_sibling_charts(inputs: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    import shutil

    for name in ("multiples.png", "seasonal.png", "ranking.png"):
        shutil.copy(inputs["chart"], inputs["dir"] / name)
    pdf = inputs["dir"] / "auto.pdf"
    assert mr.main(["--analysis", str(inputs["analysis"]), "--chart", str(inputs["chart"]), "--out", str(pdf), "--today", "2026-09-27"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert set(summary["extra_charts_included"]) == {"multiples", "seasonal", "ranking"}
    with_charts = summary["pages"]
    assert mr.main(["--analysis", str(inputs["analysis"]), "--chart", str(inputs["chart"]), "--out", str(pdf), "--no-auto-charts"]) == 0
    assert json.loads(capsys.readouterr().out)["pages"] < with_charts


# --- many editions, CJK titles, stale sibling charts -----------------------------------


def test_rl_escapes_and_wraps_cjk_runs() -> None:
    assert mr.rl("a < b & c") == "a &lt; b &amp; c"
    assert mr.rl("ja · 断続的断食") == 'ja · <font name="HeiseiKakuGo-W5">断続的断食</font>'
    assert 'name="HYSMyeongJo-Medium">한국어' in mr.rl("ko · 한국어")
    assert mr.rl("uk · Інтервальне голодування") == "uk · Інтервальне голодування"  # Cyrillic stays in DejaVu


def test_tiles_wrap_into_rows_of_four(inputs: dict[str, Path]) -> None:
    c = content_of(inputs)
    c["tiles"] = [dict(c["tiles"][0], key=f"l{i}", label=f"l{i} · edition {i}") for i in range(7)]
    out = inputs["dir"] / "seven.pdf"
    assert mr.render_pdf(c, inputs["chart"], out) >= 1
    html = mr.render_html(c, inputs["chart"])
    assert html.count('class="tile"') == 7
    styles = {"tile_label": mr.ParagraphStyle("a"), "tile_value": mr.ParagraphStyle("b"), "tile_sub": mr.ParagraphStyle("c")}
    table = mr._tiles_table(c, styles, 500.0)
    assert len(table._cellvalues) == 8 and len(table._cellvalues[0]) == 4  # two rows of tiles, four cells each


def test_pdf_with_japanese_title_uses_cid_font(inputs: dict[str, Path]) -> None:
    c = content_of(inputs)
    c["title"] = "断続的断食"
    c["rows"][0]["article"] = "断続的断食"
    out = inputs["dir"] / "ja.pdf"
    assert mr.render_pdf(c, inputs["chart"], out) >= 1
    assert b"HeiseiKakuGo-W5" in out.read_bytes()


def test_cli_refuses_stale_sibling_charts(inputs: dict[str, Path], capsys: pytest.CaptureFixture[str]) -> None:
    import os
    import shutil
    import time

    stale = inputs["dir"] / "ranking.png"
    shutil.copy(inputs["chart"], stale)
    old = time.time() - 3600
    os.utime(stale, (old, old))
    pdf = inputs["dir"] / "stale.pdf"
    assert mr.main(["--analysis", str(inputs["analysis"]), "--chart", str(inputs["chart"]), "--out", str(pdf), "--today", "2026-09-27"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["extra_charts_included"] == {} and "ranking" in summary["extra_charts_missing"]
    assert "older" in summary["extra_charts_missing"]["ranking"]
