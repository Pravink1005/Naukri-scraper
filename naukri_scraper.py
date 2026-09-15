"""
Naukri Job Scraper - Data Analyst jobs, last 1 day
----------------------------------------------------
Requires: pip install playwright
          playwright install chromium

Usage:
    python naukri_scraper.py

Notes:
- Naukri frequently changes their CSS class names. If this stops working,
  open a job listing / search page, right-click -> Inspect on the element
  you need, and update the corresponding selector below.
- This script includes delays between requests to avoid rate-limiting/blocks.
  Do not remove these delays or scrape at high volume.
- For personal/research use only. Check naukri.com/robots.txt and their
  Terms of Use before scraping at scale or for commercial purposes.
"""

import csv
import time
import re
import json
import argparse
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# Base search URL - jobAge=1 means "posted in last 1 day"
DEFAULT_SEARCH_URL = "https://www.naukri.com/data-analyst-jobs?jobAge=1"
DEFAULT_SEARCH_URLS = [
    DEFAULT_SEARCH_URL,
    "https://www.naukri.com/business-analyst-jobs?jobAge=1",
    "https://www.naukri.com/data-engineer-jobs?jobAge=1",
    "https://www.naukri.com/data-scientist-jobs?jobAge=1",
    "https://www.naukri.com/power-bi-jobs?jobAge=1",
]
OUTPUT_FIELDS = [
    "url", "title", "company", "location", "experience", "salary",
    "skills", "qualifications_education_required", "job_description_summary",
    "posted",
]


def parse_posted_date(text):
    """
    Convert Naukri's relative date text (e.g. 'Posted 14 days ago',
    'Posted today', 'Posted 1 day ago') into an actual timestamp string
    formatted as DD/MM/YYYY HH:MM:SS.

    Naukri doesn't expose an exact posting time, only a relative day count,
    so the time component is set to 00:00:00 (we only know the DAY, not
    the exact minute it was posted).
    """
    if not text:
        return None

    text = text.lower()
    now = datetime.now()

    if "today" in text:
        target = now
    elif "just now" in text or "few hours" in text or "hour" in text:
        target = now
    else:
        match = re.search(r"(\d+)\s*day", text)
        if match:
            days_ago = int(match.group(1))
            target = now - timedelta(days=days_ago)
        else:
            return text  # couldn't parse - return original text as fallback

    return target.strftime("%d/%m/%Y %H:%M:%S")


def is_last_day_posting(text):
    """
    Accept only postings explicitly described as today or within the last
    24 hours.

    IMPORTANT: the day count is extracted and compared NUMERICALLY (not via
    a plain "1 day" substring check). A substring check would also match
    inside "11 days", "21 days", "31 days", "41 days", etc. — the digit "1",
    a space, and "day" appear in all of those too — which would silently let
    three-week-old postings through as if they were from today.
    """
    if not text:
        return False
    text = text.lower()
    if "today" in text or "just now" in text:
        return True
    if re.search(r"\bfew\s+hours?\b", text) or re.search(r"\d+\s*hour", text):
        return True
    match = re.search(r"(\d+)\s*day", text)
    if match:
        return int(match.group(1)) <= 1
    return False


def safe_text(page, selector, timeout=2000):
    """Return stripped inner text of the first match, or None if not found."""
    try:
        el = page.locator(selector).first
        el.wait_for(timeout=timeout)
        return el.inner_text().strip()
    except (PlaywrightTimeoutError, Exception):
        return None


def safe_list(page, selector):
    """Return a list of inner texts for all matching elements."""
    try:
        elements = page.locator(selector).all()
        return [e.inner_text().strip() for e in elements if e.inner_text().strip()]
    except Exception:
        return []


def first_json_ld(page):
    """Return the first JSON-LD object on the page, if it is valid JSON."""
    try:
        scripts = page.locator("script[type='application/ld+json']").all_inner_texts()
        for script in scripts:
            data = json.loads(script)
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, Exception):
        pass
    return {}


def labeled_value(page, label):
    """Return the value following a visible, single-line metadata label."""
    try:
        body = page.locator("body").inner_text()
        match = re.search(rf"(?im)^\s*{re.escape(label)}\s*:\s*(.+?)\s*$", body)
        return match.group(1).strip() if match else None
    except Exception:
        return None


def clean_company_name(value):
    """Remove the review count appended to Naukri company names."""
    if not value:
        return None
    return re.sub(r"\s*\d[\d.]*K Reviews.*$", "", value.replace("\n", " ")).strip()


def compact_text(value):
    """Keep CSV cells readable by removing repeated spaces and line breaks."""
    if not value:
        return None
    return re.sub(r"\s+", " ", value).strip()


def unavailable_page(page, response=None):
    """Identify HTTP errors and Naukri pages that are not job details."""
    if response is not None and response.status == 403:
        return "access denied"
    if response is not None and response.status >= 400:
        return f"HTTP {response.status}"

    try:
        title = page.title().lower()
        body = page.locator("body").inner_text().lower()
    except Exception:
        return "unreadable page"

    if "404: this page could not be found" in body or "page not found" in title:
        return "page not found (404)"
    if "access denied" in body or "access denied" in title:
        return "access denied"
    if "captcha" in body or "verify you are human" in body:
        return "verification page"
    return None


def clean_education(value):
    """Remove Naukri UG/PG labels while preserving the education values."""
    value = compact_text(value)
    if not value:
        return None
    value = re.sub(r"^Education\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+(?=(?:UG|PG)\s*:)", "; ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:UG|PG)\s*:\s*", "", value, flags=re.IGNORECASE)
    return compact_text(value).strip(" ;:")


def extract_education(text, structured=None):
    """Extract an explicitly stated education requirement from job text."""
    education = (structured or {}).get("educationRequirements")
    if isinstance(education, dict):
        education = education.get("educationalLevel")
    if education:
        return clean_education(str(education))

    patterns = (
        r"(?:educational qualification|educational requirements|education required|qualification required)\s*[:\-]?\s*([^.;\n]{3,180})",
        r"\b(?:qualifications?)\s*[:\-]\s*([^.;\n]{3,180})",
        r"\b(?:degree|graduate|graduation|bachelor(?:'s)?|master(?:'s)?|b\.\s*e\.?|b\.\s*tech|mca|mba)\s+(?:in|of)?\s*([^.;\n]{3,150})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            value = compact_text(match.group(0))
            if (
                value
                and not re.search(r"\b(?:years?|experience)\b", value, re.IGNORECASE)
                and value.casefold() != "qualifications, certifications and education"
            ):
                return clean_education(value)
    return "Not specified"


def clean_skills(values):
    """Normalize skill labels and remove duplicates while preserving order."""
    cleaned = []
    seen = set()
    for value in values:
        skill = compact_text(value)
        key = skill.casefold() if skill else ""
        if skill and key not in seen:
            cleaned.append(skill)
            seen.add(key)
    return "; ".join(cleaned) or "Not specified"


def get_job_links(page, search_url, max_pages=10, max_jobs=100, delay=2, interactive=False):
    """Collect unique job detail page links from search result pages."""
    job_links = []

    for page_num in range(1, max_pages + 1):
        sep = "&" if "?" in search_url else "?"
        url = f"{search_url}{sep}page={page_num}" if page_num > 1 else search_url

        print(f"[+] Loading search page {page_num}: {url}")
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except PlaywrightTimeoutError:
            print(f"    Timeout loading page {page_num}, skipping.")
            continue

        page_problem = unavailable_page(page, response)
        if page_problem == "access denied":
            if interactive:
                input("    Access Denied. Complete verification or load Naukri in the browser, then press Enter to retry... ")
                try:
                    response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except PlaywrightTimeoutError:
                    print("    Retry timed out.")
                page_problem = unavailable_page(page, response)
            if page_problem == "access denied":
                print("    Naukri returned Access Denied. Use a normal browser session or try again later.")
                return []
        elif page_problem:
            print(f"    Search page unavailable: {page_problem}.")
            return []

        # Job title links on the results page. Adjust selector if Naukri
        # changes their markup - inspect a job card's title link to confirm.
        cards = page.locator("a.title, a[class*='title']")

        try:
            # Wait for the FIRST job card to actually appear, up to 15s,
            # instead of guessing a fixed delay. Much more reliable than
            # a flat page.wait_for_timeout().
            cards.first.wait_for(state="attached", timeout=15000)
        except PlaywrightTimeoutError:
            print(f"    No job cards appeared within 15s on page {page_num}.")

        page.wait_for_timeout(1500)  # let remaining cards on the page settle
        count = cards.count()

        if count == 0:
            print(f"    No job cards found on page {page_num}. Stopping pagination.")
            page.screenshot(path=f"debug_zero_results_page{page_num}.png", full_page=True)
            with open(f"debug_zero_results_page{page_num}.html", "w", encoding="utf-8") as f:
                f.write(page.content())
            print(f"    Saved debug_zero_results_page{page_num}.png/.html for inspection.")
            break

        new_links = 0
        for i in range(count):
            href = cards.nth(i).get_attribute("href")
            if href and href.startswith("http") and href not in job_links:
                job_links.append(href)
                new_links += 1

        print(f"    Found {new_links} new job links (total: {len(job_links)})")
        if len(job_links) >= max_jobs:
            return job_links[:max_jobs]
        if new_links == 0:
            print("    No new postings on this page; moving to the next search.")
            break
        time.sleep(delay)

    return job_links


def scrape_job_detail(page, url, delay=2, save_debug=False):
    """Scrape details from a single job posting page."""
    print(f"[+] Scraping: {url}")
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeoutError:
        print("    Timeout loading job page, skipping.")
        return None

    page.wait_for_timeout(2000)

    page_problem = unavailable_page(page, response)
    if page_problem:
        print(f"    Skipping unavailable job page: {page_problem}.")
        return None

    if save_debug:
        with open("debug_job_detail.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        print("    Saved debug_job_detail.html for inspection.")

    posted_raw = safe_text(page, "[class*='jhc__stat']")
    if not is_last_day_posting(posted_raw):
        print("    Skipping: posting is not explicitly from the last 1 day.")
        return None

    main_desc = safe_text(page, "[class*='JDC__dang-inner-html']") or ""
    highlights = safe_text(page, "[class*='styles_details__'], [class*='details__']") or ""
    full_description = (main_desc + "\n\n" + highlights).strip() if highlights else main_desc
    structured = first_json_ld(page)
    salary_data = structured.get("baseSalary", {})
    salary_value = salary_data.get("value", {}) if isinstance(salary_data, dict) else {}
    salary = salary_value.get("value") if isinstance(salary_value, dict) else salary_value
    salary = str(salary) if salary else labeled_value(page, "Salary")
    page_education = safe_text(page, "[class*='education__']")
    education = extract_education(full_description, structured)
    if page_education and page_education.lower() not in ("education", "not specified"):
        education = compact_text(page_education)

    job = {
        "url": url,
        "title": safe_text(page, "h1"),
        "company": clean_company_name(safe_text(page, "[class*='jd-header-comp-name']")),
        "location": safe_text(page, "[class*='jhc__location'], [class*='jhc__loc__']"),
        "experience": safe_text(page, "[class*='jhc__exp__']"),
        "salary": "Not specified" if salary in (None, "Not disclosed") else salary,
        "skills": clean_skills(safe_list(page, "[class*='chip__'] span")),
        "qualifications_education_required": clean_education(education) if education != "Not specified" else (clean_education(labeled_value(page, "Education")) or "Not specified"),
        "job_description_summary": compact_text(full_description) or "Not specified",
        "posted": parse_posted_date(posted_raw),
    }

    time.sleep(delay)
    return job


def save_to_csv(jobs, output_file):
    if not jobs:
        print("No jobs to save.")
        return

    keys = OUTPUT_FIELDS
    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(jobs)

    print(f"\n✅ Saved {len(jobs)} jobs to {output_file}")


def main():
    parser = argparse.ArgumentParser(description="Scrape Naukri data analyst jobs (last N days)")
    parser.add_argument("--url", action="append", dest="urls", help="Naukri search URL; repeat for multiple searches")
    parser.add_argument("--pages", type=int, default=5, help="Max search result pages to crawl PER search URL")
    parser.add_argument("--max-jobs", type=int, default=100, help="Maximum job LINKS to collect PER search URL (not shared across searches)")
    parser.add_argument("--max-total", type=int, default=None, help="Optional cap on total scraped postings across ALL searches combined (default: no cap)")
    parser.add_argument("--output", default="naukri_extracted_jobs.csv", help="Output CSV filename")
    parser.add_argument("--delay", type=float, default=2.0, help="Delay (seconds) between requests")
    parser.add_argument("--show-browser", action="store_true", default=True, help="Run with a visible browser window")
    parser.add_argument("--headless", action="store_true", help="Run without opening a browser window")
    parser.add_argument("--profile-dir", default=".\\.naukri_profile", help="Persistent browser profile directory for interactive access")
    parser.add_argument("--browser", choices=["chromium", "firefox"], default="chromium", help="Which browser engine to use")
    args = parser.parse_args()
    headless = args.headless
    search_urls = args.urls or DEFAULT_SEARCH_URLS

    all_jobs = []
    seen_jobs = set()

    with sync_playwright() as p:
        engine = p.firefox if args.browser == "firefox" else p.chromium
        if args.profile_dir:
            context = engine.launch_persistent_context(
                args.profile_dir,
                headless=headless,
                user_agent=USER_AGENT,
            )
            page = context.pages[0] if context.pages else context.new_page()
            browser = context
        else:
            browser = engine.launch(headless=headless)
            page = browser.new_page(user_agent=USER_AGENT)

        # Step 1: collect job links from search results.
        # IMPORTANT: each search URL gets its OWN max_jobs budget for link
        # collection. Sharing a single budget across all search URLs meant
        # the first URL alone could exhaust it (e.g. its first page already
        # returning max_jobs links), silently skipping every other search.
        links = []
        access_blocked = False
        for search_url in search_urls:
            if access_blocked:
                break
            print(f"\n=== Search: {search_url} ===")
            search_links = get_job_links(
                page,
                search_url,
                max_pages=args.pages,
                max_jobs=args.max_jobs,
                delay=args.delay,
                interactive=bool(args.profile_dir and not headless),
            )
            if not search_links and unavailable_page(page) == "access denied":
                access_blocked = True
            for link in search_links:
                if link not in links:
                    links.append(link)
            print(f"Combined unique links: {len(links)}")
        print(f"\nTotal unique job links found: {len(links)}\n")

        # Step 2: scrape each job's detail page. is_last_day_posting() inside
        # scrape_job_detail() is what actually enforces "last 1 day" — this
        # loop does NOT stop early once enough RAW links are visited, since a
        # link isn't confirmed fresh until its detail page is checked.
        for idx, link in enumerate(links):
            job = scrape_job_detail(page, link, delay=args.delay, save_debug=(idx == 0))
            if job:
                job_key = (job["title"], job["company"], job["location"])
                if job_key in seen_jobs:
                    print("    Skipping duplicate posting.")
                    continue
                seen_jobs.add(job_key)
                all_jobs.append(job)
                if args.max_total and len(all_jobs) >= args.max_total:
                    print(f"    Reached --max-total ({args.max_total}); stopping.")
                    break

        browser.close()

    # Step 3: save results
    if not all_jobs:
        print("No fresh postings extracted; existing output files were preserved.")
        return
    save_to_csv(all_jobs, args.output)


if __name__ == "__main__":
    main()