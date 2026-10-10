"""Encoding-safe CSV reading and atomic writing.

Why this exists
---------------
Historical exports are not uniformly encoded. Some Heart Sisters files were
opened and re-saved by Excel on a Chinese Windows machine and are GB18030, so a
plain utf-8 read fails. Other exports are utf-8 with a BOM. Sniffing beats
guessing: `read_csv` tries a candidate list and scores the result, so a scraper
never has to care.

Writing always goes through `write_csv`, which is utf-8-sig (what Excel expects)
and atomic (write to a temp file, then replace), so an interrupted run cannot
leave a half-written CSV that the next stage would happily parse.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd

# Ordered by likelihood for this project. utf-8-sig first (what our scrapers
# emit), then the two Excel round-trip codepages we have actually seen.
CANDIDATE_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "cp1252", "latin1")

# Mojibake tells: if decoding produced these, the guess was wrong. U+FFFD is the
# replacement char; the rest are the classic cp1252-reading-utf8 signatures.
_BAD_DECODE_MARKERS = ("�", "â€", "Ã©", "Â ", "¡¯", "¡°", "¡±")


def sniff_encoding(path: str | Path, sample_bytes: int = 400_000) -> str:
    """Pick the encoding that decodes `path` with the fewest mojibake tells.

    A strict-mode failure eliminates a candidate outright. Among the survivors
    the one with the lowest marker count wins; ties go to the earlier candidate,
    which is why CANDIDATE_ENCODINGS is ordered by likelihood. latin1 never
    fails on any byte, so it is the guaranteed backstop at the end of the list.
    """
    raw = Path(path).read_bytes()[:sample_bytes]
    best, best_score = "utf-8", None
    for enc in CANDIDATE_ENCODINGS:
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        score = sum(text.count(marker) for marker in _BAD_DECODE_MARKERS)
        if best_score is None or score < best_score:
            best, best_score = enc, score
        if score == 0:
            break
    return best


def read_csv(path: str | Path, **kwargs) -> pd.DataFrame:
    """Read a CSV as strings, with the encoding sniffed rather than assumed.

    Everything is read as `str` with NA detection off: these are text corpora,
    and letting pandas turn "None"/"NA"/"null" in a post body into a float NaN
    silently corrupts content.
    """
    path = Path(path)
    kwargs.setdefault("dtype", str)
    kwargs.setdefault("keep_default_na", False)
    kwargs.setdefault("na_filter", False)
    kwargs.setdefault("encoding", sniff_encoding(path))
    return pd.read_csv(path, **kwargs)


def write_csv(df: pd.DataFrame, path: str | Path, columns: Sequence[str] | None = None) -> Path:
    """Write `df` to `path` as utf-8-sig, atomically.

    The temp file sits in the destination directory (not the system temp dir)
    so the final `os.replace` is a same-filesystem rename, which is atomic.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if columns is not None:
        df = df[list(columns)]
    tmp = path.with_name(path.name + ".tmp")
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, path)
    return path


def write_rows(rows: Iterable[dict], path: str | Path, columns: Sequence[str]) -> Path:
    """Same contract as `write_csv`, for code that has dicts rather than a frame.

    Used by the stdlib-only scrapers (heart_sisters, reddit), which deliberately
    avoid a pandas dependency.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)
    return path


def count_rows(path: str | Path) -> int:
    """Row count excluding the header. Streamed, so file size does not matter."""
    path = Path(path)
    if not path.exists():
        return 0
    with path.open(encoding=sniff_encoding(path), newline="") as handle:
        return max(sum(1 for _ in csv.reader(handle)) - 1, 0)
