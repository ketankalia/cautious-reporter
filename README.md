# Reporter — Usage Guide

Reporter compares structured text reports and produces text summaries, CSV exports, and a self-contained HTML report with interactive diff panels.

Two tools are available:

| Tool | Mode | Output |
|------|------|--------|
| `report_comparator.py` | File or Folder | Text summary + CSV |
| `html_reporter.py` | Folder only | Single HTML report + CSV |

---

## 1. Basic Comparison (no split-config)

### Single file pair

```powershell
python report_comparator.py report_a.txt report_b.txt
python report_comparator.py report_a.txt report_b.txt --output diff.txt
```

### Folder comparison (text output)

```powershell
python report_comparator.py folder_a/ folder_b/ --output-dir results/
```

### Folder comparison (HTML output)

```powershell
python html_reporter.py folder_a/ folder_b/ --output results/report.html
```

Without `--split-config`, the tool uses built-in page-break patterns (form-feed, `--- Page N ---`, `PAGE N`, etc.) to split files into pages and sections.

---

## 2. Common Flags

| Flag | Effect |
|------|--------|
| `--semantic` | Add TF-IDF semantic similarity score (requires scikit-learn) |
| `--ignore-dates` | Strip date/time patterns before comparing (ISO, US, timestamps) |
| `--ignore-lines PAT` | Skip lines matching regex PAT (repeatable) |
| `--no-diff` | Skip unified diff output (text mode only) |
| `--fuzzy-match` | Pair files with similar names (folder mode) |
| `--fuzzy-threshold 0.70` | Minimum name similarity for fuzzy pairing (default 0.70) |
| `--ext .txt` | File extension filter (folder mode, default .txt) |

---

## 3. The Split Config CSV (`--split-config`)

The `--split-config` flag points to a CSV file that tells the tool how to parse specific report types. Each row maps a filename pattern to parsing rules. Files that don't match any row fall back to the default built-in page-break detection.

### CSV columns

```csv
report_pattern,split_pattern,sort_pattern,max_txn_lines,separator_pattern
```

| Column | Required | Purpose |
|--------|----------|---------|
| `report_pattern` | Yes | Regex matched against the **filename** (not path). First matching row wins. |
| `split_pattern` | No | Regex applied per line. When the captured value changes, a new page starts. If blank, the built-in page-break patterns are used instead. |
| `sort_pattern` | No | Regex per line to identify transaction boundaries (enables `--transactions`). |
| `max_txn_lines` | No | Max lines per transaction block. Prevents non-transaction content (like totals) from being absorbed into the preceding transaction. Leave blank for unlimited. |
| `separator_pattern` | No | Regex matching full separator lines (e.g. lines of dashes). Enables summary-section extraction between separators. |

### Example splits.csv

```csv
report_pattern,split_pattern,sort_pattern,max_txn_lines,separator_pattern
invoices.*,^INVOICE NO:\s+(\S+),^DATE:\s+(.+),,
FERM.*,PAGE\s+(\d+),FN[A-Z0-9]+\s(\d+),3,"[-]{20,}$"
TOA.*,,,,"\\s*[-]{20,}\\s*$"
```

**Row 1 — Invoice files** (`invoices.*`):
- `split_pattern`: pages split when `INVOICE NO:` value changes (e.g. INV-001 → INV-002)
- `sort_pattern`: transactions within each page start at lines matching `^DATE:`
- `max_txn_lines`: blank → unlimited (each transaction runs until the next `^DATE:` match)
- `separator_pattern`: blank → no summary-section extraction

**Row 2 — Terminal files** (`TERM.*`):
- `split_pattern`: pages split on `PAGE N` lines
- `sort_pattern`: transactions start at lines containing a terminal sequence like `ACCD509B 005000`
- `max_txn_lines`: `3` — each transaction block is capped at 3 lines, so non-transaction content (TERMINAL TOTALS, COUNTS) is not absorbed into the preceding transaction
- `separator_pattern`: `[-]{20,}$` — lines of 20+ dashes act as section boundaries. Content between consecutive dash lines becomes a named summary section (TERMINAL TOTALS, TERMINAL COUNTS, CANISTER LEVEL, etc.)

**Row 3 — Recap files** (`RECAP.*`):
- `split_pattern`: blank → built-in page-break detection runs (splits on `PAGE N` header lines)
- `sort_pattern`: blank → no transaction extraction
- `separator_pattern`: `\s*[-]{20,}\s*$` — same dash lines, but with `\s*` prefix to handle lines indented by leading spaces

**Note on quoting:** If a regex contains a comma (e.g. `{20,}`), wrap it in double quotes in the CSV so the comma isn't treated as a column separator.

### Invalid regex detection

If any regex in splits.csv is syntactically invalid, the tool exits immediately with a clear error before running any comparison:

```
ERROR: splits.csv row 3: invalid regex in 'separator_pattern': '[-{20'
       unterminated character set at position 0
```

The error names the file, row number (1-based, counting the header as row 1), column name, the bad pattern, and the regex engine's description of the problem.

---

## 4. How split_pattern Works (Page Splitting)

The `split_pattern` regex is applied to every line via `.search()`. When a capturing group is present, `group(1)` is the split key; otherwise the whole match is used.

A new page starts when the captured value **changes** from the previous match. Lines where the pattern doesn't match stay in the current page.

```
INVOICE NO: INV-001    ← split key = "INV-001", starts Page 1
  item line ...
  item line ...
INVOICE NO: INV-001    ← same key, stays in Page 1
  item line ...
INVOICE NO: INV-002    ← key changed → starts Page 2
  item line ...
```

Non-unique keys are handled correctly: if INV-001 appears in two separate blocks, they are grouped into one page (the split fires on the value *transition*, not on each occurrence).

Files that match no CSV row, or rows with a blank `split_pattern`, use the built-in delimiter patterns. The built-in patterns are tried **one at a time in priority order** — the first pattern that splits the file into multiple pages is used and no further patterns run. Priority order (highest first):

1. Standalone `PAGE N` line
2. Any line ending with `PAGE N` (e.g. `SOMDATE 06/02/26 ... PAGE 341`)
3. Form-feed character
4. `--- Page N ---` / `=== Page N ===`
5. `Page N of M`
6. Lines of 10+ dashes or underscores
7. Lines of 10+ asterisks

This ordering ensures that separator lines (`---`) are only consumed as page breaks if no higher-priority pattern (like `PAGE N`) fires first — preserving them for section extraction.

---

## 5. How sort_pattern Works (Transaction Detection)

Requires `--transactions` flag on the command line AND a `sort_pattern` in the matching CSV row. Files with no `sort_pattern` are silently skipped for transaction comparison.

The `sort_pattern` regex is searched per line across the **entire file body** (not per-page). Every line that matches unconditionally starts a new transaction block, even when the captured value repeats. Each block runs from its match line to the next match line (exclusive).

```
  ACCD509B 009000    ← sort_pattern matches, starts Transaction 1 (key="009000")
  line 2 ...
  line 3 ...         ← max_txn_lines=3, so block capped here
  ACCD509B 009002    ← starts Transaction 2 (key="009002")
  ...
```

**max_txn_lines** caps each block at N lines from the match. Use this when non-transaction content (SOME TOTALS, COUNTER sections) follows the last transaction without another `sort_pattern` match — without the cap, those lines would be absorbed into the preceding transaction.

### Transaction comparison

Transactions from file A and file B are matched by their sort key. Same-key duplicates (e.g. two transactions both keyed "0090002") are paired by position within the group (first with first, second with second).

Results appear in:
- **HTML report**: `▶ txn` panel per file pair card, with per-transaction diff
- **CSV auto-export**: `<filename>_txn.csv` alongside the output (columns: sort_key, status, similarity_pct, lines_added, lines_deleted)

---

## 6. How separator_pattern Works (Summary Section Extraction)

Requires a `separator_pattern` in the matching CSV row. No command-line flag needed — if the pattern is present, section extraction runs automatically.

The `separator_pattern` regex identifies full separator lines (e.g. lines of 20+ dashes). Every block of content between consecutive separator lines is extracted as a named summary section. The **first non-blank line** of the block becomes the section name — no special header prefix (like `***`) is required.


**Transaction lines are excluded:** If `--transactions` is also active, lines that belong to identified transactions are skipped during section extraction. This prevents card/transaction data from appearing inside summary sections.

**Separator lines always define boundaries**, even if they fall inside a transaction block due to `max_txn_lines` overlap.

### Section comparison

Sections from file A and file B are matched by normalized name. Normalization strips:
- Leading `***` (if present)
- Timestamp prefix (HH:MM:SS, if present)
- Trailing `(date)` suffix (if present)

So `*** 13:07:03 FINAL TOTALS (06/02)` and `*** 13:08:15 FINAL TOTALS (06/03)` both normalize to `FINAL TOTALS` and are matched. Plain lines without `***` are also normalized (timestamp and date suffix stripping still applies).

Results appear in:
- **HTML report**: `▶ sections` panel per file pair card, with `▶ view` (identical) or `▶ diff` (changed) per section
- **CSV auto-export**: `<filename>_sections.csv` alongside the output (columns: section_name, status, similarity_pct, lines_added, lines_deleted)

---

## 7. What Happens When You Combine Everything

Given this splits.csv:
```csv
report_pattern,split_pattern,sort_pattern,max_txn_lines,separator_pattern
TERM.*,PAGE\s+(\d+),FN[A-Z0-9]+\s(\d+),3,"^[-]{20,}\s*$"
```

And this command:
```powershell
python html_reporter.py folder_a/ folder_b/ --output results/report.html --split-config splits.csv --transactions
```

For each TERM file pair, the processing pipeline is:

1. **Page splitting**: file split into pages at `PAGE N` value changes
2. **Body filtering**: blank lines removed, original line numbers preserved
3. **Transaction detection** (`--transactions`): `sort_pattern` scanned across whole body; each match starts a new transaction block, capped at 3 lines
4. **Transaction comparison**: transactions matched A↔B by sort key, diffed
5. **Section extraction** (`separator_pattern`): body scanned for `---` separator lines; every block between separators (excluding transaction-owned lines) → named summary section (first non-blank line = name)
6. **Section comparison**: sections matched A↔B by normalized name, diffed
7. **Full-body diff**: standard line-level diff of the entire body

The HTML report card for each file pair shows:
- `▶ pages` — per-page comparison with status filters
- `▶ txn` — transaction-level comparison with per-transaction diff
- `▶ sections` — summary section comparison (TOTALS, COUNTS, etc.) with per-section view/diff
- `▶ diff` — full body unified diff with virtual scrolling

Auto-generated alongside the HTML:
- `<file>_txn.csv` — transaction comparison results
- `<file>_sections.csv` — section comparison results
- `open_bcompare_<file>.bat` — Beyond Compare launcher

---

## 8. Quick Reference: File Mode

```powershell
# Basic
python report_comparator.py fileA.txt fileB.txt --output result.txt

# With split config, transactions, and all features
python report_comparator.py fileA.txt fileB.txt --output result.txt \
    --split-config splits.csv --transactions --ignore-dates

# Extract transactions from each file independently
python report_comparator.py fileA.txt fileB.txt --output result.txt \
    --split-config splits.csv --extract-txn
```

Output files (when `--output` is set):
- `result.txt` — text comparison report
- `result_txn.csv` — transaction comparison (if `--transactions` + `sort_pattern` match)
- `result_sections.csv` — section comparison (if `separator_pattern` match)

---

## 9. Quick Reference: Folder Mode (Text)

```powershell
python report_comparator.py folder_a/ folder_b/ --output-dir results/ \
    --split-config splits.csv --transactions --fuzzy-match
```

Output files per pair in `results/`:
- `<file>_comparison.txt`
- `<file>_txn.csv`
- `<file>_sections.csv`
- `FOLDER_SUMMARY.txt`

---

## 10. Quick Reference: Folder Mode (HTML)

```powershell
python html_reporter.py folder_a/ folder_b/ --output results/report.html \
    --split-config splits.csv --transactions --fuzzy-match
```

Output files in `results/`:
- `report.html` — self-contained interactive report
- `<file>_txn.csv` per pair
- `<file>_sections.csv` per pair
- `open_bcompare_<file>.bat` per pair
