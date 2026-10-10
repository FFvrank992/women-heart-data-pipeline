"""Superseded by `scraper/common/pii.py`. Kept as a thin forwarding shim.

This module was the Inspire scraper's own text cleaner and became the basis for
the shared engine: the mojibake table, the Unicode normalisation, the
link/email/phone handling and the `[URL:domain]` placeholder style all came
from here and are unchanged there.

What the shared engine adds, and what Inspire now gets for free:

    personal names  -> [NAME]      four tiers (self-introduction, greeting,
                                   sign-off, dictionary)
    @handles        -> [USER]      Inspire carries 24 of these
    addresses / IDs -> [ADDRESS] / [ID] / [DOB]
    an allowlist so cited clinicians and public bylines survive

Nothing in the pipeline imports this file any more -- `export.py` calls
`common.pii.redact` directly. It stays so that a checkout of the standalone
Inspire scraper, or anything else that still does `import clean`, keeps working
rather than failing with an ImportError.

New code should import `common.pii`.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from common.pii import (  # noqa: F401  (re-exported for backwards compatibility)
    EMAIL_RX,
    MOJIBAKE,
    PERSONAL_MAIL,
    QUOTES,
    SPACES,
    URL_RX,
    ZERO_WIDTH,
    domain_of,
    fix_mojibake,
    normalize_unicode,
    redact,
)
from common.pii import Stats as _Stats
from common.pii import tidy_whitespace as tidy_ws  # noqa: F401  (old name)


def new_stats():
    """A fresh counter bundle, shaped the way the old `clean_doc` expected."""
    return _Stats()


def clean_doc(text, doc_id="", doc_type="", links_out=None, stats=None):
    """Old signature, new engine.

    `doc_id`, `doc_type` and `links_out` are accepted and ignored: the shared
    engine keeps the domain inline as `[URL:domain]` rather than pulling links
    out to a side table, and the five-column schema had nowhere to put that
    table anyway.

    Returns `(cleaned_text, counters)` as before.
    """
    stats = stats if stats is not None else _Stats()
    cleaned = redact(text, stats)
    return cleaned, {
        "n_url": stats.counts.get("url", 0),
        "n_email": stats.counts.get("email", 0),
        "n_phone": stats.counts.get("phone", 0),
        "n_name": stats.counts.get("name", 0),
        "text_changed": cleaned != text,
    }
