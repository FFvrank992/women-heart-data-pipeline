"""One command: run every scraper, redact PII, merge into a single CSV.

    python run_all.py                 collect, redact, merge  (the usual one)
    python run_all.py --no-collect    redact and merge what is already on disk
    python run_all.py --include mayo  also run the scraper that needs a human
    python run_all.py --only youtube --start-date 2026-09-01 --end-date 2026-09-07
    python run_all.py status          what is on disk right now

What comes out
--------------
    output/raw/<platform>/…                each scraper's own native output
    output/processed/<platform>.csv        five columns, PII redacted
    output/merged/social_data_<date>.csv   the file to load into the database
    output/merged/pii_report_<date>.txt    the audit trail

The merged file is a full snapshot, not an increment, so re-running is always
safe: `scripts/csv_to_db.py` upserts on a content hash and skips what it has
already seen.

Mayo is not collected by default. Its scraper drives a non-headless Edge
browser, stops to let a human clear a verification page, and takes several
hours; leaving it in would mean this command could never run unattended. Its
already-collected data is still redacted and merged on every run -- only the
*collection* step is skipped. Pass `--include mayo` to crawl it too.

YouTube is collected by default through its Data API. Set `YOUTUBE_API_KEY`;
it collects the six built-in cardiac topic groups over the most recent week.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from common import io_utils, pii, schema
from common.sources import (DEFAULT_COLLECT, MERGED_DIR, PROCESSED_DIR, RAW_DIR,
                            SOURCES, harvest)


def _rule(title: str) -> None:
    print(f"\n{'=' * 66}\n{title}\n{'=' * 66}")


# ---------------- collect ----------------

def collect_one(name: str, timeout: int | None, allow_full_crawl: bool = False,
                start_date: str | None = None, end_date: str | None = None) -> bool | None:
    """Run one scraper as a subprocess.

    True   collected cleanly
    False  the scraper ran and failed
    None   not attempted -- a dependency is missing, or a guard blocked it

    A subprocess rather than an import: the six scrapers have incompatible
    module-level side effects (mayo starts a browser at import time, inspire
    takes a lock file) and one of them crashing must not take the pipeline
    down with it.
    """
    source = SOURCES[name]
    if source.collect is None:
        print(f"  {name:14} no automated collection step -- {source.notes}")
        return True

    # Pre-flight the imports. Without this, a missing dependency surfaces as a
    # traceback from inside the scraper and the operator has to work out which
    # package it wanted.
    missing = source.missing_requirements()
    if missing:
        print(f"  {name:14} [SKIP] missing {', '.join(missing)} -- "
              f"run: pip install -r requirements.txt")
        return None       # incomplete, but not a crash -- reported separately

    # A guard blocks collection that would be expensive or destructive to start
    # by accident -- today, an Inspire full re-crawl.
    if source.guard is not None and not allow_full_crawl:
        blocked = source.guard()
        if blocked:
            print(f"  {name:14} [SKIP] {blocked}")
            return None

    steps = source.collect_steps(start_date=start_date, end_date=end_date)
    started = time.time()
    for index, argv in enumerate(steps, start=1):
        # Source commands use the portable `python` spelling in the registry.
        # Resolve it to this process's interpreter so a run started inside the
        # project virtual environment does not silently launch a different
        # system Python without the scraper's dependencies.
        command = [sys.executable, *argv[1:]] if argv[0] == "python" else argv
        label = f"{name} {index}/{len(steps)}" if len(steps) > 1 else name
        print(f"  {label:14} running: {' '.join(command)}")
        try:
            result = subprocess.run(command, cwd=source.collect_cwd,
                                    timeout=timeout, check=False)
        except FileNotFoundError as exc:
            print(f"  {name:14} [FAIL] {exc}")
            return False
        except subprocess.TimeoutExpired:
            print(f"  {name:14} [FAIL] timed out after {timeout}s")
            return False
        if result.returncode != 0:
            # Stop at the first failed step: for YouTube a failed fetch means
            # the ETL that follows would only re-process stale data and report
            # success, hiding the real problem.
            print(f"  {name:14} [FAIL] step {index} exited {result.returncode} "
                  f"after {(time.time() - started) / 60:.1f} min")
            return False

    print(f"  {name:14} [ok] {(time.time() - started) / 60:.1f} min")
    return True


# ---------------- merge ----------------

def merge(frames: dict[str, pd.DataFrame], out_path: Path) -> tuple[pd.DataFrame, dict]:
    """Concatenate, dedupe, sort, validate, write."""
    if not frames:
        raise SystemExit("Nothing to merge -- no source produced any rows.")

    combined = pd.concat(list(frames.values()), ignore_index=True)[schema.COLUMNS]
    deduped, tally = schema.dedupe(combined)
    deduped = schema.sort_rows(deduped)

    problems = schema.validate(deduped, strict=True)
    blocking = [p for p in problems if "unknown" in p or "columns are" in p]
    if blocking:
        for problem in blocking:
            print(f"  [ERROR] {problem}")
        raise SystemExit("Merged data violates the five-column contract; refusing to write.")
    for problem in problems:
        print(f"  [warn] {problem}")

    io_utils.write_csv(deduped, out_path, columns=schema.COLUMNS)
    return deduped, tally


def write_report(path: Path, stats: dict[str, pii.Stats], info: dict[str, dict],
                 tally: dict, merged: pd.DataFrame) -> None:
    lines = [f"PII and merge report -- {date.today():%Y-%m-%d}", "=" * 66, ""]

    total = pii.Stats()
    for name in sorted(stats):
        lines.append(pii.format_report(stats[name], name))
        counts = info[name]
        lines.append(f"  files read           {counts.get('files', 0)}")
        within = counts.get("dedupe", {})
        lines.append(f"  rows                 {counts.get('raw_rows', 0):,} read -> "
                     f"{counts.get('rows', 0):,} kept "
                     f"({within.get('exact', 0):,} duplicate, "
                     f"{within.get('fragments', 0):,} fragment, "
                     f"{counts.get('dropped_empty', 0):,} emptied by redaction)")
        lines.append("")
        total.merge(stats[name])

    lines += ["=" * 66, pii.format_report(total, "ALL PLATFORMS"), "",
              "Merge", "-" * 66,
              f"  rows in              {tally['input']:,}",
              f"  exact duplicates     {tally['exact']:,} removed",
              f"  fragment duplicates  {tally['fragments']:,} removed",
              f"  rows out             {len(merged):,}", ""]

    lines += ["By platform", "-" * 66]
    for platform, count in merged["Platform"].value_counts().items():
        lines.append(f"  {platform:<24} {count:>8,}")
    lines += ["", "By type", "-" * 66]
    for type_name, count in merged["Type"].value_counts().items():
        lines.append(f"  {type_name:<24} {count:>8,}")

    dated = merged[merged["Date"] != ""]
    if len(dated):
        lines += ["", f"Date range               {dated['Date'].min()} .. {dated['Date'].max()}"]

    # Short rows are not an error, but Mayo's 970 orphaned search-result
    # snippets live in here and the team asked to see them called out.
    short = merged[merged["Content"].str.len() < 200]
    if len(short):
        lines += ["", f"Rows under 200 chars     {len(short):,}"]
        for platform, count in short["Platform"].value_counts().items():
            lines.append(f"  {platform:<24} {count:>8,}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------- status ----------------

def step_status() -> None:
    print(f"{'source':<16}{'raw files':>11}{'processed':>12}{'history':>10}   notes")
    print("-" * 100)
    for name, source in SOURCES.items():
        processed = PROCESSED_DIR / f"{name}.csv"
        rows = f"{io_utils.count_rows(processed):,}" if processed.exists() else "-"
        print(f"{name:<16}{len(source.raw_files()):>11}{rows:>12}"
              f"{len(source.history_files()):>10}   {source.notes[:52]}")

    merged = sorted(MERGED_DIR.glob("social_data_*.csv")) if MERGED_DIR.exists() else []
    print(f"\nmerged datasets ({MERGED_DIR})")
    if merged:
        for path in merged[-5:]:
            print(f"  {path.name:<34}{io_utils.count_rows(path):>9,} rows  "
                  f"{path.stat().st_size / 1e6:6.1f} MB")
    else:
        print("  none yet -- run `python scraper/run_all.py` from the repository root")


# ---------------- CLI ----------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="run", choices=["run", "status"])
    parser.add_argument("--only", nargs="+", metavar="SOURCE", choices=list(SOURCES),
                        help="process only these sources (default: all six)")
    parser.add_argument("--include", nargs="+", default=[], metavar="SOURCE",
                        choices=list(SOURCES),
                        help="also collect a source that is skipped by default (mayo)")
    parser.add_argument("--skip", nargs="+", default=[], metavar="SOURCE",
                        choices=list(SOURCES), help="do not collect these")
    parser.add_argument("--no-collect", action="store_true",
                        help="skip collection; redact and merge what is on disk")
    parser.add_argument("--no-history", action="store_true",
                        help="ignore the approved private history directory, use fresh scrapes only")
    parser.add_argument("--timeout", type=int, default=None,
                        help="seconds to allow each scraper before giving up")
    parser.add_argument("--start-date", metavar="YYYY-MM-DD",
                        help="collection window start for sources that support it")
    parser.add_argument("--end-date", metavar="YYYY-MM-DD",
                        help="collection window end for sources that support it")
    parser.add_argument("--allow-full-crawl", action="store_true",
                        help="override guards that block an accidental full re-crawl "
                             "(Inspire: ~12,488 threads, roughly 13 hours)")
    parser.add_argument("--out", default=None, help="merged CSV path")
    args = parser.parse_args(argv)
    if bool(args.start_date) != bool(args.end_date):
        parser.error("--start-date and --end-date must be supplied together")
    if args.start_date is None:
        args.start_date = f"{date.today() - timedelta(days=6):%Y-%m-%d}"
        args.end_date = f"{date.today():%Y-%m-%d}"

    if args.command == "status":
        step_status()
        return 0

    selected = args.only or list(SOURCES)
    started = time.time()

    # --- collect ---
    failures: list[str] = []
    unmet: list[str] = []
    if args.no_collect:
        _rule("Collect -- skipped (--no-collect)")
    else:
        to_collect = [n for n in selected
                      if (n in DEFAULT_COLLECT or n in args.include) and n not in args.skip]
        skipped = [n for n in selected if n not in to_collect]
        _rule(f"Collect -- {len(to_collect)} source(s)")
        for name in to_collect:
            outcome = collect_one(name, args.timeout, args.allow_full_crawl,
                                  args.start_date, args.end_date)
            if outcome is None:
                unmet.append(name)
            elif outcome is False:
                failures.append(name)
        for name in skipped:
            reason = "needs a human, use --include" if SOURCES[name].interactive else "--skip"
            print(f"  {name:14} not collected ({reason}); existing data still processed")

    # --- harvest: normalise + redact, per source ---
    _rule("Redact and normalise")
    frames, stats, info = {}, {}, {}
    for name in selected:
        frame, source_stats, counts = harvest(SOURCES[name],
                                              include_history=not args.no_history)
        if len(frame):
            frames[name] = frame
        stats[name], info[name] = source_stats, counts
        print(f"  {name:<14} {counts['raw_rows']:>7,} raw -> {counts['rows']:>7,} rows   "
              f"[NAME] {source_stats.counts.get('name', 0):>5,}  "
              f"[USER] {source_stats.counts.get('handle', 0):>5,}  "
              f"[URL] {source_stats.counts.get('url', 0):>4,}")

    # --- merge ---
    _rule("Merge")
    stamp = f"{date.today():%Y%m%d}"
    out_path = Path(args.out) if args.out else MERGED_DIR / f"social_data_{stamp}.csv"
    merged, tally = merge(frames, out_path)
    print(f"  {tally['input']:,} rows in -> {tally['exact']:,} exact + "
          f"{tally['fragments']:,} fragment duplicates removed -> {len(merged):,} rows out")
    print(f"  wrote {out_path}  ({out_path.stat().st_size / 1e6:.1f} MB)")

    report_path = MERGED_DIR / f"pii_report_{stamp}.txt"
    write_report(report_path, stats, info, tally, merged)
    print(f"  wrote {report_path}")

    _rule("Done")
    print(f"  {len(merged):,} rows across {merged['Platform'].nunique()} platforms "
          f"in {(time.time() - started) / 60:.1f} min")
    if unmet:
        print(f"  [warn] not collected: {', '.join(unmet)} "
              f"(see the [SKIP] lines above)")
    if failures:
        print(f"  [warn] collection FAILED for: {', '.join(failures)}")
    if unmet or failures:
        print("  Existing data for those sources was still redacted and merged.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
