import csv
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from bs4 import BeautifulSoup
try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
ROOT_URL = 'https://connect.mayoclinic.org/'
SEARCH_URL = urljoin(ROOT_URL, 'search/comments/')
QUERY = 'women heart'
SEARCH_FIRST_PAGE = 1
SEARCH_LAST_PAGE = 136
BATCH_SIZE = 136
REOPEN_EVERY_PAGES = 20
DELAY_RANGE = (5.0, 8.0)
BROWSER_ONLY = True
BROWSER_CHANNEL = 'msedge'
BROWSER_PROFILE_DIR = '.mayo_edge_profile'
HEADLESS = False
ALLOW_MANUAL_VERIFICATION = True
BROWSER_WAIT_MS = 1000
SAVE_EVERY_CHANGES = 10
RATE_LIMIT_COOLDOWN = 300
RATE_LIMIT_RETRIES = 2
COOLDOWN_EVERY_PAGES = 25
COOLDOWN_SECONDS = 120
OUT_CSV = 'mayo_connect_all.csv'
RESUME_EXISTING = True

# CSVs go to the shared raw tree so `scraper/run_all.py` can find them. The Edge
# profile stays beside this script: it is browser state rather than data, and
# must not land in an output directory that gets cleaned.
SCRAPER_DIR = Path(__file__).resolve().parents[2]
OUTPUT_DIR = SCRAPER_DIR / 'output' / 'raw' / 'mayo'
PROFILE_ROOT = Path(__file__).resolve().parent

class RateLimitError(RuntimeError):
    pass

def clean_text(value: str) -> str:
    return re.sub('[ \\t\\r\\f\\v]+', ' ', value or '').strip()

def text_with_lines(element) -> str:
    if not element:
        return ''
    text = element.get_text('\n', strip=True)
    lines = [clean_text(line) for line in text.splitlines()]
    return '\n'.join((line for line in lines if line))

def canonical_url(url: str) -> str:
    absolute = urljoin(ROOT_URL, url)
    parts = urlsplit(absolute)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))

def no_fragment_url(url: str) -> str:
    parts = urlsplit(urljoin(ROOT_URL, url))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ''))

def page_url(url: str, page_number: int) -> str:
    parts = urlsplit(canonical_url(url))
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query['pg'] = str(page_number)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))

def comment_id_from_url(url: str) -> str:
    match = re.search('/comment/(\\d+)/', urljoin(ROOT_URL, url))
    return match.group(1) if match else ''

def fetch_browser(page, url: str):
    response = page.goto(url, wait_until='domcontentloaded', timeout=60000)
    status = response.status if response is not None else None
    if status in (403, 405) and ALLOW_MANUAL_VERIFICATION and (not HEADLESS):
        print(f'\n  ! Edge returned HTTP {status}; a verification page may be open.\n  ! Complete the verification in Edge, then return here and press Enter.')
        try:
            input()
        except EOFError:
            raise RateLimitError(f'Edge returned HTTP {status}; manual verification could not be completed')
        for _ in range(15):
            page.wait_for_timeout(1000)
            current_html = page.content()
            current_soup = BeautifulSoup(current_html, 'html.parser')
            requested_path = urlsplit(url).path
            if '/search/' in requested_path:
                ready = current_soup.select_one(SEARCH_RESULT_SELECTOR) is not None
            elif '/comment/' in requested_path:
                ready = current_soup.select_one(".comment-individual-row[id^='comment-']") is not None
            else:
                ready = current_soup.select_one('.discussion-content, .single-newsfeed-post-content') is not None
            if ready:
                page.wait_for_timeout(BROWSER_WAIT_MS)
                return (current_html, page.url)
        try:
            response = page.goto(url, wait_until='domcontentloaded', timeout=60000)
        except Exception:
            page.wait_for_timeout(2000)
            current_html = page.content()
            current_soup = BeautifulSoup(current_html, 'html.parser')
            if current_soup.select_one(SEARCH_RESULT_SELECTOR):
                return (current_html, page.url)
            raise
        status = response.status if response is not None else None
    if status in (403, 405, 429):
        raise RateLimitError(f'Edge returned HTTP {status}')
    if status is not None and status >= 400:
        raise RuntimeError(f'Edge returned HTTP {status}')
    page.wait_for_timeout(BROWSER_WAIT_MS)
    return (page.content(), page.url)
SEARCH_RESULT_SELECTOR = 'div.ch-analytics-search-result.ch-search-result'
TYPE_NAMES = {'discussions': 'Discussion', 'comments': 'Comment', 'newsfeed-posts': 'Newsfeed Post', 'groups': 'Support Group', 'pages': 'Blog'}

def result_type(container) -> str:
    classes = ' '.join(container.get('class', []))
    match = re.search('ch-search-result-type-([^ ]+)', classes)
    if match:
        return TYPE_NAMES.get(match.group(1), match.group(1))
    label = container.select_one('.ch-search-type-label')
    return clean_text(label.get_text(' ', strip=True)) if label else 'Unknown'

def extract_date(text: str) -> str:
    pattern = '\\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\\s+\\d{1,2}(?:,\\s*|\\s+)\\d{4}(?:\\s+\\d{1,2}:\\d{2}\\s*(?:am|pm))?\\b'
    match = re.search(pattern, text or '', flags=re.IGNORECASE)
    return match.group(0) if match else ''

def build_search_record(container, search_page: int):
    title_link = container.select_one('a.ch-search-result-title')
    permalink = container.select_one('a.ch-search-result-permalink')
    link = title_link or permalink or container.find('a', href=True)
    if not link or not link.get('href'):
        return None
    meta = container.select_one('.ch-search-meta')
    author_link = container.select_one('a[href*="/member/"]')
    parent_link = None
    if meta:
        parent_link = next((link for link in meta.select('a[href]') if '/discussion/' in link['href'] or '/blog/' in link['href']), None)
    secondary = meta.select_one('.ch-search-result-secondary-text') if meta else None
    snippet = container.select_one('.ch-search-excerpt')
    meta_text = text_with_lines(meta)
    return {'record_type': 'search_result', 'search_page': search_page, 'result_type': result_type(container), 'comment_page': '', 'comment_id': '', 'parent_url': no_fragment_url(parent_link['href']) if parent_link else '', 'url': no_fragment_url(link['href']), 'title': clean_text(title_link.get_text(' ', strip=True)) if title_link else clean_text(link.get_text(' ', strip=True)), 'author': clean_text(author_link.get_text(' ', strip=True)) if author_link else '', 'date': extract_date(secondary.get_text(' ', strip=True)) if secondary else extract_date(meta_text), 'content': text_with_lines(snippet) if snippet else ''}

def parse_search_page(html: str, search_page: int):
    soup = BeautifulSoup(html, 'html.parser')
    containers = soup.select(SEARCH_RESULT_SELECTOR)
    records = []
    seen = set()
    for container in containers:
        record = build_search_record(container, search_page)
        if not record or record['url'] in seen:
            continue
        seen.add(record['url'])
        records.append(record)
    return (records, soup)

def detail_title(soup: BeautifulSoup) -> str:
    title = soup.select_one('h1.chv4-discussion-title, h1.ch-newsfeed-single-title, h1')
    return clean_text(title.get_text(' ', strip=True)) if title else ''

def detail_content(soup: BeautifulSoup) -> str:
    content = soup.select_one('.discussion-content, .single-newsfeed-post-content')
    if content:
        return text_with_lines(content)
    main = soup.select_one('main')
    if not main:
        return ''
    return text_with_lines(main)

def detail_author_and_date(soup: BeautifulSoup):
    meta = soup.select_one('.ch-newsfeed-single-meta')
    if not meta:
        title = soup.select_one('h1.chv4-discussion-title')
        meta = title.parent if title else None
    author_link = meta.select_one('a[href*="/member/"]') if meta else None
    meta_text = text_with_lines(meta)
    return (clean_text(author_link.get_text(' ', strip=True)) if author_link else '', extract_date(meta_text))

def parse_comment(node, parent_url: str, comment_page: int):
    comment_id = node.get('id', '')
    match = re.match('comment-(\\d+)$', comment_id)
    if not match:
        return None
    meta = node.select_one('.ch-comment-meta')
    username = node.select_one('.post-author-username')
    content = node.select_one('.comment-content')
    body = node.select_one('.single-comment-body')
    content_text = text_with_lines(content or body)
    content_text = re.sub('\\s*\\+\\s*\\(show\\)\\s*-\\s*\\(hide\\)', '', content_text)
    if not content_text:
        return None
    comment_url = urljoin(ROOT_URL, f'comment/{match.group(1)}/')
    return {'record_type': 'comment', 'search_page': '', 'result_type': 'Comment', 'comment_page': comment_page, 'comment_id': match.group(1), 'parent_url': parent_url, 'url': comment_url, 'title': '', 'author': clean_text(username.get_text(' ', strip=True)) if username else '', 'date': extract_date(text_with_lines(meta)) if meta else '', 'content': content_text}

def parse_comments(soup: BeautifulSoup, parent_url: str, comment_page: int):
    comments = []
    seen = set()
    for node in soup.select(".comment-individual-row[id^='comment-']"):
        comment = parse_comment(node, parent_url, comment_page)
        if not comment or comment['comment_id'] in seen:
            continue
        seen.add(comment['comment_id'])
        comments.append(comment)
    return comments

def comment_page_count(soup: BeautifulSoup) -> int:
    header = soup.select_one('#chv4-comment-stream-header')
    if not header:
        return 1
    numbers = []
    for link in header.select('a[href]'):
        query = dict(parse_qsl(urlsplit(link['href']).query))
        if query.get('pg', '').isdigit():
            numbers.append(int(query['pg']))
    return max(numbers, default=1)

def parse_detail_page(html: str, detail_url: str, search_pages, result_types):
    soup = BeautifulSoup(html, 'html.parser')
    author, date = detail_author_and_date(soup)
    record = {'record_type': 'page', 'search_page': ','.join(map(str, sorted(search_pages))), 'result_type': ','.join(sorted(result_types)), 'comment_page': '', 'comment_id': '', 'parent_url': '', 'url': detail_url, 'title': detail_title(soup), 'author': author, 'date': date, 'content': detail_content(soup)}
    return (record, soup)
CSV_FIELDS = ['Date', 'Type', 'Platform', 'URL', 'Content']

def normalize_date(value: str) -> str:
    text = clean_text(value)
    if not text:
        return ''
    iso_date = re.search(r'\b\d{4}-\d{2}-\d{2}\b', text)
    if iso_date:
        return iso_date.group(0)
    text = re.sub(r'\s+\d{1,2}:\d{2}\s*(?:am|pm)\b', '', text, flags=re.IGNORECASE)
    for date_format in ('%b %d, %Y', '%b %d %Y', '%B %d, %Y', '%B %d %Y'):
        try:
            return datetime.strptime(text, date_format).strftime('%Y-%m-%d')
        except ValueError:
            pass
    return ''

def export_type(row) -> str:
    record_type = clean_text(row.get('record_type', '')).lower()
    result = clean_text(row.get('result_type', '')).lower()
    url = (row.get('parent_url') or row.get('url') or '').lower()
    if record_type in ('comment', 'search_result'):
        return 'Comment'
    if '/blog/' in url or 'blog' in result or 'newsfeed post' in result:
        return 'Blog'
    return 'Discussion'

def export_row(row):
    return {'Date': normalize_date(row.get('date', '')), 'Type': export_type(row), 'Platform': 'Mayo Clinic Connect', 'URL': row.get('url', ''), 'Content': row.get('content', '')}

def import_export_row(row):
    item_type = clean_text(row.get('Type', ''))
    record_type = 'search_result' if item_type == 'Comment' else 'page'
    url = row.get('URL', '')
    return {'record_type': record_type, 'search_page': '', 'result_type': item_type, 'comment_page': '', 'comment_id': '', 'parent_url': '', 'url': url, 'title': '', 'author': '', 'date': row.get('Date', ''), 'content': row.get('Content', '')}

def newer_path(primary: Path, fallback: Path) -> Path:
    if fallback.exists() and (not primary.exists() or fallback.stat().st_mtime >= primary.stat().st_mtime):
        return fallback
    return primary

def load_existing(output_dir: Path):
    csv_path = newer_path(output_dir / OUT_CSV, output_dir / 'mayo_connect_all_pending.csv')
    if csv_path.exists():
        try:
            with csv_path.open('r', newline='', encoding='utf-8-sig') as file_obj:
                rows = list(csv.DictReader(file_obj))
            if rows and 'record_type' not in rows[0]:
                rows = [import_export_row(row) for row in rows]
            return ([row for row in rows if row.get('record_type') == 'search_result'], [row for row in rows if row.get('record_type') == 'page'], [row for row in rows if row.get('record_type') == 'comment'])
        except (OSError, csv.Error) as exc:
            print(f'! Existing CSV could not be read; starting with empty data: {exc}')
    return ([], [], [])

def save(search_records, page_records, comment_records, output_dir: Path):
    rows = search_records + page_records + comment_records
    if not rows:
        print('\nNo data was collected; no file was created.')
        return
    csv_path = output_dir / OUT_CSV
    try:
        if csv_path.exists():
            with csv_path.open('a', encoding='utf-8'):
                pass
    except PermissionError:
        csv_path = output_dir / 'mayo_connect_all_pending.csv'
        print('>>> The main CSV is open in another program; writing to the pending CSV.')
    with csv_path.open('w', newline='', encoding='utf-8-sig') as file_obj:
        writer = csv.DictWriter(file_obj, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows((export_row(row) for row in rows))
    print(f'\n>>> Saved search results: {len(search_records)}')
    print(f'>>> Saved detail pages: {len(page_records)}')
    print(f'>>> Saved comments: {len(comment_records)}')
    print(f'>>> CSV: {csv_path}')

def main():
    start_ts = time.time()
    output_dir = OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    if BROWSER_ONLY and (not HAS_PLAYWRIGHT):
        print('This script requires Playwright for Microsoft Edge. Install it first:')
        print('  py -m pip install playwright')
        return
    if RESUME_EXISTING:
        search_records, page_records, comment_records = load_existing(output_dir)
    else:
        search_records, page_records, comment_records = ([], [], [])
    seen_search_urls = {record.get('url', '') for record in search_records}
    detail_targets = {}
    existing_page_urls = {canonical_url(record.get('url', '')) for record in page_records if record.get('url')}
    seen_detail_urls = set()
    seen_comment_ids = {record.get('comment_id', '') for record in comment_records if record.get('comment_id')}

    def register_detail_target(record):
        target_url = record.get('parent_url') or record.get('url')
        if not target_url:
            return
        target = detail_targets.setdefault(target_url, {'search_pages': set(), 'result_types': set(), 'comment_ids': set()})
        page_value = record.get('search_page', '')
        for value in str(page_value).split(','):
            if value.strip().isdigit():
                target['search_pages'].add(int(value.strip()))
        if record.get('result_type'):
            target['result_types'].add(record['result_type'])
        comment_id = comment_id_from_url(record.get('url', ''))
        if comment_id:
            target['comment_ids'].add(comment_id)
    for existing_record in search_records:
        register_detail_target(existing_record)
    pw = browser = context = page_obj = None

    def start_browser():
        nonlocal pw, browser, context, page_obj
        if page_obj is not None:
            return
        if not HAS_PLAYWRIGHT:
            raise RuntimeError('Playwright is not installed')
        pw = sync_playwright().start()
        context = pw.chromium.launch_persistent_context(user_data_dir=str(PROFILE_ROOT / BROWSER_PROFILE_DIR), channel=BROWSER_CHANNEL, headless=HEADLESS, locale='en-US')
        page_obj = context.pages[0] if context.pages else context.new_page()

    def reopen_browser_page():
        nonlocal page_obj
        if context is None:
            start_browser()
            return
        if page_obj is not None:
            try:
                page_obj.close()
            except Exception:
                pass
        page_obj = context.new_page()

    def get_html(url: str):
        rate_limit_attempt = 0
        while True:
            try:
                try:
                    start_browser()
                    return fetch_browser(page_obj, url)
                except RateLimitError:
                    raise
                except Exception as browser_error:
                    raise RuntimeError(f'Edge browser mode failed; no static fallback was used: {browser_error}') from browser_error
            except RateLimitError as rate_error:
                rate_limit_attempt += 1
                if rate_limit_attempt > RATE_LIMIT_RETRIES:
                    raise rate_error
                wait = RATE_LIMIT_COOLDOWN * rate_limit_attempt
                print(f'  ! {rate_error}; waiting {wait}s before retrying this page ({rate_limit_attempt}/{RATE_LIMIT_RETRIES})...')
                time.sleep(wait)
    rate_limited = False
    try:
        completed_pages = {int(str(record.get('search_page', '')).strip()) for record in search_records if str(record.get('search_page', '')).strip().isdigit()}
        next_page = max(completed_pages, default=SEARCH_FIRST_PAGE - 1) + 1
        run_first_page = max(SEARCH_FIRST_PAGE, next_page)
        run_last_page = min(SEARCH_LAST_PAGE, run_first_page + BATCH_SIZE - 1)
        if run_first_page > SEARCH_LAST_PAGE:
            print('Search pages 1-136 are complete; continuing with missing comment details.')
        else:
            print(f'>>> Current batch: search pages {run_first_page}-{run_last_page}')
        for search_page in range(run_first_page, run_last_page + 1):
            query = urlencode({'search': QUERY, 'pg': search_page})
            search_url = f'{SEARCH_URL}?{query}'
            print(f'[Search page {search_page}/{SEARCH_LAST_PAGE}] Fetching...', end=' ', flush=True)
            try:
                html, final_url = get_html(search_url)
                records, soup = parse_search_page(html, search_page)
                if not records and search_page == run_first_page and HAS_PLAYWRIGHT:
                    print('The browser page returned no results; retrying... ', end='')
                    start_browser()
                    html, final_url = fetch_browser(page_obj, search_url)
                    records, soup = parse_search_page(html, search_page)
            except RateLimitError as exc:
                print(f'Rate limited: {exc}; stopping this run and saving current data.')
                rate_limited = True
                break
            except Exception as exc:
                print(f'Failed: {exc}')
                if search_page == run_first_page:
                    print('>> The first search page failed; stopping this run.')
                    return
                continue
            new_count = 0
            for record in records:
                if record['url'] in seen_search_urls:
                    continue
                seen_search_urls.add(record['url'])
                search_records.append(record)
                register_detail_target(record)
                new_count += 1
            print(f'Parsed {len(records)} records; added {new_count}; total search results: {len(search_records)}')
            save(search_records, page_records, comment_records, output_dir)
            if (search_page - run_first_page + 1) % REOPEN_EVERY_PAGES == 0:
                print('>> Completed 20 pages; reopening the browser page...')
                reopen_browser_page()
            time.sleep(random.uniform(*DELAY_RANGE))
            if (search_page - run_first_page + 1) % COOLDOWN_EVERY_PAGES == 0 and search_page < SEARCH_LAST_PAGE:
                print(f'>> Completed page {search_page}; cooling down for {COOLDOWN_SECONDS}s...')
                time.sleep(COOLDOWN_SECONDS)
        if rate_limited:
            save(search_records, page_records, comment_records, output_dir)
            print('>> Run the script again later; it will resume from the next unfinished page.')
            return
        print(f'\n>>> Search collection complete; unique detail targets: {len(detail_targets)}')
        changes_since_save = 0
        for index, (target_url, target_info) in enumerate(detail_targets.items(), start=1):
            wanted_ids = target_info['comment_ids']
            if wanted_ids and wanted_ids.issubset(seen_comment_ids):
                continue
            print(f'[Detail {index}/{len(detail_targets)}] {target_url}')
            try:
                target_search_pages = ','.join(map(str, sorted(target_info['search_pages'])))
                found_here = 0
                for comment_id in sorted(wanted_ids):
                    if comment_id in seen_comment_ids:
                        continue
                    comment_url = urljoin(ROOT_URL, f'comment/{comment_id}/')
                    comment_html, comment_final_url = get_html(comment_url)
                    comment_soup = BeautifulSoup(comment_html, 'html.parser')
                    comment_detail_url = canonical_url(comment_final_url)
                    if comment_detail_url not in existing_page_urls:
                        detail_record, _ = parse_detail_page(comment_html, comment_detail_url, target_info['search_pages'], target_info['result_types'])
                        page_records.append(detail_record)
                        existing_page_urls.add(comment_detail_url)
                        changes_since_save += 1
                    comment_query = dict(parse_qsl(urlsplit(comment_final_url).query))
                    comment_page_text = comment_query.get('pg', '1')
                    comment_page = int(comment_page_text) if comment_page_text.isdigit() else 1
                    page_comments = parse_comments(comment_soup, comment_detail_url, comment_page)
                    matched = [comment for comment in page_comments if comment['comment_id'] in wanted_ids and comment['comment_id'] not in seen_comment_ids]
                    if not matched:
                        print(f'  ! No full text was found for comment {comment_id}')
                        continue
                    for comment in matched:
                        comment['search_page'] = target_search_pages
                        seen_comment_ids.add(comment['comment_id'])
                        comment_records.append(comment)
                        found_here += 1
                        changes_since_save += 1
                    if changes_since_save >= SAVE_EVERY_CHANGES:
                        save(search_records, page_records, comment_records, output_dir)
                        changes_since_save = 0
                    time.sleep(random.uniform(*DELAY_RANGE))
                print(f'  Saved one parent page and {found_here} matching comments; total comments: {len(comment_records)}')
            except RateLimitError as exc:
                print(f'  ! Detail page rate limited: {exc}; stopping and saving current data.')
                rate_limited = True
                break
            except Exception as exc:
                print(f'  ! Detail request failed: {exc}')
        if changes_since_save:
            save(search_records, page_records, comment_records, output_dir)
    except KeyboardInterrupt:
        print('\n>> Interrupted; saving collected data...')
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
    save(search_records, page_records, comment_records, output_dir)
    print(f'Total elapsed time: {time.time() - start_ts:.0f} seconds.')
if __name__ == '__main__':
    main()
