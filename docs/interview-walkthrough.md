# Interview walkthrough

## 30-second summary

“The original project had six independently developed collectors. They disagreed
on schemas, encodings, date formats and PII handling, so I designed a shared
adapter layer and a strict five-column contract. Every source now passes through
one idempotent PII engine, source-aware de-duplication and validation, all behind
one orchestration command. I then built a deterministic cleaning stage that
turns the merged result directly into model-ready data.”

## Suggested five-minute flow

1. Start with the diagram in the main README and frame the problem as integration,
   privacy and reproducibility—not “six scraping scripts.”
2. Open `scraper/common/sources.py` and explain why registry-driven adapters make
   a seventh source cheap to add.
3. Open `scraper/common/pii.py` and show the ordered redaction pipeline, especially
   why URLs are handled before emails and why clinical false positives matter.
4. Open `scraper/common/schema.py` and discuss canonical types, date parsing and
   why URL-only de-duplication is unsafe for threaded sources.
5. Open `scraper/run_all.py` and explain subprocess isolation, crawl guards,
   atomic output and the audit report.
6. Finish with `src/data_preprocessing/prepare_dataset.py`: deterministic language
   detection, noise/relevance rules, stable IDs and the training schema.

## Strong technical examples

- A generic phone regex can damage clinical values such as blood pressure,
  dosages and troponin measurements; regression tests protect these cases.
- The word `LAD` can be a coronary artery, `May` a month, and `Will` an ordinary
  verb. A naive name dictionary would erase domain meaning.
- A URL is not always a row key: all comments may share the parent thread/video
  URL. De-duplication rules must reflect source semantics.
- Raw provenance and clean training text serve different purposes. The pipeline
  preserves the former privately while making the latter reproducible.

## Honest limitations

- Regex/dictionary name detection is auditable and lightweight but not perfect.
- De-identification does not remove indirect re-identification risk.
- Reddit collection is currently an offline snapshot parser pending authorised
  API access.
- Mayo collection is interactive and deliberately excluded from unattended runs.

## 中文提示

讲解时建议一直围绕三条主线：**统一标准、隐私边界、可复现训练数据**。
重点不是“我写了六个爬虫”，而是“我把六种互不一致的数据源变成了一条可审计、
可扩展、能稳定交付训练数据的工程流水线”。遇到限制时主动说明取舍，会比声称
“已经完全匿名化”更专业。
