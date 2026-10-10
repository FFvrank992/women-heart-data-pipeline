import argparse
import json
import shutil
import sys
from pathlib import Path
from datetime import datetime

SCRIPT_DIR = Path(__file__).resolve().parent
SCRAPER_DIR = SCRIPT_DIR.parents[1]

sys.path.insert(0, str(SCRAPER_DIR))
from common import io_utils, schema

DEFAULT_INPUT_DIR = SCRAPER_DIR / "output" / "youtube_raw"
CSV_OUTPUT_DIR = SCRAPER_DIR / "output" / "raw" / "youtube"

# Convert a YouTube ISO 8601 timestamp (e.g. 2026-08-23T08:42:39Z) to a
# SQL DATE literal (YYYY-MM-DD), dropping the time-of-day component
def to_sql_date(iso_str: str):
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace('Z', '+00:00')).strftime('%Y-%m-%d')
    except ValueError:
        return None


def is_in_comment_window(timestamp: str, start_date: str | None,
                         end_date: str | None) -> bool:
    """Keep every main-schema row inside the requested comment window."""
    if start_date is None or end_date is None:
        return True
    date_value = to_sql_date(timestamp)
    return bool(date_value and start_date <= date_value <= end_date)

# Extract contract fields from new native payloads and track files to archive.
def process_youtube_files(input_dir: Path, *, start_date: str | None = None,
                          end_date: str | None = None):
    records = []
    processed_files = []

    if not input_dir.exists():
        print(f"[Error] Input directory does not exist: {input_dir}")
        return records, processed_files

    print(f"Scanning for JSON files in: {input_dir}")

    # The fetcher writes pending payloads directly in input_dir. Do not recurse:
    # `archive/` holds already converted JSON and must never be re-exported.
    for file_path in input_dir.glob("*.json"):
        # skip manifest files
        if file_path.name.endswith("_manifest.json"):
            continue

        with open(file_path, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                for payload in data:
                    video_info = payload.get("video_info", {})
                    video_id = video_info.get("id", {}).get("videoId")
                    if not video_id:
                        continue

                    video_url = f"https://www.youtube.com/watch?v={video_id}"

                    # transform comments
                    comments_data = payload.get("comments_data", [])
                    for c in comments_data:
                        # top comment
                        top_snippet = c.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
                        top_text_raw = top_snippet.get("textOriginal", "")
                        top_date = top_snippet.get("publishedAt")

                        if is_in_comment_window(top_date, start_date, end_date):
                            records.append({
                                "Date": to_sql_date(top_date) or "",
                                "Type": "comment",
                                "Platform": "YouTube",
                                "URL": video_url,
                                "Content": top_text_raw,
                            })

                        # replies
                        replies = c.get("replies", {}).get("comments", [])
                        for reply in replies:
                            reply_snippet = reply.get("snippet", {})
                            reply_text_raw = reply_snippet.get("textOriginal", "")
                            reply_date = reply_snippet.get("publishedAt")

                            if is_in_comment_window(reply_date, start_date, end_date):
                                combined_text = f"[In reply to: {top_text_raw}]: {reply_text_raw}"
                                records.append({
                                    "Date": to_sql_date(reply_date) or "",
                                    "Type": "comment",
                                    "Platform": "YouTube",
                                    "URL": video_url,
                                    "Content": combined_text,
                                })

                # add processed list for safe deletion
                processed_files.append(file_path)

            except Exception as e:
                print(f"[!] Error reading {file_path}: {e}")

    return records, processed_files

# export data to csv
def export_to_csv(records, output_dir: Path) -> bool:
    timestamp = datetime.now().strftime("%Y%m%d")
    csv_path = output_dir / f"youtube_{timestamp}.csv"

    # This is the raw CSV boundary for the shared harvest step. Extra YouTube
    # fields stay in the native JSON archive rather than leaking into the
    # five-column dataset or pretending to be database JSONB metadata.
    headers = schema.COLUMNS

    try:
        io_utils.write_rows(records, csv_path, headers)
        print(f"Successfully exported {len(records)} records to CSV:")
        print(f"    -> {csv_path}")
        return True
    except Exception as e:
        print(f"[Error] Failed to export CSV: {e}")
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert fetched YouTube JSON into the shared raw CSV format.")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=CSV_OUTPUT_DIR)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    args = parser.parse_args(argv)
    if bool(args.start_date) != bool(args.end_date):
        parser.error("--start-date and --end-date must be supplied together")
    print("Starting Youtube ETL Pipeline")

    records, processed_files = process_youtube_files(
        args.input_dir, start_date=args.start_date, end_date=args.end_date)

    if not records:
        print("[!] Pipeline finished with 0 records. No data to process.")
        return 0

    export_success = export_to_csv(records, args.output_dir)
    if export_success:
        # Archive rather than delete. This step used to `os.remove` every raw
        # JSON, which meant a bug found later could not be fixed without
        # re-spending YouTube quota -- and a search call costs 100 units of a
        # 10,000/day allowance. Moving them keeps the "do not reprocess"
        # behaviour while leaving the evidence on disk.
        archive_dir = args.input_dir / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        print(f"Archiving {len(processed_files)} raw data files...")
        for file_path in processed_files:
            try:
                shutil.move(str(file_path), str(archive_dir / file_path.name))
            except Exception as e:
                print(f"[Error] Failed to archive {file_path}: {e}")
        print(f"    -> {archive_dir}")
    else:
        print("[!] CSV export failed; raw files left in place.")

    print("-" * 50)
    print("ETL Pipeline fully completed!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
