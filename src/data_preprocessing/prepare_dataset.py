#!/usr/bin/env python3
"""Build a model-ready sentiment dataset from the unified scraper output.

Backbone: the team's first cleaning notebook (posts) and comment script, whose steps
transform() keeps in the same order, with the same nine output columns, plus `id` (joins
the results to the database and to the clustering output) and `Type` (posts, comments
and blogs share one file). As in the notebook, emoji are removed (`has_emoji` keeps the
flag) and repeated punctuation is collapsed; YouTube replies keep the
"[In reply to: <parent>]:" prefix they were scraped with.

Fixes for what the original code gets wrong on this data:
  reading    the export's '' and '" escapes are undone (load_records)
  scope      blogs are cleaned too; rows run in date order, so duplicates resolve the
             same way on every export
  text       Mayo footer also found when pii.py redacted its "Like"; Mayo's
             self-quoting "In reply to" header dropped; Heart Sisters / Her Heart
             chrome and Reddit's saved-page chrome dropped; only real HTML tags
             stripped; every PII placeholder removed, not only [NAME]
             (n_redacted_names still counts [NAME]); phone numbers, handles and URLs
             pii.py missed removed; "u/s" kept
  language   langdetect seeded; romanised Hindi is not English; short English is not
             dropped as foreign (judged on the whole Content, as in the notebook)
  relevance  keyword list extended (SVT, PVC, BP, stroke, ...); MI counts only in
             capitals; link domains removed from the text still count ("heart.org");
             Reddit is r/HeartAttack, so every post there is relevant
  noise      Mayo search-result fragments, clinic promo replies and repeated texts of
             5+ words dropped

The preferred input is the five-column CSV written by ``scraper/run_all.py``. For
backward compatibility the reader also accepts the original seven-column database
export. Row IDs are derived with the same content hash in either path, so outputs can
still be joined to downstream model results.

Standalone: needs only pandas and langdetect, no other file of this repo.

Dependencies: pip install pandas langdetect==1.0.9
Usage: python prepare_dataset.py [INPUT.csv] [--output-dir DIR]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import re
from collections import Counter
from pathlib import Path

import pandas as pd
from langdetect import DetectorFactory, LangDetectException, detect

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
OUTPUT_FIELDS = ["id", "Date", "Type", "Platform", "URL", "Content", "support_group",
                 "n_redacted_names", "has_emoji", "content_len", "lang"]
MIN_CHARS = 20                                   # notebook Task 9
TYPE_ORDER = {"post": 0, "blog": 0, "comment": 1}  # a thread root beats a same-day reply
YT_REPLY_CLOSE = "]: "

REDACT_PATTERN = re.compile(r"\[\s*NAME\s*\]", re.IGNORECASE)
REPEAT_PUNCT_PATTERN = re.compile(r"([!?.,])\1{1,}")

DetectorFactory.seed = 0   # langdetect is random otherwise


# ---------- Input: the social_data_raw export ----------

DB_HEADER = ["id", "date", "type", "platform", "url", "content", "metadata"]
MERGED_HEADER = ["date", "type", "platform", "url", "content"]
DB_RECORD = re.compile(
    r"^([^,\r\n]+_[0-9a-f]{16}),(\d{4}-\d{2}-\d{2}|),(comment|post|blog),([^,\r\n]+),(https?://[^,\r\n]+),",
    re.MULTILINE)


def parse_pg_export(body: str) -> list[tuple[str, ...]]:
    """PostgreSQL `COPY ... CSV ESCAPE ''''` output, the dialect of social_data_raw.csv.

    Quoted fields escape with an apostrophe (`''` is ', `'"` is "), while unquoted
    fields are raw and may hold apostrophes, so no csv.Dialect reads it. Records are
    cut at the fixed id,date,type,platform,url prefix instead. Metadata is always `{}`:
    csv_to_db.py only packs columns the five-column CSV does not have.
    """
    starts = list(DB_RECORD.finditer(body))
    if not starts or body[:starts[0].start()].strip():
        raise ValueError("no record at the start of the export")
    rows = []
    for m, nxt in zip(starts, starts[1:] + [None]):
        payload = body[m.end():nxt.start() if nxt else len(body)].rstrip("\r\n")
        if not payload.endswith(",{}"):
            raise ValueError(f"{m.group(1)}: metadata other than {{}} is not supported")
        content = payload[:-3]
        if len(content) >= 2 and content[0] == content[-1] == '"':
            content = re.sub(r"'(['\"])", r"\1", content[1:-1])
        rows.append((*m.groups(), content))
    return rows


def parse_standard_csv(body: str) -> list[tuple[str, ...]]:
    csv.field_size_limit(max(csv.field_size_limit(), len(body)))
    rows = []
    for values in csv.reader(io.StringIO(body, newline=""), strict=True):
        if not values:
            continue
        if len(values) != len(DB_HEADER):
            raise ValueError(f"expected {len(DB_HEADER)} fields, got {len(values)}")
        rows.append(tuple(values[:6]))
    return rows


def row_id(platform: str, url: str, content: str) -> str:
    """Stable identifier shared with the collection layer and database loader."""
    raw = f"{url or 'unknown'}{content or 'unknown'}".encode("utf-8")
    return f"{(platform or 'unknown').strip()}_{hashlib.sha256(raw).hexdigest()[:16]}"


def parse_merged_csv(text: str) -> list[dict[str, str]]:
    """Read the five-column contract produced directly by ``run_all.py``."""
    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = [str(name or "").strip().lower() for name in (reader.fieldnames or [])]
    if header != MERGED_HEADER:
        raise ValueError(f"Expected columns {MERGED_HEADER}")

    records = []
    for source in reader:
        values = {str(key).strip().lower(): str(value or "")
                  for key, value in source.items()}
        records.append({
            "id": row_id(values["platform"], values["url"], values["content"]),
            "date": values["date"],
            "type": values["type"],
            "platform": values["platform"],
            "url": values["url"],
            "content": values["content"],
        })
    return records


# ---------- Site structure ----------

MAYO_REPLY_HEAD = re.compile(r'^In reply to \S+ "')
# The team's footer pattern, widened: pii.py read the footer's "Like" as a name, so
# 30 posts end "[NAME] Helpful Hug ..." and the original pattern missed them.
MAYO_FOOTER = re.compile(r"\s*(?:Like|\[NAME\])\s+Helpful Hug.*?Go to the (.*?) Support Group\.?", re.DOTALL)


def strip_mayo_reply_header(content: str) -> str:
    """Drop Mayo's `In reply to @x "<first ~100 chars of THIS comment>..." ` header.

    The quote is the page's own preview of the comment that follows, not the parent,
    so keeping it only duplicates the opening. The quote may contain `" `, so the cut
    is the first `" ` after which the quoted text actually reappears.
    """
    m = MAYO_REPLY_HEAD.match(content)
    if not m:
        return content
    rest = content[m.end():]
    for close in re.finditer(r'" ', rest):
        quote = rest[:close.start()].removesuffix("...").removesuffix("…")
        body = rest[close.end():]
        if body.startswith(quote[:30]):
            return body
    first = rest.find('" ')
    return rest[first + 2:] if first != -1 else content


# ---------- Text patterns ----------

PLACEHOLDER = re.compile(
    r"\[\s*(?:NAME|USER|EMAIL|PHONE|ADDRESS|ID|DOB|URL)(?::[^\]\s]*)?\s*\]", re.IGNORECASE)

# The team's `<[^>]+>` also ate medical text: "<5% excreted ... >" spanned paragraphs,
# "EF <35 ... BP >" likewise. Only real tag names count here.
HTML_TAG = re.compile(
    r"</?(?:a|abbr|b|blockquote|br|code|div|em|font|h[1-6]|hr|i|iframe|img|li|ol|p|pre|s|"
    r"small|span|strong|sub|sup|table|tbody|td|th|tr|u|ul)\b[^<>]*>", re.IGNORECASE)

# A URL never ends in sentence punctuation: "see x.com/a." keeps its full stop.
_URL_TAIL = r"(?:[^\s)\]]*[^\s)\].,;:!?'\"])?"
RAW_URL = re.compile(r"(?:https?://|www\.)" + _URL_TAIL, re.IGNORECASE)
# pii.py only replaces URLs with a scheme or www, so "drscottlee.com" and
# "amazon.com/gp/product/..." survive. Country TLDs need a path, so that a missing space
# ("fine.in the morning") is not read as a domain.
BARE_URL = re.compile(
    r"\b(?:[a-z0-9-]+\.)+(?:com|org|net|gov|edu|io|ly|gl)(?:\.[a-z]{2})?\b(?:/" + _URL_TAIL + ")?"
    r"|\b(?:[a-z0-9-]+\.)+[a-z]{2}/" + _URL_TAIL,
    re.IGNORECASE)

# Safety net for identifiers pii.py missed (seen in the 2026-09-12 data): bare 10-11
# digit phone numbers ("msg me on 98xxxxxxxx") and handles that start with a digit or
# dash, which pii.HANDLE_RX does not accept. Formats are explicit so that clinical
# number runs ("HR 60 70 80 90 100", "2015-2016") are never touched.
PHONE_LIKE = re.compile(
    r"(?<![\w/%.=])(?<!ISBN )(?<!ISBN: )(?:\+?\d{1,3}[ -]?|0)?"
    r"(?:\d{10}|\(?\d{3}\)?[ -]?\d{3}[ -]?\d{4}|\d{3}[ -]\d{7}|\d{5}[ -]\d{5}|\d{4}[ -]\d{3}[ -]\d{3})"
    r"(?![\w/%:-]|\.\d)")

# YouTube renders some mentions as "@a@audnbob1": the doubled initial hides the real
# handle from pii.HANDLE_RX (too short, then preceded by a letter).
HANDLE = re.compile(r"(?<![\w@])@(?:\w@)?[\w.-]*\w")
# The team's markdown patterns, except user refs need 3+ characters: the original
# `\bu/\w+` also deleted "u/s", a common abbreviation for ultrasound.
MARKDOWN_PATTERNS = [
    (re.compile(r"\*\*(.*?)\*\*"), r"\1"),
    (re.compile(r"\*(.*?)\*"), r"\1"),
    (re.compile(r"/?\br/\w{3,}"), ""),
    (re.compile(r"/?\bu/[\w-]{3,}"), ""),
    (re.compile(r"&#x200B;"), ""),
]
# Blog chrome found by counting lines repeated across documents. Case-sensitive on
# purpose: "Back to work was hard." is content, "Back to Spontaneous Coronary Artery
# Dissection (SCAD)" is navigation.
BLOG_LINE_DROP = re.compile(
    r"^(?:by Carolyn Thomas\b.*"                                    # Heart Sisters byline
    r"|.*\bmy book\b.*(?:Johns Hopkins|HTWN|order it).*|.*\bHTWN\b.*"  # Heart Sisters book promo
    r"|This article was originally published\b.*"                   # Her Heart syndication note
    r"|Back to (?:[A-Z][\w'&-]*\s?)+(?:\([A-Z]+\))?"                # Her Heart series navigation
    r"|\d{1,2}\.\s(?=.*\b(?:19|20)\d{2}\b).*(?:\bet al\b|\bdoi\b|\bJournal\b|\bJ Am\b|\bJAMA\b).*"
    r")$"                                                           # ^ numbered references
)
SEE_ALSO = re.compile(r"\n\s*See also:?\s*\n.*\Z", re.IGNORECASE | re.DOTALL)   # link list closing a post
SEE_ALSO_INLINE = re.compile(r"\s*\bSee also: [^\n]*")                          # "...next day. See also: <post title>"
SIGNOFF_LINE = re.compile(r"^Take care, stay safe[\s.♥]*$", re.IGNORECASE)   # Heart Sisters replies
# Reddit posts come from saved pages (parse_reddit_heartattack.py): 75 repeat their title
# as "r/HeartAttack - <title>", and 31 saved from a Chinese-locale browser carry its
# gallery and pagination labels ("第 1 页 (当前页面)", "项 1/2").
REDDIT_CHROME_LINE = re.compile(r"^(?:r/\w+ - .*|第 \d+ 页(?: \(当前页面\))?|项 \d+/\d+)$")
# Include the variation selector and zero-width joiner used inside compound emoji;
# otherwise removing the visible glyph leaves invisible tokenizer noise behind.
EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\uFE0F\u200D]")


def strip_reddit_chrome(text: str) -> str:
    return "\n".join(line for line in text.split("\n") if not REDDIT_CHROME_LINE.match(line.strip()))


# ---------- Language ----------

# Romanised Hindi/Urdu function words. Words that are also English ("me", "to",
# "mere", "tab", "jab", "din", "yeh") are left out, so an English sentence cannot reach
# the threshold by accident.
HINGLISH_WORDS = frozenset("""
hai hain nahi nhi nahin kya kyu kyun kyon mujhe mujhko mera meri aap aapka apka
apke aapke karo karna karein kare raha rahi rahe hota hoti hote gaya gayi bhi kuch
bahut bohot bhai ji ko ka ki ke se liye lekin aur tha thi abhi kaise kaisa hamara
humko tum tera unko usko bataye batao dard dil ho kripya dhanyawad sahi hua hui huye
wala wali mein mai hoon diya kiya kab kitna kitne saal
h hy hn hu hun koi kabhi kbhi ye muje mjhe rha rhi rhe rehta rehti hoga hogi phir ek
baad lgta lagta lagti lgti boli thik theek kar karke karta karti karte krna krte krta
kr sab sb aapki apki aapko apko apne apna apni kitni kaisi kese kesi kyunki kyuki
jaise wale tere isme usme iska uska humein hme hume mne maine agar magar bas acha
accha achha jyada zyada dikhaye bataiye bataya btaya karwaye karwaya gya gyi wo woh
vo kaha kahan yaha yahan waha dawai dawa ilaj ilaaj sahab pehle pahle sakte sakta
sakti laga lagaya aaya aayi aai jata jati jaata jaati bhut jaldi jldi dekho chahiye
""".split())
# Short English replies are where langdetect fails ("Have u gone to cardiologist?" ->
# not English); a high share of English function words settles them first.
ENGLISH_WORDS = frozenset("""
i i'm im you your you're the and a an is are was were am be been to of in on for with
my me it it's its this that have has had do did don't dont not no so but or if at as
about after what how here there we they she he her his our just can will would too
also very any all get got really same thank thanks please anyone else
""".split())
LATIN_LETTER = re.compile(r"[A-Za-z]")
LETTER = re.compile(r"[^\W\d_]")


def is_hinglish(text: str) -> bool:
    tokens = re.findall(r"[a-z]+", text.lower())
    if not tokens:
        return False
    hits = sum(tok in HINGLISH_WORDS for tok in tokens)
    ratio = hits / len(tokens)
    return (hits >= 3 and ratio >= 0.15) or (hits >= 2 and ratio >= 0.3)


def is_english(text: str) -> bool:
    tokens = re.findall(r"[a-z']+", text.lower())
    hits = sum(tok in ENGLISH_WORDS for tok in tokens)
    return hits >= 2 and hits / len(tokens) >= 0.3


# ---------- Relevance and spam ----------

# The team's keyword list, kept as written; mentions_heart() adds what it misses.
HEART_KEYWORDS = re.compile(
    r'heart|cardiac|cardio|coronary|artery|atheroscleros|arrhythmi|tachycardi|bradycardi|'
    r'SCAD|stent|EKG|ECG|palpitation|chest pain|blood pressure|cholesterol|'
    r'myocardial|\bMI\b|angina|prinzmetal|ischemi|'
    r'atrial fibrillation|\bafib\b|\ba-fib\b|\bPSVT\b|cardiomyopath|congestive heart|\bCHF\b|'
    r'\bHFpEF\b|\bHFrEF\b|ejection fraction|\bEF\b|\bCAD\b|microvascular|\bMVD\b|'
    r'valve|mitral|aortic|tricuspid|stenosis|regurgitat|'
    r'endocarditis|pericarditis|\batrial\b|congenital heart|\bLVAD\b|\bPOTS\b|dysautonomia|'
    r'bypass|\bCABG\b|angiogram|angioplasty|catheteriz|\bcath lab\b|\bTAVR\b|'
    r'echocardiogram|\becho\b|stress test|pacemaker|\bICD\b|defibrillator|ablation|troponin|calcium score|'
    r'beta.?blocker|metoprolol|toprol|statin|nitroglycerin|\bnitro\b|'
    r'plavix|clopidogrel|warfarin|coumadin|eliquis|xarelto|'
    r'diltiazem|amlodipine|imdur|ranexa|entresto|amiodarone|'
    r'lisinopril|blood thinner|calcium channel blocker|shortness of breath',
    re.IGNORECASE,
)
EXTRA_HEART_TERMS = re.compile(
    r"\bSVT\b|\bPVCs?\b|\bPACs?\b|arr?h?yth?mia|hypertension|\bHTN\b|\bBP\b|\bstroke\b|"
    r"aspirin|holter|\bzio\b|\bLDL\b|triglyceride|lipoprotein|pre-?eclampsia|"
    r"chest tightness|vasospasm|takotsubo|brilinta|ticagrelor",
    re.IGNORECASE,
)
EXTRA_HEART_ACRONYMS = re.compile(r"\b(?:TIA|QT|WPW|M?INOCA)\b")   # "Tia", "qt" are not cardiology
# HEART_KEYWORDS matches its acronyms case-insensitively. Lower-case "mi" is mostly
# Italian/Spanish "my", Hinglish "me" or miles, so MI counts only in capitals.
UPPERCASE_ONLY = frozenset({"mi"})
# Clinic booking/contact templates. Phrases patients also use are avoided: channel
# doctors end genuine answers with "Thanks for watching", and viewers write "why not
# book an appointment with a cardiologist" or "how can I get an online consultation?".
PROMO = re.compile(
    r"we can connect you|consultation fee|thank you for contacting dr|"
    r"thank you for showing your interest|(?:available for|offer|for) (?:an )?online consultations?|"
    r"book your appointment|if you wish to book|whats ?app|call us\b|contact us\b|visit our|"
    r"subscribe to (?:our|my) channel|download (?:our|the) app|play\.google\.com|"
    # a cardiology channel's pinned template, pasted under its own video titles (61 rows)
    r"suggestions,? comments and topics for us|improvise the channel|voice command search",
    re.IGNORECASE,
)


def is_mayo_fragment(platform: str, rtype: str, content: str) -> bool:
    """Search-result excerpts: `text... text ... text`, mostly under 250 chars.

    Mayo's full comments are long (median ~1,700 chars) and open with a reply header
    when they answer someone; the excerpts are short and stitched with ellipses.
    Tested on the raw content because the team's repeated-punctuation rule turns
    "..." into "." and erases the signal.
    """
    return (platform == "Mayo Clinic Connect" and rtype == "comment" and len(content) <= 400
            and "..." in content and not content.startswith("In reply to"))


# ---------- Sentiment dataset ----------

def load_records(path: Path) -> list[dict[str, str]]:
    """Read a five-column merged CSV or the legacy seven-column database export."""
    text = path.read_text(encoding="utf-8-sig")
    header, _, body = text.partition("\n")
    columns = [h.strip().lower() for h in header.rstrip("\r").split(",")]
    if columns == MERGED_HEADER:
        return parse_merged_csv(text)
    if columns != DB_HEADER:
        raise ValueError(f"Expected columns {MERGED_HEADER} or {DB_HEADER}")
    try:
        rows = parse_standard_csv(body)
    except (csv.Error, ValueError):
        rows = parse_pg_export(body)
    return [dict(zip(DB_HEADER, row)) for row in rows]


def normalize_whitespace(text: str) -> str:
    text = re.sub(r"\r\n|\r", "\n", text)      # normalize line endings
    text = re.sub(r"\n{2,}", "\n", text)       # collapse multiple newlines
    text = re.sub(r"[ \t]{2,}", " ", text)     # collapse repeated spaces/tabs
    return text.strip()


def strip_site_chrome(text: str) -> str:
    """Heart Sisters byline, book promo, "See also" links, references; Her Heart notes."""
    text = SEE_ALSO_INLINE.sub("", SEE_ALSO.sub("", text))
    return "\n".join(line for line in text.split("\n")
                     if not BLOG_LINE_DROP.match(line.strip()) and not SIGNOFF_LINE.match(line.strip()))


def transform(text: str) -> dict:
    """The notebook's Tasks 2-8, in its order, with the text fixes listed above."""
    group = MAYO_FOOTER.search(text)
    text = MAYO_FOOTER.sub("", text).strip()
    text = HTML_TAG.sub(" ", text)
    names = len(REDACT_PATTERN.findall(text))
    text = PLACEHOLDER.sub("", text)
    text = BARE_URL.sub("", RAW_URL.sub("", text))
    text = HANDLE.sub("", PHONE_LIKE.sub("", text))
    text = re.sub(r"\s{2,}", " ", text).strip()
    for pattern, repl in MARKDOWN_PATTERNS:
        text = pattern.sub(repl, text)
    text = normalize_whitespace(text)
    emoji = bool(EMOJI.search(text))
    text = EMOJI.sub("", text)
    text = REPEAT_PUNCT_PATTERN.sub(r"\1", text)
    return {"Content": text, "support_group": group.group(1).strip() if group else "",
            "n_redacted_names": names, "has_emoji": emoji, "content_len": len(text)}


def language(text: str) -> str:
    """langdetect as in the notebook, except where it is known to be wrong.

    `mul`: a fifth or more of the letters in another script (Hindi video titles with
    English words, Unicode small-caps spam, Arabic or Malayalam replies).
    """
    letters = LETTER.findall(text)
    if letters and len(LATIN_LETTER.findall(text)) / len(letters) < 0.8:
        try:
            code = detect(text)
        except LangDetectException:
            code = "unknown"
        return "mul" if code in ("en", "unknown") else code
    if is_hinglish(text):
        return "hi-Latn"
    if is_english(text):
        return "en"
    try:
        return detect(text)
    except LangDetectException:
        return "unknown"


def mentions_heart(text: str) -> bool:
    """HEART_KEYWORDS, with capitals-only MI/EF/CAD/ICD and the missing terms added."""
    if any(m.group(0).lower() not in UPPERCASE_ONLY or m.group(0)[:2].isupper()
           for m in HEART_KEYWORDS.finditer(text)):
        return True
    return bool(EXTRA_HEART_TERMS.search(text) or EXTRA_HEART_ACRONYMS.search(text))


def removed_links(text: str) -> str:
    """Domains and URLs that transform() deletes, for the keyword check only."""
    found = re.findall(r"\[URL:([^\]]*)\]", text) + RAW_URL.findall(text) + BARE_URL.findall(text)
    return " ".join(found)


def own_text(content: str) -> str:
    """The reply itself, without the parent prefix (a reply is not promo because its parent is)."""
    if content.startswith("[In reply to: ") and YT_REPLY_CLOSE in content:
        return content.split(YT_REPLY_CLOSE, 1)[1]
    return content


def run(input_path: Path, output_path: Path) -> tuple[list[dict], Counter]:
    if input_path.resolve() == output_path.resolve():
        raise ValueError("Output must not overwrite the input file")
    DetectorFactory.seed = 0
    records = sorted(load_records(input_path),
                     key=lambda r: (r["date"], TYPE_ORDER.get(r["type"].strip().casefold(), 9), r["id"]))
    seen = {rtype: set() for rtype in TYPE_ORDER}
    seen_text = set()
    kept, dropped = [], Counter()
    for original in records:
        rtype, platform = original["type"].strip().casefold(), original["platform"]
        raw = original["content"]
        if rtype not in seen:
            dropped["unknown type"] += 1
            continue
        if raw in seen[rtype]:                   # notebook Task 1: identical raw content
            dropped["duplicate"] += 1
            continue
        seen[rtype].add(raw)
        if is_mayo_fragment(platform, rtype, raw):
            dropped["Mayo search fragment"] += 1
            continue
        text = raw
        if platform == "Mayo Clinic Connect" and rtype == "comment":
            text = strip_mayo_reply_header(text)
        if rtype == "blog" or platform == "Heart Sisters":
            text = strip_site_chrome(text)
        if platform == "Reddit":
            text = strip_reddit_chrome(text)
        features = transform(text)
        if features["content_len"] < MIN_CHARS:
            dropped["too short"] += 1
            continue
        features["lang"] = language(features["Content"])
        if features["lang"] != "en":
            dropped["not English"] += 1
            continue
        if platform == "YouTube" and PROMO.search(own_text(features["Content"])):
            dropped["clinic promo"] += 1
            continue
        if not (platform == "Reddit" or mentions_heart(features["Content"]) or mentions_heart(removed_links(text))):
            dropped["no heart keyword"] += 1
            continue
        key = re.sub(r"[^a-z0-9]+", " ", features["Content"].lower()).strip()
        if len(key.split()) >= 5:
            if key in seen_text:
                dropped["duplicate"] += 1
                continue
            seen_text.add(key)
        date = pd.to_datetime(original["date"], errors="coerce")
        kept.append({"id": original["id"], "Date": "" if pd.isna(date) else str(date.date()),
                     "Type": rtype, "Platform": platform, "URL": original["url"], **features})
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(kept, columns=OUTPUT_FIELDS).to_csv(output_path, index=False, encoding="utf-8-sig")
    return kept, dropped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, nargs="?", default=DATA_DIR / "social_data_raw.csv")
    parser.add_argument("--output-dir", type=Path, default=DATA_DIR)
    args = parser.parse_args()
    output_path = args.output_dir / f"{args.input.stem}_sentiment.csv"
    kept, dropped = run(args.input, output_path)
    print(pd.DataFrame(kept).groupby(["Type", "Platform"]).size().to_string())
    print("\nDropped:", ", ".join(f"{k} {v:,}" for k, v in dropped.most_common()))
    print(f"Saved {len(kept):,} rows to {output_path}")


if __name__ == "__main__":
    main()
