"""The five-column contract, and everything needed to hold six platforms to it.

    Date      publication date, YYYY-MM-DD
    Type      post | comment | blog
    Platform  one of PLATFORMS
    URL       address of the item, or of its parent thread where the platform
              gives replies no address of their own
    Content   cleaned, PII-redacted body text

Before this module the six scrapers between them emitted six `Type`
vocabularies (`post`/`reply`, `blogs`/`comments`, `Post`, `Blog`,
`Comment`/`Discussion`/`Blog`), three date formats and two spellings of
"youtube". `normalise` maps all of it onto the contract above, and `validate`
refuses to let anything else reach the merged file.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

import pandas as pd

COLUMNS = ["Date", "Type", "Platform", "URL", "Content"]

# The canonical Type vocabulary. Three values, lowercase.
#
# `transcript` was here until the team settled on these three. Nothing ever
# produced it: the YouTube ETL had a branch for it, but the fetch step never
# wrote a transcript field, so it emitted zero rows. `validate(strict=True)`
# now rejects it, which is the point -- a fourth value cannot reappear by
# accident.
TYPE_POST = "post"
TYPE_COMMENT = "comment"
TYPE_BLOG = "blog"
TYPES = {TYPE_POST, TYPE_COMMENT, TYPE_BLOG}

# Every spelling any of the six scrapers has ever emitted, mapped onto the
# contract. Keys are matched case-insensitively after stripping.
TYPE_ALIASES = {
    "post": TYPE_POST, "posts": TYPE_POST, "discussion": TYPE_POST,
    "discussions": TYPE_POST, "thread": TYPE_POST, "newsfeed post": TYPE_POST,
    "reply": TYPE_COMMENT, "replies": TYPE_COMMENT, "comment": TYPE_COMMENT,
    "comments": TYPE_COMMENT,
    "blog": TYPE_BLOG, "blogs": TYPE_BLOG, "article": TYPE_BLOG,
    "blog post": TYPE_BLOG,
}

# Canonical platform names, keyed by every spelling seen in the wild.
PLATFORM_ALIASES = {
    "inspire": "Inspire", "womenheart": "Inspire", "womenheart inspire": "Inspire",
    "heart sisters": "Heart Sisters", "heartsisters": "Heart Sisters",
    "myheartsisters": "Heart Sisters",
    "reddit": "Reddit",
    "her heart": "Her Heart", "herheart": "Her Heart",
    "mayo clinic connect": "Mayo Clinic Connect", "mayo": "Mayo Clinic Connect",
    "mayo clinic": "Mayo Clinic Connect",
    "youtube": "YouTube",
}
PLATFORMS = sorted(set(PLATFORM_ALIASES.values()))

# Platforms where one URL addresses exactly one item, so two rows sharing a URL
# must be two renderings of the same item.
#
# Three platforms are absent on purpose:
#   Inspire, YouTube -- every comment carries its thread's or its video's URL,
#       so a shared URL means "same discussion", not "same row".
#   Reddit -- its URLs are synthetic title slugs built by the TXT parser, not
#       real permalinks, so two unrelated posts both titled "Aspirin" collide
#       on `/r/HeartAttack/comments/aspirin/`. 40 rows in the delivered
#       snapshot share a slug with another post.
URL_IDENTIFIES_ROW = {"Mayo Clinic Connect", "Heart Sisters", "Her Heart"}

_ISO_RX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_YMD_SLASH_RX = re.compile(r"^(\d{4})/(\d{1,2})/(\d{1,2})")
_DMY_RX = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})")
_TEXT_DATE_FORMATS = ("%b %d, %Y", "%b %d %Y", "%B %d, %Y", "%B %d %Y",
                      "%d %b %Y", "%d %B %Y")


def normalise_type(value: str) -> str:
    """Map any scraper's Type spelling onto the three-value vocabulary."""
    key = str(value or "").strip().lower()
    if key in TYPES:
        return key
    return TYPE_ALIASES.get(key, key)


def normalise_platform(value: str) -> str:
    key = str(value or "").strip().lower()
    return PLATFORM_ALIASES.get(key, str(value or "").strip())


def normalise_date(value: str) -> str:
    """Return YYYY-MM-DD, or "" when the value cannot be read as a date.

    Handles the three formats the delivered corpus actually contains: ISO
    (reddit, mayo, inspire), `2016/1/3` (heart_sisters, from an Excel round
    trip) and `20/04/2026` (her_heart).

    The bare `d/m/Y` branch is read **day-first**. Her Heart is the only source
    that emits it and it is an Australian site, confirmed by values such as
    `20/04/2026` where a month-first reading is impossible. Do not "fix" this
    to month-first without re-checking that source.
    """
    text = str(value or "").strip()
    if not text:
        return ""

    match = _ISO_RX.match(text)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

    match = _YMD_SLASH_RX.match(text)
    if match:
        year, month, day = (int(g) for g in match.groups())
        return _safe_date(year, month, day)

    match = _DMY_RX.match(text)
    if match:
        day, month, year = (int(g) for g in match.groups())
        return _safe_date(year, month, day)

    cleaned = re.sub(r"\s+\d{1,2}:\d{2}\s*(?:am|pm)?", "", text, flags=re.I).strip()
    for fmt in _TEXT_DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _safe_date(year: int, month: int, day: int) -> str:
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def row_id(platform: str, url: str, content: str) -> str:
    """The id `scripts/csv_to_db.py` will compute for this row.

    Mirrored here so dedupe across files agrees with the database's primary key
    and a re-import cannot create a second copy of a row under a new id.
    """
    raw = f"{url or 'unknown'}{content or 'unknown'}".encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:16]
    return f"{(platform or 'unknown').strip()}_{digest}"


def normalise(df: pd.DataFrame, *, platform: str | None = None) -> pd.DataFrame:
    """Coerce any scraper's frame onto the five columns, normalised.

    Accepts the column spellings seen across the six sources -- `text`,
    `Text/Content`, `Source/Platform`, lowercase `date`/`url` -- so a scraper's
    native output can be handed over without a bespoke rename at each call site.
    """
    renames = {"text": "Content", "Text/Content": "Content", "content": "Content",
               "Source/Platform": "Platform", "platform": "Platform",
               "date": "Date", "url": "URL", "type": "Type"}
    df = df.rename(columns={k: v for k, v in renames.items() if k in df.columns})

    if platform is not None:
        df["Platform"] = platform
    for column in COLUMNS:
        if column not in df.columns:
            df[column] = ""

    out = pd.DataFrame({
        "Date": df["Date"].map(normalise_date),
        "Type": df["Type"].map(normalise_type),
        "Platform": df["Platform"].map(normalise_platform),
        "URL": df["URL"].astype(str).str.strip(),
        "Content": df["Content"].astype(str),
    })
    return out


def dedupe(df: pd.DataFrame, *, collapse_fragments: bool = True) -> tuple[pd.DataFrame, dict]:
    """Drop duplicate rows. Returns the frame and a per-rule tally.

    Two rules, in order:

    1. **Exact duplicates** -- identical (Platform, Type, URL, Content). These
       come from re-running a scraper over a corpus it already collected, and
       from the delivered history overlapping a fresh crawl.

    2. **Fragments** -- on platforms in `URL_IDENTIFIES_ROW`, rows sharing
       (Platform, Type, URL) are renderings of one item, so only the longest
       Content is kept. This is what removes Mayo's 1,365 search-result
       snippets that duplicate a full comment already collected at the same URL.
       It is deliberately not applied to Inspire or YouTube, where a shared URL
       means "same thread", not "same row".
    """
    before = len(df)
    df = df.drop_duplicates(subset=["Platform", "Type", "URL", "Content"], keep="first")
    exact_removed = before - len(df)

    fragments_removed = 0
    if collapse_fragments:
        collapsible = df["Platform"].isin(URL_IDENTIFIES_ROW) & (df["URL"].str.strip() != "")
        head, tail = df[collapsible], df[~collapsible]
        if not head.empty:
            n_before = len(head)
            order = head["Content"].str.len().sort_values(ascending=False).index
            head = (head.loc[order]
                        .drop_duplicates(subset=["Platform", "Type", "URL"], keep="first"))
            fragments_removed = n_before - len(head)
            df = pd.concat([head, tail], ignore_index=True)

    return df, {"exact": exact_removed, "fragments": fragments_removed,
                "kept": len(df), "input": before}


def sort_rows(df: pd.DataFrame) -> pd.DataFrame:
    """Chronological, then grouped by platform and thread.

    Undated rows sort last rather than first: an empty Date string would
    otherwise lead the file, which reads as a corrupt export.
    """
    key = df["Date"].where(df["Date"] != "", "9999-99-99")
    return (df.assign(_k=key)
              .sort_values(["_k", "Platform", "URL", "Type"], kind="stable")
              .drop(columns="_k")
              .reset_index(drop=True))


def validate(df: pd.DataFrame, *, strict: bool = True) -> list[str]:
    """Check the frame against the contract. Returns a list of problems.

    With `strict=True` an unknown Type or Platform is an error; the merge step
    calls it that way, so a scraper that invents a new vocabulary fails loudly
    at build time rather than silently polluting the database.
    """
    problems: list[str] = []
    if list(df.columns) != COLUMNS:
        problems.append(f"columns are {list(df.columns)}, expected {COLUMNS}")
        return problems

    bad_dates = df[~df["Date"].str.fullmatch(r"\d{4}-\d{2}-\d{2}|")]
    if len(bad_dates):
        problems.append(f"{len(bad_dates)} rows have a malformed Date, "
                        f"e.g. {bad_dates['Date'].head(3).tolist()}")

    empty_dates = int((df["Date"] == "").sum())
    if empty_dates:
        problems.append(f"{empty_dates} rows have no Date")

    empty_content = int((df["Content"].str.strip() == "").sum())
    if empty_content:
        problems.append(f"{empty_content} rows have empty Content")

    if strict:
        unknown_types = sorted(set(df["Type"]) - TYPES)
        if unknown_types:
            problems.append(f"unknown Type values: {unknown_types}")
        unknown_platforms = sorted(set(df["Platform"]) - set(PLATFORMS))
        if unknown_platforms:
            problems.append(f"unknown Platform values: {unknown_platforms}")
    return problems
