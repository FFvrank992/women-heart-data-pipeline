# Inspire Scraper

Scrapes heart-health discussion from the [WomenHeart](https://www.inspire.com/groups/womenheart/)
group on Inspire.com and emits a flat **five-column CSV** ready to load into the shared
database.

Everything already collected ships with the pipeline as a crawl record, so **nothing is
ever crawled twice**. A run picks up only threads that are genuinely new.

---

## The five columns

This is the contract. Every CSV in `data/processed/` has exactly these columns, in this
order.

| Column | Values | Notes |
|---|---|---|
| `Date` | `2016-07-01` | Publication date, day precision. Rows are ordered by the full timestamp, so ordering within a day is still correct. |
| `Type` | `post` or `comment` | `comment` is what the scraper internally calls a reply — renamed to match the shared schema. |
| `Platform` | `Inspire` | Constant. |
| `URL` | thread address | Comments have no URL of their own and carry their thread's URL. **Equal URL means same thread** — it is the join key in this flat file. |
| `Content` | cleaned body text | Mojibake repaired, Unicode normalized, and PII replaced with placeholders by the shared engine. |

Files are UTF-8 with BOM (`utf-8-sig`), which is what Excel expects. In pandas:

```python
import glob, pandas as pd
df = pd.concat([pd.read_csv(f, encoding="utf-8-sig")
                for f in glob.glob("data/processed/*.csv")], ignore_index=True)
```

---

## Quick start

```bash
pip install -r requirements.txt

python pipeline.py status     # what is on disk right now
python pipeline.py run        # crawl anything new, rebuild the dataset
```

`run` skips the export step when nothing new was found, so repeating it is cheap
(~20 seconds, almost all of it spent reading the sitemap).

### Commands

| Command | Effect |
|---|---|
| `python pipeline.py run` | Crawl new threads, then rebuild the dataset if anything was added |
| `python pipeline.py run --force-export` | Rebuild the dataset even when nothing new was found |
| `python pipeline.py crawl` | Crawl only |
| `python pipeline.py export` | Rebuild the dataset from the raw CSVs only (no network) |
| `python pipeline.py status` | Crawl record size, row counts, file timestamps |

Useful options: `--target N` stops after N new threads (handy for a smoke test),
`--from` / `--to` set the dataset's date range, `--quiet` logs to file without printing.

---

## What is on disk

```
inspire_scraper/
├── pipeline.py                            entry point
├── src/
│   ├── scraper.py                         sitemap enumeration, reply pagination, extraction
│   ├── clean.py                           superseded -- forwards to scraper/common/pii.py
│   └── export.py                          flatten to the five columns
└── data/
    ├── state/crawled_urls.csv             ← crawl record. Ships pre-seeded. Do not delete.
    ├── raw/inspire_raw_posts.csv          ← raw scrape, all fields, append-only
    │   inspire_raw_replies.csv
    └── processed/
        ├── inspire_dataset_history.csv    ← delivered history, 12,850 rows
        └── inspire_dataset.csv            ← this installation's own crawls
```

**Load every `*.csv` in `data/processed/`.** They share the five-column schema. The
pipeline only ever writes `inspire_dataset.csv`; the history file is never touched.

`data/raw/` holds the full scraped records (author, topic, timestamps, reply counts, and
so on) and exists for provenance and so the dataset can be rebuilt without re-crawling.
It starts empty — only threads this installation crawls itself land there. **It is
gitignored and must not be shared**: raw records are not PII-redacted, which is what
makes it possible to re-apply an improved redaction policy later without re-crawling.

`inspire_dataset_history.csv` **is** redacted, by `scraper/common/pii.py`, and is
committed. It previously carried 3,331 personal names and 24 @handles in clear text —
patient narratives such as "my name is …" — because the original `clean.py` only
handled links, emails and phone numbers. Re-running the redaction over it is a no-op,
so the pipeline stays idempotent.

---

## The crawl record

`data/state/crawled_urls.csv` lists every thread URL already collected, with the reply
count and when it was crawled. The crawler reads it at startup and skips those URLs.

It ships pre-seeded with **12,488 threads / 103,625 replies** spanning 2006–2026 — the
full existing corpus. That is why a fresh checkout has empty raw CSVs but still does not
re-crawl anything: the history is delivered directly as
`inspire_dataset_history.csv` (2016 onward, 1,931 posts + 10,919 comments = 12,850 rows).

Seeded rows all carry the same `crawled_at` timestamp, since the original run recorded
one date rather than a per-thread time.

> Deleting this file triggers a full re-crawl of all 12,488 threads — roughly **13 hours**
> at the built-in request rate. Keep it.

---

## Incremental behaviour

**New threads are discovered; new comments on old threads are not.**

Once a thread is in the record it is never revisited, so a comment added later to an
existing thread will not appear. This is deliberate: measured over 30 days, only 3 threads
saw any new activity, and the group averages about 2 new threads per month — not worth
tracking per-thread modification times for.

Over the trailing 12 months the group added about 20 threads and 107 replies, roughly
**0.07%** of the corpus. Running this once a quarter is plenty; there is no value in
scheduling it more often.

---

## Date range

The dataset covers **2016-01-01 onward by default**, matching what was previously shared
with the team.

```bash
python pipeline.py export                       # 2016 onward (default)
python pipeline.py export --from all            # everything back to 2006
python pipeline.py export --from 2020-01-01 --to 2024-12-31
```

Only threads whose **original post** falls in range are kept, along with all of their
comments. That guarantees every comment has its parent post in the same file, so no
comment ends up without the message it replies to.

Note that `--from all` widens only what this installation crawled itself. The delivered
history file is 2016+; if you need pre-2016 rows as well, ask me for the raw corpus
rather than re-crawling.

---

## PII

Cleaning now runs through **`scraper/common/pii.py`**, the engine shared by all
six platforms in this project, rather than this package's own `clean.py`.

`clean.py` grew here first and became the basis for that engine -- the mojibake
table, the Unicode normalisation and the `[URL:domain]` placeholder style all
came from here unchanged. What the shared engine adds for Inspire is **personal
names** (`[NAME]`, via self-introduction / greeting / sign-off / dictionary
tiers) and **@handles** (`[USER]`), neither of which `clean.py` handled. On the
delivered corpus that is about 6,800 names and 48 handles that previously went
into the shared CSV in the clear.

`clean.py` remains as a forwarding shim so a standalone checkout of this scraper
keeps working. New code should import `common.pii`.

Cited clinicians (`Dr. ...`, `..., MD`) and allowlisted bylines are deliberately
kept -- see [the scraper README](../../README.md#pii).

## Two things worth knowing

1. **`Date` is the site's rendered local time and the timezone is unconfirmed** — it is
   not known whether the baseline is UTC or US Eastern. Fine for date-level grouping;
   don't build hour-of-day analysis on it without pinning that down.

2. **The crawler is rate-limited on purpose** — single-threaded with a 1.5–3.0 second
   delay between requests (~18 requests/minute), entering through the site's sitemap.
   Please don't parallelise it or remove the delays.

---

## Interrupt safety

Each thread is written to disk the moment it finishes — raw rows first, then the record
line. Ctrl+C or a crash loses at most the thread in flight, and re-running the same
command resumes from the record. If the process dies between the two writes, that thread
is simply crawled again next time and the duplicate is dropped at export (rows are
deduplicated by id).

A `.pipeline.lock` file prevents two instances running at once; a stale lock left by a
crashed process is detected and taken over automatically.
