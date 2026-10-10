# Her Heart scraper

Collects Her Heart blog posts through the public WordPress REST API at
`https://herheart.org/wp-json/wp/v2`. No API key required.

```bash
python herheart_scraper.py
python herheart_scraper.py --since 2016-01-01 --out-dir some/other/dir
```

Writes `scraper/output/raw/her_heart/herheart_blog_posts.csv` — about 208 posts
from 2016 onward, in under a minute. Normally you do not run it directly:
`scraper/run_all.py` calls it as part of the six-source pipeline.

## Notebook to script

`herheart_scraper.py` replaces `HerHeart_scraper.ipynb`. The collection logic is
unchanged — same endpoint, same 2016 cutoff, same 0.5s delay, same fields — but
the script can run unattended, which a notebook cannot.

The original notebook is intentionally omitted from this portfolio repository;
the script is the maintained, automation-safe implementation.

## Output

The CSV keeps the API's own shape, eight columns:

```
url, title, date, categories, tags, text, Platform, Type
```

This is *raw* output: it is what the API returned, deliberately including the
WPBakery `[vc_row]` page-builder shortcodes that appear in 29 of the 208 posts,
and it is **not PII-redacted**. Both are handled on the way to the five-column
dataset — `common/pii.py` strips the shortcodes and redacts the text, and
`common/schema.py` maps `text` → `Content`, `url` → `URL`, `date` → `Date`.

Keeping raw output unmodified is what lets the PII policy be improved and
re-applied later without re-crawling. It also means `scraper/output/raw/` must
never be shared — share `scraper/output/merged/`.

## Dates

The API returns ISO dates, so this scraper writes `YYYY-MM-DD` directly. Note
that some historical exports use `d/m/Y` instead
(`20/04/2026`), read day-first by `schema.normalise_date` because this is an
Australian source. If a future export ever switches to month-first, that
function is where to fix it.
