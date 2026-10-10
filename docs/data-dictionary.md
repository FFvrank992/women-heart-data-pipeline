# Data dictionary

## Unified collection output

| Field | Type | Description |
|---|---|---|
| `Date` | ISO date or blank | Publication date normalised to `YYYY-MM-DD` |
| `Type` | category | `post`, `comment`, or `blog` |
| `Platform` | category | Canonical source name |
| `URL` | string | Item or parent discussion URL |
| `Content` | string | De-identified source text |

## Training-ready output

| Field | Type | Description |
|---|---|---|
| `id` | string | Stable `<platform>_<16 hex>` content identifier |
| `Date` | ISO date or blank | Publication date |
| `Type` | category | Source record type |
| `Platform` | category | Canonical source name |
| `URL` | string | Provenance URL; keep controlled with the dataset |
| `Content` | string | Cleaned English text used by the model |
| `support_group` | string | Mayo support-group label when present |
| `n_redacted_names` | integer | Count of `[NAME]` placeholders removed |
| `has_emoji` | boolean | Whether the pre-cleaned text contained emoji |
| `content_len` | integer | Character length after text cleaning |
| `lang` | string | Deterministic language label; retained rows are English |
