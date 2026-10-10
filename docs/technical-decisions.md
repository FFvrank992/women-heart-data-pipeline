# Technical decisions

## One privacy engine after collection

PII handling belongs at the common boundary, not in six collector
implementations. This guarantees that every source receives the same policy and
that a policy update can be audited in one file. Redaction is idempotent because
historical files may already contain older placeholder formats.

## De-identification rather than aggressive anonymisation

The pipeline removes direct identifiers such as personal names, email addresses,
phone numbers, handles, street addresses, dates of birth and medical-record
numbers. It retains clinically meaningful ages and locations and retains cited
clinicians in their professional capacity. That choice preserves research value
but requires the output to remain controlled data.

## Regex plus context and dictionaries for names

The name detector combines self-introduction, greeting and sign-off context with
a curated first-name list. A separate ambiguity list protects clinical and common
words such as `LAD`, `May`, `Will` and `Victoria`. This is lightweight and
auditable, but it has lower recall for rare or non-Anglophone names than a trained
NER model. The limitation is explicit rather than hidden.

## Source-aware de-duplication

A shared URL does not always identify a shared row. Inspire comments share a
thread URL and YouTube comments share a video URL, so URL-only de-duplication
would delete valid discussion. URL-fragment collapsing is therefore limited to
sources where the URL identifies one item; exact content de-duplication remains
global.

## Deterministic, safe output

- Language detection uses a fixed seed.
- Rows are sorted before export.
- IDs use a stable hash of URL and content.
- CSV is written through a temporary file and atomically replaced.
- Input encoding is detected from a small known candidate set to handle Excel
  round-trips without silently corrupting text.
