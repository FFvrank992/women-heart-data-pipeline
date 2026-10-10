"""Entry point for the Inspire scraper pipeline.

    python pipeline.py run       crawl new threads -> rebuild the dataset (the usual one)
    python pipeline.py crawl     crawl only
    python pipeline.py export    rebuild the dataset from the raw CSVs only
    python pipeline.py status    show what is on disk

What comes out
--------------
    data/raw/inspire_raw_posts.csv      raw scrape, all scraped fields, append-only
    data/raw/inspire_raw_replies.csv
    data/processed/inspire_dataset.csv  the five-column dataset to load into the database
    data/state/crawled_urls.csv         crawl record -- URLs here are never crawled again

The crawl record ships pre-seeded with everything already collected, so the first run on
a new machine picks up only threads that are genuinely new.

Note that the raw CSVs start out empty: only threads this installation crawls itself land
there. The historical corpus is handed over as a ready-made five-column CSV, so there is
nothing to re-crawl and nothing to re-process.
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

GROUP = "womenheart"

# Every path is anchored to this file's directory, so the pipeline runs correctly from
# any working directory.
DATA = ROOT / "data"
RAW_PREFIX = DATA / "raw" / "inspire_raw"
DATASET = DATA / "processed" / "inspire_dataset.csv"
RECORD = DATA / "state" / "crawled_urls.csv"
LOGDIR = ROOT / "logs"
LOCK = ROOT / ".pipeline.lock"

log = logging.getLogger("pipeline")


# ---------------- Infrastructure ----------------

def ensure_dirs():
    for d in (DATA / "raw", DATA / "processed", DATA / "state", LOGDIR):
        d.mkdir(parents=True, exist_ok=True)


def setup_logging(verbose=True):
    LOGDIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOGDIR / f"pipeline_{datetime.now():%Y%m%d}.log",
                             encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if verbose:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


class RunLock:
    """Stop two instances from appending to the same CSVs and interleaving bad rows."""

    def __enter__(self):
        if LOCK.exists():
            try:
                info = json.loads(LOCK.read_text(encoding="utf-8"))
            except Exception:
                info = {}
            pid = info.get("pid")
            if pid and _pid_alive(pid):
                raise SystemExit(
                    f"Another instance is already running (PID {pid}, "
                    f"started {info.get('started')}).\n"
                    f"If you are sure it is gone, delete {LOCK.name} and retry.")
            log.warning("Found a stale lock (PID %s no longer exists), taking it over", pid)
        LOCK.write_text(json.dumps(
            {"pid": os.getpid(), "started": datetime.now().isoformat(timespec="seconds")}),
            encoding="utf-8")
        return self

    def __exit__(self, *exc):
        LOCK.unlink(missing_ok=True)
        return False


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def _count_rows(path):
    """Row count of a CSV, excluding the header. Streamed, so file size does not matter."""
    if not Path(path).exists():
        return 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        import csv
        return max(sum(1 for _ in csv.reader(f)) - 1, 0)


# ---------------- Steps ----------------

def step_crawl(target, relevance):
    from scraper import InspireCrawler

    c = InspireCrawler(group=GROUP, target_count=target, raw_prefix=str(RAW_PREFIX),
                       record=str(RECORD), relevance=relevance, resume=True)
    log.info("Crawl started | %s threads already in the record", f"{len(c.seen_urls):,}")
    t0 = time.time()
    new_p, new_r = c.run()
    log.info("Crawl finished | added %s threads / %s replies | took %.1f min",
             f"{new_p:,}", f"{new_r:,}", (time.time() - t0) / 60)
    if c.failed:
        log.warning("Failed to crawl %d URLs (first 10 below)", len(c.failed))
        for u in c.failed[:10]:
            log.warning("    %s", u)
    return new_p, new_r


def step_export(date_from, date_to, quiet):
    import export

    t0 = time.time()
    log.info("Export started")
    res = export.main(raw_prefix=str(RAW_PREFIX), out=str(DATASET),
                      date_from=date_from, date_to=date_to, quiet=quiet)
    log.info("Export finished | %s rows (%s posts + %s comments) | took %.1f min",
             f"{res['rows']:,}", f"{res['posts']:,}", f"{res['comments']:,}",
             (time.time() - t0) / 60)
    return res


def _describe(label, path):
    p = Path(path)
    if p.exists():
        print(f"  {label:26} {_count_rows(p):>9,} rows  "
              f"{p.stat().st_size / 1e6:7.1f} MB  "
              f"{datetime.fromtimestamp(p.stat().st_mtime):%Y-%m-%d %H:%M}")
    else:
        print(f"  {label:26} {'-':>9}        -  not generated yet")


def step_status():
    print(f"Crawl record   {_count_rows(RECORD):,} threads already crawled  "
          f"({RECORD.name})")

    print("\nRaw scrape  (data/raw/)")
    _describe("inspire_raw_posts.csv", f"{RAW_PREFIX}_posts.csv")
    _describe("inspire_raw_replies.csv", f"{RAW_PREFIX}_replies.csv")

    # Everything in data/processed/ shares the five-column schema, so the team loads the
    # whole directory: the delivered history plus whatever this installation has added.
    print("\nFive-column datasets  (data/processed/)")
    found = sorted((DATA / "processed").glob("*.csv")) if (DATA / "processed").exists() else []
    if found:
        total = 0
        for p in found:
            n = _count_rows(p)
            total += n
            _describe(p.name, p)
        print(f"  {'':26} {total:>9,} rows total")
    else:
        print("  none yet")

    if not Path(f"{RAW_PREFIX}_posts.csv").exists():
        print("\nThe raw CSVs stay empty until this installation crawls something itself."
              "\nThe historical corpus is delivered as a ready-made five-column CSV.")
    if LOCK.exists():
        print(f"\nNote: {LOCK.name} exists, an instance may be running.")


# ---------------- CLI ----------------

def main():
    ap = argparse.ArgumentParser(
        description="Inspire scraper pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python pipeline.py run                  crawl new threads, rebuild dataset\n"
               "  python pipeline.py run --force-export   rebuild even with nothing new\n"
               "  python pipeline.py crawl --target 20    stop after 20 new threads\n"
               "  python pipeline.py export --from all    rebuild covering 2006 onward\n"
               "  python pipeline.py status               show what is on disk\n")
    ap.add_argument("command", choices=["run", "crawl", "export", "status"])
    ap.add_argument("--target", type=int, default=None,
                    help="stop after this many NEW threads; omit to crawl everything new")
    ap.add_argument("--relevance", choices=["terms", "topic", "off"], default="terms",
                    help="relevance filter mode (default: terms, the cardiovascular lexicon)")
    ap.add_argument("--from", dest="date_from", default=None,
                    help="earliest post date for the dataset (default 2016-01-01); "
                         "pass 'all' for the whole corpus back to 2006")
    ap.add_argument("--to", dest="date_to", default=None,
                    help="latest post date for the dataset (default: no upper bound)")
    ap.add_argument("--force-export", action="store_true",
                    help="on run, rebuild the dataset even when no new threads were found")
    ap.add_argument("--quiet", action="store_true",
                    help="write to the log file only, do not print to the terminal")
    args = ap.parse_args()

    if args.command == "status":
        step_status()
        return 0

    import export
    date_from = export.DEFAULT_FROM if args.date_from is None else \
        (None if args.date_from == "all" else args.date_from)

    ensure_dirs()
    setup_logging(verbose=not args.quiet)
    log.info("=" * 58)
    log.info("pipeline %s starting", args.command)

    try:
        with RunLock():
            if args.command == "crawl":
                step_crawl(args.target, args.relevance)
            elif args.command == "export":
                step_export(date_from, args.date_to, args.quiet)
            else:  # run
                new_p, _ = step_crawl(args.target, args.relevance)
                if new_p or args.force_export:
                    step_export(date_from, args.date_to, args.quiet)
                else:
                    log.info("No new threads, skipping export "
                             "(use --force-export to rebuild anyway)")
    except KeyboardInterrupt:
        log.warning("Interrupted by user. Everything crawled so far is already on disk; "
                    "re-running the same command resumes from the crawl record.")
        return 130
    except Exception:
        log.exception("Pipeline failed")
        return 1

    log.info("pipeline %s done", args.command)
    return 0


if __name__ == "__main__":
    sys.exit(main())
