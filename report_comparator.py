"""
report_comparator.py
====================
A framework for comparing text-based reports that contain headers, footers,
and page splits.  Supports both single-file and whole-folder comparison.

─────────────────────────────────────────────
SINGLE-FILE MODE
─────────────────────────────────────────────
    python report_comparator.py report_a.txt report_b.txt
    python report_comparator.py report_a.txt report_b.txt --output diff.txt
    python report_comparator.py report_a.txt report_b.txt --semantic
    python report_comparator.py report_a.txt report_b.txt --ignore-dates

─────────────────────────────────────────────
FOLDER MODE  (pass two directory paths)
─────────────────────────────────────────────
    python report_comparator.py folder_a/ folder_b/
    python report_comparator.py folder_a/ folder_b/ --output-dir results/
    python report_comparator.py folder_a/ folder_b/ --ext .txt --semantic
    python report_comparator.py folder_a/ folder_b/ --fuzzy-match
    python report_comparator.py folder_a/ folder_b/ --ignore-dates
    python report_comparator.py folder_a/ folder_b/ --no-diff

Folder mode produces:
  • One comparison file per matched pair  →  <output-dir>/<filename>_comparison.txt
  • A master summary across all files     →  <output-dir>/FOLDER_SUMMARY.txt

Mismatch handling:
  • Files present in only one folder are flagged as UNMATCHED.
  • With --fuzzy-match, files whose names are similar (≥ 70 % ratio) are
    paired even when names differ slightly (e.g. "report_v1.txt" ↔ "report_v2.txt").
    The fuzzy-match log is included in the master summary.

Dependencies:
    - difflib       (stdlib)
    - re            (stdlib)
    - argparse      (stdlib)
    - pathlib       (stdlib)
    - pandas        (pip install pandas)
    - scikit-learn  (optional — pip install scikit-learn)
"""

import re
import bisect
import csv
import difflib
import argparse
import sys
import os
import math
import pandas as pd
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Set

from date_utils import filter_lines, has_dates, first_date_match


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Page:
    number: int
    raw_lines: List[str]
    header_lines: List[str] = field(default_factory=list)
    footer_lines: List[str] = field(default_factory=list)
    body_lines: List[str] = field(default_factory=list)
    body_line_numbers: List[int] = field(default_factory=list)  # 1-based raw file position


@dataclass
class Section:
    title: str
    level: int          # 1 = top-level, 2 = sub-section, etc.
    lines: List[str] = field(default_factory=list)
    line_numbers: List[int] = field(default_factory=list)   # 1-based raw file position

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip()


@dataclass
class Transaction:
    sort_key: str          # raw extracted key string (used for matching A↔B)
    sort_val: object       # comparable: datetime | float | str (used for ordering)
    lines: List[str] = field(default_factory=list)
    line_numbers: List[int] = field(default_factory=list)


@dataclass
class ParsedReport:
    filename: str
    pages: List[Page] = field(default_factory=list)
    sections: List[Section] = field(default_factory=list)
    global_header: List[str] = field(default_factory=list)   # repeating header
    global_footer: List[str] = field(default_factory=list)   # repeating footer
    body_lines: List[str] = field(default_factory=list)      # all body text (flat)
    body_line_numbers: List[int] = field(default_factory=list)  # 1-based raw file position per body line


@dataclass
class ComparisonResult:
    structural: Dict
    metadata: Dict
    content: Dict
    sections: List[Dict]
    summary: Dict
    page_comparisons: List[Dict] = field(default_factory=list)


@dataclass
class SplitRule:
    """One row from a --split-config CSV: filename pattern → page-split + optional sort pattern."""
    report_re: re.Pattern
    split_re: re.Pattern
    sort_re: Optional[re.Pattern] = None
    max_txn_lines: Optional[int] = None


def load_split_config(csv_path: str) -> List["SplitRule"]:
    """Load report_pattern,split_pattern[,sort_pattern[,max_txn_lines]] CSV into SplitRule list."""
    rules: List[SplitRule] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sort_pat = row.get("sort_pattern", "").strip()
            mtl_raw  = row.get("max_txn_lines", "").strip()
            rules.append(SplitRule(
                report_re=re.compile(row["report_pattern"]),
                split_re=re.compile(row["split_pattern"]),
                sort_re=re.compile(sort_pat) if sort_pat else None,
                max_txn_lines=int(mtl_raw) if mtl_raw else None,
            ))
    return rules


def find_split_pattern(filename: str,
                       rules: Optional[List["SplitRule"]]) -> Optional[re.Pattern]:
    """Return the first split_re whose report_re matches the filename, else None."""
    if not rules:
        return None
    name = Path(filename).name
    for rule in rules:
        if rule.report_re.search(name):
            return rule.split_re
    return None


def find_sort_pattern(filename: str,
                      rules: Optional[List["SplitRule"]]) -> Optional[re.Pattern]:
    """Return the first sort_re whose report_re matches the filename, else None."""
    if not rules:
        return None
    name = Path(filename).name
    for rule in rules:
        if rule.report_re.search(name):
            return rule.sort_re
    return None


def find_max_txn_lines(filename: str,
                       rules: Optional[List["SplitRule"]]) -> Optional[int]:
    """Return max_txn_lines for the first matching rule, else None (unlimited)."""
    if not rules:
        return None
    name = Path(filename).name
    for rule in rules:
        if rule.report_re.search(name):
            return rule.max_txn_lines
    return None


# ---------------------------------------------------------------------------
# 1.  PAGE SPLITTER
# ---------------------------------------------------------------------------

# Common page-break patterns in text reports
_PAGE_BREAK_PATTERNS = [
    r'^\s*PAGE\s+\d+\s*$',                             # PAGE 1 / PAGE  2
    r'\f',                                          # form-feed character
    r'^-{3,}\s*[Pp]age\s*\d+\s*-{3,}$',           # --- Page 3 ---
    r'^={3,}\s*[Pp]age\s*\d+\s*={3,}$',            # === Page 3 ===
    r'^\s*[Pp]age\s+\d+\s+of\s+\d+\s*$',           # Page 3 of 10
    r'^[-_]{10,}\s*$',                               # ──────────────  (must start at col 0)
    r'^\*{10,}\s*$',                                 # ************   (must start at col 0)
]

_PAGE_BREAK_RE = re.compile(
    "|".join(_PAGE_BREAK_PATTERNS), re.MULTILINE
)


def _split_by_value_change(text: str, pattern: re.Pattern) -> List[List[str]]:
    """Split text into pages wherever the value captured by pattern changes.

    Each line is tested with pattern.search().  The captured value is group(1)
    when the pattern has a capturing group, otherwise the whole match string.
    Lines that don't match are left in the current page with no break.
    The line that triggers a value change becomes the first line of the new page.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    all_lines = text.split("\n")

    pages: List[List[str]] = []
    current: List[str] = []
    last_val: Optional[str] = None

    def _flush(buf: List[str]) -> None:
        while buf and not buf[0].strip():
            buf.pop(0)
        while buf and not buf[-1].strip():
            buf.pop()
        if buf:
            pages.append(buf)

    for line in all_lines:
        m = pattern.search(line)
        if m:
            val = m.group(1) if m.lastindex else m.group(0)
            if last_val is not None and val != last_val:
                _flush(current)
                current = []
            last_val = val
        current.append(line)

    _flush(current)
    return pages if pages else [all_lines]


def split_into_pages(text: str,
                     split_pattern: Optional[re.Pattern] = None) -> List[List[str]]:
    """Split raw report text into a list of pages (each page = list of lines)."""
    if split_pattern is not None:
        return _split_by_value_change(text, split_pattern)

    # Normalise line endings
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Split on page-break markers
    chunks = _PAGE_BREAK_RE.split(text)

    pages = []
    for chunk in chunks:
        lines = chunk.split("\n")
        # Strip purely blank leading/trailing lines per page
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            pages.append(lines)

    # Fallback: if no page breaks found, treat whole file as one page
    if not pages:
        lines = text.split("\n")
        pages.append(lines)

    return pages


# ---------------------------------------------------------------------------
# 2.  HEADER / FOOTER DETECTOR
# ---------------------------------------------------------------------------

def detect_repeating_lines(pages: List[List[str]],
                            check_lines: int = 3,
                            min_frequency: float = 0.6) -> Tuple[List[str], List[str]]:
    """
    Identify lines that appear in the same position (top or bottom) across
    a majority of pages — these are likely headers/footers.

    Returns (header_candidates, footer_candidates) as lists of stripped strings.
    """
    if len(pages) < 2:
        return [], []

    threshold = max(2, int(len(pages) * min_frequency))

    top_series = pd.Series(
        [line.strip() for page in pages for line in page[:check_lines]]
    )
    bot_series = pd.Series(
        [line.strip() for page in pages for line in page[-check_lines:]]
    )

    top_counts = top_series[top_series != ""].value_counts()
    bot_counts = bot_series[bot_series != ""].value_counts()

    headers = top_counts[top_counts >= threshold].index.tolist()
    footers = bot_counts[bot_counts >= threshold].index.tolist()

    return headers, footers


def strip_header_footer(page_lines: List[str],
                        headers: List[str],
                        footers: List[str],
                        check_lines: int = 3) -> Tuple[List[str], List[str], List[str]]:
    """
    Remove header and footer lines from a page.
    Returns (header_lines, body_lines, footer_lines).
    """
    lines = list(page_lines)
    found_headers = []
    found_footers = []

    # Strip from top
    i = 0
    while i < min(check_lines, len(lines)):
        if lines[i].strip() in headers:
            found_headers.append(lines[i])
            lines.pop(i)
        else:
            i += 1

    # Strip from bottom
    i = len(lines) - 1
    stripped_foot = 0
    while i >= max(0, len(lines) - check_lines) and stripped_foot < check_lines:
        if lines[i].strip() in footers:
            found_footers.insert(0, lines[i])
            lines.pop(i)
            stripped_foot += 1
        i -= 1

    return found_headers, lines, found_footers


# ---------------------------------------------------------------------------
# 3.  SECTION DETECTOR
# ---------------------------------------------------------------------------

# Matches "PAGE N", "Page N", or "page N" at the start of a line (captures the number)
_PAGE_SECTION_RE = re.compile(r'^\s*[Pp][Aa][Gg][Ee]\s+(\d+)\b')


def extract_sections(body_lines: List[str], body_line_numbers: List[int] = None) -> List[Section]:
    """
    Split body text into sections by page markers ("PAGE N", "Page N", "page N").
    Content before the first marker becomes "Page 0".

    Vectorized pipeline:
      1. str.extract() applies the regex to every line at C speed.
      2. cumsum() on the is_marker boolean assigns a section-ID to every line.
      3. groupby(section_id) collects each section's lines in one pass.
    """
    if not body_lines:
        return [Section(title="Page 0", level=1, lines=[])]

    s        = pd.Series(body_lines)
    page_num = s.str.extract(_PAGE_SECTION_RE.pattern, expand=False)
    is_marker = page_num.notna()
    section_id = is_marker.cumsum()

    df = pd.DataFrame({
        "line":       body_lines,
        "page_num":   page_num,
        "is_marker":  is_marker,
        "section_id": section_id,
    })

    sections: List[Section] = []
    for _sid, grp in df.groupby("section_id", sort=True):
        first = grp.iloc[0]
        if first["is_marker"]:
            title = f"Page {first['page_num']}"
            sub = grp.iloc[1:]
        else:
            title = "Page 0"
            sub = grp
        lines = sub["line"].tolist()
        nums = [body_line_numbers[i] for i in sub.index] if body_line_numbers else []
        sections.append(Section(title=title, level=1, lines=lines, line_numbers=nums))

    return sections


# ---------------------------------------------------------------------------
# 4.  REPORT PARSER
# ---------------------------------------------------------------------------

def parse_report(filename: str, ignore_dates: bool = False,
                 ignore_line_patterns: Optional[List[str]] = None,
                 split_pattern: Optional[re.Pattern] = None) -> ParsedReport:
    """Top-level parser: read file → pages → header/footer → sections."""
    try:
        with open(filename, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except FileNotFoundError:
        print(f"ERROR: File not found: {filename}")
        sys.exit(1)

    report = ParsedReport(filename=filename)

    raw_pages = split_into_pages(text, split_pattern=split_pattern)
    global_headers, global_footers = detect_repeating_lines(raw_pages)

    report.global_header = global_headers
    report.global_footer = global_footers

    # Forward-scan tracker: maps each body line to its 1-based position in the raw file.
    # Scanning forward ensures correctness even when the same text appears on multiple pages.
    raw_text_lines = text.replace("\r\n", "\n").replace("\r", "\n").split('\n')
    _scan_pos = 0

    def _raw_line_num(line_text: str) -> int:
        nonlocal _scan_pos
        for j in range(_scan_pos, len(raw_text_lines)):
            if raw_text_lines[j] == line_text:
                _scan_pos = j + 1
                return j + 1
        return _scan_pos  # fallback: shouldn't normally be reached

    _cpats = [re.compile(p) for p in ignore_line_patterns] if ignore_line_patterns else []

    all_body_lines: List[str] = []
    all_body_line_nums: List[int] = []

    for i, raw_lines in enumerate(raw_pages):
        h, body, f = strip_header_footer(raw_lines, global_headers, global_footers)
        # Compute positions against the pre-mask body so they match the raw file
        page_nums = [_raw_line_num(ln) for ln in body]
        if _cpats:
            keep = [not any(c.search(l) for c in _cpats) for l in body]
            body = [l for l, k in zip(body, keep) if k]
            page_nums = [n for n, k in zip(page_nums, keep) if k]
        if ignore_dates:
            body = filter_lines(body)
        page = Page(number=i + 1,
                    raw_lines=raw_lines,
                    header_lines=h,
                    footer_lines=f,
                    body_lines=body,
                    body_line_numbers=page_nums)
        report.pages.append(page)
        all_body_lines.extend(body)
        all_body_line_nums.extend(page_nums)

    report.body_lines = all_body_lines
    report.body_line_numbers = all_body_line_nums
    report.sections = extract_sections(all_body_lines, all_body_line_nums)

    return report


# ---------------------------------------------------------------------------
# 5.  DIFF UTILITIES
# ---------------------------------------------------------------------------

_MAX_TITLE_CMP_LEN  = 500     # cap title length fed to title similarity heuristics
_MAX_EDIT_RATIO_LEN = 500    # cap strings passed to O(n*m) edit-distance DP
_TITLE_TOP_K        = 10     # max candidates passed to O(n*m) edit DP per A-title
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_STOP_TOKENS = frozenset({
    "the", "and", "for", "with", "from", "into", "over", "under",
    "between", "that", "this", "these", "those", "have", "has",
    "had", "are", "was", "were", "a", "an", "of", "in", "on",
    "to", "by", "at", "as", "or", "if", "is"
})


def _normalize_title(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _title_tokens(text: str) -> Set[str]:
    tokens = [tok for tok in _TOKEN_RE.findall(text) if len(tok) > 1]
    if not tokens:
        return {text}
    meaningful = [tok for tok in tokens if tok not in _STOP_TOKENS]
    return set(meaningful or tokens)


def _edit_ratio(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    len_a, len_b = len(a), len(b)
    if len_a < len_b:
        a, b = b, a
        len_a, len_b = len_b, len_a
    prev_row = list(range(len_b + 1))
    for i, ca in enumerate(a, start=1):
        curr_row = [i]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            curr_row.append(min(prev_row[j] + 1, curr_row[-1] + 1, prev_row[j - 1] + cost))
        prev_row = curr_row
    distance = prev_row[-1]
    return 1.0 - (distance / max(len_a, len_b))


def _string_similarity(a: str, b: str) -> float:
    a_norm = _normalize_title(a)
    b_norm = _normalize_title(b)
    if len(a_norm) > _MAX_EDIT_RATIO_LEN or len(b_norm) > _MAX_EDIT_RATIO_LEN:
        a_norm = a_norm[:_MAX_EDIT_RATIO_LEN]
        b_norm = b_norm[:_MAX_EDIT_RATIO_LEN]
    return _edit_ratio(a_norm, b_norm)


def diff_opcodes(
    lines_a: List[str], lines_b: List[str]
) -> List[Tuple[str, int, int, int, int]]:
    """Return difflib-style opcodes for two line sequences.

    Uses pandas (line, rank) merge + patience-sort LIS instead of
    SequenceMatcher — O(n log n) time and O(n) space at any file size.

    The (line, rank) merge pairs the k-th occurrence of a line in A with
    the k-th in B, so even files full of repeated lines produce at most
    O(n) candidate matches before the LIS step.
    """
    if not lines_a and not lines_b:
        return []
    if not lines_a:
        return [('insert', 0, 0, 0, len(lines_b))]
    if not lines_b:
        return [('delete', 0, len(lines_a), 0, 0)]

    # Step 1 — assign in-order occurrence ranks (fully vectorised)
    df_a = pd.DataFrame({'line': lines_a, 'pos_a': range(len(lines_a))})
    df_a['rank'] = df_a.groupby('line', sort=False).cumcount()

    df_b = pd.DataFrame({'line': lines_b, 'pos_b': range(len(lines_b))})
    df_b['rank'] = df_b.groupby('line', sort=False).cumcount()

    # Step 2 — merge on (line, rank) → valid in-order match pairs
    matches = (
        df_a.merge(df_b, on=['line', 'rank'])[['pos_a', 'pos_b']]
        .sort_values('pos_a')
        .reset_index(drop=True)
    )

    if matches.empty:
        return [('replace', 0, len(lines_a), 0, len(lines_b))]

    pos_a: List[int] = matches['pos_a'].tolist()
    pos_b: List[int] = matches['pos_b'].tolist()

    # Step 3 — LIS on pos_b (sorted by pos_a) via patience sort: O(n log n)
    tails:      List[int] = []   # tails[i] = smallest pos_b ending IS of length i+1
    tail_idx:   List[int] = []   # index into pos_b of that element
    predecessor: List[int] = [-1] * len(pos_b)

    for k, v in enumerate(pos_b):
        ins = bisect.bisect_left(tails, v)
        if ins == len(tails):
            tails.append(v)
            tail_idx.append(k)
        else:
            tails[ins] = v
            tail_idx[ins] = k
        if ins > 0:
            predecessor[k] = tail_idx[ins - 1]

    # Reconstruct LCS index list
    lcs_indices: List[int] = []
    k = tail_idx[-1] if tail_idx else -1
    while k >= 0:
        lcs_indices.append(k)
        k = predecessor[k]
    lcs_indices.reverse()

    lcs_a = [pos_a[i] for i in lcs_indices]
    lcs_b = [pos_b[i] for i in lcs_indices]

    # Step 4 — merge consecutive adjacent LCS pairs into equal runs → opcodes
    opcodes: List[Tuple[str, int, int, int, int]] = []
    prev_a = prev_b = 0
    i = 0
    n = len(lcs_a)

    while i < n:
        run_s = i
        while (i + 1 < n
               and lcs_a[i + 1] == lcs_a[i] + 1
               and lcs_b[i + 1] == lcs_b[i] + 1):
            i += 1

        ea_s, eb_s = lcs_a[run_s], lcs_b[run_s]
        ea_e, eb_e = lcs_a[i] + 1, lcs_b[i] + 1

        if ea_s > prev_a or eb_s > prev_b:
            if ea_s > prev_a and eb_s > prev_b:
                tag = 'replace'
            elif ea_s > prev_a:
                tag = 'delete'
            else:
                tag = 'insert'
            opcodes.append((tag, prev_a, ea_s, prev_b, eb_s))

        opcodes.append(('equal', ea_s, ea_e, eb_s, eb_e))
        prev_a, prev_b = ea_e, eb_e
        i += 1

    if prev_a < len(lines_a) or prev_b < len(lines_b):
        if prev_a < len(lines_a) and prev_b < len(lines_b):
            tag = 'replace'
        elif prev_a < len(lines_a):
            tag = 'delete'
        else:
            tag = 'insert'
        opcodes.append((tag, prev_a, len(lines_a), prev_b, len(lines_b)))

    return opcodes


def _grouped_opcodes(
    opcodes: List[Tuple[str, int, int, int, int]], n: int = 3
) -> List[List[Tuple[str, int, int, int, int]]]:
    """Group opcodes into context-bounded hunks (port of SequenceMatcher.get_grouped_opcodes)."""
    if not opcodes:
        return []
    codes = list(opcodes)
    # Trim leading/trailing equal blocks to at most n lines
    if codes[0][0] == 'equal':
        tag, i1, i2, j1, j2 = codes[0]
        codes[0] = tag, max(i1, i2 - n), i2, max(j1, j2 - n), j2
    if codes[-1][0] == 'equal':
        tag, i1, i2, j1, j2 = codes[-1]
        codes[-1] = tag, i1, min(i2, i1 + n), j1, min(j2, j1 + n)

    nn = n + n
    groups = []
    group: List[Tuple[str, int, int, int, int]] = []
    for tag, i1, i2, j1, j2 in codes:
        if tag == 'equal' and i2 - i1 > nn:
            group.append((tag, i1, min(i2, i1 + n), j1, min(j2, j1 + n)))
            groups.append(group)
            group = []
            i1, j1 = max(i1, i2 - n), max(j1, j2 - n)
        group.append((tag, i1, i2, j1, j2))
    if group and not (len(group) == 1 and group[0][0] == 'equal'):
        groups.append(group)
    return groups


def line_diff_stats(lines_a: List[str], lines_b: List[str]) -> Dict:
    """Compute line-level diff stats (pandas multiset hash-join, O(n+m)).

    Uses value_counts() + index intersection to count matching lines by
    frequency (Dice coefficient). Consistent at all input sizes — no
    difflib recursion risk and no split-threshold inconsistency.
    """
    cnt_a  = pd.Series(lines_a).value_counts()
    cnt_b  = pd.Series(lines_b).value_counts()
    idx    = cnt_a.index.intersection(cnt_b.index)
    common = int(cnt_a[idx].combine(cnt_b[idx], min).sum()) if len(idx) else 0
    added   = len(lines_b) - common
    deleted = len(lines_a) - common
    total   = max(len(lines_a), len(lines_b), 1)
    denom   = len(lines_a) + len(lines_b)
    ratio   = (2 * common / denom) if denom else 1.0
    sim     = round(ratio, 4)
    if added + deleted > 0:
        sim = min(sim, 0.9999)
    return {
        "lines_added":      added,
        "lines_deleted":    deleted,
        "lines_common":     common,
        "similarity_ratio": sim,
        "change_pct":       round((added + deleted) / total * 100, 2),
    }


def unified_diff_text(lines_a: List[str], lines_b: List[str],
                      label_a: str = "Report A",
                      label_b: str = "Report B",
                      context: int = 3) -> str:
    """Return a unified diff string (pandas LCS engine; scales to any file size)."""
    opcodes = diff_opcodes(lines_a, lines_b)
    if not opcodes or all(op[0] == 'equal' for op in opcodes):
        return ""

    out = [f"--- {label_a}", f"+++ {label_b}"]
    for group in _grouped_opcodes(opcodes, n=context):
        i1, i2 = group[0][1], group[-1][2]
        j1, j2 = group[0][3], group[-1][4]
        out.append(f"@@ -{i1 + 1},{i2 - i1} +{j1 + 1},{j2 - j1} @@")
        for tag, i1, i2, j1, j2 in group:
            if tag == 'equal':
                out.extend(' ' + ln for ln in lines_a[i1:i2])
            else:
                if tag in ('replace', 'delete'):
                    out.extend('-' + ln for ln in lines_a[i1:i2])
                if tag in ('replace', 'insert'):
                    out.extend('+' + ln for ln in lines_b[j1:j2])
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 6.  SEMANTIC SIMILARITY (optional — requires scikit-learn)
# ---------------------------------------------------------------------------

def semantic_similarity(text_a: str, text_b: str) -> Optional[float]:
    """
    Compute TF-IDF cosine similarity between two text blocks.
    Returns a float in [0, 1], or None if scikit-learn is not installed.
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity as cos_sim
        import numpy as np  # noqa: F401
    except ImportError:
        return None

    if not text_a.strip() or not text_b.strip():
        return 0.0

    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2))
    try:
        tfidf = vec.fit_transform([text_a, text_b])
        score = cos_sim(tfidf[0:1], tfidf[1:2])[0][0]
        return round(float(score), 4)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# 7.  SECTION-AWARE COMPARISON
# ---------------------------------------------------------------------------

def _idf_weights(token_sets: List[Set[str]]) -> Dict[str, float]:
    N = len(token_sets)
    if N == 0:
        return {}
    df: Dict[str, int] = {}
    for ts in token_sets:
        for tok in ts:
            df[tok] = df.get(tok, 0) + 1
    # Smoothed IDF: high-frequency tokens (e.g. "SETTLEMENT" in every heading)
    # get near-zero weight; rare discriminating tokens get high weight.
    return {tok: math.log((1 + N) / (1 + cnt)) + 1.0 for tok, cnt in df.items()}


def _title_cosine_candidates(
    tokens_a: Set[str],
    idf: Dict[str, float],
    token_index: Dict[str, List[int]],
    b_norms: List[float],
    used_b: Set[int],
) -> List[Tuple[float, int]]:
    dot: Dict[int, float] = {}
    w_a_sq = 0.0
    for tok in tokens_a:
        w = idf.get(tok, 1.0)
        w_a_sq += w * w
        for j in token_index.get(tok, []):
            if j not in used_b:
                dot[j] = dot.get(j, 0.0) + w * w
    if not dot:
        return []
    norm_a = math.sqrt(w_a_sq)
    out = []
    for j, dv in dot.items():
        nb = b_norms[j]
        if nb > 0 and norm_a > 0:
            out.append((dv / (norm_a * nb), j))
    out.sort(reverse=True)
    return out[:_TITLE_TOP_K]


def match_sections(sections_a: List[Section],
                   sections_b: List[Section],
                   threshold: float = 0.6) -> List[Tuple[Optional[Section], Optional[Section]]]:
    """
    Match sections between two reports by title similarity.

    Two-phase pipeline:
      1. IDF-weighted cosine pre-filter narrows each A-title to at most
         _TITLE_TOP_K B candidates. High-frequency shared tokens (e.g.
         "SETTLEMENT") get near-zero IDF weight so they do not inflate
         candidate sets — fixing the O(A*B) DP blowup when many headings
         share a common prefix.
      2. Exact edit-distance (_edit_ratio) runs only on those top-K candidates.

    Returns a list of (section_a, section_b) pairs (None where unmatched).
    """
    if not sections_a and not sections_b:
        return []

    titles_a = [s.title[:_MAX_TITLE_CMP_LEN] for s in sections_a]
    titles_b = [s.title[:_MAX_TITLE_CMP_LEN] for s in sections_b]

    normed_a = [_normalize_title(t) for t in titles_a]
    normed_b = [_normalize_title(t) for t in titles_b]

    token_sets_a = [_title_tokens(t) for t in normed_a]
    token_sets_b = [_title_tokens(t) for t in normed_b]

    idf = _idf_weights(token_sets_a + token_sets_b)

    token_index: Dict[str, List[int]] = {}
    for j, ts in enumerate(token_sets_b):
        for tok in ts:
            token_index.setdefault(tok, []).append(j)

    b_norms: List[float] = [
        math.sqrt(sum(idf.get(tok, 1.0) ** 2 for tok in ts))
        for ts in token_sets_b
    ]

    matched: List[Tuple[Optional[Section], Optional[Section]]] = []
    used_b: Set[int] = set()

    for i, sec_a in enumerate(sections_a):
        # Phase 1: cosine pre-filter
        cosine_top = _title_cosine_candidates(
            token_sets_a[i], idf, token_index, b_norms, used_b
        )
        candidates = [j for _, j in cosine_top]

        if not candidates:
            # Fallback: rank all unused B by title-length ratio (O(B), no DP)
            len_a = max(len(normed_a[i]), 1)
            fallback = sorted(
                (
                    (min(len_a, max(len(normed_b[j]), 1)) /
                     max(len_a, max(len(normed_b[j]), 1)), j)
                    for j in range(len(sections_b)) if j not in used_b
                ),
                reverse=True,
            )[:_TITLE_TOP_K]
            candidates = [j for _, j in fallback]

        # Phase 2: exact edit-distance on top-K only
        best_ratio = 0.0
        best_j = -1
        for j in candidates:
            ratio = _string_similarity(titles_a[i], titles_b[j])
            if ratio > best_ratio:
                best_ratio = ratio
                best_j = j

        if best_ratio >= threshold and best_j >= 0:
            matched.append((sec_a, sections_b[best_j]))
            used_b.add(best_j)
        else:
            matched.append((sec_a, None))

    for j, sec_b in enumerate(sections_b):
        if j not in used_b:
            matched.append((None, sec_b))

    return matched


def compare_sections(sections_a: List[Section],
                     sections_b: List[Section],
                     use_semantic: bool = False) -> List[Dict]:
    """Compare sections pairwise and return per-section results."""
    pairs = match_sections(sections_a, sections_b)
    results = []

    for sec_a, sec_b in pairs:
        entry: Dict = {}

        if sec_a and sec_b:
            entry["status"] = "matched"
            entry["title_a"] = sec_a.title
            entry["title_b"] = sec_b.title
            entry["title_changed"] = sec_a.title.strip() != sec_b.title.strip()
            # Filter out empty lines for fairer comparison; track raw file positions in parallel
            a_nums = sec_a.line_numbers or [0] * len(sec_a.lines)
            b_nums = sec_b.line_numbers or [0] * len(sec_b.lines)
            lines_a_pairs = [(l, n) for l, n in zip(sec_a.lines, a_nums) if l.strip()]
            lines_b_pairs = [(l, n) for l, n in zip(sec_b.lines, b_nums) if l.strip()]
            lines_a = [l for l, _ in lines_a_pairs]
            lines_b = [l for l, _ in lines_b_pairs]
            entry["lines_a"] = lines_a
            entry["lines_b"] = lines_b
            entry["line_numbers_a"] = [n for _, n in lines_a_pairs]
            entry["line_numbers_b"] = [n for _, n in lines_b_pairs]
            diff = line_diff_stats(lines_a, lines_b)
            entry["diff"] = diff
            if use_semantic:
                entry["semantic_similarity"] = semantic_similarity(
                    sec_a.text, sec_b.text
                )
        elif sec_a:
            entry["status"] = "removed"
            entry["title_a"] = sec_a.title
            entry["lines"] = len(sec_a.lines)
            entry["lines_filtered"] = len([l for l in sec_a.lines if l.strip()])
        else:
            entry["status"] = "added"
            entry["title_b"] = sec_b.title
            entry["lines"] = len(sec_b.lines)
            entry["lines_filtered"] = len([l for l in sec_b.lines if l.strip()])

        results.append(entry)

    return results


# ---------------------------------------------------------------------------
# 7b. TRANSACTION DETECTION  (enabled by --transactions)
# ---------------------------------------------------------------------------

from datetime import datetime as _dt

_SORT_DT_FORMATS = (
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d", "%Y/%m/%d",
    "%m/%d/%Y", "%d/%m/%Y",
    "%d-%b-%Y", "%b %d, %Y", "%B %d, %Y",
)


def _parse_sort_val(raw: str) -> object:
    """Parse a sort-key string → datetime | float | str for ordering."""
    s = raw.strip()
    for fmt in _SORT_DT_FORMATS:
        try:
            return _dt.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return s


def _sort_key_comparable(val: object) -> tuple:
    """Return a (type_rank, value) tuple so datetimes, floats, and strings can be sorted together."""
    if isinstance(val, _dt):
        return (0, val.timestamp())
    if isinstance(val, float):
        return (1, val)
    return (2, str(val))


def _is_dash_separator(line: str, threshold: float = 0.5) -> bool:
    """True when most non-whitespace characters in the line are dashes (e.g. '---...---' rule lines)."""
    s = line.strip()
    if not s:
        return False
    dash_chars = sum(1 for c in s if c in '-–—')
    return dash_chars / len(s) >= threshold


def identify_transactions(
    lines: List[str],
    line_numbers: List[int],
    sort_re: Optional[re.Pattern] = None,
    max_txn_lines: Optional[int] = None,
) -> List[Transaction]:
    """Identify transaction blocks within a body line list.

    With sort_re (two-pass): every line that matches sort_re unconditionally
    starts a new transaction, even when the captured value repeats.  Each
    transaction block runs from its match line up to (but not including) the
    next match line, capped at max_txn_lines if set; leading/trailing blank
    lines are stripped.  Lines before the first match are not assigned to any
    transaction.

    Without sort_re (date-anchor mode): lines containing date/time patterns
    are anchors; blank lines close the current block.  Non-date lines before
    the first anchor in a block are absorbed into that block.

    Returns [] when no transactions are detected.
    """
    if not lines:
        return []

    nums = line_numbers if line_numbers else list(range(1, len(lines) + 1))
    txns: List[Transaction] = []

    if sort_re is not None:
        # Two-pass: collect match positions first so each match unconditionally
        # starts a new transaction — same-value duplicates (e.g. two card
        # transactions sharing a sequence number) are kept separate.
        match_idx: List[int] = []
        for i, ln in enumerate(lines):
            if sort_re.search(ln):
                match_idx.append(i)

        for k, mi in enumerate(match_idx):
            end = match_idx[k + 1] if k + 1 < len(match_idx) else len(lines)
            if max_txn_lines is not None:
                end = min(end, mi + max_txn_lines)
            m   = sort_re.search(lines[mi])
            val = m.group(1) if m.lastindex else m.group(0)
            blk_l = lines[mi:end]
            blk_n = nums[mi:end]
            s, e = 0, len(blk_l)
            while s < e and not blk_l[s].strip():
                s += 1
            while e > s and not blk_l[e - 1].strip():
                e -= 1
            blk = blk_l[s:e]
            if blk:
                txns.append(Transaction(
                    sort_key=val,
                    sort_val=_parse_sort_val(val),
                    lines=blk,
                    line_numbers=blk_n[s:e],
                ))

    else:
        # Date-anchor mode: has_dates() marks anchor lines; blank lines close blocks.
        # pend_* accumulates non-date lines before the first date in a block so they
        # are absorbed into that block (e.g. "INVOICE NO: INV-001" before "DATE: ...").
        cur_lines: List[str] = []
        cur_nums: List[int] = []
        pend_lines: List[str] = []
        pend_nums: List[int] = []
        anchored = False

        def _flush_date() -> None:
            nonlocal anchored
            block = pend_lines + cur_lines
            bnums = pend_nums + cur_nums
            s, e = 0, len(block)
            while s < e and not block[s].strip():
                s += 1
            while e > s and not block[e - 1].strip():
                e -= 1
            blk = block[s:e]
            if blk:
                key = first_date_match(next((l for l in blk if has_dates(l)), ""))
                txns.append(Transaction(
                    sort_key=key,
                    sort_val=_parse_sort_val(key) if key else "",
                    lines=blk,
                    line_numbers=bnums[s:e],
                ))
            anchored = False

        for ln, num in zip(lines, nums):
            if not ln.strip():
                if anchored:
                    _flush_date()
                    cur_lines, cur_nums = [], []
                pend_lines, pend_nums = [], []
            elif has_dates(ln):
                if not anchored:
                    anchored = True
                    cur_lines = [ln]
                    cur_nums = [num]
                else:
                    cur_lines.append(ln)
                    cur_nums.append(num)
            else:
                if anchored:
                    cur_lines.append(ln)
                    cur_nums.append(num)
                else:
                    pend_lines.append(ln)
                    pend_nums.append(num)

        if anchored and cur_lines:
            _flush_date()

    return txns


def compare_transactions(txns_a: List[Transaction],
                         txns_b: List[Transaction]) -> List[Dict]:
    """Match transactions by sort_key and return matched/added/removed dicts.

    Transactions with the same sort_key are paired by index within the group
    (first-in-A with first-in-B, etc.) to handle duplicate keys.
    """
    from collections import defaultdict

    groups_a: Dict[str, List[Transaction]] = defaultdict(list)
    groups_b: Dict[str, List[Transaction]] = defaultdict(list)
    for t in txns_a:
        groups_a[t.sort_key].append(t)
    for t in txns_b:
        groups_b[t.sort_key].append(t)

    all_keys = sorted(
        set(groups_a) | set(groups_b),
        key=lambda k: _sort_key_comparable(_parse_sort_val(k)),
    )

    results: List[Dict] = []
    for key in all_keys:
        as_ = groups_a.get(key, [])
        bs_ = groups_b.get(key, [])
        for i in range(max(len(as_), len(bs_))):
            if i < len(as_) and i < len(bs_):
                results.append({
                    "status":   "matched",
                    "sort_key": key,
                    "txn_a":    as_[i],
                    "txn_b":    bs_[i],
                    "diff":     line_diff_stats(as_[i].lines, bs_[i].lines),
                })
            elif i < len(as_):
                results.append({"status": "removed", "sort_key": key, "txn_a": as_[i]})
            else:
                results.append({"status": "added",   "sort_key": key, "txn_b": bs_[i]})
    return results


def write_txn_csv(file_txn_comparisons: List[Dict], csv_path: Path) -> None:
    """Write transaction comparison results to a CSV file."""
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sort_key", "status", "similarity_pct", "lines_added", "lines_deleted"])
        for tc in file_txn_comparisons:
            key    = tc["sort_key"]
            status = tc["status"]
            if status == "matched":
                d = tc["diff"]
                w.writerow([key, status,
                             round(d["similarity_ratio"] * 100, 1),
                             d["lines_added"], d["lines_deleted"]])
            elif status == "added":
                w.writerow([key, status, 0, len(tc["txn_b"].lines), 0])
            else:
                w.writerow([key, status, 0, 0, len(tc["txn_a"].lines)])


def extract_txn_csv_for_file(path: Path,
                             split_rules: Optional[List[SplitRule]],
                             ignore_dates: bool,
                             ignore_line_patterns: Optional[List[str]],
                             out_dir: Path,
                             prefix: str = "") -> Optional[Path]:
    """Extract every transaction in *path* to a standalone CSV (--extract-txn).

    Only runs when a --split-config rule matching *path* defines sort_pattern;
    returns None otherwise (or when no transactions are found). Rows are
    ordered by sort key, one row per raw line: (sort_key, line).
    """
    sort_re = find_sort_pattern(str(path), split_rules)
    if sort_re is None:
        return None

    split_pat  = find_split_pattern(str(path), split_rules)
    max_txn_ln = find_max_txn_lines(str(path), split_rules)
    report = parse_report(str(path), ignore_dates=ignore_dates,
                          ignore_line_patterns=ignore_line_patterns, split_pattern=split_pat)
    pairs = [(l, n) for l, n in zip(report.body_lines, report.body_line_numbers) if l.strip()]
    body = [l for l, _ in pairs]
    nums = [n for _, n in pairs]

    txns = identify_transactions(body, nums, sort_re, max_txn_ln)
    if not txns:
        return None
    txns.sort(key=lambda t: _sort_key_comparable(t.sort_val))

    stem = f"{prefix}_{path.stem}" if prefix else path.stem
    out_path = out_dir / f"{stem}_txn_extract.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for t in txns:
            for line in t.lines:
                w.writerow([t.sort_key, line])
    return out_path


# ---------------------------------------------------------------------------
# 8.  TOP-LEVEL COMPARISON
# ---------------------------------------------------------------------------

def compare_reports(report_a: ParsedReport,
                    report_b: ParsedReport,
                    use_semantic: bool = False) -> ComparisonResult:
    """Run all comparison dimensions and return a ComparisonResult."""

    # -- Structural --
    structural = {
        "page_count_a": len(report_a.pages),
        "page_count_b": len(report_b.pages),
        "page_count_delta": len(report_b.pages) - len(report_a.pages),
        "section_count_a": len(report_a.sections),
        "section_count_b": len(report_b.sections),
        "section_count_delta": len(report_b.sections) - len(report_a.sections),
        "body_line_count_a": len(report_a.body_lines),
        "body_line_count_b": len(report_b.body_lines),
    }

    # -- Metadata (header/footer) --
    hdr_a = set(report_a.global_header)
    hdr_b = set(report_b.global_header)
    ftr_a = set(report_a.global_footer)
    ftr_b = set(report_b.global_footer)

    metadata = {
        "headers_only_in_a": sorted(hdr_a - hdr_b),
        "headers_only_in_b": sorted(hdr_b - hdr_a),
        "headers_common": sorted(hdr_a & hdr_b),
        "footers_only_in_a": sorted(ftr_a - ftr_b),
        "footers_only_in_b": sorted(ftr_b - ftr_a),
        "footers_common": sorted(ftr_a & ftr_b),
    }

    # -- Content (whole-body diff) --
    # Filter out empty lines that may result from date removal for fairer comparison
    body_a = [line for line in report_a.body_lines if line.strip()]
    body_b = [line for line in report_b.body_lines if line.strip()]
    
    content = line_diff_stats(body_a, body_b)
    if use_semantic:
        content["semantic_similarity"] = semantic_similarity(
            "\n".join(body_a),
            "\n".join(body_b)
        )

    # -- Section-level --
    section_results = compare_sections(
        report_a.sections, report_b.sections, use_semantic=use_semantic
    )

    # -- Summary --
    matched  = sum(1 for s in section_results if s["status"] == "matched")
    added    = sum(1 for s in section_results if s["status"] == "added")
    removed  = sum(1 for s in section_results if s["status"] == "removed")
    changed  = sum(
        1 for s in section_results
        if s["status"] == "matched" and s["diff"]["similarity_ratio"] < 1.0
    )
    identical = sum(
        1 for s in section_results
        if s["status"] == "matched" and s["diff"]["similarity_ratio"] == 1.0
    )

    summary = {
        "overall_similarity_ratio": content["similarity_ratio"],
        "overall_change_pct": content["change_pct"],
        "sections_matched": matched,
        "sections_added": added,
        "sections_removed": removed,
        "sections_with_changes": changed,
        "sections_identical": identical,
        "verdict": _verdict(content["similarity_ratio"]),
    }

    # -- Per-page comparison (only when at least one report has multiple pages) --
    page_comparisons: List[Dict] = []
    pages_a = report_a.pages
    pages_b = report_b.pages

    if len(pages_a) > 1 or len(pages_b) > 1:
        n_matched = min(len(pages_a), len(pages_b))
        for i in range(n_matched):
            pg_body_a = [ln for ln in pages_a[i].body_lines if ln.strip()]
            pg_body_b = [ln for ln in pages_b[i].body_lines if ln.strip()]
            pg_diff = line_diff_stats(pg_body_a, pg_body_b)
            pg_diff_text = unified_diff_text(
                pg_body_a, pg_body_b,
                label_a=f"A  page {i + 1}",
                label_b=f"B  page {i + 1}",
            )
            page_comparisons.append({
                "page_num_a": i + 1,
                "page_num_b": i + 1,
                "status": "matched",
                "diff": pg_diff,
                "diff_text": pg_diff_text,
            })
        for i in range(n_matched, len(pages_a)):
            page_comparisons.append({
                "page_num_a": i + 1,
                "page_num_b": None,
                "status": "removed",
                "lines": len(pages_a[i].body_lines),
            })
        for i in range(n_matched, len(pages_b)):
            page_comparisons.append({
                "page_num_a": None,
                "page_num_b": i + 1,
                "status": "added",
                "lines": len(pages_b[i].body_lines),
            })

    return ComparisonResult(
        structural=structural,
        metadata=metadata,
        content=content,
        sections=section_results,
        summary=summary,
        page_comparisons=page_comparisons,
    )


def _verdict(ratio: float) -> str:
    if ratio >= 0.98:
        return "IDENTICAL or near-identical"
    elif ratio >= 0.85:
        return "MINOR differences"
    elif ratio >= 0.60:
        return "MODERATE differences"
    else:
        return "SIGNIFICANT differences"


# ---------------------------------------------------------------------------
# 9.  OUTPUT FORMATTER
# ---------------------------------------------------------------------------

def format_report(result: ComparisonResult,
                  report_a: ParsedReport,
                  report_b: ParsedReport,
                  include_diff: bool = True,
                  txn_comparisons: Optional[List[Dict]] = None) -> str:
    lines = []

    def h(title: str, char: str = "="):
        lines.append("")
        lines.append(char * 60)
        lines.append(f"  {title}")
        lines.append(char * 60)

    def kv(k: str, v):
        lines.append(f"  {k:<35} {v}")

    # Header
    lines.append("=" * 60)
    lines.append("  REPORT COMPARISON SUMMARY")
    lines.append("=" * 60)
    lines.append(f"  Report A : {report_a.filename}")
    lines.append(f"  Report B : {report_b.filename}")
    lines.append("")

    # Summary
    h("OVERALL SUMMARY")
    kv("Verdict:", result.summary["verdict"])
    kv("Overall similarity:", f"{result.summary['overall_similarity_ratio'] * 100:.1f}%")
    kv("Overall change:", f"{result.summary['overall_change_pct']}% of lines affected")

    # Structural
    h("STRUCTURAL COMPARISON", "-")
    s = result.structural
    kv("Pages (A / B):", f"{s['page_count_a']} / {s['page_count_b']}  (delta: {s['page_count_delta']:+d})")
    kv("Sections (A / B):", f"{s['section_count_a']} / {s['section_count_b']}  (delta: {s['section_count_delta']:+d})")
    kv("Body lines (A / B):", f"{s['body_line_count_a']} / {s['body_line_count_b']}")

    # Metadata
    h("HEADER / FOOTER COMPARISON", "-")
    m = result.metadata
    if m["headers_common"]:
        lines.append(f"  Common headers   : {', '.join(m['headers_common'])}")
    if m["headers_only_in_a"]:
        lines.append(f"  Headers only in A: {', '.join(m['headers_only_in_a'])}")
    if m["headers_only_in_b"]:
        lines.append(f"  Headers only in B: {', '.join(m['headers_only_in_b'])}")
    if m["footers_common"]:
        lines.append(f"  Common footers   : {', '.join(m['footers_common'])}")
    if m["footers_only_in_a"]:
        lines.append(f"  Footers only in A: {', '.join(m['footers_only_in_a'])}")
    if m["footers_only_in_b"]:
        lines.append(f"  Footers only in B: {', '.join(m['footers_only_in_b'])}")
    if not any(m.values()):
        lines.append("  No persistent headers/footers detected.")

    # Content diff stats
    h("CONTENT DIFF STATISTICS", "-")
    c = result.content
    kv("Lines added:", c["lines_added"])
    kv("Lines deleted:", c["lines_deleted"])
    kv("Lines common:", c["lines_common"])
    kv("Similarity ratio:", c["similarity_ratio"])
    if "semantic_similarity" in c and c["semantic_similarity"] is not None:
        kv("Semantic similarity (TF-IDF):", c["semantic_similarity"])

    # Section comparison
    h("SECTION-BY-SECTION COMPARISON", "-")
    sm = result.summary
    kv("Sections matched:", sm["sections_matched"])
    kv("Sections added (only in B):", sm["sections_added"])
    kv("Sections removed (only in A):", sm["sections_removed"])
    kv("Matched sections with changes:", sm["sections_with_changes"])
    kv("Matched sections identical:", sm["sections_identical"])

    lines.append("")
    for sec in result.sections:
        status = sec["status"].upper()
        if sec["status"] == "matched":
            title = sec["title_a"]
            ratio = sec["diff"]["similarity_ratio"]
            flag = "" if ratio == 1.0 else f"  [similarity: {ratio * 100:.1f}%]"
            lines.append(f"  [MATCHED ] {title}{flag}")
            if sec.get("title_changed"):
                lines.append(f"           Title changed → {sec['title_b']}")
            d = sec["diff"]
            if d["lines_added"] or d["lines_deleted"]:
                lines.append(
                    f"           +{d['lines_added']} added  "
                    f"-{d['lines_deleted']} deleted"
                )
            if "semantic_similarity" in sec and sec["semantic_similarity"] is not None:
                lines.append(f"           Semantic similarity: {sec['semantic_similarity']}")
        elif sec["status"] == "added":
            lines.append(f"  [ADDED   ] {sec['title_b']}  ({sec['lines']} lines)")
        else:
            lines.append(f"  [REMOVED ] {sec['title_a']}  ({sec['lines']} lines)")

    # Transaction comparison (file-level, whole body)
    if txn_comparisons is not None:
        h("TRANSACTION COMPARISON", "-")
        if txn_comparisons:
            n_m = sum(1 for t in txn_comparisons if t["status"] == "matched")
            n_a = sum(1 for t in txn_comparisons if t["status"] == "added")
            n_r = sum(1 for t in txn_comparisons if t["status"] == "removed")
            lines.append(f"  Transactions: {n_m} matched, {n_a} added, {n_r} removed")
            lines.append("")
            for tc in txn_comparisons:
                key = tc["sort_key"] or "(no key)"
                if tc["status"] == "matched":
                    r2 = tc["diff"]["similarity_ratio"]
                    flag = f"  similarity: {r2:.1%}" if r2 < 1.0 else "  identical"
                    chg = (f"  +{tc['diff']['lines_added']}/-{tc['diff']['lines_deleted']}"
                           if r2 < 1.0 else "")
                    lines.append(f"  [MATCHED ] {key}{flag}{chg}")
                elif tc["status"] == "added":
                    lines.append(f"  [ADDED   ] {key}")
                else:
                    lines.append(f"  [REMOVED ] {key}")
        else:
            lines.append("  (no transactions detected)")

    # Diff section: per-page when available, otherwise whole-body
    if include_diff:
        if result.page_comparisons:
            h("PER-PAGE DIFF", "-")
            for pc in result.page_comparisons:
                if pc["status"] == "matched":
                    lines.append("")
                    lines.append(f"  {'─' * 56}")
                    lines.append(f"  Page {pc['page_num_a']}  (A) vs  Page {pc['page_num_b']}  (B)")
                    d = pc["diff"]
                    lines.append(
                        f"  Similarity: {d['similarity_ratio']:.1%}   "
                        f"Change: {d['change_pct']:.1f}%   "
                        f"+{d['lines_added']} / -{d['lines_deleted']} lines"
                    )
                    if pc["diff_text"].strip():
                        lines.append(pc["diff_text"])
                    else:
                        lines.append("  (identical)")
                elif pc["status"] == "added":
                    lines.append("")
                    lines.append(f"  [ADDED  ] Page {pc['page_num_b']} (only in B, {pc['lines']} lines)")
                else:
                    lines.append(f"  [REMOVED] Page {pc['page_num_a']} (only in A, {pc['lines']} lines)")
        else:
            h("UNIFIED DIFF (body content)", "-")
            diff_text = unified_diff_text(
                report_a.body_lines,
                report_b.body_lines,
                label_a=report_a.filename,
                label_b=report_b.filename,
            )
            if diff_text.strip():
                lines.append(diff_text)
            else:
                lines.append("  No differences in body content.")

    lines.append("")
    lines.append("=" * 60)
    lines.append("  END OF COMPARISON REPORT")
    lines.append("=" * 60)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 10.  FOLDER SCANNER
# ---------------------------------------------------------------------------

def scan_folder(folder: Path, ext: str = ".txt") -> Dict[str, Path]:
    """
    Return a dict of { filename (lowercase) → absolute Path } for all files
    with the given extension directly inside *folder* (non-recursive).
    """
    if not folder.is_dir():
        print(f"ERROR: Not a directory: {folder}", file=sys.stderr)
        sys.exit(1)
    return {
        p.name.lower(): p
        for p in sorted(folder.iterdir())
        if p.is_file() and p.suffix.lower() == ext.lower()
    }


# ---------------------------------------------------------------------------
# 11.  FILENAME MATCHER  (exact + fuzzy)
# ---------------------------------------------------------------------------

@dataclass
class FolderMatchResult:
    """Encapsulates the file-level matching outcome for two folders."""
    exact_pairs:   List[Tuple[Path, Path]]          # same filename in both
    fuzzy_pairs:   List[Tuple[Path, Path, float]]   # similar name, (file_a, file_b, ratio)
    only_in_a:     List[Path]                        # no counterpart in B
    only_in_b:     List[Path]                        # no counterpart in A


def match_filenames(files_a: Dict[str, Path],
                    files_b: Dict[str, Path],
                    fuzzy: bool = False,
                    fuzzy_threshold: float = 0.70) -> FolderMatchResult:
    """
    Match files between two folders.

    Strategy:
      1. Exact match  — identical filename (case-insensitive).
      2. Fuzzy match  — if --fuzzy-match is set, pair unmatched files whose
                        names are similar enough (custom normalized string
                        similarity ≥ threshold). Each file is paired at most
                        once (greedy best-first).
      3. Unmatched    — remainder go into only_in_a / only_in_b.
    """
    keys_a: Set[str] = set(files_a.keys())
    keys_b: Set[str] = set(files_b.keys())

    # 1. Exact matches
    exact_keys = keys_a & keys_b
    exact_pairs = [(files_a[k], files_b[k]) for k in sorted(exact_keys)]

    unmatched_a = sorted(keys_a - exact_keys)
    unmatched_b = sorted(keys_b - exact_keys)

    fuzzy_pairs: List[Tuple[Path, Path, float]] = []

    # 2. Fuzzy matches
    if fuzzy and unmatched_a and unmatched_b:
        # Build all candidate ratios and keep only those above threshold.
        candidates: List[Tuple[float, str, str]] = []
        for ka in unmatched_a:
            for kb in unmatched_b:
                ratio = _string_similarity(ka, kb)
                if ratio >= fuzzy_threshold:
                    candidates.append((ratio, ka, kb))

        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)

            used_a: Set[str] = set()
            used_b: Set[str] = set()

            for ratio, ka, kb in candidates:
                if ka in used_a or kb in used_b:
                    continue
                fuzzy_pairs.append((files_a[ka], files_b[kb], round(ratio, 4)))
                used_a.add(ka)
                used_b.add(kb)

            unmatched_a = [k for k in unmatched_a if k not in used_a]
            unmatched_b = [k for k in unmatched_b if k not in used_b]

    return FolderMatchResult(
        exact_pairs=exact_pairs,
        fuzzy_pairs=fuzzy_pairs,
        only_in_a=[files_a[k] for k in unmatched_a],
        only_in_b=[files_b[k] for k in unmatched_b],
    )


# ---------------------------------------------------------------------------
# 12.  FOLDER-LEVEL COMPARISON RUNNER
# ---------------------------------------------------------------------------

@dataclass
class FilePairOutcome:
    file_a: Path
    file_b: Path
    match_type: str           # "exact" | "fuzzy"
    fuzzy_ratio: float        # name-similarity ratio (1.0 for exact)
    result: Optional[ComparisonResult]
    error: Optional[str] = None
    body_lines_a: List[str] = field(default_factory=list)
    body_lines_b: List[str] = field(default_factory=list)
    body_line_numbers_a: List[int] = field(default_factory=list)
    body_line_numbers_b: List[int] = field(default_factory=list)
    per_page_lines: List[Tuple[List[str], List[str]]] = field(default_factory=list)
    per_page_line_numbers: List[Tuple[List[int], List[int]]] = field(default_factory=list)
    # file-level transaction comparisons (whole body, not per-page)
    file_txn_comparisons: List[Dict] = field(default_factory=list)


def compare_folder_pair(path_a: Path,
                        path_b: Path,
                        match_type: str,
                        fuzzy_ratio: float,
                        use_semantic: bool,
                        ignore_dates: bool = False,
                        ignore_line_patterns: Optional[List[str]] = None,
                        split_rules: Optional[List[SplitRule]] = None,
                        transactions: bool = False) -> FilePairOutcome:
    """Parse and compare one pair of files. Catches errors gracefully."""
    try:
        split_pat = find_split_pattern(str(path_a), split_rules)
        ra = parse_report(str(path_a), ignore_dates=ignore_dates, ignore_line_patterns=ignore_line_patterns, split_pattern=split_pat)
        rb = parse_report(str(path_b), ignore_dates=ignore_dates, ignore_line_patterns=ignore_line_patterns, split_pattern=split_pat)

        def _filter(lines: List[str], nums: List[int]):
            pairs = [(l, n) for l, n in zip(lines, nums) if l.strip()]
            return [l for l, _ in pairs], [n for _, n in pairs]

        body_a, nums_a = _filter(ra.body_lines, ra.body_line_numbers)
        body_b, nums_b = _filter(rb.body_lines, rb.body_line_numbers)
        n_matched = min(len(ra.pages), len(rb.pages))
        per_page, per_page_nums = [], []
        for i in range(n_matched):
            pla, pna = _filter(ra.pages[i].body_lines, ra.pages[i].body_line_numbers)
            plb, pnb = _filter(rb.pages[i].body_lines, rb.pages[i].body_line_numbers)
            per_page.append((pla, plb))
            per_page_nums.append((pna, pnb))

        file_txn: List[Dict] = []
        if transactions:
            sort_re    = find_sort_pattern(str(path_a), split_rules)
            max_txn_ln = find_max_txn_lines(str(path_a), split_rules)
            if sort_re is not None:
                txns_a = identify_transactions(body_a, nums_a, sort_re, max_txn_ln)
                txns_b = identify_transactions(body_b, nums_b, sort_re, max_txn_ln)
                if txns_a or txns_b:
                    file_txn = compare_transactions(txns_a, txns_b)

        result = compare_reports(ra, rb, use_semantic=use_semantic)
        return FilePairOutcome(path_a, path_b, match_type, fuzzy_ratio, result,
                               body_lines_a=body_a, body_lines_b=body_b,
                               body_line_numbers_a=nums_a, body_line_numbers_b=nums_b,
                               per_page_lines=per_page, per_page_line_numbers=per_page_nums,
                               file_txn_comparisons=file_txn)
    except Exception as exc:
        return FilePairOutcome(path_a, path_b, match_type, fuzzy_ratio,
                               result=None, error=str(exc))


def run_folder_comparison(folder_a: Path,
                           folder_b: Path,
                           ext: str = ".txt",
                           fuzzy: bool = False,
                           fuzzy_threshold: float = 0.70,
                           use_semantic: bool = False,
                           output_dir: Optional[Path] = None,
                           include_diff: bool = True,
                           ignore_dates: bool = False,
                           ignore_line_patterns: Optional[List[str]] = None,
                           split_rules: Optional[List[SplitRule]] = None,
                           transactions: bool = False,
                           extract_txn: bool = False) -> None:
    """
    Top-level folder comparison.  Scans both folders, matches files,
    runs per-file comparisons, writes individual reports, and writes
    a master FOLDER_SUMMARY.txt.
    """
    print(f"\nScanning folder A : {folder_a}", file=sys.stderr)
    files_a = scan_folder(folder_a, ext)
    print(f"Scanning folder B : {folder_b}", file=sys.stderr)
    files_b = scan_folder(folder_b, ext)

    print(f"  Found {len(files_a)} file(s) in A,  {len(files_b)} file(s) in B", file=sys.stderr)

    match = match_filenames(files_a, files_b, fuzzy=fuzzy,
                            fuzzy_threshold=fuzzy_threshold)

    total_pairs = len(match.exact_pairs) + len(match.fuzzy_pairs)
    print(f"  Matched : {len(match.exact_pairs)} exact  +  "
          f"{len(match.fuzzy_pairs)} fuzzy  =  {total_pairs} pairs", file=sys.stderr)
    print(f"  Unmatched : {len(match.only_in_a)} only-in-A,  "
          f"{len(match.only_in_b)} only-in-B", file=sys.stderr)

    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    # ---- Extract transactions (--extract-txn, independent of comparison) ----
    if extract_txn:
        if not output_dir:
            print("  --extract-txn requires --output-dir (no destination for extracted CSVs)",
                  file=sys.stderr)
        elif not split_rules:
            print("  --extract-txn requires --split-config (no sort_pattern rules loaded)",
                  file=sys.stderr)
        else:
            print("  Extracting transactions (--extract-txn)...", file=sys.stderr)
            for p in list(files_a.values()):
                out = extract_txn_csv_for_file(p, split_rules, ignore_dates, ignore_line_patterns,
                                               output_dir, prefix="A")
                if out:
                    print(f"    TXN EXTRACT → {out}", file=sys.stderr)
            for p in list(files_b.values()):
                out = extract_txn_csv_for_file(p, split_rules, ignore_dates, ignore_line_patterns,
                                               output_dir, prefix="B")
                if out:
                    print(f"    TXN EXTRACT → {out}", file=sys.stderr)

    # ---- Run comparisons ----
    outcomes: List[FilePairOutcome] = []

    for pa, pb in match.exact_pairs:
        print(f"  Comparing [exact ] {pa.name}", file=sys.stderr)
        outcome = compare_folder_pair(pa, pb, "exact", 1.0, use_semantic, ignore_dates, ignore_line_patterns, split_rules=split_rules, transactions=transactions)
        outcomes.append(outcome)
        _write_pair_report(outcome, output_dir, include_diff, ignore_dates, ignore_line_patterns, split_rules=split_rules, transactions=transactions)

    for pa, pb, ratio in match.fuzzy_pairs:
        print(f"  Comparing [fuzzy ] {pa.name} ↔ {pb.name}  (name similarity {ratio:.0%})",
              file=sys.stderr)
        outcome = compare_folder_pair(pa, pb, "fuzzy", ratio, use_semantic, ignore_dates, ignore_line_patterns, split_rules=split_rules, transactions=transactions)
        outcomes.append(outcome)
        _write_pair_report(outcome, output_dir, include_diff, ignore_dates, ignore_line_patterns, split_rules=split_rules, transactions=transactions)

    # ---- Master summary ----
    summary_text = _format_folder_summary(
        folder_a, folder_b, match, outcomes, ext
    )

    if output_dir:
        summary_path = output_dir / "FOLDER_SUMMARY.txt"
        summary_path.write_text(summary_text, encoding="utf-8")
        print(f"\n  Master summary → {summary_path}", file=sys.stderr)
    else:
        print(summary_text)


def _write_pair_report(outcome: FilePairOutcome,
                       output_dir: Optional[Path],
                       include_diff: bool,
                       ignore_dates: bool = False,
                       ignore_line_patterns: Optional[List[str]] = None,
                       split_rules: Optional[List[SplitRule]] = None,
                       transactions: bool = False) -> None:
    """Write a single pair's comparison report to output_dir (or stdout)."""
    if outcome.error:
        text = (f"ERROR comparing {outcome.file_a.name} ↔ {outcome.file_b.name}\n"
                f"{outcome.error}\n")
    else:
        split_pat = find_split_pattern(str(outcome.file_a), split_rules)
        # Re-parse to get full objects for the formatter
        ra = parse_report(str(outcome.file_a), ignore_dates=ignore_dates, ignore_line_patterns=ignore_line_patterns, split_pattern=split_pat)
        rb = parse_report(str(outcome.file_b), ignore_dates=ignore_dates, ignore_line_patterns=ignore_line_patterns, split_pattern=split_pat)
        txn_comps = outcome.file_txn_comparisons if transactions else None
        text = format_report(outcome.result, ra, rb, include_diff=include_diff,
                             txn_comparisons=txn_comps)

    if output_dir:
        # Use file_a's stem as base; append _vs_<file_b_stem> for fuzzy pairs
        if outcome.match_type == "fuzzy":
            stem = f"{outcome.file_a.stem}_vs_{outcome.file_b.stem}"
        else:
            stem = outcome.file_a.stem
        out_path = output_dir / f"{stem}_comparison.txt"
        out_path.write_text(text, encoding="utf-8")
        if outcome.file_txn_comparisons:
            csv_path = output_dir / f"{stem}_txn.csv"
            write_txn_csv(outcome.file_txn_comparisons, csv_path)
            print(f"    TXN CSV → {csv_path}", file=sys.stderr)
    else:
        print(text)
        print()


# ---------------------------------------------------------------------------
# 13.  FOLDER SUMMARY FORMATTER
# ---------------------------------------------------------------------------

def _format_folder_summary(folder_a: Path,
                            folder_b: Path,
                            match: FolderMatchResult,
                            outcomes: List[FilePairOutcome],
                            ext: str) -> str:
    W = 70
    lines = []

    def h(title: str, char: str = "="):
        lines.append("")
        lines.append(char * W)
        lines.append(f"  {title}")
        lines.append(char * W)

    def kv(k: str, v, w: int = 38):
        lines.append(f"  {k:<{w}} {v}")

    def verdict_icon(v: str) -> str:
        if "IDENTICAL" in v:   return "✓"
        if "MINOR"    in v:    return "~"
        if "MODERATE" in v:    return "!"
        return "✗"

    # Title
    lines.append("=" * W)
    lines.append("  FOLDER COMPARISON — MASTER SUMMARY")
    lines.append("=" * W)
    lines.append(f"  Folder A : {folder_a}")
    lines.append(f"  Folder B : {folder_b}")
    lines.append(f"  File ext : {ext}")

    # Counts
    h("FILE MATCHING OVERVIEW")
    total_a = len(match.exact_pairs) + len(match.fuzzy_pairs) + len(match.only_in_a)
    total_b = len(match.exact_pairs) + len(match.fuzzy_pairs) + len(match.only_in_b)
    kv("Files in folder A:", total_a)
    kv("Files in folder B:", total_b)
    kv("Exactly matched pairs:", len(match.exact_pairs))
    kv("Fuzzy-matched pairs:", len(match.fuzzy_pairs))
    kv("Unmatched — only in A:", len(match.only_in_a))
    kv("Unmatched — only in B:", len(match.only_in_b))

    # Unmatched files detail
    if match.only_in_a:
        h("UNMATCHED FILES — ONLY IN FOLDER A", "-")
        for p in match.only_in_a:
            lines.append(f"  [MISSING IN B]  {p.name}")

    if match.only_in_b:
        h("UNMATCHED FILES — ONLY IN FOLDER B", "-")
        for p in match.only_in_b:
            lines.append(f"  [MISSING IN A]  {p.name}")

    # Fuzzy match log
    if match.fuzzy_pairs:
        h("FUZZY-MATCHED PAIRS (name similarity)", "-")
        lines.append(f"  {'File in A':<30} {'File in B':<30} {'Name sim':>8}")
        lines.append(f"  {'-'*30} {'-'*30} {'-'*8}")
        for pa, pb, ratio in match.fuzzy_pairs:
            lines.append(f"  {pa.name:<30} {pb.name:<30} {ratio:>7.1%}")

    # Per-file results table
    h("PER-FILE COMPARISON RESULTS")
    col = [28, 28, 8, 8, 8]
    hdr = (f"  {'File A':<{col[0]}} {'File B':<{col[1]}} "
           f"{'Similar':>{col[2]}} {'Change%':>{col[3]}} {'Verdict':>{col[4]}}")
    lines.append(hdr)
    lines.append("  " + "-" * (sum(col) + len(col) - 1))

    errors = []
    similarity_scores = []

    for outcome in outcomes:
        fa = outcome.file_a.name
        fb = outcome.file_b.name
        mtype = "" if outcome.match_type == "exact" else f"[fuzzy {outcome.fuzzy_ratio:.0%}] "

        if outcome.error:
            errors.append(outcome)
            lines.append(f"  {(mtype+fa):<{col[0]}} {fb:<{col[1]}} {'ERROR':>{col[2]+col[3]+col[4]+2}}")
            continue

        s = outcome.result.summary
        ratio = s["overall_similarity_ratio"]
        chg   = s["overall_change_pct"]
        verd  = verdict_icon(s["verdict"])
        similarity_scores.append(ratio)

        lines.append(
            f"  {(mtype+fa):<{col[0]}} {fb:<{col[1]}} "
            f"{ratio:>{col[2]}.1%} {chg:>{col[3]}.1f}% {verd:>{col[4]}}"
        )

    # Aggregate stats
    h("AGGREGATE STATISTICS")
    if similarity_scores:
        avg_sim = sum(similarity_scores) / len(similarity_scores)
        min_sim = min(similarity_scores)
        max_sim = max(similarity_scores)
        kv("Files compared successfully:", len(similarity_scores))
        kv("Average similarity:", f"{avg_sim:.1%}")
        kv("Most similar pair:", f"{max_sim:.1%}")
        kv("Least similar pair:", f"{min_sim:.1%}")

        identical  = sum(1 for r in similarity_scores if r >= 0.98)
        minor      = sum(1 for r in similarity_scores if 0.85 <= r < 0.98)
        moderate   = sum(1 for r in similarity_scores if 0.60 <= r < 0.85)
        significant= sum(1 for r in similarity_scores if r < 0.60)

        lines.append("")
        kv("✓  IDENTICAL / near-identical:", identical)
        kv("~  MINOR differences:", minor)
        kv("!  MODERATE differences:", moderate)
        kv("✗  SIGNIFICANT differences:", significant)

    if errors:
        h("ERRORS ENCOUNTERED", "-")
        for o in errors:
            lines.append(f"  {o.file_a.name} ↔ {o.file_b.name}")
            lines.append(f"    {o.error}")

    lines.append("")
    lines.append("=" * W)
    lines.append("  END OF FOLDER SUMMARY")
    lines.append("=" * W)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 14.  CLI ENTRY POINT  (file mode + folder mode)
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Compare text-based reports (single file pair OR two whole folders).\n"
            "Pass two FILE paths for single-file mode.\n"
            "Pass two DIRECTORY paths for folder mode."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source_a",
                        help="Baseline report file  OR  baseline folder")
    parser.add_argument("source_b",
                        help="Comparison report file  OR  comparison folder")

    # ---- shared options ----
    parser.add_argument("--semantic", action="store_true",
                        help="Enable TF-IDF semantic similarity (requires scikit-learn)")
    parser.add_argument("--no-diff", action="store_true",
                        help="Skip unified diff output (recommended for large files)")
    parser.add_argument("--ignore-dates", action="store_true",
                        help="Remove date/time patterns from comparison (ISO, US, timestamps, etc.)")
    parser.add_argument("--ignore-lines", action="append", default=[], metavar="PATTERN",
                        help="Skip lines matching this regex (can be repeated: --ignore-lines PAT1 --ignore-lines PAT2)")
    parser.add_argument("--split-config", metavar="CSV",
                        help="CSV with report_pattern,split_pattern[,sort_pattern] columns; "
                             "matched files use value-change page splitting instead of delimiter patterns")
    parser.add_argument("--transactions", action="store_true",
                        help="Enable transaction-level comparison within pages; "
                             "transactions are identified by date/time anchors or sort_pattern in --split-config")
    parser.add_argument("--extract-txn", action="store_true",
                        help="Extract every transaction from each file whose --split-config rule "
                             "defines sort_pattern into <file>_txn_extract.csv (one CSV per file, "
                             "sorted by sort key, one row per line: sort_key, line). "
                             "Independent of --transactions; requires --split-config and an output "
                             "destination (--output-dir in folder mode, --output in file mode).")

    # ---- single-file options ----
    parser.add_argument("--output", "-o",
                        help="[File mode] Save result to this path (default: stdout)")

    # ---- folder options ----
    parser.add_argument("--output-dir", "-d",
                        help="[Folder mode] Directory for per-file reports + summary "
                             "(default: print to stdout)")
    parser.add_argument("--ext", default=".txt",
                        help="[Folder mode] File extension to include (default: .txt)")
    parser.add_argument("--fuzzy-match", action="store_true",
                        help="[Folder mode] Also pair files with similar (but not identical) names")
    parser.add_argument("--fuzzy-threshold", type=float, default=0.70,
                        help="[Folder mode] Min name-similarity ratio for fuzzy match (default: 0.70)")

    args = parser.parse_args()

    split_rules = load_split_config(args.split_config) if args.split_config else None

    path_a = Path(args.source_a)
    path_b = Path(args.source_b)

    # ------------------------------------------------------------------ FOLDER
    if path_a.is_dir() and path_b.is_dir():
        output_dir = Path(args.output_dir) if args.output_dir else None
        run_folder_comparison(
            folder_a=path_a,
            folder_b=path_b,
            ext=args.ext,
            fuzzy=args.fuzzy_match,
            fuzzy_threshold=args.fuzzy_threshold,
            use_semantic=args.semantic,
            output_dir=output_dir,
            include_diff=not args.no_diff,
            ignore_dates=args.ignore_dates,
            ignore_line_patterns=args.ignore_lines or None,
            split_rules=split_rules,
            transactions=args.transactions,
            extract_txn=args.extract_txn,
        )

    # ------------------------------------------------------------------ FILE
    elif path_a.is_file() and path_b.is_file():
        split_pat  = find_split_pattern(str(path_a), split_rules)
        sort_re    = find_sort_pattern(str(path_a), split_rules)
        max_txn_ln = find_max_txn_lines(str(path_a), split_rules)
        print(f"Parsing  : {path_a}", file=sys.stderr)
        report_a = parse_report(str(path_a), ignore_dates=args.ignore_dates, ignore_line_patterns=args.ignore_lines or None, split_pattern=split_pat)
        print(f"Parsing  : {path_b}", file=sys.stderr)
        report_b = parse_report(str(path_b), ignore_dates=args.ignore_dates, ignore_line_patterns=args.ignore_lines or None, split_pattern=split_pat)

        print("Comparing...", file=sys.stderr)
        result = compare_reports(report_a, report_b, use_semantic=args.semantic)

        txn_comps: Optional[List[Dict]] = None
        if args.transactions and sort_re is not None:
            body_a = [l for l in report_a.body_lines if l.strip()]
            body_b = [l for l in report_b.body_lines if l.strip()]
            txns_a = identify_transactions(body_a, report_a.body_line_numbers, sort_re, max_txn_ln)
            txns_b = identify_transactions(body_b, report_b.body_line_numbers, sort_re, max_txn_ln)
            txn_comps = compare_transactions(txns_a, txns_b) if (txns_a or txns_b) else []

        output = format_report(result, report_a, report_b,
                                include_diff=not args.no_diff,
                                txn_comparisons=txn_comps)

        if args.output:
            out_p = Path(args.output)
            out_p.write_text(output, encoding="utf-8")
            print(f"Saved to : {args.output}", file=sys.stderr)
            if txn_comps:
                csv_path = out_p.parent / f"{out_p.stem}_txn.csv"
                write_txn_csv(txn_comps, csv_path)
                print(f"TXN CSV  → {csv_path}", file=sys.stderr)
            if args.extract_txn and split_rules:
                for p, prefix in ((path_a, "A"), (path_b, "B")):
                    extracted = extract_txn_csv_for_file(
                        p, split_rules, args.ignore_dates, args.ignore_lines or None,
                        out_p.parent, prefix=prefix)
                    if extracted:
                        print(f"TXN EXTRACT → {extracted}", file=sys.stderr)
        else:
            print(output)
            if args.extract_txn:
                print("--extract-txn requires --output in file mode", file=sys.stderr)

    # ------------------------------------------------------------------ ERROR
    else:
        print("ERROR: Both arguments must be files, or both must be directories.",
              file=sys.stderr)
        print(f"  source_a = {path_a}  ({'dir' if path_a.is_dir() else 'file' if path_a.is_file() else 'NOT FOUND'})",
              file=sys.stderr)
        print(f"  source_b = {path_b}  ({'dir' if path_b.is_dir() else 'file' if path_b.is_file() else 'NOT FOUND'})",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
