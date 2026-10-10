# Data ethics and publication boundary

This project processes public health discussions, but public availability does
not make patient narratives risk-free to republish.

## What the public repository excludes

- Raw or processed real-world narratives
- The full training dataset
- Crawler state containing collected source URLs
- Manual Reddit snapshots
- Browser profiles, credentials and API keys

Only synthetic data is checked in.

## What the PII layer removes

URLs in text, consumer and institutional email addresses, phone numbers,
usernames, personal names, street addresses, SSN/medical-record style identifiers
and dates of birth are replaced with typed placeholders.

## Residual risk

The result is de-identified, not anonymous. Indirect details such as an unusual
condition, age, location, hospital and event date may re-identify someone when
combined. Comment-level output should remain in approved storage and should not
be included in a public GitHub repository without an explicit ethics and legal
review.

The accompanying PII report is an operational audit, not proof of anonymity.
