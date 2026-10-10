from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import heart_sisters_scraper as scraper


class HeartSistersScraperTests(unittest.TestCase):
    def test_html_to_text_preserves_readable_paragraphs(self) -> None:
        html = "<p>A <strong>heart</strong> &amp; health.</p><p>Next<br>line.</p><script>bad()</script>"
        self.assertEqual(scraper.html_to_text(html), "A heart & health.\n\nNext\nline.")

    def test_contact_pii_is_redacted(self) -> None:
        """The shared vocabulary from common/pii.py, not the old local one."""
        text = "Email reader@example.com, call +61 412 345 678, or visit https://example.com and @reader."
        cleaned = scraper.redact_contact_pii(text)
        self.assertIn("[EMAIL:ORG]", cleaned)
        self.assertIn("[PHONE]", cleaned)
        self.assertIn("[URL:example.com]", cleaned)
        self.assertIn("[USER]", cleaned)
        self.assertNotIn("reader@example.com", cleaned)

    def test_blog_bodies_are_redacted_too(self) -> None:
        """Regression: blog text used to bypass redaction entirely."""
        row = scraper.blog_row(
            {
                "date": "2020-02-03T10:00:00Z",
                "URL": "https://myheartsisters.org/example/",
                "title": "Women and heart health",
                "content": "<p>Reach me at reader@example.com or @somebody.</p>",
            },
            "2016-01-01",
            "2026-12-31",
        )
        self.assertNotIn("reader@example.com", row["Content"])
        self.assertNotIn("@somebody", row["Content"])
        self.assertIn("[EMAIL:ORG]", row["Content"])

    def test_public_byline_is_not_redacted(self) -> None:
        """Cited professionals and the blog's own byline stay readable."""
        kept = scraper.redact_contact_pii(
            "by Carolyn Thomas @HeartSisters. Dr. Martha Gulati led the study.")
        self.assertIn("Carolyn Thomas", kept)
        self.assertIn("@HeartSisters", kept)
        self.assertIn("Dr. Martha Gulati", kept)

    def test_relevance_filter(self) -> None:
        self.assertTrue(scraper.is_relevant_comment("My cardiologist diagnosed my heart disease last year."))
        self.assertTrue(scraper.is_relevant_comment("I had chest pain and went to hospital for treatment."))
        self.assertFalse(scraper.is_relevant_comment("I reblogged this on Facebook for my friends."))

    def test_blog_row_has_exact_team_columns(self) -> None:
        row = scraper.blog_row(
            {
                "date": "2020-02-03T10:00:00Z",
                "URL": "https://myheartsisters.org/example/",
                "title": "Women and heart health",
                "content": "<p>Complete article body.</p>",
            },
            "2016-01-01",
            "2026-12-31",
        )
        self.assertIsNotNone(row)
        self.assertEqual(list(row), scraper.CSV_FIELDS)
        self.assertEqual(row["Type"], "blog")
        self.assertEqual(row["Platform"], "Heart Sisters")

    def test_csv_round_trip_and_deduplication(self) -> None:
        row = {
            "Date": "2024-01-02",
            "Type": "comment",
            "Platform": "Heart Sisters",
            "URL": "https://myheartsisters.org/example/#comment-1",
            "Content": "My cardiologist treated my heart disease.",
        }
        rows, duplicate_count = scraper.dedupe([row, dict(row)])
        self.assertEqual(duplicate_count, 1)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "result.csv"
            scraper.write_csv(path, rows, overwrite=False)
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(reader.fieldnames, scraper.CSV_FIELDS)
                self.assertEqual(list(reader), [row])


if __name__ == "__main__":
    unittest.main()
