# Reddit TXT to CSV

Parses manually copied Reddit listing text with `parse_reddit_heartattack.py`.
Uses Python 3.10+ and the standard library only; no API credentials are needed.

## Current collection approach

For now, TXT parsing replaces automatic Reddit updates. The target subreddit
updates relatively slowly, so the team manually refreshes the TXT snapshot and
re-runs the parser when needed; this is not an automatically refreshed feed.

For this project's research use, the team is pursuing Reddit's official research
API approval as its compliant automated data-access route. The application has
already been submitted and is awaiting authorisation. Once approved, the team
will update the implementation to collect data through the authorised API while
retaining the shared five-column output format.

## Run

Place the source listing text in `subreddit.txt` beside the script. From the
repository root, run:

```powershell
python scraper/src/reddit/parse_reddit_heartattack.py --reference-time 2026-08-31T19:46:33.994569+10:00
```

The default output is `scraper/output/reddit/heartattack_posts.csv`, containing
exactly `Date,Type,Platform,URL,Content`. This location is scanned by
`scripts/csv_to_db.py`. Default paths are anchored to the script, so the command
also works from other working directories.

The command above pins the reference time for the committed snapshot, using the
original source file's modification time. Git does not preserve file modification
times. When replacing the TXT with a new capture, record its actual capture time
and update the command accordingly; running without `--reference-time` uses the
local input file's modification time instead.

- `Date`: estimated from the relative time in the text and the input file's
  modification time. Preserve that timestamp when copying the input, or specify
  `--reference-time 2026-08-31T19:46:33+10:00` using the actual capture time.
- `Type`: `Post`; the copied listing does not contain the full comment trees.
- `Platform`: `Reddit`.
- `URL`: synthetic title-slug URL without Reddit's real post ID; not a verified
  permalink.
- `Content`: the parsed title and body, PII-redacted.

The default community is `r/HeartAttack`; use `--subreddit r/NAME` for another
community. An explicit input path, `--out-dir`, and `--prefix` are supported.
Re-running overwrites the same output CSV. Keep `--detailed-outputs` outside
`scraper/output/` (via `--out-dir`) because its additional CSV does not follow
the five-column database contract.

The team explicitly includes `subreddit.txt` in Git for this handover. Generated
exports remain ignored.

## PII

Exported text runs through `scraper/common/pii.py`, the engine shared by all six
platforms: emails, phone numbers, links, `@handles` and personal names are
replaced with placeholders. The parser previously had **no PII handling at all**
-- post bodies went into the shared CSV with whatever names and contact details
their authors had typed.

Two limits specific to this source:

- **Author names are not collected**, because the copied listing stores them
  separately from the body text and only the body reaches `Content`.
- This is de-identification, not anonymisation. Ages, locations and other
  indirect identifiers remain. See
  [the scraper README](../../README.md#pii).

`subreddit.txt` itself is the unredacted capture and is committed for handover;
treat it accordingly.
