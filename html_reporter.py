"""
html_reporter.py
================
Generates a self-contained HTML comparison report from two folders of text
reports, using the comparison engine in report_comparator.py.

Usage:
    # Basic (paths resolved from wherever the script runs)
    python html_reporter.py folder_a/ folder_b/ --output results/report.html

    # With Windows path mapping (when running inside a Linux sandbox)
    python html_reporter.py folder_a/ folder_b/ --output results/report.html \\
        --linux-base /sessions/dreamy-admiring-noether/mnt/outputs \\
        --windows-base "C:\\Users\\kalia\\...\\outputs"

    # Ignore date/time differences
    python html_reporter.py folder_a/ folder_b/ --output results/report.html --ignore-dates

    # Extra file extensions
    python html_reporter.py folder_a/ folder_b/ --output results/report.html --ext .log
"""

import argparse
import base64
import difflib
import gzip
import json as _json
import re
import sys
from html import escape
from pathlib import Path
from typing import List, Optional, Tuple, Union
from datetime import datetime

# ---------------------------------------------------------------------------
# Import the comparison engine
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))
from report_comparator import (
    scan_folder, match_filenames, compare_folder_pair,
    FolderMatchResult, FilePairOutcome,
    diff_opcodes,
    load_split_config, SplitRule, FieldDef,
    write_txn_csv, extract_txn_csv_for_file,
    write_section_csv,
    write_csv_xlsx,
)
from date_utils import remove_dates_from_line, has_dates

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def to_win_path(path: Path, linux_base: str, windows_base: str) -> str:
    """Remap a Linux absolute path to its Windows equivalent."""
    linux_base = linux_base.rstrip("/")
    rel = str(path).replace(linux_base, "").lstrip("/")
    win = windows_base.rstrip("\\") + "\\" + rel.replace("/", "\\")
    return win


def to_file_url(win_or_posix_path: str) -> str:
    """Convert an absolute path string to a browser-safe file:// URL."""
    p = win_or_posix_path.replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p          # Windows drive letter: C:/... → /C:/...
    return "file://" + p


def resolve_paths(path: Path,
                  linux_base: Optional[str],
                  windows_base: Optional[str]) -> tuple:
    """
    Return (windows_path_str, file_url) for a given Path.
    Falls back to the resolved posix path if no mapping is given.
    """
    if linux_base and windows_base:
        win = to_win_path(path, linux_base, windows_base)
        url = to_file_url(win)
    else:
        resolved = str(path.resolve())
        win = resolved
        url = to_file_url(resolved)
    return win, url


# ---------------------------------------------------------------------------
# Verdict helpers
# ---------------------------------------------------------------------------

def verdict_css(ratio: float) -> str:
    if ratio == 1.00: return "identical"
    if ratio >= 0.85: return "minor"
    if ratio >= 0.60: return "moderate"
    return "significant"


def verdict_label(ratio: float) -> str:
    if ratio == 1.00: return "IDENTICAL"
    if ratio >= 0.85: return "MINOR"
    if ratio >= 0.60: return "MODERATE"
    return "SIGNIFICANT"


def section_css(sec: dict) -> str:
    if sec["status"] == "added":   return "s-added"
    if sec["status"] == "removed": return "s-removed"
    r = sec.get("diff", {}).get("similarity_ratio", 1.0)
    if r == 1.00: return "s-identical"
    if r >= 0.85: return "s-minor"
    if r >= 0.60: return "s-moderate"
    return "s-significant"


# ---------------------------------------------------------------------------
# HTML fragments
# ---------------------------------------------------------------------------

def bar(pct: float, css_class: str) -> str:
    return (
        f'<div class="bar-wrap">'
        f'<div class="bar {css_class}" style="width:{pct:.1f}%"></div>'
        f'<span class="bar-label">{pct:.1f}%</span>'
        f'</div>'
    )


def stat_box(label: str, value, sub: str = "") -> str:
    return (
        f'<div class="stat-box">'
        f'<span class="stat-val">{value}</span>'
        f'<span class="stat-lbl">{label}</span>'
        f'{"<span class=stat-sub>" + sub + "</span>" if sub else ""}'
        f'</div>'
    )


def file_link(label: str, url: str, name: str) -> str:
    return (
        f'<a class="file-link" href="{url}" title="{url}" target="_blank">'
        f'<svg viewBox="0 0 16 16" width="14" height="14"><path fill="currentColor" '
        f'd="M4 0h6l4 4v11a1 1 0 01-1 1H3a1 1 0 01-1-1V1a1 1 0 011-1zm6 0v4h4"/></svg>'
        f' {label}: <code>{name}</code></a>'
    )



# ---------------------------------------------------------------------------
# Diff rendering
# ---------------------------------------------------------------------------

def word_diff_inline(old: str, new: str, normalize=None) -> Tuple[str, str]:
    """
    Return (old_html, new_html) where changed words are wrapped in <mark>
    tags so each row shows exactly which tokens were altered.

    Tokenises on whitespace and common field delimiters (,;|) so individual
    CSV/delimited fields are highlighted independently.

    When normalize is provided (e.g. remove_dates_from_line), SequenceMatcher
    operates on normalized tokens so date-only token differences are not
    highlighted — but the original token text is always displayed.
    """
    tok_old = re.split(r'(\s+|[,;|])', old)
    tok_new = re.split(r'(\s+|[,;|])', new)

    # Gate normalize behind has_dates: re.search short-circuits immediately for
    # tokens with no date patterns, avoiding 200× re.sub calls per line.
    if normalize:
        cmp_old = [normalize(t) if has_dates(t) else t for t in tok_old]
        cmp_new = [normalize(t) if has_dates(t) else t for t in tok_new]
    else:
        cmp_old = tok_old
        cmp_new = tok_new

    # autojunk=False is exact but O(n²) when repeated tokens (spaces, commas) dominate
    # long sequences. For sequences > 200 tokens, enable autojunk so repeated delimiter
    # tokens are treated as junk and matching stays O(content_tokens) rather than O(n²).
    use_autojunk = len(cmp_old) > 200 or len(cmp_new) > 200
    sm = difflib.SequenceMatcher(None, cmp_old, cmp_new, autojunk=use_autojunk)
    old_parts: List[str] = []
    new_parts: List[str] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        chunk_old = escape(''.join(tok_old[i1:i2]))
        chunk_new = escape(''.join(tok_new[j1:j2]))
        if tag == 'equal':
            old_parts.append(chunk_old)
            new_parts.append(chunk_new)
        elif tag == 'delete':
            old_parts.append(f'<mark class="wd">{chunk_old}</mark>')
        elif tag == 'insert':
            new_parts.append(f'<mark class="wi">{chunk_new}</mark>')
        elif tag == 'replace':
            old_parts.append(f'<mark class="wd">{chunk_old}</mark>')
            new_parts.append(f'<mark class="wi">{chunk_new}</mark>')

    return ''.join(old_parts), ''.join(new_parts)


def _diff_rows(lines_a: List[str], lines_b: List[str],
               context: int = 3,
               line_nums_a: Optional[List[int]] = None,
               line_nums_b: Optional[List[int]] = None,
               normalize=None,
               word_diff: bool = True) -> list:
    """
    Compute diff opcodes and return a compact, JSON-serialisable row list.

    Each row is one of:
      [0, lna, lnb, escaped_html]  — context line
      [1, lna, html]               — deleted line  (html may contain <mark> from word diff)
      [2, lnb, html]               — inserted line
      [3, count]                   — collapsed skip marker
    """
    opcodes = diff_opcodes(lines_a, lines_b, normalize=normalize)
    rows: list = []

    def lna(i: int) -> int: return line_nums_a[i] if line_nums_a else i + 1
    def lnb(j: int) -> int: return line_nums_b[j] if line_nums_b else j + 1

    for tag, i1, i2, j1, j2 in opcodes:
        if tag == 'equal':
            n = i2 - i1
            if n <= 2 * context:
                for k in range(n):
                    rows.append([0, lna(i1+k), lnb(j1+k), escape(lines_a[i1+k])])
            else:
                for k in range(context):
                    rows.append([0, lna(i1+k), lnb(j1+k), escape(lines_a[i1+k])])
                rows.append([3, n - 2 * context])
                for k in range(n - context, n):
                    rows.append([0, lna(i1+k), lnb(j1+k), escape(lines_a[i1+k])])
        elif tag == 'insert':
            for k in range(j2 - j1):
                rows.append([2, lnb(j1+k), escape(lines_b[j1+k])])
        elif tag == 'delete':
            for k in range(i2 - i1):
                rows.append([1, lna(i1+k), escape(lines_a[i1+k])])
        elif tag == 'replace':
            del_lines = lines_a[i1:i2]
            ins_lines = lines_b[j1:j2]
            if word_diff and len(del_lines) == len(ins_lines):
                for k, (old, new) in enumerate(zip(del_lines, ins_lines)):
                    old_html, new_html = word_diff_inline(old, new, normalize=normalize)
                    rows.append([1, lna(i1+k), old_html])
                    rows.append([2, lnb(j1+k), new_html])
            else:
                for k, line in enumerate(del_lines):
                    rows.append([1, lna(i1+k), escape(line)])
                for k, line in enumerate(ins_lines):
                    rows.append([2, lnb(j1+k), escape(line)])
    return rows


def _b64gz(obj) -> str:
    """Serialise obj to gzip-compressed base64 JSON string."""
    raw = _json.dumps(obj, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return base64.b64encode(gzip.compress(raw, compresslevel=1)).decode('ascii')


def _diff_rows_1v1(line_a: str, line_b: str, lna: int, lnb: int,
                   normalize=None, field_defs=None) -> list:
    """Fast path for single-line-to-single-line diff used by CSV row comparison.

    When field_defs is provided (format-mapper XLSX was specified), each line is
    parsed into named fields ("FIELD_NAME: value") and diffed as separate rows —
    making it clear which named fields changed while unchanged fields appear as
    context.  Without field_defs the raw-line word-diff path is used.
    """
    if field_defs:
        lines_a = [f"{fd.name}: {fd.extract(line_a)}" for fd in field_defs]
        lines_b = [f"{fd.name}: {fd.extract(line_b)}" for fd in field_defs]
        return _diff_rows(lines_a, lines_b, context=3, normalize=normalize)
    cmp_a = normalize(line_a) if normalize else line_a
    cmp_b = normalize(line_b) if normalize else line_b
    if cmp_a == cmp_b:
        return [[0, lna, lnb, escape(line_a)]]
    old_html, new_html = word_diff_inline(line_a, line_b, normalize=normalize)
    return [[1, lna, old_html], [2, lnb, new_html]]


def build_diff_html(lines_a: List[str], lines_b: List[str],
                    context: int = 3,
                    line_nums_a: Optional[List[int]] = None,
                    line_nums_b: Optional[List[int]] = None,
                    normalize=None) -> str:
    """
    Build a unified-style HTML diff table (eager rendering).

    Uses _diff_rows() internally; kept for non-browser / test callers.
    The HTML reporter uses _diff_rows() with consolidated card blob rendering.
    """
    html_rows: List[str] = []
    for row in _diff_rows(lines_a, lines_b, context, line_nums_a, line_nums_b, normalize=normalize):
        t = row[0]
        if t == 0:
            html_rows.append(
                f'<tr class="dc"><td class="ln">{row[1]}</td><td class="ln">{row[2]}</td>'
                f'<td class="dx"> {row[3]}</td></tr>')
        elif t == 1:
            html_rows.append(
                f'<tr class="dd"><td class="ln">{row[1]}</td><td class="ln"></td>'
                f'<td class="dx">−&nbsp;{row[2]}</td></tr>')
        elif t == 2:
            html_rows.append(
                f'<tr class="di"><td class="ln"></td><td class="ln">{row[1]}</td>'
                f'<td class="dx">+&nbsp;{row[2]}</td></tr>')
        else:
            n = row[1]
            html_rows.append(
                f'<tr class="ds"><td colspan="3">'
                f'⋯ {n} unchanged line{"s" if n != 1 else ""} ⋯</td></tr>')
    return (
        '<table class="diff-table">'
        '<colgroup>'
        '<col style="width:44px"><col style="width:44px"><col>'
        '</colgroup>'
        '<thead><tr>'
        '<th class="ln">A</th><th class="ln">B</th><th>Content</th>'
        '</tr></thead>'
        '<tbody>' + ''.join(html_rows) + '</tbody>'
        '</table>'
    )


def sections_table(sections: List[dict]) -> str:
    rows = []
    for sec in sections:
        css = section_css(sec)
        if sec["status"] == "matched":
            title = sec["title_a"]
            ratio = sec["diff"]["similarity_ratio"]
            sim   = f"{ratio*100:.1f}%"
            chg   = (f'+{sec["diff"]["lines_added"]} '
                     f'-{sec["diff"]["lines_deleted"]}')
            status_badge = (
                "IDENTICAL" if ratio == 1.00 else
                "MINOR"     if ratio >= 0.85 else
                "MODERATE"  if ratio >= 0.60 else
                "CHANGED"
            )
            title_note = (f' <span class="title-changed">→ {sec["title_b"]}</span>'
                          if sec.get("title_changed") else "")
        elif sec["status"] == "added":
            title = sec["title_b"]
            sim   = "—"
            chg   = f'{sec["lines"]} lines'
            status_badge = "ADDED"
            title_note = ""
        else:
            title = sec["title_a"]
            sim   = "—"
            chg   = f'{sec["lines"]} lines'
            status_badge = "REMOVED"
            title_note = ""

        rows.append(
            f'<tr class="{css}">'
            f'<td>{title}{title_note}</td>'
            f'<td><span class="badge {css}">{status_badge}</span></td>'
            f'<td class="num">{sim}</td>'
            f'<td class="num mono">{chg}</td>'
            f'</tr>'
        )
    return (
        '<table class="sec-table">'
        '<thead><tr>'
        '<th>Section</th><th>Status</th>'
        '<th class="num">Similarity</th><th class="num">Changes</th>'
        '</tr></thead>'
        '<tbody>' + "".join(rows) + '</tbody>'
        '</table>'
    )


def pages_panel(page_comparisons: List[dict],
                per_page_lines: List[Tuple[List[str], List[str]]],
                panel_id: str,
                per_page_line_numbers: Optional[List[Tuple[List[int], List[int]]]] = None,
                normalize=None) -> Tuple[str, str, Optional[dict]]:
    """Return (toggle_html, shell_html, data_dict) for per-page comparison.
    Returns ('', '', None) when there is nothing to show (single-page files).
    Data is returned as a plain dict to be included in the consolidated card blob;
    renderPages() in JS receives it via el._pagesData set by the card-init path.
    """
    if not page_comparisons:
        return '', '', None

    status_counts: dict = {}
    for pc in page_comparisons:
        if pc["status"] == "matched":
            r = pc["diff"]["similarity_ratio"]
            k = ("s-identical" if r == 1.00 else
                 "s-minor"     if r >= 0.85 else
                 "s-moderate"  if r >= 0.60 else "s-significant")
        elif pc["status"] == "added":
            k = "s-added"
        else:
            k = "s-removed"
        status_counts[k] = status_counts.get(k, 0) + 1

    pages: list = []
    matched_idx = 0

    for pc in page_comparisons:
        if pc["status"] == "matched":
            r    = pc["diff"]["similarity_ratio"]
            add  = pc["diff"]["lines_added"]
            ndel = pc["diff"]["lines_deleted"]

            if per_page_line_numbers and matched_idx < len(per_page_line_numbers):
                _rng_a, _rng_b = per_page_line_numbers[matched_idx]
                _ra = f'L{_rng_a[0]}–{_rng_a[-1]}' if _rng_a else ''
                _rb = f'L{_rng_b[0]}–{_rng_b[-1]}' if _rng_b else ''
                lnr = ' / '.join(x for x in [_ra, _rb] if x)
            else:
                lnr = ''

            # Embed diff data inline in the pages JSON rather than separate <script> tags.
            # JS decompresses on demand when the user expands a page's diff.
            diff_b64 = None
            if r < 1.0 and matched_idx < len(per_page_lines):
                la, lb = per_page_lines[matched_idx]
                if per_page_line_numbers and matched_idx < len(per_page_line_numbers):
                    la_nums, lb_nums = per_page_line_numbers[matched_idx]
                else:
                    la_nums, lb_nums = None, None
                _rows = _diff_rows(la, lb, line_nums_a=la_nums, line_nums_b=lb_nums, normalize=normalize)
                _raw = _json.dumps(_rows, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
                diff_b64 = base64.b64encode(gzip.compress(_raw, compresslevel=1)).decode('ascii')

            # [0, page_num_a, page_num_b, ratio, add, del, lnr, diff_b64_or_null]
            pages.append([0, pc["page_num_a"], pc["page_num_b"], r, add, ndel, lnr, diff_b64])
            matched_idx += 1
        elif pc["status"] == "added":
            # [1, page_num_b, lines]
            pages.append([1, pc["page_num_b"], pc["lines"]])
        else:
            # [2, page_num_a, lines]
            pages.append([2, pc["page_num_a"], pc["lines"]])

    data = {"counts": status_counts, "pages": pages}

    # Empty shell div — data lives in the consolidated card blob, wired by JS init.
    shell = f'<div class="sec-detail" id="{panel_id}" data-pages-src="{panel_id}-pages-data"></div>'
    toggle = (
        f'<span class="card-toggle" onclick="toggle(this,\'{panel_id}\')">'
        f'▶ pages</span>'
    )
    return toggle, shell, data





def pair_card(outcome: FilePairOutcome,
              file_url_a: str,
              file_url_b: str,
              card_id: str,
              body_lines_a: List[str],
              body_lines_b: List[str],
              per_page_lines: Optional[List[Tuple[List[str], List[str]]]] = None,
              file_txn_comparisons: Optional[List[dict]] = None,
              file_section_comparisons: Optional[List[dict]] = None,
              file_csv_comparisons: Optional[List[dict]] = None,
              normalize=None,
              card_idx: int = 0,
              field_defs=None,
              xlsx_filename: Optional[str] = None) -> Tuple[str, Optional[dict]]:
    """Return (shell_html, card_data_dict).
    shell_html has empty panel divs; card_data_dict holds all panel data
    for the consolidated blob. card_data_dict is None for error cards.
    """
    if outcome.error:
        html = (
            f'<div class="card error" id="{card_id}" data-card-idx="{card_idx}">'
            f'<div class="card-header"><span class="badge significant">ERROR</span>'
            f' {outcome.file_a.name} ↔ {outcome.file_b.name}</div>'
            f'<p class="err-msg">{outcome.error}</p>'
            f'</div>'
        )
        return html, None

    r     = outcome.result
    sm    = r.summary
    st    = r.structural
    ct    = r.content
    ratio = sm["overall_similarity_ratio"]
    v_css = verdict_css(ratio)
    v_lbl = verdict_label(ratio)
    match_tag = (
        f'<span class="fuzzy-tag">fuzzy {outcome.fuzzy_ratio:.0%}</span>'
        if outcome.match_type == "fuzzy" else ""
    )

    diff_id = f"dif-{card_id}"
    pg_id   = f"pg-{card_id}"

    eff_pcs = r.page_comparisons
    eff_ppl = per_page_lines or []
    eff_pln: Optional[List[Tuple[List[int], List[int]]]] = outcome.per_page_line_numbers or None

    if not eff_pcs and r.sections:
        eff_pcs = []
        eff_ppl = []
        eff_pln = []
        for sec in r.sections:
            title_a = sec.get("title_a", sec.get("title_b", "Page 0"))
            title_b = sec.get("title_b", sec.get("title_a", "Page 0"))
            try:
                pnum_a = int(title_a.rsplit(None, 1)[-1])
            except (ValueError, IndexError):
                pnum_a = 0
            try:
                pnum_b = int(title_b.rsplit(None, 1)[-1])
            except (ValueError, IndexError):
                pnum_b = 0
            if sec["status"] == "matched":
                la = sec.get("lines_a", [])
                lb = sec.get("lines_b", [])
                la_nums = sec.get("line_numbers_a", [])
                lb_nums = sec.get("line_numbers_b", [])
                eff_pcs.append({"status": "matched", "page_num_a": pnum_a,
                                "page_num_b": pnum_b, "diff": sec["diff"]})
                eff_ppl.append((la, lb))
                eff_pln.append((la_nums, lb_nums))
            elif sec["status"] == "removed":
                eff_pcs.append({"status": "removed", "page_num_a": pnum_a,
                                "page_num_b": None, "lines": sec["lines"]})
            else:
                eff_pcs.append({"status": "added", "page_num_a": None,
                                "page_num_b": pnum_b, "lines": sec["lines"]})

    # CSV mode: skip pages panel — storing 20K raw CSV lines as one-page diff would
    # add ~150 MB to the blob. The ▶ csv panel covers all row-level differences.
    if file_csv_comparisons is not None:
        eff_pcs = []
        eff_ppl = []
        eff_pln = None

    pages_toggle, pages_detail, pages_data = pages_panel(eff_pcs, eff_ppl, pg_id, eff_pln, normalize=normalize)

    # ── Diff rows (main diff panel) ──────────────────────────────────────────
    diff_rows: Optional[list] = None
    diff_panel  = ''
    diff_toggle = ''

    if file_csv_comparisons is not None:
        # CSV mode: the ▶ csv panel shows per-row diffs — skip the raw unified diff.
        # Building key-sorted diff_lines for 20K × 5 KB rows produces ~100 MB of blob
        # data that the browser must decompress before any panel can open.
        diff_toggle = '<span class="card-toggle no-diff">✓ no diff</span>' if ratio == 1.0 else ''
    else:
        diff_lines_a = body_lines_a
        diff_lines_b = body_lines_b
        diff_lna = outcome.body_line_numbers_a or None
        diff_lnb = outcome.body_line_numbers_b or None
        diff_label = "Inline diff — body content (headers &amp; footers excluded)"

        if ratio < 1.0 and (diff_lines_a or diff_lines_b):
            diff_rows = _diff_rows(diff_lines_a, diff_lines_b,
                                   line_nums_a=diff_lna, line_nums_b=diff_lnb,
                                   normalize=normalize,
                                   word_diff=True)
            diff_panel = (
                f'<div class="diff-outer" id="{diff_id}">'
                f'<div class="diff-toolbar">'
                f'  <span>{diff_label}</span>'
                f'  <span class="diff-legend">'
                f'    <span class="dl-del">− removed</span>'
                f'    <span class="dl-chg">~ changed word</span>'
                f'    <span class="dl-ins">+ added</span>'
                f'  </span>'
                f'</div>'
                f'<div class="diff-wrap" data-diff-src="{diff_id}-data"></div>'
                f'</div>'
            )
            diff_toggle = f'<span class="card-toggle" onclick="toggle(this,\'{diff_id}\')">▶ diff</span>'
        else:
            diff_toggle = '<span class="card-toggle no-diff">✓ no diff</span>'

    # ── Transaction panel ────────────────────────────────────────────────────
    txn_toggle = ''
    txn_panel  = ''
    txn_data: Optional[dict] = None
    if file_txn_comparisons:
        txn_id = f"txn-{card_id}"
        txn_entries: list = []
        for tc in file_txn_comparisons:
            key = tc["sort_key"]
            if tc["status"] == "matched":
                tr2 = tc["diff"]["similarity_ratio"]
                ta  = tc["diff"]["lines_added"]
                td2 = tc["diff"]["lines_deleted"]
                diff_b64 = None
                if tr2 < 1.0:
                    _r = _diff_rows(tc["txn_a"].lines, tc["txn_b"].lines,
                                    line_nums_a=tc["txn_a"].line_numbers or None,
                                    line_nums_b=tc["txn_b"].line_numbers or None,
                                    normalize=normalize)
                    diff_b64 = _b64gz(_r)
                txn_entries.append([0, key, tr2, ta, td2, diff_b64])
            elif tc["status"] == "added":
                txn_entries.append([1, key, len(tc["txn_b"].lines)])
            else:
                txn_entries.append([2, key, len(tc["txn_a"].lines)])

        n_m = sum(1 for t in file_txn_comparisons if t["status"] == "matched")
        n_a = sum(1 for t in file_txn_comparisons if t["status"] == "added")
        n_r = sum(1 for t in file_txn_comparisons if t["status"] == "removed")
        txn_data = {"summary": {"matched": n_m, "added": n_a, "removed": n_r}, "txns": txn_entries}
        # Shell div only — data arrives via consolidated card blob
        txn_panel  = f'<div class="sec-detail" id="{txn_id}" data-txn-src="{txn_id}-data"></div>'
        txn_toggle = f'<span class="card-toggle" onclick="toggle(this,\'{txn_id}\')">▶ txn</span>'

    # ── Sections panel ───────────────────────────────────────────────────────
    sec_toggle = ''
    sec_panel  = ''
    sec_data: Optional[dict] = None
    if file_section_comparisons:
        sec_id = f"sec-{card_id}"
        sec_entries: list = []
        for sc in file_section_comparisons:
            if sc["status"] in ("identical", "changed"):
                d   = sc["diff"]
                sr  = d["similarity_ratio"]
                sa  = d["lines_added"]
                sd  = d["lines_deleted"]
                diff_b64 = _b64gz(_diff_rows(sc["lines_a"], sc["lines_b"], normalize=normalize))
                name_b = sc.get("name_b", sc["name"])
                name_display = sc["name"] if name_b == sc["name"] else f'{sc["name"]} → {name_b}'
                sec_entries.append([0, name_display, sr, sa, sd, diff_b64])
            elif sc["status"] == "added":
                sec_entries.append([1, sc["name"], len(sc["lines_b"])])
            else:
                sec_entries.append([2, sc["name"], len(sc["lines_a"])])

        n_id = sum(1 for s in file_section_comparisons if s["status"] == "identical")
        n_ch = sum(1 for s in file_section_comparisons if s["status"] == "changed")
        n_ad = sum(1 for s in file_section_comparisons if s["status"] == "added")
        n_rm = sum(1 for s in file_section_comparisons if s["status"] == "removed")
        sec_data = {"summary": {"identical": n_id, "changed": n_ch, "added": n_ad, "removed": n_rm},
                    "sections": sec_entries}
        sec_panel  = f'<div class="sec-detail" id="{sec_id}" data-sec-src="{sec_id}-data"></div>'
        sec_toggle = f'<span class="card-toggle" onclick="toggle(this,\'{sec_id}\')">▶ sections</span>'

    # ── CSV rows panel ───────────────────────────────────────────────────────
    csv_toggle = ''
    csv_panel  = ''
    csv_data: Optional[dict] = None
    if file_csv_comparisons:
        n_m = sum(1 for r2 in file_csv_comparisons if r2["status"] == "matched")
        n_a = sum(1 for r2 in file_csv_comparisons if r2["status"] == "added")
        n_r = sum(1 for r2 in file_csv_comparisons if r2["status"] == "removed")
        if xlsx_filename:
            # Diff detail lives in the XLSX — skip embedding the blob in the HTML card.
            # Show a lightweight link instead so the card still communicates what was exported.
            changed = sum(1 for r2 in file_csv_comparisons
                          if r2["status"] == "matched" and r2["diff"]["similarity_ratio"] < 1.0)
            parts = []
            if n_m:  parts.append(f"{n_m:,} matched ({changed:,} changed)")
            if n_a:  parts.append(f"{n_a:,} added")
            if n_r:  parts.append(f"{n_r:,} removed")
            summary = " · ".join(parts)
            csv_toggle = (
                f'<span class="card-toggle no-diff" title="{summary}">'
                f'<a href="{xlsx_filename}" style="color:inherit;text-decoration:none">'
                f'⬇ {xlsx_filename}</a></span>'
            )
        else:
            csv_id = f"csv-{card_id}"
            csv_entries = []
            for rc in file_csv_comparisons:
                key = rc["key"]
                if rc["status"] == "matched":
                    cr = rc["diff"]["similarity_ratio"]
                    ca = rc["diff"]["lines_added"]
                    cd = rc["diff"]["lines_deleted"]
                    diff_rows_entry = None
                    if cr < 1.0:
                        diff_rows_entry = _diff_rows_1v1(
                            rc["row_a"].line, rc["row_b"].line,
                            rc["row_a"].line_number, rc["row_b"].line_number,
                            normalize=normalize, field_defs=field_defs)
                    csv_entries.append([0, key, cr, ca, cd, diff_rows_entry])
                elif rc["status"] == "added":
                    csv_entries.append([1, key])
                else:
                    csv_entries.append([2, key])
            csv_data = {"summary": {"matched": n_m, "added": n_a, "removed": n_r}, "rows": csv_entries}
            csv_panel  = f'<div class="sec-detail" id="{csv_id}" data-csv-src="{csv_id}-data"></div>'
            csv_toggle = f'<span class="card-toggle" onclick="toggle(this,\'{csv_id}\')">▶ csv</span>'

    # ── Card shell HTML (no embedded data scripts) ───────────────────────────
    html = f"""
<div class="card {v_css}" id="{card_id}" data-card-idx="{card_idx}" data-verdict="{v_css}">

  <div class="card-header">
    <span class="badge {v_css}">{v_lbl}</span>
    <span class="card-title">{outcome.file_a.name}</span>
    {match_tag}
    {pages_toggle}
    {txn_toggle}
    {sec_toggle}
    {csv_toggle}
    {diff_toggle}
  </div>

  <div class="file-links">
    {file_link("Folder&nbsp;A", file_url_a, outcome.file_a.name)}
    {file_link("Folder&nbsp;B", file_url_b, outcome.file_b.name)}
  </div>

  <div class="metrics">
    <div class="sim-block">
      <div class="sim-label">Overall similarity</div>
      {bar(ratio * 100, v_css)}
    </div>
    <div class="stats-grid">
      {stat_box("Lines added",    ct["lines_added"],   "+")}
      {stat_box("Lines deleted",  ct["lines_deleted"],  "−")}
      {stat_box("Lines common",   ct["lines_common"],   "=")}
      {stat_box("Change",         f'{ct["change_pct"]}%')}
      {stat_box("Pages A→B",      f'{st["page_count_a"]}→{st["page_count_b"]}')}
      {stat_box("Sections A→B",   f'{st["section_count_a"]}→{st["section_count_b"]}')}
      {stat_box("Body lines A→B", f'{st["body_line_count_a"]}→{st["body_line_count_b"]}')}
    </div>
  </div>

  {"".join([f'<div class="hf-note">⚠ Header changed: {h}</div>' for h in r.metadata.get("headers_only_in_a", [])])}
  {"".join([f'<div class="hf-note hf-b">⚠ New header in B: {h}</div>' for h in r.metadata.get("headers_only_in_b", [])])}

  {pages_detail}

  {txn_panel}

  {sec_panel}

  {csv_panel}

  {diff_panel}

</div>"""

    card_data = {
        "diff_rows": diff_rows,
        "pages":     pages_data,
        "txn":       txn_data,
        "sec":       sec_data,
        "csv":       csv_data,
    }
    return html, card_data


def unmatched_section(only_in_a: list, only_in_b: list,
                       linux_base: Optional[str],
                       windows_base: Optional[str]) -> str:
    if not only_in_a and not only_in_b:
        return ""

    rows_a = ""
    for p in only_in_a:
        _, url = resolve_paths(p, linux_base, windows_base)
        rows_a += (f'<tr><td>{file_link("A", url, p.name)}</td>'
                   f'<td><span class="badge significant">MISSING IN B</span></td></tr>')

    rows_b = ""
    for p in only_in_b:
        _, url = resolve_paths(p, linux_base, windows_base)
        rows_b += (f'<tr><td>{file_link("B", url, p.name)}</td>'
                   f'<td><span class="badge significant">MISSING IN A</span></td></tr>')

    table = (
        '<table class="sec-table">'
        '<thead><tr><th>File</th><th>Status</th></tr></thead>'
        '<tbody>' + rows_a + rows_b + '</tbody>'
        '</table>'
    )
    return f'<div class="card unmatched"><div class="card-header"><span class="badge significant">UNMATCHED FILES</span></div>{table}</div>'


# ---------------------------------------------------------------------------
# Master HTML template
# ---------------------------------------------------------------------------

CSS = """
:root{
  --bg:#f4f6fa;--card:#fff;--border:#e2e6ed;
  --text:#1a202c;--muted:#6b7280;--code:#374151;
  --identical:#16a34a;--minor:#ca8a04;--moderate:#ea580c;--significant:#dc2626;
  --identical-bg:#dcfce7;--minor-bg:#fef9c3;--moderate-bg:#ffedd5;--significant-bg:#fee2e2;
  --s-added:#2563eb;--s-removed:#9ca3af;
  --btn-bc:#2563eb;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);font-size:14px;line-height:1.5}
a{color:var(--btn-bc);text-decoration:none}a:hover{text-decoration:underline}
code{font-family:'Cascadia Code','Consolas',monospace;font-size:12px}

/* Layout */
.wrap{max-width:1100px;margin:0 auto;padding:24px 16px}
h1{font-size:22px;font-weight:700;margin-bottom:4px}
.subtitle{color:var(--muted);font-size:13px;margin-bottom:20px}

/* Dashboard */
.dashboard{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:24px;
           background:var(--card);border:1px solid var(--border);border-radius:10px;padding:18px}
.dash-stat{flex:1;min-width:110px;text-align:center}
.dash-val{font-size:28px;font-weight:700;display:block}
.dash-lbl{font-size:12px;color:var(--muted);display:block}
.dash-divider{width:1px;background:var(--border)}

/* Filters */
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px;align-items:center}
.filters span{font-size:12px;color:var(--muted);margin-right:4px}
.filter-btn{border:1px solid var(--border);background:#fff;border-radius:20px;
            padding:4px 12px;font-size:12px;cursor:pointer;transition:all .15s}
.filter-btn:hover,.filter-btn.active{background:var(--text);color:#fff;border-color:var(--text)}
.filter-btn.f-identical.active{background:var(--identical);border-color:var(--identical)}
.filter-btn.f-minor.active{background:var(--minor);border-color:var(--minor)}
.filter-btn.f-moderate.active{background:var(--moderate);border-color:var(--moderate)}
.filter-btn.f-significant.active{background:var(--significant);border-color:var(--significant)}

/* Cards */
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;
      margin-bottom:16px;overflow:hidden;border-left:4px solid var(--border);
      content-visibility:auto;contain-intrinsic-size:0 120px}
.card.identical{border-left-color:var(--identical)}
.card.minor{border-left-color:var(--minor)}
.card.moderate{border-left-color:var(--moderate)}
.card.significant,.card.error{border-left-color:var(--significant)}
.card-header{display:flex;align-items:center;gap:8px;padding:14px 18px;
             border-bottom:1px solid var(--border);background:#fafbfc;flex-wrap:wrap}
.card-title{font-weight:600;font-size:15px;flex:1}
.card-toggle{font-size:12px;color:var(--btn-bc);cursor:pointer;margin-left:auto;
             user-select:none;white-space:nowrap}
.card-toggle:hover{text-decoration:underline}
.fuzzy-tag{font-size:11px;background:#ede9fe;color:#6d28d9;border-radius:20px;padding:2px 8px}
.err-msg{padding:16px 18px;color:var(--significant);font-family:monospace}

/* File links */
.file-links{display:flex;gap:16px;padding:10px 18px;border-bottom:1px solid var(--border);
            flex-wrap:wrap;background:#fafbfc}
.file-link{display:inline-flex;align-items:center;gap:5px;font-size:13px;
           color:var(--btn-bc);padding:3px 0}
.file-link svg{opacity:.7;flex-shrink:0}
.file-link code{font-size:12px}



/* Metrics */
.metrics{display:flex;gap:0;border-bottom:1px solid var(--border);flex-wrap:wrap}
.sim-block{padding:14px 18px;min-width:220px;border-right:1px solid var(--border)}
.sim-label{font-size:12px;color:var(--muted);margin-bottom:6px}
.bar-wrap{display:flex;align-items:center;gap:10px;height:22px}
.bar{height:14px;border-radius:7px;min-width:4px;transition:width .3s}
.bar.identical{background:var(--identical)}
.bar.minor{background:var(--minor)}
.bar.moderate{background:var(--moderate)}
.bar.significant{background:var(--significant)}
.bar-label{font-size:15px;font-weight:700;white-space:nowrap}
.stats-grid{display:flex;flex-wrap:wrap;gap:0;flex:1}
.stat-box{padding:10px 14px;min-width:90px;border-right:1px solid var(--border);
          border-bottom:1px solid var(--border);text-align:center}
.stat-val{display:block;font-size:18px;font-weight:700}
.stat-lbl{display:block;font-size:11px;color:var(--muted)}
.stat-sub{display:block;font-size:16px;font-weight:700;color:var(--muted)}

/* Header/footer notes */
.hf-note{font-size:12px;color:var(--moderate);padding:4px 18px;background:#fff7ed}
.hf-b{color:var(--btn-bc);background:#eff6ff}

/* Section detail */
.sec-detail{display:none;padding:16px 18px}
.sec-detail.open{display:block}
.sec-summary{display:flex;flex-wrap:wrap;gap:0;margin-bottom:12px;
             border:1px solid var(--border);border-radius:6px;overflow:hidden}
.sec-summary .stat-box{border-bottom:none;min-width:80px}

/* Section table */
.sec-table{width:100%;border-collapse:collapse;font-size:13px;table-layout:fixed}
.sec-table th{text-align:left;padding:6px 10px;background:#f9fafb;
              border-bottom:2px solid var(--border);color:var(--muted);font-weight:600;font-size:12px}
.sec-table td{padding:6px 10px;border-bottom:1px solid var(--border);vertical-align:middle}
.sec-table tr:last-child td{border-bottom:none}
.sec-table .num{text-align:right}
.sec-table .mono{font-family:monospace;font-size:12px}
.sec-table th:nth-child(2),.sec-table td:nth-child(2){width:90px}
.sec-table th:nth-child(3),.sec-table td:nth-child(3){width:80px}
.sec-table th:nth-child(4),.sec-table td:nth-child(4){width:90px}
.sec-table th:nth-child(5),.sec-table td:nth-child(5){width:65px}
.sec-table tr:not(.diff-row) td:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sec-table tr.s-identical td:first-child{border-left:3px solid var(--identical)}
.sec-table tr.s-minor td:first-child{border-left:3px solid var(--minor)}
.sec-table tr.s-moderate td:first-child{border-left:3px solid var(--moderate)}
.sec-table tr.s-significant td:first-child{border-left:3px solid var(--significant)}
.sec-table tr.s-added td:first-child{border-left:3px solid var(--s-added)}
.sec-table tr.s-removed td:first-child{border-left:3px solid var(--s-removed)}
.title-changed{font-size:11px;color:var(--moderate);margin-left:6px}
.pg-lnr{font-size:10px;color:var(--muted);font-family:'Cascadia Code','Consolas',monospace}

/* Badges */
.badge{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.5px;
       border-radius:4px;padding:2px 7px;text-transform:uppercase}
.badge.identical,.badge.s-identical{background:var(--identical-bg);color:var(--identical)}
.badge.minor,.badge.s-minor{background:var(--minor-bg);color:var(--minor)}
.badge.moderate,.badge.s-moderate{background:var(--moderate-bg);color:var(--moderate)}
.badge.significant,.badge.s-significant,.badge.error{background:var(--significant-bg);color:var(--significant)}
.badge.s-added{background:#dbeafe;color:var(--s-added)}
.badge.s-removed{background:#f3f4f6;color:#374151}

/* Unmatched card */
.card.unmatched{border-left-color:var(--significant)}
.no-results{text-align:center;color:var(--muted);padding:40px;font-size:14px}

/* Diff panel */
.diff-outer{display:none;border-top:1px solid var(--border)}
.diff-outer.open{display:block}
.diff-wrap{overflow-x:auto;overflow-y:auto;max-height:min(600px,calc(100vh - 120px))}
.diff-row{display:none}.diff-row.open{display:table-row}.diff-row td{padding:0;border-top:none}
.diff-toolbar{display:flex;align-items:center;
              padding:6px 14px;background:#f9fafb;border-bottom:1px solid var(--border);
              font-size:12px;color:var(--muted);flex-wrap:wrap;gap:8px}
.diff-toolbar>span:first-child{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.diff-legend{display:flex;flex-wrap:wrap;gap:4px 12px}
.dl-del{color:#991b1b;font-weight:600}
.dl-ins{color:#166534;font-weight:600}
.dl-chg{color:#92400e;font-weight:600}
.diff-table{width:100%;border-collapse:collapse;font-family:'Cascadia Code','Consolas',monospace;font-size:12px;line-height:1.6}
.diff-table thead th{padding:3px 8px;background:#f1f5f9;border-bottom:2px solid var(--border);
                     color:var(--muted);font-size:11px;font-weight:600;position:sticky;top:0}
.diff-table .ln{width:44px;text-align:right;color:#9ca3af;background:#f8fafc;
                border-right:1px solid var(--border);padding:1px 6px;
                font-size:11px;user-select:none;white-space:nowrap}
.diff-table .dx{padding:1px 12px;white-space:pre}

/* Row colours */
.dc .dx{background:#fff;color:var(--text)}
.dc .ln{background:#f8fafc}
.dd .dx{background:#fff5f5;color:#991b1b}
.dd .ln{background:#fee2e2;color:#b91c1c}
.di .dx{background:#f0fff4;color:#166534}
.di .ln{background:#dcfce7;color:#15803d}
.ds td{background:#f9fafb;color:var(--muted);text-align:center;
        padding:5px;font-style:italic;font-family:sans-serif;font-size:11px;border-top:1px dashed var(--border)}

/* Word-level marks */
mark.wd{background:#fecaca;color:#7f1d1d;border-radius:2px;padding:0 1px}
mark.wi{background:#bbf7d0;color:#14532d;border-radius:2px;padding:0 1px}

/* Toggle button variants */
.card-toggle.no-diff{color:var(--identical);cursor:default}

/* Transaction panel */
.txn-detail{padding:10px 0 4px}
.txn-summary{font-size:12px;color:var(--muted);margin-bottom:8px}
.txn-table td.mono{font-family:'Cascadia Code','Consolas',monospace;font-size:12px}

/* Page-level filter bar (inside each card's pages panel) */
.pg-filter-bar{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
.pf-btn{display:inline-flex;align-items:center;gap:5px;border:1px solid var(--border);
        background:#fff;border-radius:20px;padding:3px 10px;font-size:11px;font-weight:600;
        cursor:pointer;transition:all .15s;white-space:nowrap;line-height:1.6}
.pf-btn:hover{border-color:#9ca3af}
.pf-dot{display:inline-block;width:8px;height:8px;border-radius:50%;flex-shrink:0}
.pf-dot.s-identical{background:var(--identical)}
.pf-dot.s-minor{background:var(--minor)}
.pf-dot.s-moderate{background:var(--moderate)}
.pf-dot.s-significant{background:var(--significant)}
.pf-dot.s-added{background:var(--s-added)}
.pf-dot.s-removed{background:var(--s-removed)}
.pf-btn.pf-all.active{background:var(--text);color:#fff;border-color:var(--text)}
.pf-btn.pf-s-identical.active{background:var(--identical-bg);color:var(--identical);border-color:var(--identical)}
.pf-btn.pf-s-minor.active{background:var(--minor-bg);color:var(--minor);border-color:var(--minor)}
.pf-btn.pf-s-moderate.active{background:var(--moderate-bg);color:var(--moderate);border-color:var(--moderate)}
.pf-btn.pf-s-significant.active{background:var(--significant-bg);color:var(--significant);border-color:var(--significant)}
.pf-btn.pf-s-added.active{background:#dbeafe;color:var(--s-added);border-color:var(--s-added)}
.pf-btn.pf-s-removed.active{background:#f3f4f6;color:#374151;border-color:#9ca3af}
"""

JS = """
const ROW_H=22,V_BUF=50;
function makeSpacerRow(){
  const tr=document.createElement('tr');
  const td=document.createElement('td');
  td.colSpan=3;td.style.cssText='padding:0;border:0;font-size:0;line-height:0';
  tr.appendChild(td);
  return{tr,td};
}
function buildDiffRow(row){
  const tr=document.createElement('tr');
  const[t,a,b,c]=row;
  if(t===0){tr.className='dc';tr.innerHTML='<td class="ln">'+a+'</td><td class="ln">'+b+'</td><td class="dx"> '+c+'</td>';}
  else if(t===1){tr.className='dd';tr.innerHTML='<td class="ln">'+a+'</td><td class="ln"></td><td class="dx">− '+b+'</td>';}
  else if(t===2){tr.className='di';tr.innerHTML='<td class="ln"></td><td class="ln">'+a+'</td><td class="dx">+ '+b+'</td>';}
  else{tr.className='ds';tr.innerHTML='<td colspan="3">⋯ '+a+' unchanged line'+(a!==1?'s':'')+' ⋯</td>';}
  return tr;
}
function applyDiffWindow(state,scrollTop){
  const{rows,tbody,top,bot,el}=state;
  const visH=el.clientHeight||600;
  const first=Math.max(0,Math.floor(scrollTop/ROW_H)-V_BUF);
  const last=Math.min(rows.length,first+Math.ceil(visH/ROW_H)+V_BUF*2);
  top.td.style.height=(first*ROW_H)+'px';
  bot.td.style.height=Math.max(0,(rows.length-last)*ROW_H)+'px';
  tbody.replaceChildren();
  tbody.appendChild(top.tr);
  for(let i=first;i<last;i++)tbody.appendChild(buildDiffRow(rows[i]));
  tbody.appendChild(bot.tr);
}
async function renderDiff(el){
  if(el.dataset.rendered)return;
  let rows=el._diffRows;
  if(!rows){
    let b64,isGz=true;
    if(el._diffB64){
      b64=el._diffB64;el._diffB64=null;
    }else{
      const script=document.getElementById(el.dataset.diffSrc);
      if(!script)return;
      isGz=script.dataset.enc==='gz';
      b64=script.textContent.trim();
      script.remove();
    }
    try{
      let json;
      if(isGz){
        const bin=atob(b64);
        const bytes=new Uint8Array(bin.length);
        for(let i=0;i<bin.length;i++)bytes[i]=bin.charCodeAt(i);
        const ds=new DecompressionStream('gzip');
        const w=ds.writable.getWriter();
        w.write(bytes);w.close();
        json=new TextDecoder().decode(await new Response(ds.readable).arrayBuffer());
      }else{
        json=b64;
      }
      rows=JSON.parse(json);
      el._diffRows=rows;
    }catch(e){
      el.insertAdjacentHTML('beforeend','<p style="color:red;padding:8px;font-family:sans-serif">Diff render error: '+e.message+'</p>');
      el.dataset.rendered='1';
      return;
    }
  }
  const tbody=document.createElement('tbody');
  const top=makeSpacerRow(),bot=makeSpacerRow();
  const state={rows,tbody,top,bot,el};
  applyDiffWindow(state,0);
  const tbl=document.createElement('table');
  tbl.className='diff-table';
  tbl.innerHTML='<colgroup><col style="width:44px"><col style="width:44px"><col></colgroup>'
    +'<thead><tr><th class="ln">A</th><th class="ln">B</th><th>Content</th></tr></thead>';
  tbl.appendChild(tbody);
  const wrap=document.createElement('div');
  wrap.className='panel-content';
  wrap.appendChild(tbl);
  el.appendChild(wrap);
  const rect=el.getBoundingClientRect();
  const spare=window.innerHeight-rect.top-40;
  if(spare>80&&spare<el.clientHeight)el.style.maxHeight=spare+'px';
  const ac=new AbortController();
  el._diffAbort=ac;
  let _raf=0;
  el.addEventListener('scroll',()=>{
    cancelAnimationFrame(_raf);
    _raf=requestAnimationFrame(()=>applyDiffWindow(state,el.scrollTop));
  },{signal:ac.signal});
  el.dataset.rendered='1';
}
function escHtml(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function _makePanelScaffold(el,hdrHtml,thHtml){
  const wrap=document.createElement('div');
  wrap.className='panel-content';
  wrap.innerHTML=hdrHtml;
  const tbl=document.createElement('table');
  tbl.className='sec-table txn-table';
  tbl.innerHTML=`<thead><tr>${thHtml}</tr></thead>`;
  const tbody=document.createElement('tbody');
  tbl.appendChild(tbody);
  wrap.appendChild(tbl);
  el.appendChild(wrap);
  el.dataset.rendered='1';
  return tbody;
}
function _chunkRows(items,tbody,buildRow){
  let i=0;const CHUNK=100;
  function addChunk(){
    const frag=document.createDocumentFragment();
    const end=Math.min(i+CHUNK,items.length);
    for(;i<end;i++)frag.appendChild(buildRow(items[i],i));
    tbody.appendChild(frag);
    if(i<items.length)requestAnimationFrame(addChunk);
  }
  addChunk();
}
async function toggleEntryDiff(btn,panelId,entryIdx){
  const panel=document.getElementById(panelId);
  const row=btn.closest('tr');
  const next=row.nextElementSibling;
  if(next&&next.classList.contains('diff-row')){
    const dw=next.querySelector('.diff-wrap');
    if(dw&&dw._diffAbort){dw._diffAbort.abort();delete dw._diffAbort;}
    next.remove();
    const entry=panel._panelData[entryIdx];
    btn.textContent=entry[2]===1?'▶ view':'▶ diff';
    return;
  }
  const entry=panel._panelData[entryIdx];
  const diffData=entry[5];
  if(!diffData)return;
  const diffRow=document.createElement('tr');
  diffRow.className='diff-row open';
  const td=document.createElement('td');td.colSpan=5;
  const outer=document.createElement('div');outer.className='diff-outer open';
  outer.innerHTML=`<div class="diff-toolbar"><span>${escHtml(String(entry[1]))}</span><span class="diff-legend"><span class="dl-del">− removed</span><span class="dl-chg">~ changed</span><span class="dl-ins">+ added</span></span></div>`;
  const dw=document.createElement('div');dw.className='diff-wrap';
  // Array = raw diff rows (CSV entries); string = gzip+b64 (txn/sec entries)
  if(Array.isArray(diffData)){dw._diffRows=diffData;}else{dw._diffB64=diffData;}
  outer.appendChild(dw);td.appendChild(outer);diffRow.appendChild(td);
  row.after(diffRow);
  btn.textContent='▼'+btn.textContent.slice(1);
  await renderDiff(dw);
}
function renderTxnPanel(el,txnSrcId){
  if(el.dataset.rendered)return;
  let d=el._txnData;
  if(!d){
    const script=document.getElementById(txnSrcId);
    if(!script)return;
    d=JSON.parse(script.textContent);el._txnData=d;script.remove();
  }
  el._panelData=d.txns;
  const s=d.summary;
  const pid=el.id;
  let hdr=`<div class="txn-summary">Transactions: <strong>${s.matched}</strong> matched`;
  if(s.added)hdr+=` · <strong style="color:var(--s-added)">${s.added}</strong> added`;
  if(s.removed)hdr+=` · <strong style="color:var(--s-removed)">${s.removed}</strong> removed`;
  hdr+='</div>';
  const tbody=_makePanelScaffold(el,hdr,'<th>Sort Key</th><th>Status</th><th class="num">Similarity</th><th class="num">Changes</th><th></th>');
  _chunkRows(d.txns,tbody,(t,i)=>{
    const tr=document.createElement('tr');
    if(t[0]===0){
      const[,key,ratio,add,ndel,diffB64]=t;
      const css=ratio===1?'s-identical':ratio>=0.85?'s-minor':ratio>=0.60?'s-moderate':'s-significant';
      const badge=ratio===1?'IDENTICAL':ratio>=0.85?'MINOR':ratio>=0.60?'MODERATE':'CHANGED';
      const dcell=diffB64?`<span class="card-toggle" onclick="toggleEntryDiff(this,'${pid}',${i})">▶ diff</span>`:'<span class="card-toggle no-diff">✓</span>';
      tr.className=css;
      tr.innerHTML=`<td class="mono" title="${escHtml(key)}">${escHtml(key)}</td><td><span class="badge ${css}">${badge}</span></td><td class="num">${(ratio*100).toFixed(1)}%</td><td class="num mono">+${add} −${ndel}</td><td>${dcell}</td>`;
    }else if(t[0]===1){tr.className='s-added';tr.innerHTML=`<td class="mono">${escHtml(t[1])}</td><td><span class="badge s-added">ADDED</span></td><td class="num">—</td><td class="num mono">${t[2]} lines</td><td></td>`;}
    else{tr.className='s-removed';tr.innerHTML=`<td class="mono">${escHtml(t[1])}</td><td><span class="badge s-removed">REMOVED</span></td><td class="num">—</td><td class="num mono">${t[2]} lines</td><td></td>`;}
    return tr;
  });
}
function renderSectionsPanel(el,srcId){
  if(el.dataset.rendered)return;
  let d=el._secData;
  if(!d){
    const script=document.getElementById(srcId);
    if(!script)return;
    d=JSON.parse(script.textContent);el._secData=d;script.remove();
  }
  el._panelData=d.sections;
  const s=d.summary;
  const pid=el.id;
  let hdr='<div class="txn-summary">Sections:';
  if(s.identical)hdr+=` <strong>${s.identical}</strong> identical`;
  if(s.changed)hdr+=` · <strong style="color:var(--s-moderate)">${s.changed}</strong> changed`;
  if(s.added)hdr+=` · <strong style="color:var(--s-added)">${s.added}</strong> added`;
  if(s.removed)hdr+=` · <strong style="color:var(--s-removed)">${s.removed}</strong> removed`;
  hdr+='</div>';
  const tbody=_makePanelScaffold(el,hdr,'<th>Section</th><th>Status</th><th class="num">Similarity</th><th class="num">Changes</th><th></th>');
  _chunkRows(d.sections,tbody,(sc,i)=>{
    const tr=document.createElement('tr');
    if(sc[0]===0){
      const[,name,ratio,add,ndel,diffB64]=sc;
      const css=ratio===1?'s-identical':ratio>=0.85?'s-minor':ratio>=0.60?'s-moderate':'s-significant';
      const badge=ratio===1?'IDENTICAL':ratio>=0.85?'MINOR':ratio>=0.60?'MODERATE':'CHANGED';
      const lbl=ratio===1?'▶ view':'▶ diff';
      tr.className=css;
      tr.innerHTML=`<td class="mono" title="${escHtml(name)}">${escHtml(name)}</td><td><span class="badge ${css}">${badge}</span></td><td class="num">${(ratio*100).toFixed(1)}%</td><td class="num mono">+${add} −${ndel}</td><td><span class="card-toggle" onclick="toggleEntryDiff(this,'${pid}',${i})">${lbl}</span></td>`;
    }else if(sc[0]===1){tr.className='s-added';tr.innerHTML=`<td class="mono">${escHtml(sc[1])}</td><td><span class="badge s-added">ADDED</span></td><td class="num">—</td><td class="num mono">${sc[2]} lines</td><td></td>`;}
    else{tr.className='s-removed';tr.innerHTML=`<td class="mono">${escHtml(sc[1])}</td><td><span class="badge s-removed">REMOVED</span></td><td class="num">—</td><td class="num mono">${sc[2]} lines</td><td></td>`;}
    return tr;
  });
}
function _csvBuildRow(r,origIdx,pid){
  const tr=document.createElement('tr');
  if(r[0]===0){
    const[,key,ratio,add,ndel,diffData]=r;
    const css=ratio===1?'s-identical':ratio>=0.85?'s-minor':ratio>=0.60?'s-moderate':'s-significant';
    const badge=ratio===1?'IDENTICAL':ratio>=0.85?'MINOR':ratio>=0.60?'MODERATE':'CHANGED';
    const dcell=diffData?`<span class="card-toggle" onclick="toggleEntryDiff(this,'${pid}',${origIdx})">▶ diff</span>`:'<span class="card-toggle no-diff">✓</span>';
    tr.className=css;
    tr.innerHTML=`<td class="mono" title="${escHtml(key)}">${escHtml(key)}</td><td><span class="badge ${css}">${badge}</span></td><td class="num">${(ratio*100).toFixed(1)}%</td><td class="num mono">+${add} −${ndel}</td><td>${dcell}</td>`;
  }else if(r[0]===1){
    tr.className='s-added';
    tr.innerHTML=`<td class="mono" title="${escHtml(r[1])}">${escHtml(r[1])}</td><td><span class="badge s-added">ADDED</span></td><td class="num">—</td><td class="num mono">—</td><td></td>`;
  }else{
    tr.className='s-removed';
    tr.innerHTML=`<td class="mono" title="${escHtml(r[1])}">${escHtml(r[1])}</td><td><span class="badge s-removed">REMOVED</span></td><td class="num">—</td><td class="num mono">—</td><td></td>`;
  }
  return tr;
}
function _csvChunk(el,rows,pid){
  // Generation counter: incrementing cancels any in-flight chunked render.
  el._csvGen=(el._csvGen||0)+1;
  const myGen=el._csvGen;
  const tbody=el._csvTbody;
  tbody.innerHTML='';
  const idxMap=el._csvIdxMap;
  let i=0;const CHUNK=100;
  function go(){
    if(el._csvGen!==myGen)return;
    const frag=document.createDocumentFragment();
    const end=Math.min(i+CHUNK,rows.length);
    for(;i<end;i++){const r=rows[i];frag.appendChild(_csvBuildRow(r,idxMap.get(r)??i,pid));}
    tbody.appendChild(frag);
    if(i<rows.length)requestAnimationFrame(go);
  }
  requestAnimationFrame(go);
}
function csvSetFilter(pid,showAll){
  const el=document.getElementById(pid);if(!el)return;
  const f0=document.getElementById(pid+'-f0');
  const f1=document.getElementById(pid+'-f1');
  if(f0)f0.classList.toggle('active',!showAll);
  if(f1)f1.classList.toggle('active',showAll);
  _csvChunk(el,showAll?el._csvAllRows:el._csvDiffRows,pid);
}
function renderCsvPanel(el,srcId){
  if(el.dataset.rendered)return;
  let d=el._csvData;
  if(!d){
    const script=document.getElementById(srcId);
    if(!script)return;
    d=JSON.parse(script.textContent);el._csvData=d;script.remove();
  }
  const allRows=d.rows;
  const diffRows=allRows.filter(r=>r[0]!==0||r[2]<1.0);
  const hasFilter=diffRows.length<allRows.length;
  el._panelData=allRows;
  el._csvAllRows=allRows;
  el._csvDiffRows=diffRows;
  el._csvIdxMap=new Map(allRows.map((r,i)=>[r,i]));
  const pid=el.id;
  const s=d.summary;
  let hdr=`<div class="txn-summary">CSV rows: <strong>${s.matched}</strong> matched`;
  if(s.added)hdr+=` · <strong style="color:var(--s-added)">${s.added}</strong> added`;
  if(s.removed)hdr+=` · <strong style="color:var(--s-removed)">${s.removed}</strong> removed`;
  hdr+='</div>';
  const wrap=document.createElement('div');wrap.className='panel-content';
  wrap.innerHTML=hdr;
  if(hasFilter){
    const fb=document.createElement('div');fb.className='pg-filter-bar';
    fb.innerHTML=`<button class="pf-btn pf-all active" id="${pid}-f0" onclick="csvSetFilter('${pid}',false)">Changed &amp; different (${diffRows.length})</button><button class="pf-btn pf-all" id="${pid}-f1" onclick="csvSetFilter('${pid}',true)">All rows (${allRows.length})</button>`;
    wrap.appendChild(fb);
  }
  const tbl=document.createElement('table');tbl.className='sec-table txn-table';
  tbl.innerHTML='<thead><tr><th>Key</th><th>Status</th><th class="num">Similarity</th><th class="num">Changes</th><th></th></tr></thead>';
  const tbody=document.createElement('tbody');tbl.appendChild(tbody);wrap.appendChild(tbl);
  el.appendChild(wrap);el.dataset.rendered='1';
  el._csvTbody=tbody;
  _csvChunk(el,hasFilter?diffRows:allRows,pid);
}
function renderPages(el){
  if(el.dataset.rendered)return;
  let d=el._pagesData;
  if(!d){
    const script=document.getElementById(el.dataset.pagesSrc);
    if(!script)return;
    d=JSON.parse(script.textContent);
    el._pagesData=d;
    script.remove();
  }
  const SM=[['s-identical','Identical'],['s-minor','Minor'],['s-moderate','Moderate'],['s-significant','Changed'],['s-added','Added'],['s-removed','Removed']];
  const pid=el.id;
  const total=Object.values(d.counts).reduce((a,b)=>a+b,0);
  let fb=`<button class="pf-btn pf-all active" onclick="setPageFilter('${pid}','all')">All (${total})</button>`;
  for(const[css,lbl]of SM){const cnt=d.counts[css]||0;if(cnt)fb+=`<button class="pf-btn pf-${css}" onclick="setPageFilter('${pid}','${css}')"><span class="pf-dot ${css}"></span>${lbl} (${cnt})</button>`;}
  const wrap=document.createElement('div');
  wrap.className='panel-content';
  const fbar=document.createElement('div');
  fbar.className='pg-filter-bar';
  fbar.innerHTML=fb;
  wrap.appendChild(fbar);
  const tbl=document.createElement('table');
  tbl.className='sec-table';
  tbl.innerHTML='<thead><tr><th>Page</th><th>Status</th><th class="num">Similarity</th><th class="num">Changes</th><th></th></tr></thead>';
  const tbody=document.createElement('tbody');
  tbl.appendChild(tbody);
  wrap.appendChild(tbl);
  el.appendChild(wrap);
  el.dataset.rendered='1';
  // Chunked rendering — 100 rows per animation frame so the panel opens immediately
  let i=0;
  const CHUNK=100;
  function addChunk(){
    const curFilter=el._pgFilter||'all';
    const frag=document.createDocumentFragment();
    const end=Math.min(i+CHUNK,d.pages.length);
    for(;i<end;i++){
      const pg=d.pages[i];
      const tr=document.createElement('tr');
      if(pg[0]===0){
        const[,pna,pnb,ratio,add,ndel,lnr,diffB64]=pg;
        const css=ratio===1?'s-identical':ratio>=0.85?'s-minor':ratio>=0.60?'s-moderate':'s-significant';
        const badge=ratio===1?'IDENTICAL':ratio>=0.85?'MINOR':ratio>=0.60?'MODERATE':'CHANGED';
        const sim=(ratio*100).toFixed(1)+'%';
        const chg=`+${add} −${ndel}`;
        const lnrSpan=lnr?`<br><span class="pg-lnr">${lnr}</span>`:'';
        const dcell=diffB64?`<span class="card-toggle" onclick="togglePageDiff(this,'${pid}',${i})">▶ diff</span>`:'<span class="card-toggle no-diff">✓</span>';
        tr.className=css;
        tr.innerHTML=`<td>Page ${pna}${lnrSpan}</td><td><span class="badge ${css}">${badge}</span></td><td class="num">${sim}</td><td class="num mono">${chg}</td><td class="num">${dcell}</td>`;
      }else if(pg[0]===1){
        tr.className='s-added';
        tr.innerHTML=`<td>Page ${pg[1]}</td><td><span class="badge s-added">ADDED</span></td><td class="num">—</td><td class="num mono">${pg[2]} lines</td><td></td>`;
      }else{
        tr.className='s-removed';
        tr.innerHTML=`<td>Page ${pg[1]}</td><td><span class="badge s-removed">REMOVED</span></td><td class="num">—</td><td class="num mono">${pg[2]} lines</td><td></td>`;
      }
      if(curFilter!=='all'&&!tr.classList.contains(curFilter))tr.style.display='none';
      frag.appendChild(tr);
    }
    tbody.appendChild(frag);
    if(i<d.pages.length)requestAnimationFrame(addChunk);
  }
  addChunk();
}
async function togglePageDiff(btn,panelId,pageIdx){
  const panel=document.getElementById(panelId);
  const row=btn.closest('tr');
  const next=row.nextElementSibling;
  if(next&&next.classList.contains('diff-row')){
    const dw=next.querySelector('.diff-wrap');
    if(dw&&dw._diffAbort){dw._diffAbort.abort();delete dw._diffAbort;}
    next.remove();
    btn.textContent='▶ diff';
    return;
  }
  const pg=panel._pagesData.pages[pageIdx];
  const pna=pg[1];
  const diffB64=pg[7];
  if(!diffB64)return;
  const diffRow=document.createElement('tr');
  diffRow.className='diff-row open';
  const td=document.createElement('td');
  td.colSpan=5;
  const outer=document.createElement('div');
  outer.className='diff-outer open';
  outer.innerHTML=`<div class="diff-toolbar"><span>Page ${pna} — inline diff</span><span class="diff-legend"><span class="dl-del">− removed</span><span class="dl-chg">~ changed word</span><span class="dl-ins">+ added</span></span></div>`;
  const dw=document.createElement('div');
  dw.className='diff-wrap';
  dw._diffB64=diffB64;
  outer.appendChild(dw);
  td.appendChild(outer);
  diffRow.appendChild(td);
  row.after(diffRow);
  btn.textContent='▼ diff';
  await renderDiff(dw);
}
function clearPanel(el){
  // Abort scroll listeners on regular diff panels (data-diff-src)
  [el,...Array.from(el.querySelectorAll('[data-diff-src]'))].forEach(dw=>{
    if(dw._diffAbort){dw._diffAbort.abort();delete dw._diffAbort;}
    if(dw.dataset.diffSrc){dw.style.maxHeight='';delete dw.dataset.rendered;}
  });
  // Abort scroll listeners on on-demand page diff panels (no data-diff-src)
  el.querySelectorAll('.diff-row .diff-wrap').forEach(dw=>{
    if(dw._diffAbort){dw._diffAbort.abort();delete dw._diffAbort;}
  });
  el.querySelectorAll('.panel-content').forEach(c=>c.remove());
  delete el.dataset.rendered;
}
// ── Card lazy-init via consolidated blob ─────────────────────────────────────
let _cardBlobs=null;
function _getCardBlobs(){
  if(!_cardBlobs){
    const s=document.getElementById('all-card-data');
    _cardBlobs=s?JSON.parse(s.textContent):[];
  }
  return _cardBlobs;
}
async function _decompressB64(b64){
  const bin=atob(b64);
  const bytes=new Uint8Array(bin.length);
  for(let i=0;i<bin.length;i++)bytes[i]=bin.charCodeAt(i);
  const ds=new DecompressionStream('gzip');
  const w=ds.writable.getWriter();
  w.write(bytes);w.close();
  return new TextDecoder().decode(await new Response(ds.readable).arrayBuffer());
}
function _initCard(card){
  if(card._initPromise)return card._initPromise;
  const idx=+card.dataset.cardIdx;
  const blobs=_getCardBlobs();
  card._initPromise=(async()=>{
    if(idx>=blobs.length)return;
    const data=JSON.parse(await _decompressB64(blobs[idx]));
    // Wire diff rows directly — renderDiff checks _diffRows before _diffB64
    if(data.diff_rows){
      const dEl=card.querySelector('[data-diff-src]');
      if(dEl)dEl._diffRows=data.diff_rows;
    }
    if(data.pages){const pEl=card.querySelector('[data-pages-src]');if(pEl)pEl._pagesData=data.pages;}
    if(data.txn){const tEl=card.querySelector('[data-txn-src]');if(tEl)tEl._txnData=data.txn;}
    if(data.sec){const sEl=card.querySelector('[data-sec-src]');if(sEl)sEl._secData=data.sec;}
    if(data.csv){const cEl=card.querySelector('[data-csv-src]');if(cEl)cEl._csvData=data.csv;}
    card.dataset.initialized='1';
  })();
  return card._initPromise;
}
// Preload cards as they approach the viewport (1200px margin keeps init ahead of user)
const _cardIO=new IntersectionObserver(entries=>{
  entries.forEach(e=>{if(e.isIntersecting&&!e.target._initPromise)_initCard(e.target);});
},{rootMargin:'1200px'});

async function toggle(btn,id){
  const el=document.getElementById(id);
  const wasOpen=el.classList.contains('open');
  if(wasOpen){
    clearPanel(el);
  }else{
    // Await card init so panel data is ready before first render
    const card=el.closest('[data-card-idx]');
    if(card&&!card.dataset.initialized)await _initCard(card);
    if(el.dataset.pagesSrc&&!el.dataset.rendered)renderPages(el);
    else if(el.dataset.txnSrc&&!el.dataset.rendered)renderTxnPanel(el,el.dataset.txnSrc);
    else if(el.dataset.secSrc&&!el.dataset.rendered)renderSectionsPanel(el,el.dataset.secSrc);
    else if(el.dataset.csvSrc&&!el.dataset.rendered)renderCsvPanel(el,el.dataset.csvSrc);
    else{
      const target=el.dataset.diffSrc?el:el.querySelector('[data-diff-src]');
      if(target&&!target.dataset.rendered)await renderDiff(target);
    }
  }
  el.classList.toggle('open');
  btn.textContent=btn.textContent.replace(wasOpen?'▼':'▶',wasOpen?'▶':'▼');
}

function setFilter(verdict){
  const btns=document.querySelectorAll('.filter-btn');
  btns.forEach(b=>b.classList.remove('active'));
  const cards=document.querySelectorAll('.card[data-verdict]');
  if(verdict==='all'){
    cards.forEach(c=>c.style.display='');
    document.querySelector('.f-all').classList.add('active');
  } else {
    document.querySelector('.f-'+verdict).classList.add('active');
    cards.forEach(c=>{
      c.style.display=(c.dataset.verdict===verdict)?'':'none';
    });
  }
}
function setPageFilter(panelId,css){
  const panel=document.getElementById(panelId);
  panel._pgFilter=css;
  panel.querySelectorAll('.pf-btn').forEach(b=>b.classList.remove('active'));
  panel.querySelector('.pf-'+(css==='all'?'all':css)).classList.add('active');
  panel.querySelectorAll('table.sec-table > tbody > tr:not(.diff-row)').forEach(row=>{
    const show=css==='all'||row.classList.contains(css);
    row.style.display=show?'':'none';
    const next=row.nextElementSibling;
    if(next&&next.classList.contains('diff-row'))next.style.display=show?'':'none';
  });
}
document.addEventListener('DOMContentLoaded',()=>{
  document.querySelector('.f-all').classList.add('active');
  document.querySelectorAll('[data-card-idx]').forEach(c=>_cardIO.observe(c));
});
"""


def build_html(folder_a: Path, folder_b: Path,
               match: FolderMatchResult,
               outcomes: List[FilePairOutcome],
               cards_html: str,
               unmatched_html: str,
               ext: str,
               all_card_blobs: Optional[List[str]] = None) -> str:

    total = len(outcomes)
    ok    = [o for o in outcomes if not o.error]
    scores = [o.result.summary["overall_similarity_ratio"] for o in ok]
    avg   = (sum(scores) / len(scores) * 100) if scores else 0
    counts = {
        "identical":   sum(1 for r in scores if r == 1.00),
        "minor":       sum(1 for r in scores if 0.85 <= r <= 0.99),
        "moderate":    sum(1 for r in scores if 0.60 <= r < 0.85),
        "significant": sum(1 for r in scores if r < 0.60),
    }

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    filter_badges = "".join(
        f'<button class="filter-btn f-{v}" onclick="setFilter(\'{v}\')">'
        f'{counts[v]} {v.capitalize()}</button>'
        for v in ("identical", "minor", "moderate", "significant")
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Report Comparison — {folder_a.name} vs {folder_b.name}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">

  <h1>📄 Report Comparison</h1>
  <p class="subtitle">
    <strong>Folder A:</strong> {folder_a.resolve()} &nbsp;|&nbsp;
    <strong>Folder B:</strong> {folder_b.resolve()} &nbsp;|&nbsp;
    <strong>Generated:</strong> {now} &nbsp;|&nbsp;
    <strong>Ext:</strong> {ext}
  </p>

  <div class="dashboard">
    <div class="dash-stat">
      <span class="dash-val">{len(match.exact_pairs)+len(match.fuzzy_pairs)}</span>
      <span class="dash-lbl">Pairs compared</span>
    </div>
    <div class="dash-divider"></div>
    <div class="dash-stat">
      <span class="dash-val">{len(match.exact_pairs)}</span>
      <span class="dash-lbl">Exact matches</span>
    </div>
    <div class="dash-divider"></div>
    <div class="dash-stat">
      <span class="dash-val">{len(match.fuzzy_pairs)}</span>
      <span class="dash-lbl">Fuzzy matches</span>
    </div>
    <div class="dash-divider"></div>
    <div class="dash-stat">
      <span class="dash-val" style="color:var(--significant)">{len(match.only_in_a)+len(match.only_in_b)}</span>
      <span class="dash-lbl">Unmatched files</span>
    </div>
    <div class="dash-divider"></div>
    <div class="dash-stat">
      <span class="dash-val">{avg:.1f}%</span>
      <span class="dash-lbl">Avg similarity</span>
    </div>
    <div class="dash-divider"></div>
    <div class="dash-stat">
      <span class="dash-val" style="color:var(--identical)">{counts['identical']}</span>
      <span class="dash-lbl">Identical</span>
    </div>
    <div class="dash-stat">
      <span class="dash-val" style="color:var(--minor)">{counts['minor']}</span>
      <span class="dash-lbl">Minor</span>
    </div>
    <div class="dash-stat">
      <span class="dash-val" style="color:var(--moderate)">{counts['moderate']}</span>
      <span class="dash-lbl">Moderate</span>
    </div>
    <div class="dash-stat">
      <span class="dash-val" style="color:var(--significant)">{counts['significant']}</span>
      <span class="dash-lbl">Significant</span>
    </div>
  </div>

  <div class="filters">
    <span>Filter:</span>
    <button class="filter-btn f-all" onclick="setFilter('all')">All ({total})</button>
    {filter_badges}
  </div>

  {cards_html}
  {unmatched_html}

</div>
<script>{JS}</script>
<script type="application/json" id="all-card-data">{_json.dumps(all_card_blobs or [])}</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run(folder_a: Path, folder_b: Path,
        output: Path,
        ext: Union[str, List[str]],
        fuzzy: bool,
        fuzzy_threshold: float,
        use_semantic: bool,
        linux_base: Optional[str],
        windows_base: Optional[str],
        ignore_dates: bool = False,
        ignore_line_patterns: Optional[List[str]] = None,
        split_rules: Optional[List[SplitRule]] = None,
        transactions: bool = False,
        extract_txn: bool = False) -> None:

    output.parent.mkdir(parents=True, exist_ok=True)

    print(f"Scanning  : {folder_a}", file=sys.stderr)
    files_a = scan_folder(folder_a, ext)
    print(f"Scanning  : {folder_b}", file=sys.stderr)
    files_b = scan_folder(folder_b, ext)
    print(f"  A={len(files_a)} files   B={len(files_b)} files", file=sys.stderr)

    if extract_txn:
        if not split_rules:
            print("  --extract-txn requires --split-config (no sort_pattern rules loaded)",
                  file=sys.stderr)
        else:
            print("  Extracting transactions (--extract-txn)...", file=sys.stderr)
            for p in list(files_a.values()):
                out = extract_txn_csv_for_file(p, split_rules, ignore_dates, ignore_line_patterns,
                                               output.parent, prefix="A")
                if out:
                    print(f"    TXN EXTRACT → {out}", file=sys.stderr)
            for p in list(files_b.values()):
                out = extract_txn_csv_for_file(p, split_rules, ignore_dates, ignore_line_patterns,
                                               output.parent, prefix="B")
                if out:
                    print(f"    TXN EXTRACT → {out}", file=sys.stderr)

    match = match_filenames(files_a, files_b, fuzzy=fuzzy,
                             fuzzy_threshold=fuzzy_threshold)
    print(f"  Exact={len(match.exact_pairs)}  Fuzzy={len(match.fuzzy_pairs)}  "
          f"UnmatchedA={len(match.only_in_a)}  UnmatchedB={len(match.only_in_b)}",
          file=sys.stderr)

    outcomes: List[FilePairOutcome] = []
    card_shells: List[str] = []
    all_card_blobs: List[str] = []

    all_pairs = (
        [(pa, pb, "exact", 1.0) for pa, pb in match.exact_pairs] +
        [(pa, pb, "fuzzy", r) for pa, pb, r in match.fuzzy_pairs]
    )

    for i, (pa, pb, mtype, mratio) in enumerate(all_pairs):
        print(f"  Comparing [{mtype:5}] {pa.name} ↔ {pb.name}", file=sys.stderr)
        outcome = compare_folder_pair(pa, pb, mtype, mratio, use_semantic, ignore_dates, ignore_line_patterns, split_rules=split_rules, transactions=transactions)
        outcomes.append(outcome)

        _, url_a = resolve_paths(pa, linux_base, windows_base)
        _, url_b = resolve_paths(pb, linux_base, windows_base)

        body_a = outcome.body_lines_a
        body_b = outcome.body_lines_b
        per_page_lines = outcome.per_page_lines

        stem = pa.stem if mtype == "exact" else f"{pa.stem}_vs_{pb.stem}"

        if outcome.file_txn_comparisons:
            csv_path = output.parent / f"{stem}_txn.csv"
            write_txn_csv(outcome.file_txn_comparisons, csv_path)
            print(f"  TXN CSV → {csv_path}", file=sys.stderr)

        if outcome.file_section_comparisons:
            csv_path = output.parent / f"{stem}_sections.csv"
            write_section_csv(outcome.file_section_comparisons, csv_path)
            print(f"  SEC CSV → {csv_path}", file=sys.stderr)

        card_id = f"card-{i}"
        normalize = remove_dates_from_line if ignore_dates else None

        xlsx_fname: Optional[str] = None
        if outcome.file_csv_comparisons:
            xlsx_path = output.parent / f"{stem}_csv.xlsx"
            write_csv_xlsx(outcome.file_csv_comparisons, xlsx_path,
                           field_defs=outcome.field_defs, normalize=normalize)
            xlsx_fname = xlsx_path.name   # relative name for the HTML link

        shell, card_data = pair_card(outcome, url_a, url_b, card_id,
                                     body_a, body_b, per_page_lines,
                                     file_txn_comparisons=outcome.file_txn_comparisons or None,
                                     file_section_comparisons=outcome.file_section_comparisons or None,
                                     file_csv_comparisons=outcome.file_csv_comparisons or None,
                                     normalize=normalize,
                                     card_idx=i,
                                     field_defs=outcome.field_defs,
                                     xlsx_filename=xlsx_fname)
        card_shells.append(shell)
        # Encode card data as a single gzip+base64 blob; None for error cards → empty dict
        all_card_blobs.append(_b64gz(card_data if card_data is not None else {}))

    unmatched_html = unmatched_section(match.only_in_a, match.only_in_b,
                                        linux_base, windows_base)

    html = build_html(folder_a, folder_b, match, outcomes,
                       "".join(card_shells), unmatched_html, ext,
                       all_card_blobs=all_card_blobs)

    output.write_text(html, encoding="utf-8")
    print(f"\nHTML report → {output}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Generate an HTML comparison report from two report folders."
    )
    p.add_argument("folder_a", help="Baseline folder")
    p.add_argument("folder_b", help="Comparison folder")
    p.add_argument("--output", "-o", default="results/comparison_report.html",
                   help="Output HTML file (default: results/comparison_report.html)")
    p.add_argument("--ext", action="append", dest="ext", metavar="EXT",
                   help="File extension to compare (default: .txt); may be repeated: --ext .txt --ext .csv")
    p.add_argument("--fuzzy-match", action="store_true",
                   help="Pair files with similar (but not identical) names")
    p.add_argument("--fuzzy-threshold", type=float, default=0.70,
                   help="Min name-similarity ratio for fuzzy match (default: 0.70)")
    p.add_argument("--semantic", action="store_true",
                   help="Enable TF-IDF semantic similarity (requires scikit-learn)")
    p.add_argument("--ignore-dates", action="store_true",
                   help="Remove date/time patterns from comparison (ISO, US, timestamps, etc.)")
    p.add_argument("--ignore-lines", action="append", default=[], metavar="PATTERN",
                   help="Skip lines matching this regex (can be repeated: --ignore-lines PAT1 --ignore-lines PAT2)")
    p.add_argument("--linux-base",
                   help="Linux absolute path of the outputs root (for path remapping)")
    p.add_argument("--windows-base",
                   help="Windows absolute path of the outputs root (for file:// links)")
    p.add_argument("--split-config", metavar="CSV",
                   help="CSV with report_pattern,split_pattern[,sort_pattern] columns; "
                        "matched files use value-change page splitting instead of delimiter patterns")
    p.add_argument("--transactions", action="store_true",
                   help="Enable transaction-level comparison within pages; "
                        "transactions identified by date/time anchors or sort_pattern in --split-config")
    p.add_argument("--extract-txn", action="store_true",
                   help="Extract every transaction from each file in both folders whose --split-config "
                        "rule defines sort_pattern into <file>_txn_extract.csv next to the HTML report "
                        "(one CSV per file, sorted by sort key, one row per line: sort_key, line). "
                        "Independent of --transactions; requires --split-config.")

    args = p.parse_args()
    if not args.ext:
        args.ext = [".txt"]

    run(
        folder_a       = Path(args.folder_a),
        folder_b       = Path(args.folder_b),
        output         = Path(args.output),
        ext            = args.ext,
        fuzzy          = args.fuzzy_match,
        fuzzy_threshold= args.fuzzy_threshold,
        use_semantic   = args.semantic,
        linux_base     = args.linux_base,
        windows_base   = args.windows_base,
        ignore_dates          = args.ignore_dates,
        ignore_line_patterns  = args.ignore_lines or None,
        split_rules           = load_split_config(args.split_config) if args.split_config else None,
        transactions          = args.transactions,
        extract_txn           = args.extract_txn,
    )


if __name__ == "__main__":
    main()
