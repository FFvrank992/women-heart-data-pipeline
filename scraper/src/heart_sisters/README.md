# Heart Sisters scraper

Collects public Heart Sisters blog posts and reader comments through the public
WordPress.com REST API. No API key is required.

## Output contract

The scraper writes separate blog and comment CSV files using the exact team schema:

```text
Date,Type,Platform,URL,Content
```

- `Date`: publication date in `YYYY-MM-DD` format
- `Type`: `blogs` or `comments`
- `Platform`: `Heart Sisters`
- `URL`: public post or comment URL
- `Content`: complete blog text or cleaned comment text

The default date window starts at `2016-01-01` and ends on the day the script runs.

## Run a small live test

From the repository root:

```powershell
python scraper/src/heart_sisters/heart_sisters_scraper.py --max-posts 2 --max-comments-per-post 5 --overwrite
```

## Collect the complete period

```powershell
python scraper/src/heart_sisters/heart_sisters_scraper.py --overwrite
```

To use a fixed end date:

```powershell
python scraper/src/heart_sisters/heart_sisters_scraper.py --start-date 2016-01-01 --end-date 2026-09-11 --overwrite
```

The default output folder is `scraper/output/raw/heart_sisters/`, so
`scraper/run_all.py` finds it without being told. It contains:

- `heart_sisters_blogs_2016_present.csv`
- `heart_sisters_comments_2016_present.csv`
- `manifest.json` with collection and filtering counts
- `raw/` with the API responses used to produce the CSV files

Generated output and raw data should not be committed to Git.

## Run tests

The scraper uses only the Python standard library. Run the offline tests from the
repository root:

```powershell
python scraper/src/heart_sisters/test_heart_sisters_scraper.py
```

## Data handling

Comment author profile fields are deliberately excluded from API requests.

**Blog bodies and comment text both** run through `scraper/common/pii.py`, the
engine shared by all six platforms: emails, phone numbers, links, `@handles` and
personal names are replaced with placeholders. Blog bodies previously bypassed
redaction entirely, carrying 452 `@handles` and 33 links into the shared CSV;
`test_heart_sisters_scraper.py` now guards against that regression.

Cited clinicians (`Dr. ...`, `..., MD`) and the publication's own byline
(`Carolyn Thomas`, `@HeartSisters`) are deliberately kept -- see
[the repository privacy notes](../../../docs/data-ethics.md).

The output is de-identified, not anonymised: locations, ages and other indirect
identifiers may remain. Comment-level patient narratives should not be publicly
displayed unless the project's ethics approval permits it.
