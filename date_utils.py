"""
date_utils.py
=============
Utilities for detecting and removing date/datetime patterns from text.

Supports common formats:
  - ISO dates: 2026-05-17, 2026/05/17
  - US dates: 5/17/2026, 05/17/2026, May 17, 2026
  - Timestamps: 12:34:56, 12:34:56 PM
  - Full datetime: 2026-05-17 12:34:56, 5/17/2026 3:45 PM
  - Month names: January, February, etc.
"""

import re
from typing import List


# Comprehensive date/time pattern collection
_DATE_TIME_PATTERNS = [
    # ISO date format: YYYY-MM-DD or YYYY/MM/DD
    r'\d{4}[-/]\d{2}[-/]\d{2}',
    
    # US date format: M/D/YYYY or MM/DD/YYYY
    r'\d{1,2}/\d{1,2}/\d{4}',
    
    # Month name + day + year: May 17, 2026 or May 17 2026
    r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}',
    r'(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}',
    
    # Time format: HH:MM:SS or HH:MM
    r'\d{1,2}:\d{2}(?::\d{2})?(?:\s*(?:AM|PM|am|pm))?',
    
    # Full datetime ISO: 2026-05-17T12:34:56 or 2026-05-17 12:34:56
    r'\d{4}[-/]\d{2}[-/]\d{2}[T\s]\d{1,2}:\d{2}(?::\d{2})?',
    
    # Day names: Monday, Tuesday, etc.
    r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?',
    
    # Abbreviated day names
    r'\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b',
    
    # Date range: 2026-05-17 - 2026-05-18
    r'\d{4}[-/]\d{2}[-/]\d{2}\s*[-–—]\s*\d{4}[-/]\d{2}[-/]\d{2}',
]

# Compile patterns into a single regex with alternation.
# No outer capturing groups — we only need substitution, not capture.
_DATE_TIME_RE = re.compile(
    "|".join(_DATE_TIME_PATTERNS),
    re.IGNORECASE
)

_MULTI_SPACE_RE = re.compile(r' {2,}')


def remove_dates_from_line(line: str) -> str:
    """
    Remove or mask date/time patterns from a line of text.
    
    Replaces matched patterns with a placeholder to preserve line structure,
    or removes them entirely if they comprise the whole line.
    
    Args:
        line: A line of text potentially containing dates/times
    
    Returns:
        The line with date/time patterns removed or masked
    """
    # Remove patterns but keep spacing by replacing with empty string
    result = _DATE_TIME_RE.sub("", line)
    
    # Clean up multiple consecutive spaces
    result = _MULTI_SPACE_RE.sub(' ', result)
    
    # Strip leading/trailing whitespace
    result = result.strip()
    
    return result


def filter_lines(lines: List[str]) -> List[str]:
    """
    Remove date/time patterns from a list of lines.
    
    Lines that become empty after date removal are preserved as empty strings
    to maintain line indexing consistency.
    
    Args:
        lines: List of text lines
    
    Returns:
        List of lines with dates/times removed
    """
    return [remove_dates_from_line(line) for line in lines]


def has_dates(line: str) -> bool:
    """
    Check if a line contains any date/time patterns.

    Args:
        line: A line of text

    Returns:
        True if line contains date/time patterns, False otherwise
    """
    return bool(_DATE_TIME_RE.search(line))


def first_date_match(line: str) -> str:
    """Return the first date/time string found in line, or empty string."""
    m = _DATE_TIME_RE.search(line)
    return m.group(0) if m else ""
