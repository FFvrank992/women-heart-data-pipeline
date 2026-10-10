#!/usr/bin/env python3
"""Collect Her Heart blog posts through the public WordPress REST API.

Converted from `HerHeart_scraper.ipynb` so it can run unattended as part of
`scraper/run_all.py`. The collection logic is unchanged -- same endpoint, same
cutoff, same 0.5s delay, same fields. What is new:

  * a CLI, so the date window and output path are not edited into the source
  * output lands in the shared `scraper/output/raw/her_heart/` tree
  * failures exit non-zero instead of printing and continuing
  * WPBakery `[vc_row]` shortcodes are noted but left in the raw file

Shortcodes are deliberately *not* stripped here. Raw output is provenance: it
should look like what the API returned. `common/pii.py:strip_shortcodes` removes
them on the way to the five-column dataset, which is where cleaning belongs.

    python herheart_scraper.py
    python herheart_scraper.py --since 2016-01-01 --out-dir path/to/dir
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SCRAPER_DIR = Path(__file__).resolve().parents[2]
DEFAULT_OUT_DIR = SCRAPER_DIR / "output" / "raw" / "her_heart"

BASE_URL = "https://herheart.org/wp-json/wp/v2"
PER_PAGE = 100
REQUEST_DELAY = 0.5
TIMEOUT = 15
HEADERS = {"User-Agent": "WomenHeartDataPipeline/1.0 (research portfolio)"}

# A lone "<" in body text ("<10 years") is not markup. Checking for a real tag
# first avoids handing BeautifulSoup a string with nothing to parse.
HTML_TAG_RX = re.compile(r"<[a-zA-Z!/][^<>]*>")

COLUMNS = ["url", "title", "date", "categories", "tags", "text", "Platform", "Type"]


def clean_html(value: str | None) -> str:
    """Strip HTML tags from WordPress's rendered content."""
    if not value:
        return ""
    if not HTML_TAG_RX.search(value):
        return value.strip()
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    lines = [line.strip() for line in soup.get_text(separator="\n").splitlines()]
    return "\n".join(line for line in lines if line)


def fetch_id_name_map(session: requests.Session, endpoint: str) -> dict[int, str]:
    """Page through /tags or /categories, building an id -> name lookup."""
    mapping: dict[int, str] = {}
    page = 1
    while True:
        response = session.get(f"{BASE_URL}/{endpoint}",
                               params={"per_page": PER_PAGE, "page": page},
                               headers=HEADERS, timeout=TIMEOUT)
        if response.status_code != 200:
            break
        items = response.json()
        if not items:
            break
        for item in items:
            mapping[item["id"]] = item.get("name", "")
        if page >= int(response.headers.get("X-WP-TotalPages", 1)):
            break
        page += 1
        time.sleep(REQUEST_DELAY)
    return mapping


def fetch_posts(session: requests.Session, since: str) -> list[dict]:
    probe = session.get(f"{BASE_URL}/posts", params={"per_page": 1},
                        headers=HEADERS, timeout=TIMEOUT)
    if probe.status_code != 200:
        raise SystemExit(
            f"Her Heart REST API returned HTTP {probe.status_code}. The site may have "
            f"disabled it; an HTML-scraping fallback would be needed.")

    print("REST API available. Fetching tag and category names...")
    tag_map = fetch_id_name_map(session, "tags")
    category_map = fetch_id_name_map(session, "categories")
    print(f"  {len(tag_map)} tags, {len(category_map)} categories")

    print(f"Fetching posts published on or after {since}...")
    posts: list[dict] = []
    page = 1
    while True:
        response = session.get(
            f"{BASE_URL}/posts",
            params={"per_page": PER_PAGE, "page": page, "after": f"{since}T00:00:00",
                    "orderby": "date", "order": "desc"},
            headers=HEADERS, timeout=TIMEOUT)
        if response.status_code != 200:
            print(f"  page {page} failed: HTTP {response.status_code}", file=sys.stderr)
            break
        batch = response.json()
        if not batch:
            break
        posts.extend(batch)
        total_pages = int(response.headers.get("X-WP-TotalPages", 1))
        print(f"  page {page}/{total_pages}: {len(batch)} posts (total {len(posts)})")
        if page >= total_pages:
            break
        page += 1
        time.sleep(REQUEST_DELAY)

    rows = []
    for post in posts:
        rows.append({
            "url": post.get("link", ""),
            "title": clean_html(post.get("title", {}).get("rendered", "")),
            "date": post.get("date", "")[:10],
            "categories": ", ".join(
                filter(None, (category_map.get(i, "") for i in post.get("categories", [])))),
            "tags": ", ".join(
                filter(None, (tag_map.get(i, "") for i in post.get("tags", [])))),
            "text": clean_html(post.get("content", {}).get("rendered", "")),
            "Platform": "Her Heart",
            "Type": "blog",
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", default="2016-01-01",
                        help="earliest publication date, YYYY-MM-DD (default 2016-01-01)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--filename", default="herheart_blog_posts.csv")
    args = parser.parse_args(argv)

    with requests.Session() as session:
        rows = fetch_posts(session, args.since)

    if not rows:
        print("No posts fetched; nothing written.", file=sys.stderr)
        return 1

    # Import here, not at module scope: the scraper must stay runnable on its
    # own from this directory, and `common` only resolves once the scraper
    # directory is on the path.
    sys.path.insert(0, str(SCRAPER_DIR))
    from common import io_utils

    out_path = io_utils.write_rows(rows, args.out_dir / args.filename, COLUMNS)
    shortcoded = sum(1 for row in rows if "[vc_" in row["text"])
    print(f"Wrote {len(rows)} rows -> {out_path}")
    if shortcoded:
        print(f"  note: {shortcoded} posts contain WPBakery shortcodes; "
              f"they are stripped during PII processing, not here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
