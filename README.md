# Reporter — Usage Guide

Reporter compares structured text reports and CSV data files, producing text summaries, CSV exports, and a self-contained HTML report with interactive diff panels.

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
| `--ext EXT` | File extension filter (default `.txt`). **Repeatable** — use `--ext .txt --ext .csv` to scan both extensions in one pass |
| `--fuzzy-match` | Pair files with similar names when exact name match fails (folder mode) |
| `--fuzzy-threshold 0.70` | Minimum name similarity for fuzzy pairing (default 0.70) |
| `--semantic` | Add TF-IDF semantic similarity score (requires scikit-learn) |
| `--ignore-dates` | Treat date/time differences as equal — original values are still **displayed** but not highlighted in diffs |
| `--ignore-lines PAT` | Skip lines matching regex PAT (repeatable) |
| `--no-diff` | Skip unified diff output (text mode only) |
| `--split-config PATH` | CSV file mapping filename patterns to parsing rules (see below) |
| `--transactions` | Enable transaction-level comparison (requires `sort_pattern` in split config) |
| `--extract-txn` | Export each file's transactions to `<file>_txn_extract.csv` independently of comparison |

---

## 3. The Split Config CSV (`--split-config`)

The `--split-config` flag points to a CSV file that tells the tool how to parse specific report types and how to perform CSV row matching. Each row maps a filename pattern to parsing rules. Files that don't match any row fall back to default built-in page-break detection.

### CSV columns

```csv
report_pattern,split_pattern,sort_pattern,max_txn_lines,separator_pattern,csv_key_col,csv_has_header
```

| Column | Required | Purpose |
|--------|----------|---------|
| `report_pattern` | Yes | Python regex matched against the **filename** (not path). First matching row wins. Must be a valid regex — `*.csv` is invalid; use `.*\.csv` |
| `split_pattern` | No | Regex per line; when the captured value changes, a new page starts. Blank → built-in page-break detection |
| `sort_pattern` | No | Regex per line identifying transaction boundaries. Enables `--transactions` |
| `max_txn_lines` | No | Max lines per transaction block. Prevents non-transaction content from being absorbed into the preceding transaction. Blank = unlimited |
| `separator_pattern` | No | Regex matching full separator lines (e.g. lines of dashes). Enables summary-section extraction |
| `csv_key_col` | No | 0-based column index (or `"i,j"` for composite key) used to match rows across files. Blank → positional comparison |
| `csv_has_header` | No | `true` or `false`. If `true`, row 0 is treated as a header (compared separately, excluded from row matching) |

Rows with fewer columns than the header are safe — missing columns default to blank.

### Example splits.csv

```csv
report_pattern,split_pattern,sort_pattern,max_txn_lines,separator_pattern,csv_key_col,csv_has_header
invoices.*,^INVOICE NO:\s+(\S+),^DATE:\s+(.+),,
TERM.*,PAGE\s+(\d+),FN[A-Z0-9]+\s(\d+),3,"[-]{20,}$"
RECAP.*,,,,"\s*[-]{20,}\s*$"
.*\.csv,,,,,"0",true
```

**Row 1 — Invoice files** (`invoices.*`):
- Pages split when `INVOICE NO:` value changes
- Transactions start at lines matching `^DATE:`
- No CSV key matching (text file)

**Row 2 — Terminal files** (`TERM.*`):
- Pages split on `PAGE N` lines
- Transactions start at terminal sequence lines (e.g. `ACCD509B 005000`), capped at 3 lines
- Lines of 20+ dashes act as section boundaries

**Row 3 — Recap files** (`RECAP.*`):
- Built-in page-break detection (blank split_pattern)
- Dash separator lines (with optional leading spaces) define summary sections

**Row 4 — CSV data files** (`.*\.csv`):
- Column 0 is the match key — rows with the same value in column 0 are paired across files
- Row 0 is the header (excluded from row matching, shown separately)

**Note on quoting:** If a regex contains a comma (e.g. `{20,}`), wrap the field in double quotes so the comma isn't treated as a column separator.

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

Non-unique keys are handled correctly: if INV-001 appears in two separate blocks they are grouped into one page (the split fires on the value *transition*, not on each occurrence).

Files that match no CSV row, or rows with a blank `split_pattern`, use the built-in delimiter patterns. The built-in patterns are tried **one at a time in priority order** — the first pattern that splits the file into multiple pages is used. Priority order (highest first):

1. Standalone `PAGE N` line
2. Any line ending with `PAGE N` (e.g. `SOMEDATE 06/02/26 ... PAGE 341`)
3. Form-feed character
4. `--- Page N ---` / `=== Page N ===`
5. `Page N of M`
6. Lines of 10+ dashes or underscores
7. Lines of 10+ asterisks

---

## 5. How sort_pattern Works (Transaction Detection)

Requires `--transactions` flag AND a `sort_pattern` in the matching CSV row. Files with no `sort_pattern` are silently skipped.

The `sort_pattern` regex is searched per line across the **entire file body**. Every matching line unconditionally starts a new transaction block. Each block runs from its match line to the next match line (exclusive).

```
  ACCD509B 009000    ← sort_pattern matches, starts Transaction 1 (key="009000")
  line 2 ...
  line 3 ...         ← max_txn_lines=3, so block capped here
  ACCD509B 009002    ← starts Transaction 2 (key="009002")
  ...
```

`max_txn_lines` caps each block at N lines. Use this when non-transaction content (totals, counters) follows the last transaction without another sort_pattern match.

### Transaction comparison

Transactions from file A and B are matched by sort key. Duplicate keys are paired by position within the group (first with first, second with second).

Results appear in:
- **HTML report**: `▶ txn` panel per file pair card with per-transaction diff
- **CSV auto-export**: `<filename>_txn.csv` (columns: sort_key, status, similarity_pct, lines_added, lines_deleted)

---

## 6. How separator_pattern Works (Summary Section Extraction)

Requires a `separator_pattern` in the matching CSV row. No extra command-line flag needed.

The `separator_pattern` regex identifies full separator lines (e.g. lines of 20+ dashes). Every block of content between consecutive separator lines is extracted as a named summary section. The **first non-blank line** of the block becomes the section name.

Transaction lines (when `--transactions` is active) are excluded from section content. Separator lines always define boundaries.

### Section comparison

Sections are matched **positionally** — section 1 of A vs section 1 of B, section 2 of A vs section 2 of B, and so on. Extra sections in either file appear as added or removed.

Results appear in:
- **HTML report**: `▶ sections` panel per file pair card with per-section diff
- **CSV auto-export**: `<filename>_sections.csv` (columns: section_name, status, similarity_pct, lines_added, lines_deleted)

---

## 7. How csv_key_col Works (CSV Row Matching)

Applies to files where the matching split-config row has a `csv_key_col` value. No extra command-line flag needed — but the file must be picked up by `--ext .csv` (or whichever extension matches).

Rows are matched **by key value** rather than by position. This correctly handles CSV files where the same rows appear in different orders in A and B.

```
A rows (shuffled):          B rows (differently shuffled):
TXN0045,2026-03-12,...      TXN0101,2026-01-08,...
TXN0012,2026-01-20,...      TXN0045,2026-03-12,...   ← matched to A row 1
TXN0101,2026-01-08,...      TXN0012,2026-01-20,...   ← matched to A row 2
```

**Composite keys** — set `csv_key_col` to a quoted comma-separated list (e.g. `"0,2"`) to match on multiple columns. The key is the pipe-joined concatenation of values in those columns.

**Duplicate keys** — rows sharing the same key are paired by occurrence index within the group (first duplicate with first, second with second).

**Header row** — when `csv_has_header=true`, row 0 is treated as the header. It is shown separately in the HTML panel and excluded from row matching.

### Diff display for CSV files

The main diff panel for a CSV file pair diffs the **key-sorted lines** (not the original file order) so the diff reflects real value changes rather than row-order differences. With `--ignore-dates`, date-valued columns are not highlighted when diffed.

Results appear in:
- **HTML report**: `▶ csv` panel per file pair card showing matched / added / removed rows with per-row diff
- **CSV auto-export**: `<filename>_csv.csv` (columns: key, status, similarity_pct, lines_added, lines_deleted)

---

## 8. What Happens When You Combine Everything

Given this splits.csv:
```csv
report_pattern,split_pattern,sort_pattern,max_txn_lines,separator_pattern,csv_key_col,csv_has_header
TERM.*,PAGE\s+(\d+),FN[A-Z0-9]+\s(\d+),3,"[-]{20,}$",,
.*\.csv,,,,,"0",true
```

And this command:
```powershell
python html_reporter.py folder_a/ folder_b/ --output results/report.html \
    --split-config splits.csv --transactions --ext .txt --ext .csv --fuzzy-match --ignore-dates
```

For each **TERM file pair**, the processing pipeline is:

1. **Page splitting**: file split into pages at `PAGE N` value changes
2. **Transaction detection**: `sort_pattern` scanned across whole body; each match starts a new block, capped at 3 lines
3. **Transaction comparison**: transactions matched A↔B by sort key, diffed; date differences suppressed
4. **Section extraction**: body scanned for `---` separator lines; blocks between separators → named summary sections
5. **Section comparison**: sections matched A↔B positionally, diffed
6. **Full-body diff**: standard line-level diff of the entire body

The HTML report card for each TERM file pair shows:
- `▶ pages` — per-page comparison with status filters
- `▶ txn` — transaction comparison with per-transaction diff
- `▶ sections` — summary section comparison with per-section diff
- `▶ diff` — full body unified diff with virtual scrolling

For each **CSV file pair**, the processing pipeline is:

1. **Row parsing**: `csv.reader` parses each row; header separated if `csv_has_header=true`
2. **Key extraction**: column 0 value used as the match key
3. **Row matching**: rows grouped by key, paired by occurrence; unmatched rows flagged added/removed
4. **Row comparison**: matched pairs diffed with per-field token highlighting; date fields suppressed with `--ignore-dates`
5. **Sorted diff**: diff panel shows both files' lines in key order so value changes are visible without positional noise

The HTML report card for each CSV file pair shows:
- `▶ csv` — row-level comparison with per-row diff
- `▶ diff` — full-body diff in key-sorted order

Auto-generated alongside the HTML for each pair:
- `<file>_txn.csv` — transaction comparison results
- `<file>_sections.csv` — section comparison results
- `<file>_csv.csv` — CSV row comparison results

---

## 9. Quick Reference: File Mode

```powershell
# Basic
python report_comparator.py fileA.txt fileB.txt --output result.txt

# With split config, transactions, and date normalization
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
- `result_csv.csv` — CSV row comparison (if `csv_key_col` match)

---

## 10. Quick Reference: Folder Mode (Text)

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

## 11. Quick Reference: Folder Mode (HTML)

```powershell
# Text files only (default)
python html_reporter.py folder_a/ folder_b/ --output results/report.html \
    --split-config splits.csv --transactions --fuzzy-match

# Text + CSV files in one pass
python html_reporter.py folder_a/ folder_b/ --output results/report.html \
    --split-config splits.csv --transactions --fuzzy-match --ext .txt --ext .csv

# With date normalization
python html_reporter.py folder_a/ folder_b/ --output results/report.html \
    --split-config splits.csv --transactions --ext .txt --ext .csv --ignore-dates
```

Output files in `results/`:
- `report.html` — self-contained interactive report (all CSS/JS embedded, works offline)
- `<file>_txn.csv` per pair (when transactions enabled)
file format for xlsx
Seq	Column Name	Field Start	Field Length
1	ACCT_NO	1	8
2	CUST_NAME	10	20
3	AMOUNT	31	10
4	TXN_DATE	42	8
5	STATUS	51	6
- `<file>_sections.csv` per pair (when separator_pattern set)
- `<file>_csv.csv` per pair (when csv_key_col set)
