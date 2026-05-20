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

    # Fuzzy matching + custom Beyond Compare path
    python html_reporter.py folder_a/ folder_b/ --output results/report.html \\
        --fuzzy-match --fuzzy-threshold 0.60 \\
        --bcompare "C:\\Program Files\\Beyond Compare 4\\BCompare.exe"

    # Ignore date/time differences
    python html_reporter.py folder_a/ folder_b/ --output results/report.html --ignore-dates

    # Extra file extensions
    python html_reporter.py folder_a/ folder_b/ --output results/report.html --ext .log
"""

import argparse
import difflib
import re
import sys
from html import escape
from pathlib import Path
from typing import List, Optional, Tuple
from datetime import datetime

# ---------------------------------------------------------------------------
# Import the comparison engine
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))
from report_comparator import (
    scan_folder, match_filenames, compare_folder_pair,
    FolderMatchResult, FilePairOutcome,
    parse_report, compare_reports,
    diff_opcodes,
)

BCOMPARE_DEFAULT_WIN = r"C:\Program Files\Beyond Compare 4\BCompare.exe"


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
# Beyond Compare launcher (.bat)
# ---------------------------------------------------------------------------

def write_bcompare_bat(file_a: Path, file_b: Path,
                        bat_path: Path,
                        bcompare_exe: str,
                        linux_base: Optional[str],
                        windows_base: Optional[str]) -> str:
    """Write a .bat launcher for Beyond Compare; return the bat file URL."""
    win_a, _ = resolve_paths(file_a, linux_base, windows_base)
    win_b, _ = resolve_paths(file_b, linux_base, windows_base)
    content = (
        "@echo off\n"
        f'"{bcompare_exe}" "{win_a}" "{win_b}"\n'
    )
    bat_path.write_text(content, encoding="utf-8")
    _, bat_url = resolve_paths(bat_path, linux_base, windows_base)
    return bat_url


# ---------------------------------------------------------------------------
# Verdict helpers
# ---------------------------------------------------------------------------

def verdict_css(ratio: float) -> str:
    if ratio >= 0.98: return "identical"
    if ratio >= 0.85: return "minor"
    if ratio >= 0.60: return "moderate"
    return "significant"


def verdict_label(ratio: float) -> str:
    if ratio >= 0.98: return "IDENTICAL"
    if ratio >= 0.85: return "MINOR"
    if ratio >= 0.60: return "MODERATE"
    return "SIGNIFICANT"


def section_css(sec: dict) -> str:
    if sec["status"] == "added":   return "s-added"
    if sec["status"] == "removed": return "s-removed"
    r = sec.get("diff", {}).get("similarity_ratio", 1.0)
    if r >= 0.98: return "s-identical"
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


def bcompare_button(bat_url: str, cmd_a: str, cmd_b: str, bcompare_exe: str) -> str:
    cmd = f'{bcompare_exe} "{cmd_a}" "{cmd_b}"'
    esc = cmd.replace('"', '&quot;').replace("'", "&#39;")
    return (
        f'<div class="bc-row">'
        f'<a class="btn-bc" href="{bat_url}" download '
        f'   title="Download launcher then run it to open in Beyond Compare">'
        f'  <svg viewBox="0 0 20 20" width="16" height="16"><path fill="currentColor" '
        f'd="M10 2a8 8 0 100 16A8 8 0 0010 2zm1 5v4.586l2.293-2.293 1.414 '
        f'1.414L10 15.414l-4.707-4.707 1.414-1.414L9 11.586V7h2z"/></svg>'
        f'  Open in Beyond Compare'
        f'</a>'
        f'<button class="btn-copy" onclick="copyCmd(this)" data-cmd="{esc}">'
        f'  📋 Copy command'
        f'</button>'
        f'<code class="bc-cmd">{cmd}</code>'
        f'</div>'
    )


# ---------------------------------------------------------------------------
# Diff rendering
# ---------------------------------------------------------------------------

def word_diff_inline(old: str, new: str) -> Tuple[str, str]:
    """
    Return (old_html, new_html) where changed words are wrapped in <mark>
    tags so each row shows exactly which tokens were altered.

    Tokenises on whitespace boundaries so spaces are preserved faithfully.
    """
    tok_old = re.split(r'(\s+)', old)
    tok_new = re.split(r'(\s+)', new)

    sm = difflib.SequenceMatcher(None, tok_old, tok_new, autojunk=False)
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


def build_diff_html(lines_a: List[str], lines_b: List[str],
                    context: int = 3) -> str:
    """
    Build a unified-style HTML diff table.

    Layout: two narrow line-number columns (A | B) then the content column.
    Equal lines are shown collapsed when there are more than 2×context of them.
    For replace operations where both sides have equal line counts, word-level
    highlights are applied via word_diff_inline().
    Uses the pandas LCS engine (diff_opcodes) — no line-count cap.
    """
    opcodes = diff_opcodes(lines_a, lines_b)
    rows: List[str] = []
    la = lb = 1   # running line-number counters

    def ctx(line: str, a: int, b: int) -> str:
        return (f'<tr class="dc"><td class="ln">{a}</td><td class="ln">{b}</td>'
                f'<td class="dx"> {escape(line)}</td></tr>')

    def deleted(html_line: str, a: int) -> str:
        return (f'<tr class="dd"><td class="ln">{a}</td><td class="ln"></td>'
                f'<td class="dx">−&nbsp;{html_line}</td></tr>')

    def inserted(html_line: str, b: int) -> str:
        return (f'<tr class="di"><td class="ln"></td><td class="ln">{b}</td>'
                f'<td class="dx">+&nbsp;{html_line}</td></tr>')

    def skipped(n: int) -> str:
        return (f'<tr class="ds"><td colspan="3">'
                f'⋯ {n} unchanged line{"s" if n != 1 else ""} ⋯</td></tr>')

    for tag, i1, i2, j1, j2 in opcodes:
        if tag == 'equal':
            block = lines_a[i1:i2]
            n = len(block)
            if n <= 2 * context:
                for line in block:
                    rows.append(ctx(line, la, lb)); la += 1; lb += 1
            else:
                for line in block[:context]:
                    rows.append(ctx(line, la, lb)); la += 1; lb += 1
                skipped_n = n - 2 * context
                rows.append(skipped(skipped_n))
                la += skipped_n; lb += skipped_n
                for line in block[n - context:]:
                    rows.append(ctx(line, la, lb)); la += 1; lb += 1

        elif tag == 'insert':
            for line in lines_b[j1:j2]:
                rows.append(inserted(escape(line), lb)); lb += 1

        elif tag == 'delete':
            for line in lines_a[i1:i2]:
                rows.append(deleted(escape(line), la)); la += 1

        elif tag == 'replace':
            del_lines = lines_a[i1:i2]
            ins_lines = lines_b[j1:j2]
            if len(del_lines) == len(ins_lines):
                # Pair lines 1-to-1 and apply word-level highlighting
                for old, new in zip(del_lines, ins_lines):
                    old_html, new_html = word_diff_inline(old, new)
                    rows.append(deleted(old_html, la));   la += 1
                    rows.append(inserted(new_html, lb));  lb += 1
            else:
                # Unequal block sizes — show raw del block then ins block
                for line in del_lines:
                    rows.append(deleted(escape(line), la)); la += 1
                for line in ins_lines:
                    rows.append(inserted(escape(line), lb)); lb += 1

    return (
        '<table class="diff-table">'
        '<colgroup>'
        '<col style="width:44px"><col style="width:44px"><col>'
        '</colgroup>'
        '<thead><tr>'
        '<th class="ln">A</th><th class="ln">B</th><th>Content</th>'
        '</tr></thead>'
        '<tbody>' + ''.join(rows) + '</tbody>'
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
                "IDENTICAL" if ratio >= 0.98 else
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
                panel_id: str) -> Tuple[str, str]:
    """Return (toggle_html, panel_html) for per-page comparison.
    Returns ('', '') when there is nothing to show (single-page files).
    """
    if not page_comparisons:
        return '', ''

    rows: List[str] = []
    diff_sections: List[str] = []
    matched_idx = 0

    for pc in page_comparisons:
        if pc["status"] == "matched":
            r = pc["diff"]["similarity_ratio"]
            css = ("s-identical" if r >= 0.98 else
                   "s-minor"     if r >= 0.85 else
                   "s-moderate"  if r >= 0.60 else "s-significant")
            badge = ("IDENTICAL" if r >= 0.98 else
                     "MINOR"     if r >= 0.85 else
                     "MODERATE"  if r >= 0.60 else "CHANGED")
            sim = f"{r * 100:.1f}%"
            chg = f'+{pc["diff"]["lines_added"]} −{pc["diff"]["lines_deleted"]}'

            if r < 1.0 and matched_idx < len(per_page_lines):
                diff_pid = f"{panel_id}-pd{matched_idx}"
                la, lb = per_page_lines[matched_idx]
                diff_cell = (
                    f'<span class="card-toggle" onclick="toggle(this,\'{diff_pid}\')">'
                    f'▶ diff</span>'
                )
                diff_sections.append(
                    f'<div class="diff-wrap" id="{diff_pid}">'
                    f'<div class="diff-toolbar">'
                    f'  <span>Page {pc["page_num_a"]} — inline diff</span>'
                    f'  <span class="diff-legend">'
                    f'    <span class="dl-del">− removed</span>'
                    f'    <span class="dl-chg">~ changed word</span>'
                    f'    <span class="dl-ins">+ added</span>'
                    f'  </span>'
                    f'</div>'
                    + build_diff_html(la, lb)
                    + '</div>'
                )
            else:
                diff_cell = '<span class="card-toggle no-diff">✓</span>'

            matched_idx += 1
            rows.append(
                f'<tr class="{css}">'
                f'<td>Page {pc["page_num_a"]}</td>'
                f'<td><span class="badge {css}">{badge}</span></td>'
                f'<td class="num">{sim}</td>'
                f'<td class="num mono">{chg}</td>'
                f'<td class="num">{diff_cell}</td>'
                f'</tr>'
            )
        elif pc["status"] == "added":
            rows.append(
                f'<tr class="s-added">'
                f'<td>Page {pc["page_num_b"]}</td>'
                f'<td><span class="badge s-added">ADDED</span></td>'
                f'<td class="num">—</td>'
                f'<td class="num mono">{pc["lines"]} lines</td>'
                f'<td></td>'
                f'</tr>'
            )
        else:
            rows.append(
                f'<tr class="s-removed">'
                f'<td>Page {pc["page_num_a"]}</td>'
                f'<td><span class="badge s-removed">REMOVED</span></td>'
                f'<td class="num">—</td>'
                f'<td class="num mono">{pc["lines"]} lines</td>'
                f'<td></td>'
                f'</tr>'
            )

    table = (
        '<table class="sec-table">'
        '<thead><tr>'
        '<th>Page</th><th>Status</th>'
        '<th class="num">Similarity</th><th class="num">Changes</th><th></th>'
        '</tr></thead>'
        '<tbody>' + ''.join(rows) + '</tbody>'
        '</table>'
    )
    panel = (
        f'<div class="sec-detail" id="{panel_id}">'
        + table
        + ''.join(diff_sections)
        + '</div>'
    )
    toggle = (
        f'<span class="card-toggle" onclick="toggle(this,\'{panel_id}\')">'
        f'▶ pages</span>'
    )
    return toggle, panel


def pair_card(outcome: FilePairOutcome,
              bat_url: str,
              file_url_a: str,
              file_url_b: str,
              win_a: str,
              win_b: str,
              bcompare_exe: str,
              card_id: str,
              body_lines_a: List[str],
              body_lines_b: List[str],
              per_page_lines: Optional[List[Tuple[List[str], List[str]]]] = None) -> str:

    if outcome.error:
        return (
            f'<div class="card error" id="{card_id}">'
            f'<div class="card-header"><span class="badge significant">ERROR</span>'
            f' {outcome.file_a.name} ↔ {outcome.file_b.name}</div>'
            f'<p class="err-msg">{outcome.error}</p>'
            f'</div>'
        )

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

    sec_id  = f"sec-{card_id}"
    diff_id = f"dif-{card_id}"
    pg_id   = f"pg-{card_id}"

    pages_toggle, pages_detail = pages_panel(
        r.page_comparisons,
        per_page_lines or [],
        pg_id,
    )

    # Build the diff panel (only when there are differences)
    if ratio < 1.0 and (body_lines_a or body_lines_b):
        diff_panel = (
            f'<div class="diff-wrap" id="{diff_id}">'
            f'<div class="diff-toolbar">'
            f'  <span>Inline diff — body content (headers &amp; footers excluded)</span>'
            f'  <span class="diff-legend">'
            f'    <span class="dl-del">− removed</span>'
            f'    <span class="dl-chg">~ changed word</span>'
            f'    <span class="dl-ins">+ added</span>'
            f'  </span>'
            f'</div>'
            + build_diff_html(body_lines_a, body_lines_b) +
            f'</div>'
        )
        diff_toggle = f'<span class="card-toggle" onclick="toggle(this,\'{diff_id}\')">▶ diff</span>'
    else:
        diff_panel  = ''
        diff_toggle = '<span class="card-toggle no-diff">✓ no diff</span>'

    html = f"""
<div class="card {v_css}" id="{card_id}" data-verdict="{v_css}">

  <div class="card-header">
    <span class="badge {v_css}">{v_lbl}</span>
    <span class="card-title">{outcome.file_a.name}</span>
    {match_tag}
    {pages_toggle}
    <span class="card-toggle" onclick="toggle(this,'{sec_id}')">▶ sections</span>
    {diff_toggle}
  </div>

  <div class="file-links">
    {file_link("Folder&nbsp;A", file_url_a, outcome.file_a.name)}
    {file_link("Folder&nbsp;B", file_url_b, outcome.file_b.name)}
  </div>

  {bcompare_button(bat_url, win_a, win_b, bcompare_exe)}

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

  <div class="sec-detail" id="{sec_id}">
    <div class="sec-summary">
      {stat_box("Matched",   sm["sections_matched"])}
      {stat_box("Changed",   sm["sections_with_changes"])}
      {stat_box("Identical", sm["sections_identical"])}
      {stat_box("Added",     sm["sections_added"])}
      {stat_box("Removed",   sm["sections_removed"])}
    </div>
    {sections_table(r.sections)}
  </div>

  {diff_panel}

</div>"""
    return html


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
  --btn-bc:#2563eb;--btn-copy:#6b7280;
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
      margin-bottom:16px;overflow:hidden;border-left:4px solid var(--border)}
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

/* Beyond Compare */
.bc-row{display:flex;align-items:center;gap:10px;padding:10px 18px;
        border-bottom:1px solid var(--border);flex-wrap:wrap;background:#f0f4ff}
.btn-bc{display:inline-flex;align-items:center;gap:6px;background:var(--btn-bc);
        color:#fff;border-radius:6px;padding:5px 14px;font-size:13px;font-weight:500;
        border:none;cursor:pointer;text-decoration:none;white-space:nowrap}
.btn-bc:hover{background:#1d4ed8;color:#fff;text-decoration:none}
.btn-copy{display:inline-flex;align-items:center;gap:4px;background:#fff;
          color:var(--btn-copy);border:1px solid var(--border);border-radius:6px;
          padding:5px 12px;font-size:12px;cursor:pointer;white-space:nowrap}
.btn-copy:hover{border-color:#9ca3af}
.bc-cmd{font-size:11px;color:var(--muted);word-break:break-all}

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
.sec-table{width:100%;border-collapse:collapse;font-size:13px}
.sec-table th{text-align:left;padding:6px 10px;background:#f9fafb;
              border-bottom:2px solid var(--border);color:var(--muted);font-weight:600;font-size:12px}
.sec-table td{padding:6px 10px;border-bottom:1px solid var(--border);vertical-align:middle}
.sec-table tr:last-child td{border-bottom:none}
.sec-table .num{text-align:right}
.sec-table .mono{font-family:monospace;font-size:12px}
.sec-table tr.s-identical td:first-child{border-left:3px solid var(--identical)}
.sec-table tr.s-minor td:first-child{border-left:3px solid var(--minor)}
.sec-table tr.s-moderate td:first-child{border-left:3px solid var(--moderate)}
.sec-table tr.s-significant td:first-child{border-left:3px solid var(--significant)}
.sec-table tr.s-added td:first-child{border-left:3px solid var(--s-added)}
.sec-table tr.s-removed td:first-child{border-left:3px solid var(--s-removed)}
.title-changed{font-size:11px;color:var(--moderate);margin-left:6px}

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
.diff-wrap{display:none;border-top:1px solid var(--border);overflow-x:auto;max-height:600px;overflow-y:auto}
.diff-wrap.open{display:block}
.diff-toolbar{display:flex;justify-content:space-between;align-items:center;
              padding:6px 14px;background:#f9fafb;border-bottom:1px solid var(--border);
              font-size:12px;color:var(--muted);flex-wrap:wrap;gap:8px;position:sticky;top:0;z-index:1}
.diff-legend{display:flex;gap:12px}
.dl-del{color:#991b1b;font-weight:600}
.dl-ins{color:#166534;font-weight:600}
.dl-chg{color:#92400e;font-weight:600}
.diff-table{width:100%;border-collapse:collapse;font-family:'Cascadia Code','Consolas',monospace;font-size:12px;line-height:1.6}
.diff-table thead th{padding:3px 8px;background:#f1f5f9;border-bottom:2px solid var(--border);
                     color:var(--muted);font-size:11px;font-weight:600;position:sticky;top:33px}
.diff-table .ln{width:44px;text-align:right;color:#9ca3af;background:#f8fafc;
                border-right:1px solid var(--border);padding:1px 6px;
                font-size:11px;user-select:none;white-space:nowrap}
.diff-table .dx{padding:1px 12px;white-space:pre-wrap;word-break:break-all}

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
"""

JS = """
function toggle(btn,id){
  const el=document.getElementById(id);
  const wasOpen=el.classList.contains('open');
  el.classList.toggle('open');
  btn.textContent=btn.textContent.replace(wasOpen?'▼':'▶',wasOpen?'▶':'▼');
}
function copyCmd(btn){
  const cmd=btn.getAttribute('data-cmd');
  navigator.clipboard.writeText(cmd).then(()=>{
    const orig=btn.textContent;
    btn.textContent='✓ Copied!';
    setTimeout(()=>btn.textContent=orig,1800);
  });
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
document.addEventListener('DOMContentLoaded',()=>{
  document.querySelector('.f-all').classList.add('active');
});
"""


def build_html(folder_a: Path, folder_b: Path,
               match: FolderMatchResult,
               outcomes: List[FilePairOutcome],
               cards_html: str,
               unmatched_html: str,
               ext: str,
               bcompare_exe: str) -> str:

    total = len(outcomes)
    ok    = [o for o in outcomes if not o.error]
    scores = [o.result.summary["overall_similarity_ratio"] for o in ok]
    avg   = (sum(scores) / len(scores) * 100) if scores else 0
    counts = {
        "identical":   sum(1 for r in scores if r >= 0.98),
        "minor":       sum(1 for r in scores if 0.85 <= r < 0.98),
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
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run(folder_a: Path, folder_b: Path,
        output: Path,
        ext: str,
        fuzzy: bool,
        fuzzy_threshold: float,
        use_semantic: bool,
        linux_base: Optional[str],
        windows_base: Optional[str],
        bcompare_exe: str,
        ignore_dates: bool = False) -> None:

    output.parent.mkdir(parents=True, exist_ok=True)

    print(f"Scanning  : {folder_a}", file=sys.stderr)
    files_a = scan_folder(folder_a, ext)
    print(f"Scanning  : {folder_b}", file=sys.stderr)
    files_b = scan_folder(folder_b, ext)
    print(f"  A={len(files_a)} files   B={len(files_b)} files", file=sys.stderr)

    match = match_filenames(files_a, files_b, fuzzy=fuzzy,
                             fuzzy_threshold=fuzzy_threshold)
    print(f"  Exact={len(match.exact_pairs)}  Fuzzy={len(match.fuzzy_pairs)}  "
          f"UnmatchedA={len(match.only_in_a)}  UnmatchedB={len(match.only_in_b)}",
          file=sys.stderr)

    outcomes: List[FilePairOutcome] = []
    cards_html = ""

    all_pairs = (
        [(pa, pb, "exact", 1.0) for pa, pb in match.exact_pairs] +
        [(pa, pb, "fuzzy", r) for pa, pb, r in match.fuzzy_pairs]
    )

    for i, (pa, pb, mtype, mratio) in enumerate(all_pairs):
        print(f"  Comparing [{mtype:5}] {pa.name} ↔ {pb.name}", file=sys.stderr)
        outcome = compare_folder_pair(pa, pb, mtype, mratio, use_semantic, ignore_dates)
        outcomes.append(outcome)

        win_a, url_a = resolve_paths(pa, linux_base, windows_base)
        win_b, url_b = resolve_paths(pb, linux_base, windows_base)

        # Parse body lines for diff rendering (headers/footers already stripped)
        try:
            ra = parse_report(str(pa), ignore_dates=ignore_dates)
            rb = parse_report(str(pb), ignore_dates=ignore_dates)
            body_a, body_b = ra.body_lines, rb.body_lines
            n_matched = min(len(ra.pages), len(rb.pages))
            per_page_lines = [
                ([ln for ln in ra.pages[i].body_lines if ln.strip()],
                 [ln for ln in rb.pages[i].body_lines if ln.strip()])
                for i in range(n_matched)
            ]
        except Exception:
            body_a, body_b = [], []
            per_page_lines = []

        # Generate .bat launcher
        stem = pa.stem if mtype == "exact" else f"{pa.stem}_vs_{pb.stem}"
        bat_path = output.parent / f"open_bcompare_{stem}.bat"
        bat_url = write_bcompare_bat(pa, pb, bat_path, bcompare_exe,
                                      linux_base, windows_base)

        card_id = f"card-{i}"
        cards_html += pair_card(outcome, bat_url, url_a, url_b,
                                 win_a, win_b, bcompare_exe, card_id,
                                 body_a, body_b, per_page_lines)

    unmatched_html = unmatched_section(match.only_in_a, match.only_in_b,
                                        linux_base, windows_base)

    html = build_html(folder_a, folder_b, match, outcomes,
                       cards_html, unmatched_html, ext, bcompare_exe)

    output.write_text(html, encoding="utf-8")
    print(f"\nHTML report → {output}", file=sys.stderr)
    print(f"  Also wrote {len(all_pairs)} .bat launcher(s) to {output.parent}", file=sys.stderr)


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
    p.add_argument("--ext", default=".txt",
                   help="File extension to compare (default: .txt)")
    p.add_argument("--fuzzy-match", action="store_true",
                   help="Pair files with similar (but not identical) names")
    p.add_argument("--fuzzy-threshold", type=float, default=0.70,
                   help="Min name-similarity ratio for fuzzy match (default: 0.70)")
    p.add_argument("--semantic", action="store_true",
                   help="Enable TF-IDF semantic similarity (requires scikit-learn)")
    p.add_argument("--ignore-dates", action="store_true",
                   help="Remove date/time patterns from comparison (ISO, US, timestamps, etc.)")
    p.add_argument("--linux-base",
                   help="Linux absolute path of the outputs root (for path remapping)")
    p.add_argument("--windows-base",
                   help="Windows absolute path of the outputs root (for file:// links)")
    p.add_argument("--bcompare",
                   default=BCOMPARE_DEFAULT_WIN,
                   help=f"Path to BCompare.exe (default: {BCOMPARE_DEFAULT_WIN})")

    args = p.parse_args()

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
        bcompare_exe   = args.bcompare,
        ignore_dates   = args.ignore_dates,
    )


if __name__ == "__main__":
    main()
