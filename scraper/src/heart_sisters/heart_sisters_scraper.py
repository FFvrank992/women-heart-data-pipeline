"""Collect Heart Sisters blog posts and reader comments into team CSV files.

The script uses the public WordPress.com REST API and requires no API key.
It intentionally requests no author profile fields for comments.

Both blog bodies and comment text are redacted through `scraper/common/pii.py`,
the shared engine every platform in this project uses. The output is
de-identified, not anonymised: ages, locations and other indirect identifiers
remain in patient narratives.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


API_BASE = "https://public-api.wordpress.com/rest/v1.1/sites/myheartsisters.org"
PLATFORM = "Heart Sisters"
CSV_FIELDS = ["Date", "Type", "Platform", "URL", "Content"]
DEFAULT_START_DATE = "2016-01-01"

# Output goes to the shared raw tree so `scraper/run_all.py` finds it without
# being told where to look. It used to default to `heart_sisters_output/`
# relative to the working directory, which put the CSVs somewhere different
# depending on where the command was run from.
SCRAPER_DIR = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = SCRAPER_DIR / "output" / "raw" / "heart_sisters"

sys.path.insert(0, str(SCRAPER_DIR))
from common import pii  # noqa: E402  (path must be set first)

_PII_STATS = pii.Stats()

DIRECT_HEALTH_PATTERNS = [
    r"\bheart attacks?\b",
    r"\bheart disease\b",
    r"\bheart failure\b",
    r"\bcardiac\b|\bcardio rehab\b",
    r"\bcardiolog(?:y|ist|ists)\b",
    r"\bcardiovascular\b",
    r"\bcoronary\b",
    r"\barter(?:y|ies|ial)\b",
    r"\bSCAD\b|spontaneous coronary artery dissection",
    r"\bmyocardial\b",
    r"\bangina\b",
    r"\barrhythm\w*\b",
    r"\bpalpitations?\b",
    r"\bchest pain\b",
    r"\bshortness of breath\b|\bbreathless(?:ness)?\b",
    r"\bblood pressure\b",
    r"\bcholesterol\b",
    r"\bstroke\b",
    r"\btroponin\b",
    r"\bstents?\b|\bstented\b",
    r"\bbypass\b",
    r"\bangiogram\b|\bangioplasty\b",
    r"\bcatheter(?:s|ization)?\b|\bcath(?: lab)?\b",
    r"\bradial\b|\bTRI\b",
    r"\bEP docs?\b|\belectrophysiolog\w*\b",
    r"\bECG\b|\bEKG\b|\bechocardiogram\b",
    r"\bstatins?\b|\baspirin\b|\bnitro(?:glycerin)?\b|\bbeta blockers?\b",
]
WOMEN_HEALTH_PATTERNS = [
    r"\bwom[ae]n\b",
    r"\bfemale\b",
    r"\bperi-?menopause\b|\bmenopaus\w*\b",
    r"\bpregnan(?:t|cy)\b|\bpostpartum\b|\bperipartum\b",
    r"\bpre-?eclampsia\b|\bPCOS\b",
    r"\bestrogen\b|\boestrogen\b|\bhysterectomy\b",
]
CARE_PATTERNS = [
    r"\bdoctor\b|\bphysician\b|\bGP\b",
    r"\bhospital\b|\bemergency\b|\bER\b|\bED\b",
    r"\bdiagnos\w*\b|\bsymptoms?\b",
    r"\btreat(?:ed|ment|ments)?\b|\bmedicat\w*\b",
    r"\bsurger(?:y|ies)\b|\boperation\b|\bpatients?\b",
]
PERSONAL_PATTERNS = [
    r"\bI\b|\bI'm\b|\bI've\b|\bI'd\b|\bme\b|\bmy\b|\bmine\b|\bwe\b|\bour\b",
    r"\bdiagnosed\b|\bexperienced\b|\bsuffer(?:ed|ing)?\b|\bsurviv(?:ed|or)\b",
]
OFF_TOPIC_PATTERNS = [
    r"\bdisability\b|\bdisabled\b|\bgovernment help\b",
    r"\breblogged\b|\bfacebook\b|\btwitter\b",
]


class TextExtractor(HTMLParser):
    """Convert WordPress HTML to readable plain text."""

    BLOCK_TAGS = {
        "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li",
        "blockquote", "tr", "section", "article",
    }
    SKIP_TAGS = {"script", "style", "iframe"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
        elif not self.skip_depth and (tag in self.BLOCK_TAGS or tag == "br"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif not self.skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        value = "".join(self.parts).replace("\xa0", " ")
        value = re.sub(r"[^\S\n]+", " ", value)
        lines = [line.strip() for line in value.splitlines()]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


@dataclass
class Stats:
    posts_seen: int = 0
    blogs_written: int = 0
    comments_seen: int = 0
    comments_written: int = 0
    comments_short_rejected: int = 0
    comments_date_rejected: int = 0
    comments_relevance_rejected: int = 0
    duplicates_removed: int = 0
    requests: int = 0
    request_urls: list[str] = field(default_factory=list)


class ApiClient:
    def __init__(self, raw_dir: Path, delay: float, timeout: float, retries: int) -> None:
        self.raw_dir = raw_dir
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.request_number = 0
        self.urls: list[str] = []

    def get_json(self, endpoint: str, params: dict[str, Any], raw_name: str) -> dict[str, Any]:
        url = f"{API_BASE}{endpoint}?{urlencode(params)}"
        if urlsplit(url).hostname != "public-api.wordpress.com":
            raise ValueError(f"Refusing unexpected API host: {url}")
        request = Request(url, headers={"User-Agent": "WomenHeartDataPipeline/1.0"})
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                if self.request_number and self.delay:
                    time.sleep(self.delay)
                with urlopen(request, timeout=self.timeout) as response:
                    payload = response.read().decode("utf-8")
                data = json.loads(payload)
                if not isinstance(data, dict):
                    raise ValueError(f"Expected JSON object from {url}")
                self.request_number += 1
                self.urls.append(url)
                self.raw_dir.mkdir(parents=True, exist_ok=True)
                (self.raw_dir / raw_name).write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                return data
            except HTTPError as exc:
                last_error = exc
                if exc.code != 429 and exc.code < 500:
                    break
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < self.retries:
                time.sleep(2 ** attempt)
        raise RuntimeError(f"Heart Sisters API request failed for {url}: {last_error}")


def html_to_text(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("rendered") or value.get("raw") or ""
    parser = TextExtractor()
    parser.feed(str(value or ""))
    parser.close()
    return parser.text()


def redact_contact_pii(text: str) -> str:
    """Redact one document through the shared PII engine.

    This was four local regexes emitting `[email]`/`[link]`/`@user`/`[phone]`,
    a vocabulary no other scraper used, and it was applied to comments only --
    blog bodies went out untouched, carrying 452 `@handles` and 33 links into
    the shared CSV. Both problems are fixed by routing through
    `common/pii.py`: one vocabulary, and `blog_row` now calls this too.

    `common.pii` is pure standard library, so this module stays importable
    without pandas and its offline test suite still runs.
    """
    return pii.redact(text, _PII_STATS)


def date_part(value: Any) -> str:
    match = re.match(r"(\d{4}-\d{2}-\d{2})", str(value or ""))
    return match.group(1) if match else ""


def in_date_window(value: str, start: str, end: str) -> bool:
    return bool(value and start <= value <= end)


def any_match(text: str, patterns: Iterable[str]) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def is_relevant_comment(text: str) -> bool:
    direct_health = any_match(text, DIRECT_HEALTH_PATTERNS)
    women_health = any_match(text, WOMEN_HEALTH_PATTERNS)
    care = any_match(text, CARE_PATTERNS)
    personal = any_match(text, PERSONAL_PATTERNS)
    heart_with_context = bool(
        re.search(
            r"\bheart (attack|disease|failure|rate|rhythm|surgery|condition|problem|health|symptoms?)",
            text,
            re.IGNORECASE,
        )
    )
    eligible = (direct_health or heart_with_context or (women_health and care)) and (personal or care)
    if any_match(text, OFF_TOPIC_PATTERNS) and not (direct_health or heart_with_context):
        return False
    return eligible


def fetch_posts(
    client: ApiClient,
    start: str,
    end: str,
    max_posts: int,
) -> list[dict[str, Any]]:
    posts: dict[str, dict[str, Any]] = {}
    page = 1
    while True:
        data = client.get_json(
            "/posts/",
            {
                "number": 100,
                "page": page,
                "order": "ASC",
                "order_by": "date",
                "after": f"{start}T00:00:00Z",
                "before": f"{end}T23:59:59Z",
                "type": "post",
                "status": "publish",
                "fields": "ID,URL,title,content,date,comment_count",
            },
            f"posts_page_{page:03d}.json",
        )
        batch = [item for item in data.get("posts", []) if isinstance(item, dict)]
        for post in batch:
            post_id = str(post.get("ID") or "")
            if post_id:
                posts[post_id] = post
                if max_posts and len(posts) >= max_posts:
                    return list(posts.values())
        if not batch or len(posts) >= int(data.get("found") or len(posts)):
            break
        page += 1
    return list(posts.values())


def blog_row(post: dict[str, Any], start: str, end: str) -> dict[str, str] | None:
    published = date_part(post.get("date"))
    url = str(post.get("URL") or "").strip()
    if not in_date_window(published, start, end):
        return None
    if urlsplit(url).hostname not in {"myheartsisters.org", "www.myheartsisters.org"}:
        raise ValueError(f"Unexpected Heart Sisters post URL: {url}")
    title = html_to_text(post.get("title"))
    body = html_to_text(post.get("content"))
    if not title or not body:
        return None
    return {
        "Date": published,
        "Type": "blog",
        "Platform": PLATFORM,
        "URL": url,
        # Blog bodies are redacted exactly like comment bodies. They
        # were not, until now: the byline handle and every link in
        # 502 posts went straight into the shared CSV.
        "Content": redact_contact_pii(f"{title}\n\n{body}"),
    }


def fetch_comment_rows(
    client: ApiClient,
    post: dict[str, Any],
    start: str,
    end: str,
    min_chars: int,
    max_comments_per_post: int,
    stats: Stats,
) -> list[dict[str, str]]:
    post_id = str(post.get("ID") or "")
    post_url = str(post.get("URL") or "").strip()
    rows: list[dict[str, str]] = []
    received = 0
    page = 1
    while True:
        page_size = 100 if not max_comments_per_post else min(100, max_comments_per_post)
        data = client.get_json(
            f"/posts/{post_id}/replies/",
            {
                "number": page_size,
                "page": page,
                "status": "approved",
                "type": "comment",
                "author_wpcom_data": "false",
                "fields": "ID,post,date,content,URL,parent",
            },
            f"comments_post_{post_id}_page_{page:03d}.json",
        )
        comments = [item for item in data.get("comments", []) if isinstance(item, dict)]
        for comment in comments:
            stats.comments_seen += 1
            received += 1
            text = redact_contact_pii(html_to_text(comment.get("content")))
            published = date_part(comment.get("date") or post.get("date"))
            if len(text) < min_chars:
                stats.comments_short_rejected += 1
                continue
            if not in_date_window(published, start, end):
                stats.comments_date_rejected += 1
                continue
            if not is_relevant_comment(text):
                stats.comments_relevance_rejected += 1
                continue
            comment_id = str(comment.get("ID") or "").strip()
            url = str(comment.get("URL") or "").strip()
            if not url:
                url = f"{post_url.rstrip('/')}#comment-{comment_id}" if comment_id else post_url
            rows.append(
                {
                    "Date": published,
                    "Type": "comment",
                    "Platform": PLATFORM,
                    "URL": url,
                    "Content": text,
                }
            )
            if max_comments_per_post and received >= max_comments_per_post:
                break
        if (
            not comments
            or len(comments) < page_size
            or (max_comments_per_post and received >= max_comments_per_post)
        ):
            break
        page += 1
    return rows


def dedupe(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    unique: dict[str, dict[str, str]] = {}
    for row in rows:
        value = "\x1f".join(row[field].strip().lower() for field in CSV_FIELDS)
        key = hashlib.sha256(value.encode("utf-8")).hexdigest()
        unique.setdefault(key, row)
    result = sorted(unique.values(), key=lambda row: (row["Date"], row["URL"], row["Content"]))
    return result, len(rows) - len(result)


def write_csv(path: Path, rows: list[dict[str, str]], overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"Output already exists: {path}. Use --overwrite to replace it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    with temporary.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != CSV_FIELDS:
            raise ValueError(f"Unexpected CSV columns: {reader.fieldnames}")
        list(reader)
    temporary.replace(path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default=DEFAULT_START_DATE)
    parser.add_argument("--end-date", default=date.today().isoformat())
    parser.add_argument("--include", choices=["both", "blogs", "comments"], default="both")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--max-posts", type=int, default=0, help="0 means all posts")
    parser.add_argument("--max-comments-per-post", type=int, default=0, help="0 means all comments")
    parser.add_argument("--min-comment-chars", type=int, default=40)
    parser.add_argument("--request-delay", type=float, default=0.1)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    date.fromisoformat(args.start_date)
    date.fromisoformat(args.end_date)
    if args.end_date < args.start_date:
        parser.error("--end-date must be on or after --start-date")
    if min(args.max_posts, args.max_comments_per_post, args.request_delay, args.retries) < 0:
        parser.error("numeric limits cannot be negative")
    if args.min_comment_chars < 1 or args.timeout <= 0:
        parser.error("--min-comment-chars and --timeout must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir: Path = args.output_dir
    raw_dir = output_dir / "raw"
    client = ApiClient(raw_dir, args.request_delay, args.timeout, args.retries)
    stats = Stats()
    posts = fetch_posts(client, args.start_date, args.end_date, args.max_posts)
    stats.posts_seen = len(posts)

    blog_rows: list[dict[str, str]] = []
    comment_rows: list[dict[str, str]] = []
    if args.include in {"both", "blogs"}:
        blog_rows = [row for post in posts if (row := blog_row(post, args.start_date, args.end_date))]
        blog_rows, duplicates = dedupe(blog_rows)
        stats.duplicates_removed += duplicates
        stats.blogs_written = len(blog_rows)
    if args.include in {"both", "comments"}:
        for index, post in enumerate(posts, start=1):
            comment_rows.extend(
                fetch_comment_rows(
                    client,
                    post,
                    args.start_date,
                    args.end_date,
                    args.min_comment_chars,
                    args.max_comments_per_post,
                    stats,
                )
            )
            print(f"Comments: processed post {index}/{len(posts)}", flush=True)
        comment_rows, duplicates = dedupe(comment_rows)
        stats.duplicates_removed += duplicates
        stats.comments_written = len(comment_rows)

    outputs: dict[str, str] = {}
    if args.include in {"both", "blogs"}:
        path = output_dir / "heart_sisters_blogs_2016_present.csv"
        write_csv(path, blog_rows, args.overwrite)
        outputs["blogs"] = str(path.resolve())
    if args.include in {"both", "comments"}:
        path = output_dir / "heart_sisters_comments_2016_present.csv"
        write_csv(path, comment_rows, args.overwrite)
        outputs["comments"] = str(path.resolve())

    stats.requests = client.request_number
    stats.request_urls = client.urls
    manifest = {
        "source": PLATFORM,
        "api": API_BASE,
        "date_start": args.start_date,
        "date_end": args.end_date,
        "output_columns": CSV_FIELDS,
        "posts_seen": stats.posts_seen,
        "blogs_written": stats.blogs_written,
        "comments_seen": stats.comments_seen,
        "comments_written": stats.comments_written,
        "comments_short_rejected": stats.comments_short_rejected,
        "comments_date_rejected": stats.comments_date_rejected,
        "comments_relevance_rejected": stats.comments_relevance_rejected,
        "duplicates_removed": stats.duplicates_removed,
        "request_count": stats.requests,
        "outputs": outputs,
        "pii_note": (
            "Comment author fields are not requested. Blog bodies AND comment text are "
            "redacted through scraper/common/pii.py: emails, phone numbers, links, "
            "@handles and personal names are replaced with placeholders. Cited "
            "clinicians and the publication's own byline are allowlisted and kept. "
            "This is de-identification, not anonymisation: ages, locations and other "
            "indirect identifiers remain in patient narratives."
        ),
        "pii_counts": dict(_PII_STATS.counts),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
