"""Turn the raw scrape into the flat five-column dataset the team loads into the database.

Columns (exactly these five, in this order)
    Date      publication date, YYYY-MM-DD (site local time). Day precision only, but
              rows are ordered by the full timestamp, so within a day the sequence is
              still correct.
    Type      "post" or "comment". A comment is what the scraper calls a reply; the name
              is aligned with the shared schema the rest of the team uses.
    Platform  always "Inspire"
    URL       the thread address. Comments have no URL of their own and carry the URL of
              the thread they belong to -- so **equal URL means same thread**, and that
              is the join key inside this flat file.
    Content   cleaned body text (mojibake repaired, Unicode normalized, links/emails/
              phone numbers replaced with placeholders)

Range rule
    Only threads whose *original post* falls inside the date range are kept, together
    with all of their comments. That way every comment has its parent post in the same
    file and no comment is left without the message it replies to.
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

# The shared engine, not this package's local `clean.py`. `clean.py` grew here
# first and became the basis for `scraper/common/pii.py`; now that all six
# platforms run the same policy, Inspire follows it too rather than keeping a
# private copy that would drift. The extra coverage this brings Inspire is
# personal names and @handles -- `clean.py` only handled links, emails and
# phone numbers.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from common import pii

PLATFORM = "Inspire"
COLUMNS = ["Date", "Type", "Platform", "URL", "Content"]

# The corpus starts in 2006, but the shared dataset covers 2016 onward by default.
DEFAULT_FROM = "2016-01-01"


def _clean_series(texts, stats):
    """Clean a column of body text. Returns a list of cleaned strings."""
    return [pii.redact(t, stats) for t in texts]


def main(raw_prefix, out, date_from=DEFAULT_FROM, date_to=None, quiet=False):
    say = (lambda *a: None) if quiet else print

    posts_csv = Path(f"{raw_prefix}_posts.csv")
    replies_csv = Path(f"{raw_prefix}_replies.csv")
    if not posts_csv.exists():
        raise SystemExit(f"No raw data at {posts_csv}. Run the crawl step first.")

    P = pd.read_csv(posts_csv, encoding="utf-8-sig")
    R = pd.read_csv(replies_csv, encoding="utf-8-sig") if replies_csv.exists() \
        else pd.DataFrame(columns=["reply_id", "post_id", "post_url",
                                   "publish_time_iso", "content"])
    say(f"raw: {len(P):,} posts | {len(R):,} replies")

    # A crash between writing the raw rows and writing the record line makes a thread get
    # crawled twice, so drop duplicates before anything else.
    P = P.drop_duplicates(subset="post_id", keep="first")
    R = R.drop_duplicates(subset="reply_id", keep="first")

    P["d"] = pd.to_datetime(P.publish_time_iso, errors="coerce")
    R["d"] = pd.to_datetime(R.publish_time_iso, errors="coerce")
    P = P[P.d.notna()]
    R = R[R.d.notna()]

    lo = pd.Timestamp(date_from) if date_from else pd.Timestamp.min
    hi = (pd.Timestamp(date_to) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)) \
        if date_to else pd.Timestamp.max

    keep = P[(P.d >= lo) & (P.d <= hi)]
    kids = R[R.post_id.isin(set(keep.post_id))]
    lo_s = format(pd.Timestamp(date_from), "%Y-%m-%d") if date_from else "start of corpus"
    hi_s = format(pd.Timestamp(date_to), "%Y-%m-%d") if date_to else "no upper bound"
    say(f"in range ({lo_s} .. {hi_s}): {len(keep):,} posts | {len(kids):,} comments")

    say("cleaning...")
    stats = pii.Stats()
    post_text = _clean_series(keep.content.tolist(), stats)
    reply_text = _clean_series(kids.content.tolist(), stats)
    say(pii.format_report(stats, PLATFORM))

    posts = pd.DataFrame({
        "Date": keep.d.dt.strftime("%Y-%m-%d"),
        "Type": "post",
        "Platform": PLATFORM,
        "URL": keep.url.values,
        "Content": post_text,
        # Sort within a thread by time, with the original post always first: the
        # trailing 0/1 breaks the tie when a reply shares the post's timestamp.
        "_sort": keep.url.values + "|" + keep.d.dt.strftime("%Y%m%d%H%M%S") + "|0",
    })
    comments = pd.DataFrame({
        "Date": kids.d.dt.strftime("%Y-%m-%d"),
        "Type": "comment",
        "Platform": PLATFORM,
        "URL": kids.post_url.values,
        "Content": reply_text,
        "_sort": kids.post_url.values + "|" + kids.d.dt.strftime("%Y%m%d%H%M%S") + "|1",
    })

    df = (pd.concat([posts, comments], ignore_index=True)
            .sort_values("_sort", kind="stable")
            .drop(columns="_sort")[COLUMNS])

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False, encoding="utf-8-sig")

    say(f"wrote {len(df):,} rows -> {out.name} ({out.stat().st_size / 1e6:.1f} MB)")
    return {"posts": len(keep), "comments": len(kids), "rows": len(df), "path": str(out)}


if __name__ == "__main__":
    ROOT = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(
        description="Build the five-column dataset from the raw CSVs. Normally driven "
                    "by pipeline.py; run directly only for debugging.")
    ap.add_argument("--raw-prefix", default=str(ROOT / "data" / "raw" / "inspire_raw"))
    ap.add_argument("--out",
                    default=str(ROOT / "data" / "processed" / "inspire_dataset.csv"))
    ap.add_argument("--from", dest="date_from", default=DEFAULT_FROM,
                    help=f"earliest post date to include (default {DEFAULT_FROM}); "
                         f"pass 'all' for the whole corpus back to 2006")
    ap.add_argument("--to", dest="date_to", default=None,
                    help="latest post date to include (default: no upper bound)")
    args = ap.parse_args()

    main(raw_prefix=args.raw_prefix, out=args.out,
         date_from=None if args.date_from == "all" else args.date_from,
         date_to=args.date_to)
