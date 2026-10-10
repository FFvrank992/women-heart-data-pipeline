"""The single PII engine. One policy, six platforms.

Before this module each scraper redacted differently -- inspire used
`[URL:domain]`/`[EMAIL]`/`[PHONE]`, heart_sisters used `[link]`/`[email]`/
`@user` and only on comments, youtube used `[EMAIL_REMOVED]` and kept URLs,
and mayo/reddit/her_heart did nothing at all. Three of the six leaked platform
usernames straight into the shared CSV. Everything now routes through
`redact()` here, so the policy lives in one auditable file.

Policy
------
The policy protects the *data subjects* -- the patients and community members
whose narratives we collect. It does not protect published authors, cited
researchers or institutional accounts appearing in a public professional
capacity: `Dr. Martha Gulati` in a cited study stays, because redacting the
3,364 physician mentions in this corpus would destroy its medical traceability
without protecting a single patient. See `config/pii_allowlist.txt`.

What gets replaced
------------------
    URLs            -> [URL:domain]   domain kept, path dropped
    emails          -> [EMAIL] or [EMAIL:ORG]  (consumer vs institutional)
    phone numbers   -> [PHONE]
    @handles        -> [USER]
    personal names  -> [NAME]         four tiers, see `_redact_names`
    street addresses-> [ADDRESS]
    SSN / MRN       -> [ID]
    dates of birth  -> [DOB]

Order of operations (there are dependencies -- do not reorder)
-------------------------------------------------------------
    1 mojibake  ->  2 Unicode  ->  3 legacy placeholders  ->  4 URLs
    ->  5 emails  ->  6 phones  ->  7 protect  ->  8 handles  ->  9 names
    -> 10 addresses/IDs  -> 11 unprotect  -> 12 whitespace

URLs must be extracted before emails, or the `user@host` shape inside a URL is
bitten in half by the email regex first.

Protection must sit *after* the URL pass and *before* the handle and name
passes. Running it earlier breaks URLs: the allowlist contains `womenheart`,
which matches inside `www.womenheart.org` and stashes the middle of the host,
leaving a URL the extractor can no longer see. Running it later is pointless,
since the byline it protects would already be gone.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_DATA = Path(__file__).resolve().parent / "data"
_CONFIG = Path(__file__).resolve().parent.parent / "config"

# Canonical placeholders. Every scraper emits exactly these.
URL_TOKEN = "[URL]"
EMAIL_TOKEN = "[EMAIL]"
EMAIL_ORG_TOKEN = "[EMAIL:ORG]"
PHONE_TOKEN = "[PHONE]"
USER_TOKEN = "[USER]"
NAME_TOKEN = "[NAME]"
ADDRESS_TOKEN = "[ADDRESS]"
ID_TOKEN = "[ID]"
DOB_TOKEN = "[DOB]"


# ---------- 1. mojibake ----------
# Wreckage from UTF-8 decoded as cp1252. Fixed by targeted replacement rather
# than an encode/decode round trip: of the affected documents only about four
# in five round-trip cleanly, and the rest either raise or corrupt a second
# time. The last two entries are tail fixes and must stay last so they cannot
# eat the multi-character combinations above them.
MOJIBAKE = [
    ("â€™", "’"), ("â€˜", "‘"),
    ("â€œ", "“"), ("â€\x9d", "”"),
    ("â€”", "—"), ("â€“", "–"),
    ("â€¦", "…"), ("â€¢", "•"),
    ("Ã©", "é"), ("Ã¨", "è"), ("Ã¡", "á"),
    ("Ã¼", "ü"), ("Ã±", "ñ"), ("Ã¶", "ö"),
    ("Â©", "©"), ("Â®", "®"), ("Â°", "°"),
    ("Â½", "½"), ("Â£", "£"),
    ("â€", '"'),   # leftover remnant, usually a quote
    ("Â", ""),          # stray remnant, a surplus byte
]

# ---------- 2. Unicode ----------
QUOTES = {"‘": "'", "’": "'", "‚": "'", "‛": "'",
          "“": '"', "”": '"', "„": '"', "‟": '"',
          "′": "'", "″": '"'}
SPACES = {" ": " ", " ": " ", " ": " ", " ": " ",
          " ": " ", "　": " ", "\t": " "}
ZERO_WIDTH = re.compile(r"[​-‍⁠﻿]")

# ---------- 3. legacy placeholders ----------
# Historical CSVs carry each scraper's old private vocabulary. Map them onto the
# canonical set so re-processing delivered data is idempotent and the merged
# file does not mix five notations for "a link was here".
LEGACY_PLACEHOLDERS = [
    (re.compile(r"\[EMAIL_REMOVED\]", re.I), EMAIL_TOKEN),
    (re.compile(r"\[PHONE_REMOVED\]", re.I), PHONE_TOKEN),
    (re.compile(r"\[MENTION_REMOVED\]", re.I), USER_TOKEN),
    (re.compile(r"\[link\]"), URL_TOKEN),
    (re.compile(r"\[email\]"), EMAIL_TOKEN),
    (re.compile(r"\[phone\]"), PHONE_TOKEN),
    (re.compile(r"(?<!\w)@user\b"), USER_TOKEN),
]

# ---------- 5~10. the redaction patterns ----------
URL_RX = re.compile(r"https?://[^\s<>\"'\)\]}]+", re.I)
BARE_DOMAIN_RX = re.compile(
    r"\bwww\.[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:/[^\s<>\"'\)\]}]*)?", re.I)
EMAIL_RX = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")

# Separators or a country code are required throughout. A bare run of digits is
# deliberately not a phone number here: this corpus is full of lab values,
# dosages and "my EF is 20", and matching those would mangle clinical content.
PHONE_RXS = [
    re.compile(r"(?<!\d)(?:\+?1[-. ]?)?\(\d{3}\)\s?\d{3}[-. ]?\d{4}(?!\d)"),
    re.compile(r"(?<!\d)(?:\+?1[-. ])?\d{3}[-. ]\d{3}[-. ]\d{4}(?!\d)"),
    re.compile(r"(?<!\d)\+\d{1,3}[-. ]?\d{1,4}(?:[-. ]?\d{2,4}){2,3}(?!\d)"),
    re.compile(r"(?<!\d)0[2-9]\d{2}[-. ]?\d{3}[-. ]?\d{3}(?!\d)"),      # AU landline
    re.compile(r"(?<!\d)04\d{2}[-. ]?\d{3}[-. ]?\d{3}(?!\d)"),          # AU mobile
]

# A handle must not be preceded by a word character, or every email survivor and
# every "user@host" fragment becomes a handle.
HANDLE_RX = re.compile(r"(?<![\w@/])@([A-Za-z][A-Za-z0-9_.-]{1,29})\b")

STREET_RX = re.compile(
    r"\b\d{1,5}\s+(?:[A-Z][a-z]+\.?\s+){1,3}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Lane|Ln|Boulevard|Blvd|Court|Ct|"
    r"Way|Place|Pl|Terrace|Circle|Cir|Highway|Hwy|Parkway|Pkwy)\b\.?")
GOV_ID_RXS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),                       # US SSN
    re.compile(r"\bMRN\s*:?\s*\d{4,12}\b", re.I),               # medical record no.
    re.compile(r"\bmedicare\s*(?:number|no\.?|#)?\s*:?\s*\d[\d ]{8,12}\b", re.I),
]
DOB_RX = re.compile(
    r"\b(?:born(?:\s+on)?|DOB|date of birth)\b\s*:?\s*"
    r"\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}", re.I)

# Consumer mail providers. Everything else is treated as institutional, which
# keeps the `.org`/`.edu`/`.gov` signal that tells a reader whether a comment
# pointed at a charity or at a private inbox.
PERSONAL_MAIL = {
    "gmail.com", "yahoo.com", "hotmail.com", "icloud.com", "aol.com", "outlook.com",
    "msn.com", "comcast.net", "me.com", "live.com", "sbcglobal.net", "verizon.net",
    "att.net", "bellsouth.net", "juno.com", "cox.net", "charter.net", "earthlink.net",
    "mac.com", "mail.com", "ymail.com", "rocketmail.com", "gmx.com", "protonmail.com",
    "bigpond.com", "optusnet.com.au", "hotmail.co.uk", "yahoo.co.uk", "btinternet.com",
}

# ---------- 9. names ----------
# A run of capitalised tokens, allowing single initials, so "C. Noel Bairey
# Merz" and "John F. Beltrame" are matched whole rather than token by token.
_NAME_RUN = (r"(?:[A-Z]\.|[A-Z][A-Za-z'\-]+)"
             r"(?:\s+(?:[A-Z]\.|[A-Z][A-Za-z'\-]+)){0,3}")

# Academic and clinical titles confer "public professional" status: the titled
# name and the capitalised run following it are protected wholesale.
TITLE_RX = re.compile(
    rf"\b(?:Dr|Dr\.|Doctor|Prof|Prof\.|Professor|Nurse|Surgeon|Cardiologist)\s+"
    rf"{_NAME_RUN}")

# The same status, signalled by credentials rather than a title. The blogs cite
# researchers as "Janet Wei, MD, FACC" and "John F. Beltrame, MB, BS, PHD,
# FACC" -- no "Dr.", so TITLE_RX misses them and the dictionary would redact
# the author of a cited study. Credentials are matched case-insensitively
# because the corpus contains both "PhD" and "PHD".
# The trailing lookahead is load-bearing. Without it the two-letter entries
# (do, pa, mb, bs, np) match the start of ordinary words, so "And, don't push"
# parses as "<name>, DO" and the whole span gets protected as a cited
# clinician -- 187 documents in this corpus were affected.
_CRED = (r"(?i:m\.?d\.?|ph\.?d\.?|r\.?n\.?|d\.?o\.?|mbbs|mb|bs|bsc|msc|facc|"
         r"faha|fesc|fscai|mph|msn|dnp|np|pa-c|pa|frcp|facp)(?![A-Za-z])")
CREDENTIAL_RX = re.compile(rf"\b{_NAME_RUN},\s*{_CRED}(?:,?\s*{_CRED})*")

# "St Vincent's", "St. Mary's", "Saint Joseph" -- hospitals and churches, not
# people. Both appear in this corpus and both were being redacted as names.
SAINT_RX = re.compile(r"\b(?:St\.?|Saint)\s+[A-Z][a-z]+(?:'s)?")

_NAME_WORD = r"[A-Z][a-z]{1,14}"

# Tier 1 -- explicit self-identification. Precise enough to fire without the
# dictionary. "this is X" is deliberately absent: it matched "This is Monday".
_SURNAME = rf"(?P<surname>\s+[A-Z][a-z]{{1,14}})?"

SELF_INTRO_RX = re.compile(
    rf"\b(?:my name is|my name's|name is|name's|call me|i go by)\s+"
    rf"(?P<name>{_NAME_WORD}){_SURNAME}", re.I)

# Tier 2 -- greeting or direct address.
GREETING_RX = re.compile(
    rf"\b(?:Hi|Hello|Hey|Dear|Thanks|Thank you|Welcome|Congrats|Congratulations|"
    rf"Hugs|Bless you|Good luck|Take care|Sorry)[,!]?\s+"
    rf"(?P<name>{_NAME_WORD}){_SURNAME}\b")

# Tier 3 -- sign-off, either after a valediction or alone on the final line.
SIGNOFF_RX = re.compile(
    rf"\b(?:Sincerely|Regards|Best regards|Best wishes|Best|Warmly|Cheers|Love|"
    rf"Hugs|XO|XOXO|Yours|Thanks again)[,;.]?\s*\n+\s*[-~]{{0,2}}\s*"
    rf"(?P<name>{_NAME_WORD}){_SURNAME}\b", re.I)
TRAILING_NAME_RX = re.compile(
    rf"\n[ \t]*[-~]{{0,3}}[ \t]*(?P<name>{_NAME_WORD}){_SURNAME}[.!]?[ \t]*$")

# Tier 4 -- dictionary. One capitalised token at a time; membership is a set
# lookup, so this stays linear in document length.
#
# Scanning single tokens and looking ahead for a surname, rather than matching
# an optional two-token group, is deliberate. The two-token version consumed
# the following token even when the first was NOT a name, so the real name in
# "With Barbara", "After Nadine" and "Then Judy" was never examined -- any name
# preceded by a capitalised word (including every sentence opener) survived.
CAPITALISED_RX = re.compile(rf"\b{_NAME_WORD}\b")

SURNAME_STOPWORDS = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "i", "we", "it", "he", "she", "they", "the", "a", "an", "my", "his", "her",
    "there", "this", "that", "but", "and", "so", "if", "when", "after", "before",
    "hospital", "clinic", "health", "medical", "centre", "center", "university",
    "college", "church", "street", "road", "avenue", "heart", "cardiology",
}

# Words that follow a greeting but are not names. Without these, "Hi All" and
# "Thanks Everyone" become "[NAME]".
GREETING_STOPWORDS = {
    "all", "everyone", "everybody", "there", "ladies", "friends", "sisters",
    "guys", "folks", "god", "again", "so", "much", "you", "your", "yours", "my",
    "me", "like", "dr", "doctor", "nurse", "mom", "mum", "dad", "sis", "hun",
    "honey", "sweetie", "dear", "lady", "girl", "girls", "team", "group",
    "anyone", "someone", "nobody", "and", "but", "for", "the", "this", "that",
    "it", "im", "ive", "was", "is", "am", "are", "just", "very", "really",
    "heart", "sisters", "welcome", "hi", "hello", "hey", "thanks", "thank",
    # Interface strings, not people. WordPress renders "Read More" into the
    # body text, and a trailing "More" reads exactly like a sign-off to tier 3,
    # which judges by position and never consults the dictionary.
    "more", "read", "reply", "share", "like", "comment", "continue", "next",
    "previous", "back", "home", "menu", "search", "subscribe", "follow",
    "posted", "filed", "tagged", "related", "source", "sources", "reference",
    "references", "note", "notes", "update", "edit", "view", "print",
}


def _load_words(path: Path) -> set[str]:
    """Read a whitespace-separated word list, ignoring # comment lines."""
    if not path.exists():
        return set()
    words: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.update(line.lower().split())
    return words


def _load_phrases(path: Path) -> set[str]:
    """Read one phrase per line, ignoring # comment lines. Case-folded."""
    if not path.exists():
        return set()
    phrases: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            phrases.add(line.lower())
    return phrases


@lru_cache(maxsize=1)
def first_names() -> set[str]:
    return _load_words(_DATA / "first_names.txt")


@lru_cache(maxsize=1)
def ambiguous_names() -> set[str]:
    return _load_words(_DATA / "ambiguous_names.txt")


@lru_cache(maxsize=1)
def allowlist() -> set[str]:
    return _load_phrases(_CONFIG / "pii_allowlist.txt")


@lru_cache(maxsize=1)
def _allowlist_rx() -> re.Pattern | None:
    """One alternation over every allowlist phrase, longest first.

    Longest-first matters: without it "heart sisters" would never match,
    because the shorter "inspire" or a single-word entry could claim a prefix.
    """
    phrases = sorted(allowlist(), key=len, reverse=True)
    if not phrases:
        return None
    return re.compile("|".join(re.escape(p) for p in phrases), re.I)


@dataclass
class Stats:
    """Per-run tally, for the audit report. One instance covers many documents."""

    docs: int = 0
    docs_changed: int = 0
    mojibake_docs: int = 0
    counts: Counter = field(default_factory=Counter)
    email_domains: Counter = field(default_factory=Counter)
    names_redacted: Counter = field(default_factory=Counter)
    names_by_tier: Counter = field(default_factory=Counter)

    def merge(self, other: "Stats") -> "Stats":
        self.docs += other.docs
        self.docs_changed += other.docs_changed
        self.mojibake_docs += other.mojibake_docs
        self.counts.update(other.counts)
        self.email_domains.update(other.email_domains)
        self.names_redacted.update(other.names_redacted)
        self.names_by_tier.update(other.names_by_tier)
        return self


def domain_of(url: str) -> str:
    match = re.match(r"(?:https?://)?([^/\s:]+)", url, re.I)
    if not match:
        return ""
    # removeprefix, not lstrip -- lstrip strips a *set of characters*, which
    # would turn www.womenheart.org into omenheart.org (w and . are both in it).
    return match.group(1).lower().removeprefix("www.")


def fix_mojibake(text: str) -> str:
    if "â" not in text and "Ã" not in text and "Â" not in text:
        return text
    for bad, good in MOJIBAKE:
        text = text.replace(bad, good)
    return text


def normalize_unicode(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = ZERO_WIDTH.sub("", text)
    for a, b in SPACES.items():
        text = text.replace(a, b)
    for a, b in QUOTES.items():
        text = text.replace(a, b)
    return text.replace("…", "...")


def normalize_legacy_placeholders(text: str) -> str:
    for pattern, token in LEGACY_PLACEHOLDERS:
        text = pattern.sub(token, text)
    return text


def strip_shortcodes(text: str) -> str:
    """Drop WordPress page-builder shortcodes, e.g. `[vc_row]`, `[/vc_column]`.

    Her Heart runs WPBakery, so 29 of its 208 posts arrive wrapped in layout
    markup that the REST API renders verbatim into `content.rendered`. This is
    content cleaning rather than PII, but it belongs on the same pass.
    """
    text = re.sub(r"\[/?vc_[^\]]*\]", " ", text)
    text = re.sub(r"\[/?(?:et_pb|fusion|su)_[^\]]*\]", " ", text)
    return re.sub(r"\[caption[^\]]*\]|\[/caption\]", " ", text)


def tidy_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ ]+\n", "\n", text)
    text = re.sub(r"[ ]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class _Protector:
    """Swap protected spans for sentinels, then swap them back.

    A sentinel is `\\x00<index>\\x00`. NUL never occurs in scraped text, and the
    digits between the markers cannot be confused with anything a redaction
    pattern looks for, so a protected span is invisible to every stage between
    `protect` and `restore`.
    """

    def __init__(self) -> None:
        self.spans: list[str] = []

    def protect(self, text: str) -> str:
        allow = _allowlist_rx()
        if allow is not None:
            text = allow.sub(self._stash, text)
        # Credentials before titles: "Dr. Janet Wei, MD" should be stashed as
        # one span, and CREDENTIAL_RX is the one that reaches the trailing
        # credentials. Running TITLE_RX first would stash "Dr. Janet Wei" and
        # leave ", MD" dangling against a sentinel.
        text = CREDENTIAL_RX.sub(self._stash, text)
        text = SAINT_RX.sub(self._stash, text)
        return TITLE_RX.sub(self._stash, text)

    def _stash(self, match: re.Match) -> str:
        self.spans.append(match.group(0))
        return f"\x00{len(self.spans) - 1}\x00"

    def restore(self, text: str) -> str:
        def _put(match: re.Match) -> str:
            return self.spans[int(match.group(1))]

        return re.sub(r"\x00(\d+)\x00", _put, text)


def _redact_names(text: str, stats: Stats) -> str:
    """Four tiers, cheapest and most precise first.

    Tiers 1-3 read a name's *position* -- after "my name is", after "Hi", on a
    sign-off line -- and fire regardless of whether the token is a known name,
    which is what catches "Sunnyflower" and "Iloveliving". Tier 4 reads the
    token itself against the dictionary and catches names in ordinary prose.
    """
    known = first_names()
    ambiguous = ambiguous_names()
    allowed = allowlist()

    def _record(name: str, tier: str) -> None:
        stats.names_redacted[name.lower()] += 1
        stats.names_by_tier[tier] += 1
        stats.counts["name"] += 1

    def _absorb_surname(surname):
        """Is this trailing capitalised token part of the name, or a new clause?"""
        if surname is None:
            return False
        tail = surname.strip().lower()
        return not (tail in SURNAME_STOPWORDS or tail in ambiguous or tail in allowed)

    def _sub_captured(match: re.Match, tier: str, check_stopwords: bool) -> str:
        """Replace the matched name -- and its surname -- leaving the rest intact.

        Tiers 1-3 used to replace the given name only, so "my name is Sadie
        Majors" became "my name is [NAME] Majors" and the surname survived.

        Rebuilt from match offsets rather than `str.replace`: replace would hit
        the first occurrence of the name substring anywhere in the match, which
        rewrites the greeting word itself in "Hi Hillary".
        """
        name = match.group("name")
        if name.lower() in allowed:
            return match.group(0)
        if check_stopwords and name.lower() in GREETING_STOPWORDS:
            return match.group(0)

        whole, base = match.group(0), match.start()
        end = (match.end("surname") if _absorb_surname(match.group("surname"))
               else match.end("name"))
        _record(name, tier)
        return whole[:match.start("name") - base] + NAME_TOKEN + whole[end - base:]

    text = SELF_INTRO_RX.sub(lambda m: _sub_captured(m, "self_intro", False), text)
    text = GREETING_RX.sub(lambda m: _sub_captured(m, "greeting", True), text)
    text = SIGNOFF_RX.sub(lambda m: _sub_captured(m, "signoff", True), text)
    text = TRAILING_NAME_RX.sub(lambda m: _sub_captured(m, "signoff", True), text)

    tokens = list(CAPITALISED_RX.finditer(text))
    out: list[str] = []
    cursor = 0          # everything before this is already emitted
    consumed = 0        # end offset of a token absorbed as a surname

    for index, token in enumerate(tokens):
        if token.start() < consumed:
            continue
        low = token.group(0).lower()
        if low in ambiguous or low in allowed or low not in known:
            continue

        end = token.end()
        # Absorb the next capitalised token as a surname when it directly
        # follows, separated by a single space. Requiring one space (rather
        # than \s+) stops a sign-off name on the next line being swallowed
        # into the valediction above it.
        if index + 1 < len(tokens):
            nxt = tokens[index + 1]
            if text[token.end():nxt.start()] == " " and _absorb_surname(" " + nxt.group(0)):
                end, consumed = nxt.end(), nxt.end()

        out.append(text[cursor:token.start()])
        out.append(NAME_TOKEN)
        _record(token.group(0), "dictionary")
        cursor = end

    out.append(text[cursor:])
    return "".join(out)


def redact(text: str, stats: Stats | None = None, *, shortcodes: bool = False) -> str:
    """Run the whole policy over one document. This is the entry point.

    `shortcodes=True` additionally strips WordPress page-builder markup; pass it
    for Her Heart, whose REST API renders WPBakery tags into the post body.

    `stats` accumulates across calls -- create one `Stats()` per scraper run and
    pass it to every document, then hand it to `format_report`.
    """
    if stats is None:
        stats = Stats()
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    stats.docs += 1
    original = text

    text = fix_mojibake(text)
    if text != original:
        stats.mojibake_docs += 1
    text = normalize_unicode(text)
    if shortcodes:
        text = strip_shortcodes(text)
    text = normalize_legacy_placeholders(text)

    def _url(match: re.Match) -> str:
        url = match.group(0).rstrip(".,;:!?")
        domain = domain_of(url)
        stats.counts["url"] += 1
        return f"[URL:{domain}]" if domain else URL_TOKEN

    text = URL_RX.sub(_url, text)
    text = BARE_DOMAIN_RX.sub(_url, text)

    def _email(match: re.Match) -> str:
        domain = match.group(0).split("@", 1)[1].lower().rstrip(".")
        stats.counts["email"] += 1
        stats.email_domains[domain] += 1
        return EMAIL_TOKEN if domain in PERSONAL_MAIL else EMAIL_ORG_TOKEN

    text = EMAIL_RX.sub(_email, text)

    for pattern in PHONE_RXS:
        text, n = pattern.subn(PHONE_TOKEN, text)
        stats.counts["phone"] += n

    # Only now: the allowlist and the titled-name rule guard identities, and
    # everything from here on is identity redaction.
    protector = _Protector()
    text = protector.protect(text)

    def _handle(match: re.Match) -> str:
        if match.group(1).lower() in allowlist() or f"@{match.group(1).lower()}" in allowlist():
            return match.group(0)
        stats.counts["handle"] += 1
        return USER_TOKEN

    text = HANDLE_RX.sub(_handle, text)
    text = _redact_names(text, stats)
    # Handles again. HANDLE_RX needs a non-word character before the "@", so
    # "Nancy@thankful" is not a handle on the first pass -- but redacting the
    # name turns it into "[NAME]@thankful", where "]" does qualify. One extra
    # pass settles it and makes redact() idempotent.
    text = HANDLE_RX.sub(_handle, text)

    text, n = STREET_RX.subn(ADDRESS_TOKEN, text)
    stats.counts["address"] += n
    for pattern in GOV_ID_RXS:
        text, n = pattern.subn(ID_TOKEN, text)
        stats.counts["gov_id"] += n
    text, n = DOB_RX.subn(DOB_TOKEN, text)
    stats.counts["dob"] += n

    text = protector.restore(text)
    text = tidy_whitespace(text)
    if text != original:
        stats.docs_changed += 1
    return text


def format_report(stats: Stats, label: str = "") -> str:
    """Human-readable audit summary. Goes into the run log and the PII report."""
    lines = [f"PII report{f' -- {label}' if label else ''}",
             f"  documents            {stats.docs:,} ({stats.docs_changed:,} changed)",
             f"  mojibake repaired    {stats.mojibake_docs:,} docs"]
    for key, token in (("url", URL_TOKEN), ("email", EMAIL_TOKEN), ("phone", PHONE_TOKEN),
                       ("handle", USER_TOKEN), ("name", NAME_TOKEN),
                       ("address", ADDRESS_TOKEN), ("gov_id", ID_TOKEN), ("dob", DOB_TOKEN)):
        lines.append(f"  {token:<14} {stats.counts.get(key, 0):>8,}")
    if stats.names_by_tier:
        tiers = ", ".join(f"{tier} {count:,}"
                          for tier, count in stats.names_by_tier.most_common())
        lines.append(f"  name tiers           {tiers}")
    if stats.email_domains:
        domains = ", ".join(f"{domain} x{count}"
                            for domain, count in stats.email_domains.most_common(5))
        lines.append(f"  email domains        {domains}")
    return "\n".join(lines)
