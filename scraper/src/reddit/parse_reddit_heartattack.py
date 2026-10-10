#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parse manually copied Reddit listing text into a compact CSV export.

The input expected here is the text copied from Reddit's Chinese UI. Each card
normally ends with this action footer:

    赞同
    <score>
    反对
    <comment count>
    转到评论
    [<repost count>]
    转载
    共享

By default the script exports one UTF-8 CSV with these columns:

    Date, Type, Platform, URL, Content

Dates are derived by subtracting each card's relative time from the input
file's last-modified time. URLs follow the requested synthetic title-slug
format and do not contain Reddit's real post ID.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


ACTION_UPVOTE = "赞同"
ACTION_DOWNVOTE = "反对"
ACTION_GOTO_COMMENTS = "转到评论"
ACTION_REPOST = "转载"
ACTION_SHARE = "共享"
AVATAR_SUFFIX = " 头像"
BULLET = "•"
DELETED_MARKER = "[已删除]"

AUTHOR_RE = re.compile(r"^u/(?P<name>\S+?)(?:\s+头像)?$")
SUBREDDIT_RE = re.compile(r"^r/[A-Za-z0-9_]+$")
SUBREDDIT_PREVIEW_RE = re.compile(r"^r/[A-Za-z0-9_]+\s+-\s+.+$")
URL_RE = re.compile(r"https?://\S+")
MEDIA_PAGE_RE = re.compile(r"^第\s+\d+\s+页(?:\s+\(当前页面\))?$")
MEDIA_ITEM_RE = re.compile(r"^项\s+\d+/\d+$")
TIME_RE = re.compile(
    r"^(?:"
    r"\d+\s*(?:秒|分钟|小时|天|周|星期|个月|年)前|"
    r"\d+\s*(?:second|minute|hour|day|week|month|year|s|m|h|d|w|mo|y)s?\s+ago|"
    r"just now"
    r")$",
    re.IGNORECASE,
)

CHINESE_RELATIVE_TIME_RE = re.compile(
    r"^(?P<value>\d+)\s*(?P<unit>秒|分钟|小时|天|周|星期|个月|年)前$"
)
ENGLISH_RELATIVE_TIME_RE = re.compile(
    r"^(?P<value>\d+)\s*(?P<unit>seconds?|minutes?|hours?|days?|weeks?|months?|years?|s|m|h|d|w|mo|y)\s+ago$",
    re.IGNORECASE,
)


CSV_FIELDS = [
    "source_index",
    "id",
    "collection",
    "record_type",
    "post_type",
    "author",
    "relative_time",
    "title",
    "body",
    "text",
    "score",
    "comment_count",
    "repost_count",
    "embedded_subreddit",
    "embedded_relative_time",
    "embedded_title",
    "embedded_body",
    "embedded_score",
    "embedded_comment_count",
    "urls",
    "media_or_preview_lines",
    "raw_line_count",
    "source_file",
]

CONTENT_CSV_FIELDS = ["Date", "Type", "Platform", "URL", "Content"]
SCRIPT_DIR = Path(__file__).resolve().parent
SCRAPER_DIR = SCRIPT_DIR.parents[1]
DEFAULT_INPUT = SCRIPT_DIR / "subreddit.txt"
DEFAULT_OUTPUT = SCRAPER_DIR / "output" / "raw" / "reddit"

# This parser had no PII handling at all: post bodies went into the shared CSV
# with whatever names, links and contact details the authors had typed. Routing
# the exported text through the shared engine fixes that. `common.pii` is pure
# standard library, so this module stays stdlib-only as documented.
sys.path.insert(0, str(SCRAPER_DIR))
from common import pii  # noqa: E402  (path must be set first)

PII_STATS = pii.Stats()


def decoded_text_score(text: str) -> float:
    known_tokens = [
        ACTION_UPVOTE,
        ACTION_DOWNVOTE,
        ACTION_GOTO_COMMENTS,
        ACTION_REPOST,
        ACTION_SHARE,
        "小时前",
        "天前",
        "个月前",
        "个赞同",
        "条评论",
        DELETED_MARKER,
    ]
    score = sum(text.count(token) * 100 for token in known_tokens)
    score += sum(1 for char in text if "\u4e00" <= char <= "\u9fff") * 0.1
    score -= text.count("�") * 500
    score -= text.count("\x00") * 50
    return score


def read_text(path: Path) -> str:
    """Read likely text while avoiding silent UTF-8/GBK mojibake."""

    data = path.read_bytes()
    candidates: list[tuple[float, str, str]] = []

    for encoding in ("utf-8-sig", "gb18030", "utf-16", "utf-16-le", "utf-16-be"):
        try:
            decoded = data.decode(encoding)
        except UnicodeError:
            continue
        candidates.append((decoded_text_score(decoded), encoding, decoded))

    if not candidates:
        return data.decode("utf-8", errors="replace")

    candidates.sort(reverse=True, key=lambda item: item[0])
    return candidates[0][2]


def clean_line(line: str) -> str:
    return line.replace("\ufeff", "").replace("\xa0", " ").strip()


def split_cards(text: str) -> list[list[str]]:
    cards: list[list[str]] = []
    current: list[str] = []

    for raw_line in text.splitlines():
        line = clean_line(raw_line)
        if line == ACTION_SHARE:
            if any(clean_line(item) for item in current):
                cards.append(current)
            current = []
        else:
            current.append(raw_line.rstrip("\r\n"))

    if any(clean_line(item) for item in current):
        cards.append(current)

    return cards


def parse_count_value(text: str | None) -> int | None:
    if text is None:
        return None

    value_text = text.strip()
    if not value_text:
        return None

    match = re.search(r"\d+(?:[,.]\d+)?", value_text)
    if not match:
        return None

    token = match.group(0)
    if "," in token and "." not in token:
        token = token.replace(",", "")
    else:
        token = token.replace(",", "")

    try:
        value = float(token)
    except ValueError:
        return None

    suffix = value_text[match.end() :].lower()
    if "万" in suffix:
        value *= 10_000
    elif "千" in suffix or suffix.startswith("k"):
        value *= 1_000
    elif suffix.startswith("m"):
        value *= 1_000_000

    return int(round(value))


def parse_stats_line(line: str) -> dict[str, int | None] | None:
    if "个赞同" not in line and "条评论" not in line:
        return None

    score: int | None = None
    comments: int | None = None

    score_match = re.search(r"([\d,.]+(?:\s*[万千kKmM])?)\s*个赞同", line)
    if score_match:
        score = parse_count_value(score_match.group(1))

    comment_match = re.search(r"([\d,.]+(?:\s*[万千kKmM])?)\s*条评论", line)
    if comment_match:
        comments = parse_count_value(comment_match.group(1))

    if score is None and comments is None:
        return None

    return {"score": score, "comment_count": comments}


def last_index(lines: list[str], value: str, before: int | None = None) -> int | None:
    stop = len(lines) if before is None else before
    for index in range(stop - 1, -1, -1):
        if lines[index] == value:
            return index
    return None


def number_between(lines: list[str], start: int | None, end: int | None) -> int | None:
    if start is None or end is None or start >= end:
        return None

    for line in lines[start + 1 : end]:
        value = parse_count_value(line)
        if value is not None and re.fullmatch(r"[\d,.\s万千kKmM]+", line):
            return value
    return None


def extract_footer(lines: list[str]) -> tuple[list[str], dict[str, Any]]:
    repost_index = last_index(lines, ACTION_REPOST)
    goto_index = last_index(lines, ACTION_GOTO_COMMENTS, repost_index)
    downvote_index = last_index(lines, ACTION_DOWNVOTE, goto_index)
    upvote_index = last_index(lines, ACTION_UPVOTE, downvote_index)

    if None in (repost_index, goto_index, downvote_index, upvote_index):
        return lines, {
            "score": None,
            "comment_count": None,
            "repost_count": None,
            "raw_lines": [],
            "parse_warning": "footer_not_found",
        }

    assert upvote_index is not None
    assert downvote_index is not None
    assert goto_index is not None
    assert repost_index is not None

    footer_lines = lines[upvote_index : repost_index + 1]
    footer = {
        "score": number_between(lines, upvote_index, downvote_index),
        "comment_count": number_between(lines, downvote_index, goto_index),
        "repost_count": number_between(lines, goto_index, repost_index),
        "raw_lines": footer_lines,
        "parse_warning": None,
    }

    return lines[:upvote_index], footer


def author_from_line(line: str) -> str | None:
    match = AUTHOR_RE.match(line)
    if not match:
        return None
    return f"u/{match.group('name')}"


def find_author(lines: list[str]) -> tuple[int | None, str | None]:
    for index, line in enumerate(lines):
        if line == DELETED_MARKER and index + 1 < len(lines) and lines[index + 1] == BULLET:
            return index, DELETED_MARKER

        author = author_from_line(line)
        if author is None:
            continue

        if line.endswith(AVATAR_SUFFIX) and index + 1 < len(lines):
            next_author = author_from_line(lines[index + 1])
            if next_author == author:
                return index + 1, author

        return index, author

    return None, None


def equivalent_text(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return False
    return re.sub(r"\s+", " ", left).strip().casefold() == re.sub(r"\s+", " ", right).strip().casefold()


def join_paragraphs(lines: list[str]) -> str:
    return "\n".join(line for line in lines if line)


def find_embedded_post(lines: list[str]) -> int | None:
    for index in range(0, max(0, len(lines) - 2)):
        if SUBREDDIT_RE.match(lines[index]) and lines[index + 1] == BULLET:
            return index
    return None


def strip_last_stats_line(lines: list[str]) -> tuple[list[str], dict[str, int | None] | None, str | None]:
    for index in range(len(lines) - 1, -1, -1):
        stats = parse_stats_line(lines[index])
        if stats is not None:
            return lines[:index] + lines[index + 1 :], stats, lines[index]
    return lines, None, None


def collect_urls(lines: list[str]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for line in lines:
        for url in URL_RE.findall(line):
            cleaned = url.rstrip(".,)")
            if cleaned not in seen:
                seen.add(cleaned)
                urls.append(cleaned)
    return urls


def parse_card(
    raw_lines: list[str],
    source_index: int,
    source_file: Path,
    collection: str,
) -> dict[str, Any]:
    raw_text = "\n".join(raw_lines).strip()
    lines = [clean_line(line) for line in raw_lines]
    lines = [line for line in lines if line]

    body_lines, footer = extract_footer(lines)
    author_index, author = find_author(body_lines)

    pre_title_lines: list[str] = []
    relative_time: str | None = None
    header_warning: str | None = None
    content_start = 0

    if author_index is None:
        content_lines = body_lines
        header_warning = "author_not_found"
    else:
        pre_title_lines = [
            line
            for line in body_lines[:author_index]
            if author_from_line(line) is None and not line.endswith(AVATAR_SUFFIX)
        ]

        bullet_index: int | None = None
        for probe in range(author_index + 1, min(author_index + 6, len(body_lines))):
            if body_lines[probe] == BULLET:
                bullet_index = probe
                break

        if bullet_index is not None and bullet_index + 1 < len(body_lines):
            relative_time = body_lines[bullet_index + 1]
            content_start = bullet_index + 2
            if not TIME_RE.match(relative_time):
                header_warning = "relative_time_unusual"
        else:
            content_start = author_index + 1
            header_warning = "header_time_not_found"

        content_lines = body_lines[content_start:]

    listing_title = pre_title_lines[0] if pre_title_lines else None
    first_content_line = content_lines[0] if content_lines else None
    title = listing_title or first_content_line

    if content_lines and equivalent_text(content_lines[0], title):
        content_lines = content_lines[1:]

    while content_lines and equivalent_text(content_lines[0], title):
        content_lines = content_lines[1:]

    embedded_index = find_embedded_post(content_lines)
    embedded_post: dict[str, Any] | None = None

    if embedded_index is not None:
        primary_body_lines = content_lines[:embedded_index]
        embedded_lines = content_lines[embedded_index:]
        embedded_content = embedded_lines[3:] if len(embedded_lines) >= 3 else []
        embedded_content, embedded_stats, embedded_stats_line = strip_last_stats_line(embedded_content)
        embedded_title = embedded_content[0] if embedded_content else None
        embedded_body_lines = embedded_content[1:] if embedded_content else []

        embedded_post = {
            "subreddit": embedded_lines[0],
            "relative_time": embedded_lines[2] if len(embedded_lines) >= 3 else None,
            "title": embedded_title,
            "body": join_paragraphs(embedded_body_lines),
            "score": embedded_stats.get("score") if embedded_stats else None,
            "comment_count": embedded_stats.get("comment_count") if embedded_stats else None,
            "stats_line": embedded_stats_line,
        }
    else:
        primary_body_lines = content_lines

    urls = collect_urls(lines)
    media_or_preview_lines = [
        line
        for line in lines
        if SUBREDDIT_PREVIEW_RE.match(line) or MEDIA_PAGE_RE.match(line) or MEDIA_ITEM_RE.match(line)
    ]
    deleted = any(line == DELETED_MARKER for line in lines)

    post_type_parts: list[str] = []
    if deleted:
        post_type_parts.append("deleted")
    if embedded_post is not None:
        post_type_parts.append("crosspost")
    if urls:
        post_type_parts.append("link")
    if media_or_preview_lines:
        post_type_parts.append("media_or_preview")
    if not post_type_parts:
        post_type_parts.append("text")

    body = join_paragraphs(primary_body_lines)
    text_parts = [part for part in [title, body] if part]
    if embedded_post is not None:
        text_parts.extend(
            part
            for part in [embedded_post.get("title"), embedded_post.get("body")]
            if isinstance(part, str) and part
        )

    stable_key = "\n".join([str(source_index), author or "", relative_time or "", title or "", raw_text])
    record_id = hashlib.sha1(stable_key.encode("utf-8", errors="replace")).hexdigest()[:16]

    return {
        "source_index": source_index,
        "id": record_id,
        "source_file": str(source_file.resolve()),
        "collection": collection,
        "record_type": "post",
        "post_type": "+".join(post_type_parts),
        "author": author,
        "relative_time": relative_time,
        "title": title,
        "body": body,
        "text": "\n".join(text_parts),
        "score": footer["score"],
        "comment_count": footer["comment_count"],
        "repost_count": footer["repost_count"],
        "embedded_post": embedded_post,
        "urls": urls,
        "media_or_preview_lines": media_or_preview_lines,
        "footer": footer,
        "raw_text": raw_text,
        "raw_line_count": len(lines),
        "parse_warnings": [
            warning
            for warning in [header_warning, footer.get("parse_warning")]
            if warning
        ],
    }


def flatten_for_csv(record: dict[str, Any]) -> dict[str, Any]:
    embedded = record.get("embedded_post") or {}
    return {
        "source_index": record.get("source_index"),
        "id": record.get("id"),
        "collection": record.get("collection"),
        "record_type": record.get("record_type"),
        "post_type": record.get("post_type"),
        "author": record.get("author"),
        "relative_time": record.get("relative_time"),
        "title": record.get("title"),
        "body": record.get("body"),
        "text": record.get("text"),
        "score": record.get("score"),
        "comment_count": record.get("comment_count"),
        "repost_count": record.get("repost_count"),
        "embedded_subreddit": embedded.get("subreddit"),
        "embedded_relative_time": embedded.get("relative_time"),
        "embedded_title": embedded.get("title"),
        "embedded_body": embedded.get("body"),
        "embedded_score": embedded.get("score"),
        "embedded_comment_count": embedded.get("comment_count"),
        "urls": json.dumps(record.get("urls", []), ensure_ascii=False),
        "media_or_preview_lines": json.dumps(record.get("media_or_preview_lines", []), ensure_ascii=False),
        "raw_line_count": record.get("raw_line_count"),
        "source_file": record.get("source_file"),
    }


def subtract_calendar_months(value: datetime, months: int) -> datetime:
    month_index = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def relative_datetime(relative_time: str | None, reference_time: datetime) -> datetime | None:
    if not relative_time:
        return None

    value = relative_time.strip()
    if value.casefold() == "just now":
        return reference_time

    match = CHINESE_RELATIVE_TIME_RE.fullmatch(value)
    if match:
        amount = int(match.group("value"))
        unit = match.group("unit")
        if unit == "秒":
            return reference_time - timedelta(seconds=amount)
        if unit == "分钟":
            return reference_time - timedelta(minutes=amount)
        if unit == "小时":
            return reference_time - timedelta(hours=amount)
        if unit == "天":
            return reference_time - timedelta(days=amount)
        if unit in {"周", "星期"}:
            return reference_time - timedelta(weeks=amount)
        if unit == "个月":
            return subtract_calendar_months(reference_time, amount)
        if unit == "年":
            return subtract_calendar_months(reference_time, amount * 12)

    match = ENGLISH_RELATIVE_TIME_RE.fullmatch(value)
    if not match:
        return None

    amount = int(match.group("value"))
    unit = match.group("unit").casefold()
    if unit in {"s", "second", "seconds"}:
        return reference_time - timedelta(seconds=amount)
    if unit in {"m", "minute", "minutes"}:
        return reference_time - timedelta(minutes=amount)
    if unit in {"h", "hour", "hours"}:
        return reference_time - timedelta(hours=amount)
    if unit in {"d", "day", "days"}:
        return reference_time - timedelta(days=amount)
    if unit in {"w", "week", "weeks"}:
        return reference_time - timedelta(weeks=amount)
    if unit in {"mo", "month", "months"}:
        return subtract_calendar_months(reference_time, amount)
    if unit in {"y", "year", "years"}:
        return subtract_calendar_months(reference_time, amount * 12)
    return None


def title_slug(title: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", title or "")
    ascii_title = normalized.encode("ascii", errors="ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_title.casefold()).strip("_")
    return slug or "post"


def synthetic_reddit_url(collection: str | None, title: str | None) -> str:
    subreddit = (collection or "r/HeartAttack").strip().strip("/")
    if subreddit.casefold().startswith("r/"):
        subreddit = subreddit[2:]
    subreddit = subreddit or "HeartAttack"
    return f"https://www.reddit.com/r/{subreddit}/comments/{title_slug(title)}/"


def content_rows(
    records: list[dict[str, Any]], reference_time: datetime
) -> list[dict[str, str]]:
    sortable_rows: list[tuple[datetime | None, dict[str, str]]] = []
    for record in records:
        derived_time = relative_datetime(record.get("relative_time"), reference_time)
        sortable_rows.append(
            (
                derived_time,
                {
                    "Date": derived_time.date().isoformat() if derived_time else "",
                    "Type": "Post",
                    "Platform": "Reddit",
                    "URL": synthetic_reddit_url(
                        record.get("collection"), record.get("title")
                    ),
                    "Content": pii.redact(record.get("text") or "", PII_STATS),
                },
            )
        )

    dated_rows = [item for item in sortable_rows if item[0] is not None]
    undated_rows = [item for item in sortable_rows if item[0] is None]
    dated_rows.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in dated_rows + undated_rows]


def write_jsonl(records: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            file.write("\n")


def write_detailed_csv(records: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow(flatten_for_csv(record))


def write_content_csv(
    records: list[dict[str, Any]], path: Path, reference_time: datetime
) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CONTENT_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(content_rows(records, reference_time))


def write_sqlite(records: list[dict[str, Any]], path: Path) -> bool:
    if path.exists():
        path.unlink()

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE posts (
                id TEXT PRIMARY KEY,
                source_index INTEGER NOT NULL,
                source_file TEXT NOT NULL,
                collection TEXT,
                record_type TEXT,
                post_type TEXT,
                author TEXT,
                relative_time TEXT,
                title TEXT,
                body TEXT,
                text TEXT,
                score INTEGER,
                comment_count INTEGER,
                repost_count INTEGER,
                embedded_subreddit TEXT,
                embedded_relative_time TEXT,
                embedded_title TEXT,
                embedded_body TEXT,
                embedded_score INTEGER,
                embedded_comment_count INTEGER,
                urls_json TEXT,
                media_or_preview_lines_json TEXT,
                footer_json TEXT,
                raw_text TEXT,
                raw_line_count INTEGER,
                parse_warnings_json TEXT
            )
            """
        )

        rows = []
        for record in records:
            embedded = record.get("embedded_post") or {}
            rows.append(
                (
                    record["id"],
                    record["source_index"],
                    record["source_file"],
                    record.get("collection"),
                    record.get("record_type"),
                    record.get("post_type"),
                    record.get("author"),
                    record.get("relative_time"),
                    record.get("title"),
                    record.get("body"),
                    record.get("text"),
                    record.get("score"),
                    record.get("comment_count"),
                    record.get("repost_count"),
                    embedded.get("subreddit"),
                    embedded.get("relative_time"),
                    embedded.get("title"),
                    embedded.get("body"),
                    embedded.get("score"),
                    embedded.get("comment_count"),
                    json.dumps(record.get("urls", []), ensure_ascii=False),
                    json.dumps(record.get("media_or_preview_lines", []), ensure_ascii=False),
                    json.dumps(record.get("footer", {}), ensure_ascii=False),
                    record.get("raw_text"),
                    record.get("raw_line_count"),
                    json.dumps(record.get("parse_warnings", []), ensure_ascii=False),
                )
            )

        connection.executemany(
            """
            INSERT INTO posts (
                id, source_index, source_file, collection, record_type, post_type, author,
                relative_time, title, body, text, score, comment_count,
                repost_count, embedded_subreddit, embedded_relative_time,
                embedded_title, embedded_body, embedded_score,
                embedded_comment_count, urls_json, media_or_preview_lines_json,
                footer_json, raw_text, raw_line_count, parse_warnings_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

        connection.execute("CREATE INDEX idx_posts_source_index ON posts(source_index)")
        connection.execute("CREATE INDEX idx_posts_author ON posts(author)")
        connection.execute("CREATE INDEX idx_posts_relative_time ON posts(relative_time)")
        connection.execute("CREATE INDEX idx_posts_comment_count ON posts(comment_count)")
        connection.execute("CREATE INDEX idx_posts_record_type ON posts(record_type)")
        connection.execute("CREATE INDEX idx_posts_post_type ON posts(post_type)")

        fts_enabled = True
        try:
            connection.execute(
                """
                CREATE VIRTUAL TABLE posts_fts
                USING fts5(id UNINDEXED, title, body, text, author, embedded_title, embedded_body)
                """
            )
            connection.execute(
                """
                INSERT INTO posts_fts (id, title, body, text, author, embedded_title, embedded_body)
                SELECT id, title, body, text, author, embedded_title, embedded_body
                FROM posts
                """
            )
        except sqlite3.OperationalError:
            fts_enabled = False

        connection.commit()
        return fts_enabled
    finally:
        connection.close()


def build_summary(records: list[dict[str, Any]], input_path: Path, sqlite_fts: bool) -> dict[str, Any]:
    type_counter = Counter(record["post_type"] for record in records)
    warnings_counter = Counter(
        warning
        for record in records
        for warning in record.get("parse_warnings", [])
    )

    return {
        "input_file": str(input_path.resolve()),
        "record_count": len(records),
        "record_type_counts": dict(
            sorted(Counter(record["record_type"] for record in records).items())
        ),
        "unique_author_count": len({record.get("author") for record in records if record.get("author")}),
        "post_type_counts": dict(sorted(type_counter.items())),
        "records_with_urls": sum(1 for record in records if record.get("urls")),
        "records_with_embedded_post": sum(1 for record in records if record.get("embedded_post")),
        "records_with_parse_warnings": sum(1 for record in records if record.get("parse_warnings")),
        "parse_warning_counts": dict(sorted(warnings_counter.items())),
        "sqlite_fts_enabled": sqlite_fts,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="整理手动复制的 r/HeartAttack Reddit 列表文本，导出五列 CSV。"
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=str(DEFAULT_INPUT),
        help="输入文本文件，默认：脚本同目录的 subreddit.txt",
    )
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUTPUT),
        help="输出目录，默认：项目 scraper/output/reddit",
    )
    parser.add_argument(
        "--prefix",
        default="heartattack_posts",
        help="输出文件名前缀，默认：heartattack_posts",
    )
    parser.add_argument(
        "--subreddit",
        default="r/HeartAttack",
        help="这批复制数据所属的列表/社区名，默认：r/HeartAttack",
    )
    parser.add_argument(
        "--reference-time",
        default=None,
        help=(
            "相对时间的倒推基准，ISO 格式；默认使用输入 TXT 的最后修改时间。"
        ),
    )
    parser.add_argument(
        "--detailed-outputs",
        action="store_true",
        help="另外保留 JSONL、详细 CSV、SQLite 和摘要文件。",
    )
    return parser.parse_args()


def resolve_reference_time(input_path: Path, override: str | None) -> datetime:
    if override:
        value = override.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise SystemExit(f"Invalid --reference-time value: {override}") from exc
        return parsed.astimezone() if parsed.tzinfo else parsed
    return datetime.fromtimestamp(input_path.stat().st_mtime).astimezone()


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    text = read_text(input_path)
    cards = split_cards(text)
    records = [
        parse_card(card, index, input_path, args.subreddit)
        for index, card in enumerate(cards, start=1)
    ]

    reference_time = resolve_reference_time(input_path, args.reference_time)

    csv_path = out_dir / f"{args.prefix}.csv"
    write_content_csv(records, csv_path, reference_time)

    print(f"Parsed {len(records)} records from {input_path}")
    print(pii.format_report(PII_STATS, "reddit"))
    print(f"Reference time: {reference_time.isoformat()}")
    print(f"CSV: {csv_path}")

    if args.detailed_outputs:
        jsonl_path = out_dir / f"{args.prefix}.jsonl"
        detailed_csv_path = out_dir / f"{args.prefix}_detailed.csv"
        sqlite_path = out_dir / f"{args.prefix}.sqlite"
        summary_path = out_dir / f"{args.prefix}_summary.json"

        write_jsonl(records, jsonl_path)
        write_detailed_csv(records, detailed_csv_path)
        sqlite_fts = write_sqlite(records, sqlite_path)

        summary = build_summary(records, input_path, sqlite_fts)
        summary["reference_time"] = reference_time.isoformat()
        summary["outputs"] = {
            "jsonl": str(jsonl_path.resolve()),
            "csv": str(csv_path.resolve()),
            "detailed_csv": str(detailed_csv_path.resolve()),
            "sqlite": str(sqlite_path.resolve()),
            "summary": str(summary_path.resolve()),
        }
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(f"Detailed CSV: {detailed_csv_path}")
        print(f"JSONL: {jsonl_path}")
        print(f"SQLite: {sqlite_path}")
        print(f"Summary: {summary_path}")


if __name__ == "__main__":
    main()
