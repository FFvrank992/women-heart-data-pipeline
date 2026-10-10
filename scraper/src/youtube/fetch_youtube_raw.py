import argparse
import json
import sys
import os
import re
from math import ceil
from pathlib import Path
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    # A Windows console can still default to a legacy codepage such as GBK.
    # Video titles are arbitrary Unicode, so logging must never abort a crawl.
    sys.stdout.reconfigure(errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(errors="backslashreplace")

SCRIPT_DIR = Path(__file__).resolve().parent
SCRAPER_DIR = SCRIPT_DIR.parents[1]
PROJECT_ROOT = SCRIPT_DIR.parents[2]

# The .env has lived in three places across this project's history, and this
# script previously read only PROJECT_ROOT/.env -- which does not exist, while
# the file with the real API key sits next to this script. That made the
# script exit with "YOUTUBE_API_KEY cannot be found" on a correct checkout.
# Load every candidate, nearest first; load_dotenv does not overwrite an
# already-set variable, so the closest file wins.
for _candidate in (SCRIPT_DIR / ".env", SCRAPER_DIR / ".env", PROJECT_ROOT / ".env"):
    if _candidate.exists():
        load_dotenv(_candidate)

# compile cardiac regex
CARDIAC_TERMS = [
    r"heart (?:disease|attack|failure|condition|problem|issue|surgery|health|murmur|rate)",
    r"cardiac", r"cardiology", r"cardiologist", r"cardiovascular",
    r"myocardial infarction", r"\bMI\b", r"angina", r"prinzmetal", r"ischemi", r"atheroscleros",
    r"atrial fibrillation", r"\bafib\b", r"\ba-fib\b", r"arrhythmi", r"tachycardi", r"bradycardi",
    r"\bPSVT\b", r"palpitation", r"cardiomyopath", r"congestive heart", r"\bCHF\b",
    r"\bHFpEF\b", r"\bHFrEF\b", r"ejection fraction", r"\bEF\b",
    r"coronary", r"\bCAD\b", r"\bSCAD\b", r"microvascular", r"\bMVD\b",
    r"valve", r"mitral", r"aortic", r"tricuspid", r"stenosis", r"regurgitat",
    r"endocarditis", r"pericarditis", r"\batrial\b", r"congenital heart",
    r"\bLVAD\b", r"\bPOTS\b", r"dysautonomia",
    r"stent", r"bypass", r"\bCABG\b", r"angiogram", r"angioplasty", r"catheteriz",
    r"\bcath lab\b", r"\bTAVR\b", r"echocardiogram", r"\becho\b", r"\bEKG\b", r"\bECG\b",
    r"stress test", r"pacemaker", r"\bICD\b", r"defibrillator", r"ablation", r"troponin",
    r"calcium score",
    r"beta.?blocker", r"metoprolol", r"toprol", r"statin", r"nitroglycerin", r"\bnitro\b",
    r"plavix", r"clopidogrel", r"warfarin", r"coumadin", r"eliquis", r"xarelto",
    r"diltiazem", r"amlodipine", r"imdur", r"ranexa", r"entresto", r"amiodarone",
    r"lisinopril", r"blood thinner", r"calcium channel blocker",
    r"chest pain", r"shortness of breath", r"blood pressure",
]

CARDIAC_PATTERN = re.compile(r"|".join(CARDIAC_TERMS), re.IGNORECASE)

# Keep collection topics here, rather than in run_all.py, so YouTube remains a
# normal Source with one fetch command. The API accepts `|` as its OR operator.
TOPIC_QUERIES = {
    "acute_events": "women ('heart attack' | 'myocardial infarction' | 'cardiac arrest')",
    "chronic_conditions": "women ('heart disease' | 'heart failure' | 'cardiomyopathy' | 'CAD')",
    "arrhythmia": "women ('atrial fibrillation' | 'afib' | 'arrhythmia' | 'tachycardia')",
    "female_specific": "women ('SCAD' | 'microvascular' | 'MVD' | 'POTS')",
    "symptoms": "female ('chest pain' | 'shortness of breath' | 'palpitations' | 'dysautonomia')",
    "procedures": "women ('heart stent' | 'bypass surgery' | 'pacemaker' | 'ablation')",
}
VIDEO_LOOKBACK_DAYS = 365

# Returns the current UTC time
def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()

# Validate and format a YYYY-MM-DD string into RFC 3339 for YouTube API
def format_rfc3339_date(date_str: str, is_end_of_day: bool = False) -> str:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        if is_end_of_day:
            return f"{date_str}T23:59:59Z"
        return f"{date_str}T00:00:00Z"
    except ValueError:
        print(f"Error: Incorrect date format '{date_str}'. Expected YYYY-MM-DD.", file=sys.stderr)
        sys.exit(2)


def resolve_date_range(start_date: str | None, end_date: str | None) -> tuple[str, str]:
    """Return a complete inclusive UTC date range, defaulting to one week."""
    today = datetime.now(timezone.utc).date()
    if end_date:
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    else:
        end = today
    if start_date:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
    else:
        start = end - timedelta(days=6)
    if start > end:
        raise ValueError("start date must not be after end date")
    return start.isoformat(), end.isoformat()


def videos_per_topic(start_date: str, end_date: str, weekly_count: int) -> int:
    """Allocate a whole weekly quota for each started seven-day period."""
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    days = (end - start).days + 1
    return ceil(days / 7) * weekly_count


def is_in_comment_window(timestamp: str | None, start_date: str, end_date: str) -> bool:
    """Whether a YouTube comment timestamp belongs in this collection window."""
    if not timestamp:
        return False
    try:
        comment_date = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).date()
    except ValueError:
        return False
    return start_date <= comment_date.isoformat() <= end_date

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fetch_youtube_raw", description="fetch YouTube raw JSON comment data")

    # CLI argument define
    parser.add_argument("--run-id", help="The unique identifier for this scraping task")
    parser.add_argument("--topics", nargs="+", choices=TOPIC_QUERIES,
                        default=list(TOPIC_QUERIES),
                        help="Built-in topic groups to collect (default: all)")
    parser.add_argument("--videos-per-week", type=int, default=4,
                        help="Videos to find for each topic in each started seven-day period")
    parser.add_argument("--max-comments", type=int, default=100,
                        help="Maximum matching top-level comment threads per video")
    parser.add_argument("--overwrite", action="store_true", help="Force overwrite if the video JSON already exists")
    parser.add_argument("--start-date",
                        help="Earliest publication date (YYYY-MM-DD; default: seven days ago)")
    parser.add_argument("--end-date",
                        help="Latest publication date (YYYY-MM-DD; default: today)")
    args = parser.parse_args(argv)
    if not args.run_id:
        args.run_id = f"youtube_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    if args.videos_per_week < 1 or args.max_comments < 1:
        parser.error("--videos-per-week and --max-comments must both be positive")
    try:
        args.start_date, args.end_date = resolve_date_range(args.start_date, args.end_date)
    except ValueError as exc:
        parser.error(str(exc))
    max_videos = videos_per_topic(args.start_date, args.end_date, args.videos_per_week)
    video_start_date = (
        datetime.strptime(args.start_date, "%Y-%m-%d").date()
        - timedelta(days=VIDEO_LOOKBACK_DAYS)
    ).isoformat()

    output_dir = PROJECT_ROOT / "scraper" / "output" / "youtube_raw"
    output_dir.mkdir(parents=True, exist_ok=True)

    # get API key
    api_key = os.getenv("YOUTUBE_API_KEY")
    if not api_key:
        print("Error: YOUTUBE_API_KEY cannot be found. Please ensure it is configured in the .env file.", file=sys.stderr)
        return 2

    youtube = build("youtube", "v3", developerKey=api_key)
    started_at = iso_now()

    print(f"[{args.run_id}] Starting the YouTube API scraping pipeline...")

    print(f"Date range: {args.start_date} to {args.end_date}")
    print(f"Video discovery range: {video_start_date} to {args.end_date}")
    print(f"Videos per topic: {max_videos} ({args.videos_per_week} per week)")

    manifest_stats = {
        "videos_found": 0,
        "videos_processed": 0,
        "duplicates_skipped": 0,
        "comments_extracted": 0,
        "skipped_videos": 0,
        "errors": []
    }

    try:
        # Discovery Phase
        print("\n>> Discovery Phase:")
        videos = []
        for topic in args.topics:
            query = TOPIC_QUERIES[topic]
            print(f"  {topic}: {query}")
            topic_videos = []
            next_page_token = None
            while len(topic_videos) < max_videos:
                fetch_count = min(50, max_videos - len(topic_videos))
                search_response = youtube.search().list(
                    q=query,
                    type="video",
                    part="id,snippet",
                    maxResults=fetch_count,
                    pageToken=next_page_token,
                    publishedAfter=format_rfc3339_date(video_start_date),
                    publishedBefore=format_rfc3339_date(args.end_date, is_end_of_day=True),
                ).execute()
                items = search_response.get("items", [])
                if not items:
                    break
                for item in items:
                    item["_topic"] = topic
                topic_videos.extend(items)
                next_page_token = search_response.get("nextPageToken")
                if not next_page_token:
                    break
            videos.extend(topic_videos)
            print(f"    -> {len(topic_videos)} videos found")

        video_ids = [item["id"]["videoId"] for item in videos]
        for i in range(0, len(video_ids), 50):
            batch_ids = ",".join(video_ids[i:i+50])
            stats_response = youtube.videos().list(
                part="statistics", id=batch_ids
            ).execute()

            stats_map = {stat_item["id"]: stat_item["statistics"] for stat_item in stats_response.get("items", [])}
            for item in videos:
                vid = item["id"]["videoId"]
                if vid in stats_map:
                    item["statistics"] = stats_map[vid]

        manifest_stats["videos_found"] = len(videos)
        print(f"Discovery complete, {len(videos)} videos found")

        # Fetch Phase
        print(f"\n>> Fetch Phase:")
        for index, item in enumerate(videos, start=1):
            video_id = item["id"]["videoId"]
            video_title = item["snippet"]["title"]
            out_path = output_dir / f"{video_id}.json"

            archived_path = output_dir / "archive" / out_path.name
            if (out_path.exists() or archived_path.exists()) and not args.overwrite:
                print(f"[{index}/{len(videos)}] Skipped (Duplicate): {video_id}")
                manifest_stats["duplicates_skipped"] += 1
                continue

            print(f"[{index}/{len(videos)}] Extracting: {video_id} - {video_title[:40]}...")

            comments_data = []
            c_next_page_token = None

            try:
                while len(comments_data) < args.max_comments:
                    comment_response = youtube.commentThreads().list(
                        part="snippet,replies",
                        videoId=video_id,
                        maxResults=100,
                        pageToken=c_next_page_token,
                        textFormat="plainText",
                        order="time",
                    ).execute()

                    c_items = comment_response.get("items", [])
                    if not c_items:
                        break

                    for c in c_items:
                        top_snippet = c.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
                        thread_text = top_snippet.get("textOriginal", "")
                        has_comment_in_window = is_in_comment_window(
                            top_snippet.get("publishedAt"), args.start_date, args.end_date)

                        replies = c.get("replies", {}).get("comments", [])
                        for reply in replies:
                            thread_text += " \n " + reply.get("snippet", {}).get("textOriginal", "")
                            has_comment_in_window |= is_in_comment_window(
                                reply.get("snippet", {}).get("publishedAt"),
                                args.start_date, args.end_date)

                        if has_comment_in_window and CARDIAC_PATTERN.search(thread_text):
                            comments_data.append(c)

                            if len(comments_data) >= args.max_comments:
                                break

                    c_next_page_token = comment_response.get("nextPageToken")
                    if not c_next_page_token or len(comments_data) >= args.max_comments:
                        break

                raw_payload = {
                    "crawl_metadata": {
                    "run_id": args.run_id,
                    "topic": item["_topic"],
                    "query": TOPIC_QUERIES[item["_topic"]],
                        "fetched_at": iso_now(),
                        "is_regex_filtered": True
                    },
                    "video_info": item,
                    "comments_data": comments_data
                }

                with open(out_path, "w", encoding="utf-8") as f:
                    json.dump([raw_payload], f, ensure_ascii=False, indent=2)

                manifest_stats["videos_processed"] += 1
                manifest_stats["comments_extracted"] += len(comments_data)

            except HttpError as e:
                error_reason = json.loads(e.content).get('error', {}).get('message', str(e))
                print(f"  -> Skip {video_id}: {error_reason}")
                manifest_stats["skipped_videos"] += 1
                manifest_stats["errors"].append({"video_id": video_id, "reason": error_reason})

    except HttpError as e:
        error_reason = json.loads(e.content).get('error', {}).get('message', str(e))
        print(f"\n[Fatal API Error] The fetch pipeline crashed: {error_reason}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"Fatal error: The fetch pipeline has crashed: {e}", file=sys.stderr)
        return 2

    safe_args = {
        "run_id": args.run_id,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "topics": args.topics,
        "videos_per_week": args.videos_per_week,
        "videos_per_topic": max_videos,
        "max_comments": args.max_comments,
    }

    manifest = {
        "run_id": args.run_id,
        "started_at": started_at,
        "finished_at": iso_now(),
        "args": safe_args,
        "stats": manifest_stats
    }

    manifest_path = output_dir / "youtube_manifest.jsonl"
    with open(manifest_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(manifest, ensure_ascii=False) + "\n")

    print("-" * 50)
    print(f"Scraping complete!")
    print(f" -> Processed: {manifest_stats['videos_processed']} videos")
    print(f" -> Skipped (Duplicate): {manifest_stats['duplicates_skipped']} videos")
    print(f" -> Extracted: {manifest_stats['comments_extracted']} comments")
    print(f"Data stored in: {output_dir}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
