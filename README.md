# Women’s Heart Health Data Pipeline

A portfolio-focused, end-to-end data pipeline that turns heterogeneous public
women’s heart-health discussions into a consistent, de-identified dataset ready
for model training.

This repository extracts the data-engineering work from a larger team platform.
My contribution was to unify six independently built collectors behind one data
contract, apply one auditable PII policy, provide a single orchestration entry
point, and build the downstream cleaning stage used to create the training data.

## What this project demonstrates

- **Source integration:** six collectors with different APIs, HTML structures,
  schemas, encodings, date formats and operational constraints.
- **Privacy by design:** one PII engine runs on every source before merged data is
  written, with typed placeholders and an audit report.
- **Data contracts:** strict five-column schema, canonical values, validation,
  deterministic ordering and source-aware de-duplication.
- **Operational safety:** atomic CSV writes, encoding detection, crawl guards,
  dependency pre-flight checks and isolated subprocesses.
- **Model-readiness:** deterministic text cleaning, language and relevance
  filtering, spam removal, feature generation and stable row IDs.

## Pipeline

```mermaid
flowchart LR
    A[Six source collectors] --> B[Source adapters]
    B --> C[Schema normalisation]
    C --> D[Shared PII redaction]
    D --> E[Validated five-column merge]
    E --> F[Model preprocessing]
    F --> G[Training-ready CSV]
```

| Stage | Main code | Output |
|---|---|---|
| Collect | `scraper/src/` | Source-native files in `scraper/output/raw/` |
| Standardise and protect | `scraper/common/` | One redacted CSV per source |
| Orchestrate and merge | `scraper/run_all.py` | Five-column merged snapshot + PII audit |
| Prepare for modelling | `src/data_preprocessing/prepare_dataset.py` | Cleaned feature table for training |

The six sources are Inspire (WomenHeart), Heart Sisters, Her Heart, Mayo Clinic
Connect, Reddit and YouTube. See [architecture](docs/architecture.md) for the
adapter design and [technical decisions](docs/technical-decisions.md) for the
trade-offs.

## Quick demo — no network or private data

The checked-in sample is fully synthetic. It exercises the same preprocessing
path as the real merged output.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python src/data_preprocessing/prepare_dataset.py \
  data/sample/merged_synthetic.csv --output-dir data/processed
```

Expected output:

```text
data/processed/merged_synthetic_sentiment.csv
```

## Run the collection layer

Install the optional collector dependencies, copy `.env.example` to `.env`, and
add a YouTube API key if YouTube is in scope.

```bash
pip install -r requirements-scrapers.txt
python scraper/run_all.py status
python scraper/run_all.py
```

Useful modes:

```bash
# Reprocess files already on disk without making network requests
python scraper/run_all.py --no-collect

# Restrict collection and processing to selected sources
python scraper/run_all.py --only heart_sisters her_heart youtube

# Opt in to the interactive, long-running Mayo collector
python scraper/run_all.py --include mayo
```

The merged contract is intentionally small:

| Column | Contract |
|---|---|
| `Date` | `YYYY-MM-DD` |
| `Type` | `post`, `comment`, or `blog` |
| `Platform` | One of the six canonical platform names |
| `URL` | Item URL, or parent thread/video URL when replies have no URL |
| `Content` | Normalised text with direct identifiers replaced |

## Model-ready output

`prepare_dataset.py` accepts either the five-column merged CSV directly or the
legacy seven-column database export. It adds stable IDs and features while
removing records that are too short, duplicated, non-English, off-topic, known
Mayo fragments, or promotional YouTube replies.

Output columns are documented in the [data dictionary](docs/data-dictionary.md).

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests cover PII edge cases, clinical-number false positives, schema aliases,
date parsing, source-aware de-duplication, both supported input formats, and the
end-to-end transformation of synthetic rows.

## Data and ethics

No real patient narrative, raw scrape, API credential, crawler state or full
training dataset is committed. De-identification removes direct identifiers but
does **not** guarantee anonymity: age, location, diagnosis date and hospital can
still identify a person in combination. See [data ethics](docs/data-ethics.md).

## Interview walkthrough

A concise explanation of the problem, design, difficult cases and results is in
[docs/interview-walkthrough.md](docs/interview-walkthrough.md).
