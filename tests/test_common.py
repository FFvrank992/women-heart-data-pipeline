"""Offline tests for the shared PII engine and the five-column contract.

    python tests/test_common.py

No network, no fixtures on disk beyond the two name lists and the allowlist.
Every case here came from something actually found in this project's corpus --
the false positives in particular, which are the ones that regress silently.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scraper"))

import pandas as pd

from common import pii, schema
from common.sources import DEFAULT_COLLECT, SOURCES


class PiiContactDetailsTests(unittest.TestCase):
    def test_email_split_by_consumer_and_institutional(self) -> None:
        self.assertEqual(pii.redact("write to jane@gmail.com"), "write to [EMAIL]")
        self.assertEqual(pii.redact("write to info@heartfoundation.org.au"),
                         "write to [EMAIL:ORG]")

    def test_url_keeps_domain_drops_path(self) -> None:
        self.assertEqual(pii.redact("see https://womenheart.org/a/b?c=1 now"),
                         "see [URL:womenheart.org] now")
        self.assertEqual(pii.redact("see www.womenheart.org now"),
                         "see [URL:womenheart.org] now")

    def test_url_is_extracted_before_email(self) -> None:
        """Ordering guard: the email regex must not bite a URL in half."""
        self.assertEqual(pii.redact("https://x.org/user@host/page"), "[URL:x.org]")

    def test_phone_numbers(self) -> None:
        for text in ("call 555-123-4567", "call (555) 123-4567", "call +61 412 345 678"):
            self.assertIn("[PHONE]", pii.redact(text), text)

    def test_clinical_numbers_are_not_phone_numbers(self) -> None:
        """Regression: this corpus is full of lab values and dosages."""
        text = "My EF is 20, BP 120/80, troponin 0.04, metoprolol 25mg twice a day."
        self.assertEqual(pii.redact(text), text)

    def test_handles_become_user(self) -> None:
        self.assertEqual(pii.redact("thanks @angelas and @tinalove"),
                         "thanks [USER] and [USER]")

    def test_addresses_and_ids(self) -> None:
        self.assertIn("[ADDRESS]", pii.redact("I live at 123 Main Street"))
        self.assertIn("[ID]", pii.redact("MRN: 48291043"))
        self.assertIn("[ID]", pii.redact("SSN 123-45-6789"))
        self.assertIn("[DOB]", pii.redact("Born on 03/14/1968"))


class PiiNameTests(unittest.TestCase):
    def test_self_introduction(self) -> None:
        self.assertEqual(pii.redact("Hi my name is Lisa, I have heart failure"),
                         "Hi my name is [NAME], I have heart failure")

    def test_greeting(self) -> None:
        self.assertEqual(pii.redact("Hi Judy, thanks"), "Hi [NAME], thanks")

    def test_greeting_stopwords_are_not_names(self) -> None:
        for text in ("Hi All, welcome", "Thanks Everyone", "Hello Ladies"):
            self.assertNotIn("[NAME]", pii.redact(text), text)

    def test_signoff_on_final_line(self) -> None:
        self.assertEqual(pii.redact("Hope that helps.\n\nBest,\nMichelle"),
                         "Hope that helps.\n\nBest,\n[NAME]")

    def test_unknown_username_caught_by_position(self) -> None:
        """Tiers 1-3 fire on position, so a name absent from the dictionary
        is still caught. `Sunnyflower` is a real Inspire username."""
        self.assertIn("[NAME]", pii.redact("my name is Sunnyflower"))

    def test_dictionary_catches_names_in_prose(self) -> None:
        self.assertEqual(pii.redact("My son Ben kept me laughing"),
                         "My son [NAME] kept me laughing")

    def test_ambiguous_words_are_never_names(self) -> None:
        """Regression: every one of these was a false positive on real text."""
        text = ("My will to keep going was gone in May. I had hope. The stent went "
                "in the LAD at max dose. I had a heart attack in Jan. and live in "
                "Victoria near Victoria Hospice.")
        self.assertEqual(pii.redact(text), text)

    def test_titled_clinicians_are_kept(self) -> None:
        text = "Please read Dr. John Mandrola's post and Professor Chris Gale's study."
        self.assertEqual(pii.redact(text), text)

    def test_credentialed_researchers_are_kept(self) -> None:
        """Cited authors carry credentials rather than a title."""
        text = "Janet Wei, MD, FACC; C. Noel Bairey Merz, M.D., FACC"
        self.assertEqual(pii.redact(text), text)

    def test_allowlisted_byline_is_kept(self) -> None:
        text = "by Carolyn Thomas @HeartSisters"
        self.assertEqual(pii.redact(text), text)

    def test_saint_named_institutions_are_kept(self) -> None:
        """Regression: `St Vincent's` and `St Mary's` are hospitals, not people."""
        for text in ("There were St Vincent's Health Educators on hand",
                     "St Marys Kitchener in Ontario Canada",
                     "admitted to Saint Joseph last winter"):
            self.assertEqual(pii.redact(text), text, text)

    def test_surname_is_absorbed_by_the_context_tiers_too(self) -> None:
        """Regression: tiers 1-3 replaced only the given name, so
        "my name is Sadie Majors" leaked the surname as "[NAME] Majors"."""
        self.assertEqual(pii.redact("Hi, my name is Sadie Majors. I am 54."),
                         "Hi, my name is [NAME]. I am 54.")
        self.assertEqual(pii.redact("Thanks.\n\nBest,\nMichelle Brown"),
                         "Thanks.\n\nBest,\n[NAME]")

    def test_greeting_word_is_not_rewritten(self) -> None:
        """The old string-replace hit the first occurrence of the name anywhere
        in the match, mangling greetings that contain the name as a substring."""
        self.assertEqual(pii.redact("Hi Hillary, how are you"),
                         "Hi [NAME], how are you")

    def test_full_name_is_redacted_as_one_token(self) -> None:
        """A surname alone is not in the dictionary, so redacting only the given
        name would leave it exposed and read oddly."""
        self.assertEqual(pii.redact("Stroke survivor Sharon Dreher spoke"),
                         "Stroke survivor [NAME] spoke")

    def test_surname_absorption_stops_at_a_new_clause(self) -> None:
        """The token after a name is only absorbed when it could be a surname."""
        self.assertEqual(pii.redact("I told Judy Monday was fine"),
                         "I told [NAME] Monday was fine")
        self.assertEqual(pii.redact("Ben Hospital called"), "[NAME] Hospital called")


class PiiRecallTests(unittest.TestCase):
    """Misses found by auditing the redacted output, not the input.

    Sampling redacted tokens only ever finds false positives. These two came
    from probing the finished corpus for name-shaped text that survived.
    """

    def test_name_after_a_capitalised_word_is_caught(self) -> None:
        """The tier-4 regex used to consume the token after a non-name, so any
        name preceded by a capitalised word -- every sentence opener included --
        was never examined."""
        for text, expected in [
            ("With Barbara kind permission", "With [NAME] kind permission"),
            ("After Nadine post mortem", "After [NAME] post mortem"),
            ("Then Judy called me", "Then [NAME] called me"),
        ]:
            self.assertEqual(pii.redact(text), expected, text)

    def test_signoff_name_on_the_next_line_is_caught(self) -> None:
        self.assertEqual(pii.redact("Cheers,\n\nDonna"), "Cheers,\n\n[NAME]")

    def test_redaction_creates_no_new_handle_boundary(self) -> None:
        """HANDLE_RX needs a non-word char before "@", so "Nancy@thankful" is
        not a handle until the name is redacted and "]" appears in front."""
        self.assertEqual(pii.redact("Nancy@thankful said"), "[NAME][USER] said")

    def test_interface_strings_are_not_signoffs(self) -> None:
        """Tier 3 judges by position, never by dictionary, so WordPress's
        trailing "Read More" read as a signature."""
        self.assertEqual(pii.redact("Some text\nRead More"), "Some text\nRead More")

    def test_short_credentials_do_not_match_word_prefixes(self) -> None:
        """`d.o.`, `pa`, `mb`, `bs`, `np` had no trailing boundary, so
        "And, don't push" parsed as "<name>, DO" and the span was protected as
        a cited clinician. 187 documents were affected."""
        self.assertEqual(pii.redact("And, don't push yourself"),
                         "And, don't push yourself")
        self.assertEqual(pii.redact("Cheers, Donna"), "Cheers, [NAME]")
        # ...while real credentials still protect their name
        self.assertEqual(pii.redact("Janet Wei, MD, FACC"), "Janet Wei, MD, FACC")


class PiiTextRepairTests(unittest.TestCase):
    def test_mojibake_is_repaired(self) -> None:
        """Deliberately name-free: redaction runs after the repair, so a name
        here would be replaced and hide whether the repair worked."""
        self.assertEqual(pii.redact("the doctorâ€™s article"),
                         "the doctor's article")
        # fix_mojibake restores the original curly apostrophe; straightening it
        # is normalize_unicode's job, one step later.
        self.assertEqual(pii.fix_mojibake("Carolynâ€™s"),
                         "Carolyn’s")

    def test_bare_given_name_is_redacted_even_when_the_byline_is_not(self) -> None:
        """`Carolyn Thomas` is an allowlisted byline; a bare `Carolyn` is not,
        because it could be any commenter. 779 rows in the corpus are affected.
        Add `carolyn` to config/pii_allowlist.txt to change this."""
        self.assertEqual(pii.redact("by Carolyn Thomas"), "by Carolyn Thomas")
        self.assertEqual(pii.redact("Thanks Carolyn for writing this"),
                         "Thanks [NAME] for writing this")

    def test_legacy_placeholders_are_normalised(self) -> None:
        """Delivered CSVs carry each old scraper's private vocabulary."""
        self.assertEqual(pii.redact("[EMAIL_REMOVED] and [link] and @user"),
                         "[EMAIL] and [URL] and [USER]")

    def test_wordpress_shortcodes_stripped_only_when_asked(self) -> None:
        text = "[vc_row][vc_column_text]Real body.[/vc_column_text][/vc_row]"
        self.assertEqual(pii.redact(text, shortcodes=True), "Real body.")
        self.assertIn("[vc_row]", pii.redact(text))

    def test_redaction_is_idempotent(self) -> None:
        """run_all redacts a scraper's already-redacted output a second time."""
        text = "Hi Judy, mail jane@gmail.com or see https://x.org/a and @someone"
        once = pii.redact(text)
        self.assertEqual(pii.redact(once), once)

    def test_stats_are_tallied(self) -> None:
        stats = pii.Stats()
        pii.redact("Hi Judy, mail jane@gmail.com", stats)
        self.assertEqual(stats.docs, 1)
        self.assertEqual(stats.counts["email"], 1)
        self.assertEqual(stats.counts["name"], 1)


class SchemaTests(unittest.TestCase):
    def test_type_vocabulary_is_unified(self) -> None:
        for raw, expected in [("reply", "comment"), ("comments", "comment"),
                              ("Post", "post"), ("Discussion", "post"),
                              ("blogs", "blog"), ("Blog", "blog")]:
            self.assertEqual(schema.normalise_type(raw), expected, raw)

    def test_transcript_is_no_longer_a_valid_type(self) -> None:
        """The team settled on three values. Nothing ever produced `transcript`,
        and validate() must now reject it so it cannot creep back in."""
        self.assertNotIn("transcript", schema.TYPES)
        bad = pd.DataFrame([["2020-01-01", "transcript", "YouTube",
                             "https://y/1", "body"]], columns=schema.COLUMNS)
        self.assertTrue(any("unknown Type" in p for p in schema.validate(bad)))

    def test_platform_names_are_unified(self) -> None:
        self.assertEqual(schema.normalise_platform("youtube"), "YouTube")
        self.assertEqual(schema.normalise_platform("mayo"), "Mayo Clinic Connect")

    def test_youtube_uses_the_standard_collection_and_raw_boundaries(self) -> None:
        youtube = SOURCES["youtube"]
        self.assertIn("youtube", DEFAULT_COLLECT)
        self.assertEqual(youtube.raw_globs, ["output/raw/youtube/*.csv"])
        self.assertEqual(len(youtube.collect_steps()), 2)
        self.assertTrue(youtube.collect_steps()[0][-1].endswith("fetch_youtube_raw.py"))
        self.assertTrue(youtube.collect_steps()[1][-1].endswith("youtube_etl_pipeline.py"))
        ranged = youtube.collect_steps(start_date="2026-09-01", end_date="2026-09-07")
        self.assertEqual(ranged[0][-4:], ["--start-date", "2026-09-01",
                                           "--end-date", "2026-09-07"])
        self.assertEqual(ranged[1][-4:], ["--start-date", "2026-09-01",
                                           "--end-date", "2026-09-07"])

    def test_date_formats(self) -> None:
        for raw, expected in [("2026-08-31", "2026-08-31"),
                              ("2016/1/3", "2016-01-03"),          # heart_sisters
                              ("20/04/2026", "2026-04-20"),        # her_heart, day-first
                              ("Jan 25, 2016", "2016-01-25"),      # mayo
                              ("2016-01-25T10:00:00Z", "2016-01-25"),
                              ("not a date", ""), ("", "")]:
            self.assertEqual(schema.normalise_date(raw), expected, raw)

    def test_row_id_matches_the_database_loader(self) -> None:
        """csv_to_db.py computes this same id; dedupe must agree with it."""
        got = schema.row_id("Inspire", "https://x/y", "body")
        self.assertTrue(got.startswith("Inspire_"))
        self.assertEqual(len(got), len("Inspire_") + 16)

    def _frame(self, rows) -> pd.DataFrame:
        return pd.DataFrame(rows, columns=schema.COLUMNS)

    def test_exact_duplicates_are_dropped(self) -> None:
        row = ["2020-01-01", "post", "Reddit", "https://r/x", "body"]
        out, tally = schema.dedupe(self._frame([row, list(row)]))
        self.assertEqual(len(out), 1)
        self.assertEqual(tally["exact"], 1)

    def test_mayo_fragments_collapse_to_the_longest(self) -> None:
        url = "https://connect.mayoclinic.org/comment/86204/"
        out, tally = schema.dedupe(self._frame([
            ["2016-01-25", "comment", "Mayo Clinic Connect", url, "short ... snippet"],
            ["2016-01-25", "comment", "Mayo Clinic Connect", url, "the full comment text"],
        ]))
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[0]["Content"], "the full comment text")
        self.assertEqual(tally["fragments"], 1)

    def test_shared_thread_url_does_not_collapse(self) -> None:
        """Inspire comments all carry their thread's URL. Collapsing by URL
        there would delete the entire discussion."""
        url = "https://www.inspire.com/groups/womenheart/discussion/abc/"
        out, _ = schema.dedupe(self._frame([
            ["2020-01-01", "post", "Inspire", url, "the original post"],
            ["2020-01-01", "comment", "Inspire", url, "a reply"],
            ["2020-01-02", "comment", "Inspire", url, "another reply"],
        ]))
        self.assertEqual(len(out), 3)

    def test_reddit_slug_collision_does_not_collapse(self) -> None:
        """Reddit URLs are synthetic title slugs, so two different posts titled
        'Aspirin' share one URL. They are not duplicates."""
        url = "https://www.reddit.com/r/HeartAttack/comments/aspirin/"
        out, _ = schema.dedupe(self._frame([
            ["2026-08-01", "post", "Reddit", url, "first post about aspirin"],
            ["2026-08-02", "post", "Reddit", url, "an unrelated second post"],
        ]))
        self.assertEqual(len(out), 2)

    def test_validate_rejects_an_invented_type(self) -> None:
        bad = self._frame([["2020-01-01", "tweet", "Reddit", "https://r/x", "body"]])
        self.assertTrue(any("unknown Type" in p for p in schema.validate(bad)))

    def test_validate_accepts_a_good_frame(self) -> None:
        good = self._frame([["2020-01-01", "post", "Reddit", "https://r/x", "body"]])
        self.assertEqual(schema.validate(good), [])

    def test_undated_rows_sort_last(self) -> None:
        out = schema.sort_rows(self._frame([
            ["", "post", "Reddit", "https://r/a", "undated"],
            ["2020-01-01", "post", "Reddit", "https://r/b", "dated"],
        ]))
        self.assertEqual(out.iloc[0]["Content"], "dated")


if __name__ == "__main__":
    unittest.main(verbosity=2)
