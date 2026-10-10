"""Tests for the standalone model-training dataset builder."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "data_preprocessing"
sys.path.insert(0, str(SRC))

import prepare_dataset as dataset  # noqa: E402


HEART_TEXT = "My cardiologist found a blocked artery after my heart attack last year."


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_loads_five_column_merged_contract(tmp_path: Path) -> None:
    source = _write_csv(tmp_path / "merged.csv", ["Date", "Type", "Platform", "URL", "Content"], [{
        "Date": "2026-01-02", "Type": "post", "Platform": "Inspire",
        "URL": "https://example.invalid/thread/1", "Content": HEART_TEXT,
    }])
    records = dataset.load_records(source)
    assert len(records) == 1
    assert records[0]["id"].startswith("Inspire_")
    assert records[0]["content"] == HEART_TEXT


def test_loads_legacy_database_export(tmp_path: Path) -> None:
    identifier = dataset.row_id("Inspire", "https://example.invalid/thread/1", HEART_TEXT)
    source = _write_csv(tmp_path / "export.csv", dataset.DB_HEADER, [{
        "id": identifier, "date": "2026-01-02", "type": "post",
        "platform": "Inspire", "url": "https://example.invalid/thread/1",
        "content": HEART_TEXT, "metadata": "{}",
    }])
    records = dataset.load_records(source)
    assert records == [{
        "id": identifier, "date": "2026-01-02", "type": "post",
        "platform": "Inspire", "url": "https://example.invalid/thread/1",
        "content": HEART_TEXT,
    }]


def test_transform_removes_pii_tokens_but_keeps_clinical_text() -> None:
    features = dataset.transform(
        "My EF <35% after SCAD!!! Thanks [NAME]; email [EMAIL] and call 9316799267. 😢"
    )
    assert "EF <35%" in features["Content"]
    assert "[" not in features["Content"]
    assert "9316799267" not in features["Content"]
    assert "\ufe0f" not in features["Content"] and "\u200d" not in features["Content"]
    assert features["n_redacted_names"] == 1
    assert features["has_emoji"] is True


def test_run_is_deterministic_and_filters_noise(tmp_path: Path) -> None:
    source = _write_csv(tmp_path / "merged.csv", ["Date", "Type", "Platform", "URL", "Content"], [
        {"Date": "2026-01-02", "Type": "post", "Platform": "Inspire",
         "URL": "https://example.invalid/1", "Content": HEART_TEXT},
        {"Date": "2026-01-03", "Type": "comment", "Platform": "Inspire",
         "URL": "https://example.invalid/1", "Content": HEART_TEXT},
        {"Date": "2026-01-04", "Type": "comment", "Platform": "YouTube",
         "URL": "https://example.invalid/2",
         "Content": "Thanks for watching; we can connect you to a doctor for a heart checkup."},
    ])
    first, dropped = dataset.run(source, tmp_path / "first.csv")
    second, _ = dataset.run(source, tmp_path / "second.csv")
    assert first == second
    assert len(first) == 1
    assert dropped == {"duplicate": 1, "clinic promo": 1}
