"""One-page report: chart + per-language findings + caveats, as PDF and (optionally) HTML.

The HTML version is rendered from ``assets/report_template.html`` (plain
``string.Template`` placeholders, chart embedded as a data URI).  The PDF is
built with reportlab from the same content dictionary: every section in
full, at fixed sizes, one after another, over as many A4 pages as needed.  reportlab was chosen over weasyprint because the
latter needs a GTK install on Windows; with weasyprint available the HTML
template could be printed to PDF directly.

Usage (CLI):

    python scripts/make_report.py --analysis analysis.json --chart chart.png \\
        --resolved resolved.json --fetched fetched.json --out report.pdf --html report.html
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import date
from pathlib import Path
from string import Template
from typing import Any, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
import re

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.fetch_pageviews import read_json_file  # noqa: E402

TEMPLATE = Path(__file__).resolve().parents[1] / "assets" / "report_template.html"
LIMITATIONS = [
    "Wikipedia pageviews measure curiosity, not willingness to pay or product-market fit: a signal for a hypothesis, not proof.",
    "No article in a language edition does not mean no interest; nobody may have written one yet.",
    "A pageview spike is usually a news event or a viral post rather than organic growth; spike dates are listed so they can be checked.",
    "Editions are compared on indexed levels only (first 8 weeks = 100); audience sizes differ by orders of magnitude.",
]
INK = colors.HexColor("#0b0b0b")
INK2 = colors.HexColor("#52514e")
MUTED = colors.HexColor("#898781")
GRID = colors.HexColor("#e1e0d9")
CONFIDENCE_ORDER = {"High": 0, "Medium": 1, "Low": 2}
MAX_SPIKE_ROWS = 10  # largest spike events listed in the report
STALE_SECONDS = 120  # a sibling chart older than the main chart by more than this was not made in the same run


# --- content ---------------------------------------------------------------------


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:+.0%}"


def build_content(
    analysis: dict[str, Any],
    resolved: Optional[dict[str, Any]] = None,
    fetched: Optional[dict[str, Any]] = None,
    title: Optional[str] = None,
    today: Optional[date] = None,
) -> dict[str, Any]:
    """Everything the report shows, derived from the pipeline JSON (no new numbers)."""
    analyses = analysis.get("analyses", {})
    period = (fetched or {}).get("period", {})
    topic = title or (resolved or {}).get("label") or (resolved or {}).get("topic") or "Interest over time"
    langs = list(analyses)
    subtitle_bits = [f"{len(langs)} language edition(s): {', '.join(langs)}" if langs else "no series"]
    if period:
        subtitle_bits.append(f"{period.get('start')} to {period.get('end')}, {period.get('granularity', 'daily')} pageviews")
    articles = (resolved or {}).get("articles", {})

    findings: list[str] = []
    rows: list[dict[str, Any]] = []
    tiles: list[dict[str, Any]] = []
    spikes: list[dict[str, Any]] = []
    caveats: list[str] = []
    fetched_results = (fetched or {}).get("results", {})
    for key, a in analyses.items():
        name = articles.get(key, {}).get("title") or key
        conf = a.get("confidence", {})
        reasons = conf.get("reasons", [])
        basis = "year-over-year" if a.get("seasonal") else "first vs last 8 weeks"
        fetch_info = fetched_results.get(key, {})
        quality_bits = [f"{a.get('points', 0)} pts"]
        if fetch_info.get("zero_filled_points"):
            quality_bits.append(f"{fetch_info['zero_filled_points']} filled")
        if fetch_info.get("status") == "partial":
            quality_bits.append("partial")
        if a.get("spike_share"):
            quality_bits.append(f"{a['spike_share']:.0%} in spikes")
        tiles.append({
            "key": key, "label": f"{key} · {name}",
            "value": "n/a" if a.get("direction") == "insufficient" else _pct(a.get("headline_change")),
            "direction": a.get("direction"), "basis": basis, "confidence": conf.get("label", "-"),
            "views_per_day": f"{a.get('mean_views_per_day', 0):,.0f}",
        })
        for sp in a.get("spikes", []):
            spikes.append({
                "lang": key, "peak_date": sp.get("peak_date"), "peak_views": sp.get("peak_views"),
                "baseline": sp.get("baseline"), "points": sp.get("points"), "excess_views": sp.get("excess_views", 0),
            })
        if a.get("direction") == "insufficient":
            findings.append(f"{key} ({name}): not enough data for a trend. {reasons[0] if reasons else ''}".strip())
        else:
            findings.append(
                f"{key} ({name}): {a.get('direction')}, {_pct(a.get('headline_change'))} {basis}; "
                f"confidence {conf.get('label')} ({'; '.join(reasons[:2]) or 'no penalties'})."
            )
        rows.append({
            "lang": key, "article": name, "direction": a.get("direction"), "change": _pct(a.get("headline_change")),
            "basis": basis if a.get("direction") != "insufficient" else "-",
            "views_per_day": f"{a.get('mean_views_per_day', 0):,.0f}", "spikes": len(a.get("spikes", [])),
            "confidence": conf.get("label", "-"), "opportunity": a.get("opportunity_score"),
            "quality": ", ".join(quality_bits),
        })
        for c in a.get("caveats", []):
            if "willingness to pay" not in c and c not in caveats:
                caveats.append(f"{key}: {c}")
    for key, ref in articles.items():
        if not ref.get("found") and ref.get("reason"):
            caveats.append(f"{key}: no article analysed — {ref['reason']}")
    for key, result in (fetched or {}).get("results", {}).items():
        for c in result.get("caveats", []):
            entry = f"{key}: {c}"
            if entry not in caveats and "filled with 0" not in c:
                caveats.append(entry)
    for c in (resolved or {}).get("caveats", []):
        if c not in caveats:
            caveats.append(c)

    ranking = (analysis.get("comparison") or {}).get("ranking") or []
    recommendation = None
    if ranking:
        top = ranking[0]
        recommendation = (
            f"Investigate first: {top['key']} (opportunity score {top.get('opportunity_score')}, "
            f"{top.get('direction')}, confidence {top.get('confidence')}). Ranking: "
            + " > ".join(f"{r['key']} {r.get('opportunity_score')}" for r in ranking) + "."
        )
    spikes.sort(key=lambda sp: -(sp["excess_views"] or 0))
    return {
        "title": topic,
        "subtitle": " · ".join(subtitle_bits),
        "findings": findings,
        "recommendation": recommendation,
        "tiles": tiles,
        "spikes": spikes,
        "rows": rows,
        "caveats": caveats,
        "limitations": LIMITATIONS,
        "generated": (today or date.today()).isoformat(),
        "method": "Method: references/methodology.md (28-day median smoothing, log-weekly regression, MAD spike detection, year-over-year seasonality, penalty-based confidence).",
    }


# --- HTML --------------------------------------------------------------------------


def _esc(text: Any) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _data_uri(png: Optional[Path]) -> str:
    if png and Path(png).exists():
        return "data:image/png;base64," + base64.b64encode(Path(png).read_bytes()).decode("ascii")
    return ""


def render_html(
    content: dict[str, Any], chart_png: Optional[Path], template: Path = TEMPLATE,
    extra_charts: Optional[dict[str, Optional[Path]]] = None,
) -> str:
    """Fill the HTML template (charts embedded as data URIs)."""
    chart_uri = _data_uri(chart_png)
    rows_html = "".join(
        f"<tr><td>{_esc(r['lang'])}</td><td>{_esc(r['article'])}</td><td>{_esc(r['direction'])}</td><td>{_esc(r['change'])}</td>"
        f"<td>{_esc(r['basis'])}</td><td>{_esc(r['views_per_day'])}</td><td>{r['spikes']}</td><td>{_esc(r['confidence'])}</td>"
        f"<td>{'' if r['opportunity'] is None else r['opportunity']}</td><td>{_esc(r.get('quality', ''))}</td></tr>"
        for r in content["rows"]
    )
    tiles_html = "".join(
        f"<div class=\"tile\"><div class=\"tile-label\">{_esc(t['label'])}</div><div class=\"tile-value\">{_esc(t['value'])}</div>"
        f"<div class=\"tile-sub\">{_esc(t['direction'])} · {_esc(t['basis'])}</div>"
        f"<div class=\"tile-sub\">{_esc(t['views_per_day'])} views/day · confidence {_esc(t['confidence'])}</div></div>"
        for t in content.get("tiles", [])
    )
    spikes_html = "".join(
        f"<tr><td>{_esc(sp['lang'])}</td><td>{_esc(sp['peak_date'])}</td><td>{sp['peak_views']:,}</td><td>{sp['baseline']:,.0f}</td>"
        f"<td>{sp['points']}</td><td>{sp['excess_views']:,}</td><td></td></tr>"
        for sp in content.get("spikes", [])[:10]
    ) or "<tr><td colspan=\"7\">No spike events detected.</td></tr>"
    labels = {"multiples": "Views per edition (own scales)", "seasonal": "Year over year", "ranking": "Where to look first"}
    appendix_html = "".join(
        f"<h2>{labels[k]}</h2><figure><img src=\"{_data_uri(v)}\" alt=\"{labels[k]}\"></figure>"
        for k, v in (extra_charts or {}).items() if v and Path(v).exists() and k in labels
    )
    findings_html = "".join(f"<li>{_esc(f)}</li>" for f in content["findings"])
    if content.get("recommendation"):
        findings_html += f"<li><strong>{_esc(content['recommendation'])}</strong></li>"
    caveats_html = "".join(f"<li>{_esc(c)}</li>" for c in content["caveats"] + content["limitations"])
    return Template(template.read_text(encoding="utf-8")).safe_substitute(
        title=_esc(content["title"]), subtitle=_esc(content["subtitle"]), chart_data_uri=chart_uri,
        rows_html=rows_html, findings_html=findings_html, caveats_html=caveats_html, tiles_html=tiles_html,
        spikes_html=spikes_html, appendix_html=appendix_html,
        generated=_esc(content["generated"]), method=_esc(content["method"]),
    )


# --- PDF ---------------------------------------------------------------------------


_CJK_FONTS = {  # reportlab built-in CID fonts: rendered by the PDF viewer, no font files needed
    "ja": "HeiseiKakuGo-W5",
    "zh": "STSong-Light",
    "ko": "HYSMyeongJo-Medium",
}
_CJK_RUN = re.compile(
    r"[\u3040-\u30ff\u31f0-\u31ff\uff66-\uff9f]+"  # kana, halfwidth kana -> Japanese
    r"|[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]+"  # hangul -> Korean
    r"|[\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uff65]+"  # CJK ideographs, punctuation, fullwidth
)


def _register_fonts() -> tuple[str, str]:
    """DejaVu (Latin/Cyrillic/Greek, shipped with matplotlib) plus CID fonts for Japanese/Chinese/Korean."""
    for name in _CJK_FONTS.values():
        try:
            pdfmetrics.registerFont(UnicodeCIDFont(name))
        except Exception:  # pragma: no cover
            pass
    try:
        import matplotlib

        font_dir = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
        pdfmetrics.registerFont(TTFont("Report", str(font_dir / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("Report-Bold", str(font_dir / "DejaVuSans-Bold.ttf")))
        return "Report", "Report-Bold"
    except Exception:  # pragma: no cover - fallback without Unicode coverage
        return "Helvetica", "Helvetica-Bold"


def _cjk_font_for(run: str) -> str:
    if any("\u3040" <= ch <= "\u30ff" or "\u31f0" <= ch <= "\u31ff" or "\uff66" <= ch <= "\uff9f" for ch in run):
        return _CJK_FONTS["ja"]
    if any("\uac00" <= ch <= "\ud7af" or "\u1100" <= ch <= "\u11ff" or "\u3130" <= ch <= "\u318f" for ch in run):
        return _CJK_FONTS["ko"]
    return _CJK_FONTS["ja"] if _CJK_DEFAULT == "ja" else _CJK_FONTS["zh"]


_CJK_DEFAULT = "ja"  # bare ideographs (no kana/hangul nearby) are set in the Japanese font by default


def rl(text: Any) -> str:
    """Escape text for a reportlab Paragraph and wrap CJK runs in a font that has the glyphs.

    >>> rl("断続的断食 (ja) & more")
    '<font name="HeiseiKakuGo-W5">断続的断食</font> (ja) &amp; more'
    """
    escaped = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _CJK_RUN.sub(lambda m: f'<font name="{_cjk_font_for(m.group(0))}">{m.group(0)}</font>', escaped)


def _count_pages(pdf_bytes: bytes) -> int:
    return pdf_bytes.count(b"/Type /Page\n") + pdf_bytes.count(b"/Type /Page ") + pdf_bytes.count(b"/Type/Page ") + pdf_bytes.count(b"/Type/Page\n")


TILES_PER_ROW = 4


def _tiles_table(content: dict[str, Any], styles: dict[str, ParagraphStyle], width: float) -> Optional[Table]:
    """Stat tiles for every edition, wrapping into rows of TILES_PER_ROW."""
    tiles = content.get("tiles", [])
    if not tiles:
        return None
    data: list[list[Any]] = []
    style_cmds: list[tuple] = [
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fcfcfb")),
        ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]
    col_w = width / TILES_PER_ROW
    for start in range(0, len(tiles), TILES_PER_ROW):
        chunk = tiles[start : start + TILES_PER_ROW]
        cells = []
        for t in chunk:
            cells.append([
                Paragraph(rl(t["label"]), styles["tile_label"]),
                Paragraph(rl(t["value"]), styles["tile_value"]),
                Paragraph(rl(f"{t['direction']} · {t['basis']}"), styles["tile_sub"]),
                Paragraph(rl(f"{t['views_per_day']} views/day · confidence {t['confidence']}"), styles["tile_sub"]),
            ])
        while len(cells) < TILES_PER_ROW:
            cells.append([Paragraph("", styles["tile_sub"]) for _ in range(4)])
        first_row = len(data)
        for i in range(4):
            data.append([c[i] for c in cells])
        for col in range(len(chunk)):  # a box around each real tile; empty slots stay invisible
            style_cmds.append(("BOX", (col, first_row), (col, first_row + 3), 0.5, GRID))
        if start + TILES_PER_ROW < len(tiles):
            style_cmds.append(("BOTTOMPADDING", (0, first_row + 3), (-1, first_row + 3), 5))
    table = Table(data, colWidths=[col_w] * TILES_PER_ROW, hAlign="LEFT")
    table.setStyle(TableStyle(style_cmds))
    return table


def _spike_table(content: dict[str, Any], styles: dict[str, ParagraphStyle], width: float, limit: int) -> Optional[Table]:
    spikes = content.get("spikes", [])[:limit]
    if not spikes:
        return None
    header = ["Lang", "Peak date", "Peak views", "Level", "Days", "Excess views", "Likely cause (check)"]
    data = [[Paragraph(h, styles["cellh"]) for h in header]]
    for sp in spikes:
        data.append([Paragraph(rl(v), styles["cell"]) for v in (
            sp["lang"], sp["peak_date"], f"{sp['peak_views']:,}", f"{sp['baseline']:,.0f}", sp["points"], f"{sp['excess_views']:,}", "",
        )])
    table = Table(data, colWidths=[w * width for w in (0.07, 0.13, 0.12, 0.10, 0.07, 0.14, 0.37)], hAlign="LEFT")
    table.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, GRID), ("LINEBELOW", (0, 1), (-1, -1), 0.25, GRID),
        ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
    ]))
    return table


def _appendix(extra_charts: Optional[dict[str, Optional[Path]]], styles: dict[str, ParagraphStyle], width: float) -> list[Any]:
    """Extra charts after the summary sections, flowing consecutively; missing files are skipped."""
    labels = {
        "multiples": ("Views per edition (own scales)", "Raw daily views and 28-day median per edition; dots mark spike peaks."),
        "seasonal": ("Year over year", "Weekly views, last 12 months vs the previous 12 months, per edition."),
        "ranking": ("Where to look first", "Opportunity score = growth x confidence x volume (0-100); a ranking heuristic, not a forecast."),
    }
    from reportlab.lib.utils import ImageReader

    max_height = 118 * mm  # so two charts fit on one appendix page; a chart never splits across pages
    flow: list[Any] = []
    for key, path in (extra_charts or {}).items():
        if key not in labels or not path or not Path(path).exists():
            continue
        iw, ih = ImageReader(str(path)).getSize()
        w, h = width, width * ih / iw
        if h > max_height:
            w, h = max_height * iw / ih, max_height
        block = [Paragraph(labels[key][0], styles["h"]), Paragraph(labels[key][1], styles["subtitle"]), Spacer(1, 4),
                 Image(str(path), width=w, height=h, hAlign="LEFT"), Spacer(1, 10)]
        flow.append(KeepTogether(block))
    return flow


def _build_pdf(
    content: dict[str, Any], chart_png: Optional[Path], out: Path,
    extra_charts: Optional[dict[str, Optional[Path]]] = None,
) -> bytes:
    """Every section in full, at fixed sizes, one after another; the document takes as many pages as it needs."""
    scale = 1.0
    regular, bold = _register_fonts()
    styles = {
        "title": ParagraphStyle("t", fontName=bold, fontSize=15 * scale + 2, leading=19 * scale + 2, textColor=INK, alignment=TA_LEFT),
        "subtitle": ParagraphStyle("s", fontName=regular, fontSize=8.5, leading=11, textColor=INK2),
        "h": ParagraphStyle("h", fontName=bold, fontSize=9.5, leading=12, textColor=INK, spaceBefore=4),
        "body": ParagraphStyle("b", fontName=regular, fontSize=8.2 * scale, leading=10.5 * scale, textColor=INK),
        "small": ParagraphStyle("sm", fontName=regular, fontSize=7 * scale, leading=9 * scale, textColor=INK2),
        "foot": ParagraphStyle("f", fontName=regular, fontSize=6.5, leading=8, textColor=MUTED),
        "cell": ParagraphStyle("c", fontName=regular, fontSize=7.2, leading=9, textColor=INK),
        "cellh": ParagraphStyle("ch", fontName=bold, fontSize=7.2, leading=9, textColor=INK2),
        "tile_label": ParagraphStyle("tl", fontName=regular, fontSize=7, leading=9, textColor=INK2),
        "tile_value": ParagraphStyle("tv", fontName=bold, fontSize=13 * scale + 3, leading=15 * scale + 4, textColor=INK),
        "tile_sub": ParagraphStyle("ts", fontName=regular, fontSize=6.8, leading=8.5, textColor=INK2),
    }
    page_w, _ = A4
    margin = 14 * mm
    width = page_w - 2 * margin
    story: list[Any] = [Paragraph(rl(content["title"]), styles["title"]), Paragraph(rl(content["subtitle"]), styles["subtitle"]), Spacer(1, 4)]
    tiles = _tiles_table(content, styles, width)
    if tiles is not None:
        story += [tiles, Spacer(1, 5)]
    if chart_png and Path(chart_png).exists():
        # The chart is the point of the page: always full width at its own aspect ratio.
        # When the page is crowded the shrink loop trims text and table rows, never the chart.
        from reportlab.lib.utils import ImageReader

        iw, ih = ImageReader(str(chart_png)).getSize()
        story += [Image(str(chart_png), width=width, height=width * ih / iw, hAlign="LEFT"), Spacer(1, 4)]

    story.append(Paragraph("Findings", styles["h"]))
    for f in content["findings"]:
        story.append(Paragraph("• " + rl(f), styles["body"]))
    if content.get("recommendation"):
        story.append(Paragraph("<b>" + rl(content["recommendation"]) + "</b>", styles["body"]))

    header = ["Lang", "Article", "Direction", "Change", "Basis", "Views/day", "Spikes", "Confidence", "Opp.", "Data"]
    data = [[Paragraph(h, styles["cellh"]) for h in header]]
    for r in content["rows"]:
        data.append([Paragraph(rl(v), styles["cell"]) for v in (
            r["lang"], r["article"], r["direction"], r["change"], r["basis"], r["views_per_day"], r["spikes"], r["confidence"],
            "" if r["opportunity"] is None else r["opportunity"], r.get("quality", ""),
        )])
    col_w = [w * width for w in (0.05, 0.19, 0.09, 0.08, 0.13, 0.08, 0.06, 0.09, 0.05, 0.18)]
    table = Table(data, colWidths=col_w, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, GRID),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, GRID),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
    ]))
    story += [Spacer(1, 4), table, Spacer(1, 4)]

    spike_table = _spike_table(content, styles, width, limit=MAX_SPIKE_ROWS)
    if spike_table is not None:
        story += [Paragraph("Spike events to check (largest first)", styles["h"]), spike_table, Spacer(1, 4)]

    story.append(Paragraph("Caveats and limitations", styles["h"]))
    for c in content["caveats"] + content["limitations"]:
        story.append(Paragraph("• " + rl(c), styles["small"]))
    story += [Spacer(1, 3), Paragraph(rl(f"{content['method']} Generated {content['generated']} from Wikimedia Pageviews (agent=user)."), styles["foot"])]

    story += _appendix(extra_charts, styles, width)
    doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=margin, rightMargin=margin, topMargin=12 * mm, bottomMargin=10 * mm,
                            title=content["title"], author="wikipedia-trends")
    doc.build(story)
    return Path(out).read_bytes()


def render_pdf(
    content: dict[str, Any], chart_png: Optional[Path], out: Path,
    extra_charts: Optional[dict[str, Optional[Path]]] = None,
) -> int:
    """Write the PDF: summary sections in order at fixed sizes, then the extra charts; returns the page count."""
    return _count_pages(_build_pdf(content, chart_png, Path(out), extra_charts))


# --- CLI -----------------------------------------------------------------------------


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Assemble the one-page PDF report (and optional HTML) from the pipeline outputs.")
    parser.add_argument("--analysis", required=True, help="JSON from analyze_trend.py")
    parser.add_argument("--chart", help="PNG from make_chart.py")
    parser.add_argument("--resolved", help="JSON from resolve_topic.py")
    parser.add_argument("--fetched", help="JSON from fetch_pageviews.py")
    parser.add_argument("--title")
    parser.add_argument("--out", required=True, help="PDF path")
    parser.add_argument("--html", help="also write a self-contained HTML report here")
    parser.add_argument("--multiples", help="small-multiples PNG from make_chart.py (appendix page)")
    parser.add_argument("--seasonal", help="seasonal-profiles PNG from make_chart.py (appendix page)")
    parser.add_argument("--ranking", help="ranking PNG from make_chart.py (appendix page)")
    parser.add_argument("--no-auto-charts", action="store_true",
                        help="do not pick up multiples.png / seasonal.png / ranking.png found next to --chart")
    parser.add_argument("--today", help="override the generation date (YYYY-MM-DD)")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = _parse_args(argv)
    try:
        content = build_content(
            read_json_file(args.analysis),
            read_json_file(args.resolved) if args.resolved else None,
            read_json_file(args.fetched) if args.fetched else None,
            title=args.title, today=date.fromisoformat(args.today) if args.today else None,
        )
        chart = Path(args.chart) if args.chart else None
        extra = {k: (Path(v) if v else None) for k, v in (("multiples", args.multiples), ("seasonal", args.seasonal), ("ranking", args.ranking))}
        stale: dict[str, str] = {}
        if chart is not None and not args.no_auto_charts:  # pick up charts written next to the main chart
            for key in extra:
                candidate = chart.with_name(f"{key}.png")
                if extra[key] is None and candidate.exists():
                    if candidate.stat().st_mtime < chart.stat().st_mtime - STALE_SECONDS:
                        stale[key] = f"{candidate} is older than {chart.name}: re-run make_chart.py (not included)"
                    else:
                        extra[key] = candidate
        included = {k: str(v) for k, v in extra.items() if v and v.exists()}
        missing = {k: str(v) for k, v in extra.items() if v and not v.exists()}
        missing.update(stale)
        pages = render_pdf(content, chart, Path(args.out), extra)
        if args.html:
            Path(args.html).write_text(render_html(content, chart, extra_charts=extra), encoding="utf-8")
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"written": str(Path(args.out)), "pages": pages, "html": args.html, "title": content["title"],
                      "extra_charts_included": included, "extra_charts_missing": missing,
                      "findings": content["findings"], "recommendation": content["recommendation"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
