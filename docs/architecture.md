# Architecture

## Why an adapter layer

The six collectors were developed independently. They emitted different column
names, type vocabularies, encodings and date formats, and several had their own
partial privacy logic. Rewriting every collector would have coupled extraction
to downstream policy and made later changes expensive.

Instead, `scraper/common/sources.py` treats each collector as an adapter. A
`Source` entry declares where files are found, how native columns map to the
contract, how collection is invoked, which dependencies are required and which
operational safeguards apply. `harvest()` then gives every source the exact same
path:

```text
read -> map columns -> normalise values -> redact PII -> drop empty rows
     -> source-aware dedupe -> validate -> atomic write
```

Adding another source is therefore primarily a registry change, not a rewrite
of the privacy or merge pipeline.

## Important boundaries

1. **Raw boundary** — source-native data is retained locally for reproducibility
   but is private and gitignored.
2. **Processed boundary** — every row has the five-column contract and has passed
   the shared PII engine.
3. **Merged boundary** — all sources are de-duplicated, sorted and validated.
4. **Training boundary** — text-only noise is removed and modelling features are
   added without changing the source record identity.

## Source-specific operational constraints

| Source | Collection method | Constraint |
|---|---|---|
| Inspire | Rate-limited sitemap crawl | Crawl-state guard prevents an accidental ~13-hour rebuild |
| Heart Sisters | Public WordPress.com API | Full pull takes tens of minutes |
| Her Heart | Public WordPress REST API | Shortcodes are cleaned after collection, not in raw provenance |
| Mayo Clinic Connect | Browser automation | Interactive verification; opt-in only |
| Reddit | Offline snapshot parser | Temporary until approved research API access |
| YouTube | Official Data API | API key and daily quota required |

## Failure isolation

Collectors run as subprocesses because their dependencies and side effects are
different. One failing collector cannot corrupt the orchestrator process. The
pipeline can still process existing files for that source, while returning a
non-zero status so partial freshness is visible to automation.
