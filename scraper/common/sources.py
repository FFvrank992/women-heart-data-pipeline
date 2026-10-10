"""The registry of six data sources, and the harvest step that normalises them.

Each scraper keeps its own collection logic and its own native output -- this
module is the adapter layer that turns any of them into the five-column
contract. A `Source` declares three things:

    where its raw output lands          `raw_globs`
    which delivered history belongs     `history`
    how its columns are named           `column_map`

`harvest()` then does the same four things for every source: read, normalise,
redact, write. That is what keeps the six platforms honest -- there is exactly
one code path, so no platform can quietly skip PII or invent a `Type` value.

Adding a seventh platform means adding a `Source` here and nothing else.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd

from . import io_utils, pii, schema

SCRAPER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRAPER_DIR.parent

OUTPUT_DIR = SCRAPER_DIR / "output"
RAW_DIR = OUTPUT_DIR / "raw"
PROCESSED_DIR = OUTPUT_DIR / "processed"
MERGED_DIR = OUTPUT_DIR / "merged"

# Historical exports are intentionally not committed: even after direct-identifier
# redaction, patient narratives can remain indirectly identifiable. Operators can
# point the pipeline at an approved local copy without changing source code.
HISTORY_DIR = Path(os.environ.get(
    "WOMEN_HEART_HISTORY_DIR",
    PROJECT_ROOT / "data" / "private" / "history",
))


@dataclass
class Source:
    """One platform: how to collect it, where its output is, how to read it."""

    name: str
    platform: str
    # The collection step: one argv list, or several to be run in order.
    # None means the source cannot be collected automatically. A source may
    # declare several steps when it has a native fetch followed by a local
    # conversion into the raw CSV that harvest() reads.
    collect: list[str] | list[list[str]] | None = None
    collect_cwd: Path = SCRAPER_DIR
    # Globs for this scraper's own output, relative to the scraper directory.
    raw_globs: list[str] = field(default_factory=list)
    # Delivered history files, relative to HISTORY_DIR.
    history: list[str] = field(default_factory=list)
    column_map: dict[str, str] = field(default_factory=dict)
    shortcodes: bool = False
    # Set when the collection step needs a human present or takes hours.
    interactive: bool = False
    # Importable module names the collection step needs. Checked before the
    # subprocess starts, so a missing dependency produces one actionable line
    # instead of a traceback from inside someone else's scraper.
    requires: list[str] = field(default_factory=list)
    # Returns a message when collection would be a bad idea, or None to proceed.
    # Checked before the subprocess starts; `run_all.py --allow-full-crawl`
    # overrides it.
    guard: "Callable[[], str | None] | None" = None
    # Collection steps that accept the standard --start-date/--end-date window
    # passed by run_all.py. An empty list means this source has no date window.
    date_range_steps: list[int] = field(default_factory=list)
    notes: str = ""

    def collect_steps(self, *, start_date: str | None = None,
                      end_date: str | None = None) -> list[list[str]]:
        """Normalise `collect` to a list of argv lists."""
        if not self.collect:
            return []
        if isinstance(self.collect[0], str):
            steps = [list(self.collect)]     # type: ignore[list-item]
        else:
            steps = [list(step) for step in self.collect]  # type: ignore[arg-type]
        if start_date is not None and end_date is not None:
            for step_index in self.date_range_steps:
                steps[step_index] += ["--start-date", start_date,
                                      "--end-date", end_date]
        return steps

    def missing_requirements(self) -> list[str]:
        import importlib.util
        return [module for module in self.requires
                if importlib.util.find_spec(module) is None]

    def raw_files(self) -> list[Path]:
        found: list[Path] = []
        for pattern in self.raw_globs:
            found.extend(Path(p) for p in glob.glob(str(SCRAPER_DIR / pattern)))
        return sorted(set(found))

    def history_files(self) -> list[Path]:
        return [HISTORY_DIR / name for name in self.history
                if (HISTORY_DIR / name).exists()]


def _inspire_crawl_record_guard() -> str | None:
    """Refuse to silently start a 13-hour full re-crawl.

    The Inspire pipeline skips any thread listed in `data/state/crawled_urls.csv`,
    can be restored from private project storage. Without it the crawler
    re-fetches everything from scratch at its deliberate ~18 requests per
    minute -- about 13 hours. The public portfolio repo excludes the record
    because it contains source URLs, so this guard is essential on a fresh clone.
    """
    record = SCRAPER_DIR / "src" / "womenheart_inspire" / "data" / "state" / "crawled_urls.csv"
    if not record.exists():
        return ("crawl record is missing, so this would re-crawl the whole corpus "
                "(~12,488 threads, ~13 hours). Restore "
                "src/womenheart_inspire/data/state/crawled_urls.csv from approved "
                "private storage, or pass "
                "--allow-full-crawl if a full re-crawl is really what you want.")
    if io_utils.count_rows(record) < 100:
        return ("crawl record has almost nothing in it, so this would re-crawl "
                "the whole corpus (~13 hours). Pass --allow-full-crawl to proceed "
                "anyway.")
    return None


SOURCES: dict[str, Source] = {
    "inspire": Source(
        name="inspire",
        platform="Inspire",
        collect=["python", "pipeline.py", "run"],
        collect_cwd=SCRAPER_DIR / "src" / "womenheart_inspire",
        raw_globs=["src/womenheart_inspire/data/processed/*.csv"],
        history=["womenheart_share_20160101_20260907.csv"],
        requires=["pandas", "requests", "bs4"],
        guard=_inspire_crawl_record_guard,
        notes="Incremental after restoring private crawl state; guarded otherwise.",
    ),
    "heart_sisters": Source(
        name="heart_sisters",
        platform="Heart Sisters",
        collect=["python", "src/heart_sisters/heart_sisters_scraper.py", "--overwrite"],
        raw_globs=["output/raw/heart_sisters/*.csv"],
        history=["heart_sisters_blogs_2016_present.csv",
                 "heart_sisters_2016_present.csv"],
        notes="WordPress.com REST API, no key. Full pull takes tens of minutes.",
    ),
    "her_heart": Source(
        name="her_heart",
        platform="Her Heart",
        collect=["python", "src/her_heart/herheart_scraper.py"],
        raw_globs=["output/raw/her_heart/*.csv"],
        history=["herheart_blog_posts.csv"],
        column_map={"text": "Content", "url": "URL", "date": "Date"},
        requires=["requests", "bs4"],
        # WPBakery renders [vc_row] layout tags into 29 of the 208 post bodies.
        shortcodes=True,
        notes="WordPress REST API, 208 posts, under a minute.",
    ),
    "mayo": Source(
        name="mayo",
        platform="Mayo Clinic Connect",
        collect=["python", "src/mayo/mayo.py"],
        raw_globs=["output/raw/mayo/*.csv"],
        history=["mayo_2016_2026.csv"],
        column_map={"Text/Content": "Content", "Source/Platform": "Platform"},
        interactive=True,
        requires=["playwright", "bs4"],
        notes="Needs a human: non-headless Edge, manual verification, several hours. "
              "Skipped by default -- pass --include mayo to collect.",
    ),
    "reddit": Source(
        name="reddit",
        platform="Reddit",
        collect=["python", "src/reddit/parse_reddit_heartattack.py",
                 "--out-dir", str(RAW_DIR / "reddit")],
        raw_globs=["output/raw/reddit/*.csv"],
        history=["heartattack_posts.csv"],
        notes="Offline parse of a manually captured TXT snapshot; collects nothing new. "
              "Switches to the research API once access is approved.",
    ),
    "youtube": Source(
        name="youtube",
        platform="YouTube",
        # Fetch native JSON first, then convert it to the same raw CSV boundary
        # every other source gives harvest(). The second step only transforms;
        # common/pii.py remains the single redaction path.
        collect=[
            ["python", "src/youtube/fetch_youtube_raw.py"],
            ["python", "src/youtube/youtube_etl_pipeline.py"],
        ],
        date_range_steps=[0, 1],
        raw_globs=["output/raw/youtube/*.csv"],
        history=["youtube_20260908.csv"],
        requires=["googleapiclient", "dotenv"],
        notes="YouTube Data API fetch followed by local JSON-to-CSV conversion. "
              "Six built-in cardiac topics, four videos per topic per week; "
              "set YOUTUBE_API_KEY.",
    ),
}

# Collected automatically by `run_all.py` with no flags. Mayo is excluded
# because it cannot run unattended; see its `notes`.
DEFAULT_COLLECT = [name for name, source in SOURCES.items() if not source.interactive]


def load_source_frame(source: Source, *, include_history: bool = True) -> pd.DataFrame:
    """Read every file belonging to `source` into one raw frame.

    Files are read with the encoding sniffed rather than assumed: the delivered
    `heart_sisters_*.csv` are GB18030 from an Excel round trip, while the
    scrapers themselves emit utf-8-sig.
    """
    files = source.raw_files()
    if include_history:
        files += source.history_files()

    frames = []
    for path in files:
        try:
            frame = io_utils.read_csv(path)
        except Exception as exc:                        # noqa: BLE001
            print(f"    [warn] could not read {path.name}: {exc}")
            continue
        if frame.empty:
            continue
        if source.column_map:
            frame = frame.rename(columns=source.column_map)
        frame["_source_file"] = path.name
        frames.append(frame)

    if not frames:
        return pd.DataFrame(columns=schema.COLUMNS + ["_source_file"])
    return pd.concat(frames, ignore_index=True)


def harvest(source: Source, *, include_history: bool = True,
            write: bool = True) -> tuple[pd.DataFrame, pii.Stats, dict]:
    """Read -> normalise -> redact -> write. The only path data takes.

    Returns the five-column frame, the PII tally for the audit report, and a
    small dict of counts for the run log.
    """
    raw = load_source_frame(source, include_history=include_history)
    info = {"files": len(source.raw_files()) + (len(source.history_files())
                                                if include_history else 0),
            "raw_rows": len(raw)}
    if raw.empty:
        info.update(rows=0, dropped_empty=0)
        return pd.DataFrame(columns=schema.COLUMNS), pii.Stats(), info

    frame = schema.normalise(raw, platform=source.platform)

    stats = pii.Stats()
    frame["Content"] = [pii.redact(text, stats, shortcodes=source.shortcodes)
                        for text in frame["Content"]]

    # Redaction can empty a row whose entire body was a bare link or a handle.
    # Such a row carries no content for the database, so it is dropped here
    # rather than becoming a blank record someone has to explain later.
    before = len(frame)
    frame = frame[frame["Content"].str.strip() != ""].reset_index(drop=True)
    dropped_empty = before - len(frame)

    # Dedupe within the source, not only at the merge. An operator may restore
    # approved history beside a fresh scrape, so the same row can appear in both.
    # Collapsing here keeps each processed file clean and the merge tally useful.
    frame, source_tally = schema.dedupe(frame)
    frame = schema.sort_rows(frame)
    info.update(rows=len(frame), dropped_empty=dropped_empty, dedupe=source_tally)

    if write:
        PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
        io_utils.write_csv(frame, PROCESSED_DIR / f"{source.name}.csv",
                           columns=schema.COLUMNS)
    return frame, stats, info
