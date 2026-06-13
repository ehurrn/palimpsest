# palimpsest/harvester.py
import argparse
import hashlib
import sys
import time
import re
import datetime
from pathlib import Path
import httpx
from bs4 import BeautifulSoup

from palimpsest.config import load
from palimpsest.db import connect

cfg = load()
last_request_time = 0.0
consecutive_403_count = 0

def rate_limit_sleep(rps: float):
    global last_request_time
    now = time.time()
    elapsed = now - last_request_time
    wait = (1.0 / rps) - elapsed
    if wait > 0:
        time.sleep(wait)
    last_request_time = time.time()

def request_with_retry(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    global consecutive_403_count
    backoff = cfg.harvest["backoff_initial_s"]
    max_backoff = cfg.harvest["backoff_max_s"]
    
    while True:
        rate_limit_sleep(cfg.harvest["rate_limit_rps"])
        try:
            if method == "GET":
                resp = client.get(url, **kwargs)
            else:
                resp = client.post(url, **kwargs)
                
            if resp.status_code == 403:
                consecutive_403_count += 1
                if consecutive_403_count >= 3:
                    print("CRITICAL: Received 3 consecutive 403 Forbidden responses. Aborting.", file=sys.stderr)
                    # Write to HUMAN_DO_THIS.md at the project root (palimpsest/harvester.py -> repo root)
                    human_do_this = Path(__file__).resolve().parent.parent / "HUMAN_DO_THIS.md"
                    with open(human_do_this, "a") as f:
                        f.write("- OSTI may have blocked us — stop and email opennet@osti.gov\n")
                    sys.exit("Blocked by OSTI")
                wait_time = backoff
                backoff = min(backoff * 2, max_backoff)
                print(f"Received 403. Waiting {wait_time}s before retry...", file=sys.stderr)
                time.sleep(wait_time)
                continue
                
            # Reset 403 count on any non-403 success or normal error
            consecutive_403_count = 0
            
            if resp.status_code in (429, 503):
                # Backoff
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait_time = int(retry_after)
                else:
                    wait_time = backoff
                    backoff = min(backoff * 2, max_backoff)
                print(f"Received {resp.status_code}. Waiting {wait_time}s before retry...", file=sys.stderr)
                time.sleep(wait_time)
                continue
                
            return resp
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            print(f"Request error: {e}. Retrying after {backoff}s...", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

def catalog(limit: int | None = None):
    conn = connect(cfg)
    client = httpx.Client(headers={"User-Agent": cfg.harvest["user_agent"]})
    
    start = 0
    page_size = 100
    total_processed = 0
    total_entries = None
    
    base_url = cfg.harvest["base_url"] + "/search-results"
    
    while True:
        url = f"{base_url}?accession-number={cfg.harvest['accession_prefix']}*&start={start}&length={page_size}"
        print(f"Requesting catalog URL: {url}")
        resp = request_with_retry(client, "GET", url)
        if resp.status_code != 200:
            print(f"Search results request failed: {resp.status_code}", file=sys.stderr)
            break
            
        soup = BeautifulSoup(resp.text, "html.parser")
        
        # Parse total entries on first page
        if total_entries is None:
            counts_div = soup.find("div", class_="search-result-counts")
            if counts_div:
                match = re.search(r'of\s+([\d,]+)\s+entries', counts_div.text, re.IGNORECASE)
                if match:
                    total_entries = int(match.group(1).replace(",", ""))
                    print(f"Total entries to harvest: {total_entries}")
            if total_entries is None:
                total_entries = 0
                
        table = soup.find("table", id="search-results-table")
        if not table:
            print("Table #search-results-table not found.")
            break
            
        tbody = table.find("tbody")
        if not tbody:
            print("tbody not found in table.")
            break
            
        rows = tbody.find_all("tr", recursive=False)
        if not rows:
            print("No rows found in tbody.")
            break
            
        new_rows_count = 0
        for row in rows:
            cols = row.find_all("td", recursive=False)
            if len(cols) < 11:
                continue
                
            # Col 1: Title & link
            title_col = cols[0]
            title_a = title_col.find("a")
            if not title_a:
                continue
            href = title_a.get("href", "")
            match_id = re.search(r'osti-id=(\d+)', href)
            if not match_id:
                continue
            doc_id = match_id.group(1)
            title = title_col.get_text(strip=True)
            
            # Col 3: Accession
            accession = cols[2].get_text(strip=True)
            
            # Col 8: Publication Date
            pub_date = cols[7].get_text(strip=True)
            year_match = re.search(r'\b\d{4}\b', pub_date)
            year = int(year_match.group(0)) if year_match else None
            
            # Col 10: Full text link
            ft_col = cols[9]
            ft_a = ft_col.find("a")
            has_fulltext = 1 if ft_a else 0
            
            source_url = None
            if has_fulltext:
                source_url = f"https://www.osti.gov/opennet/servlets/purl/{doc_id}.pdf"
                
            # Insert into database
            with conn:
                res = conn.execute(
                    "INSERT OR IGNORE INTO documents (doc_id, accession, title, year, has_fulltext, source_url, status) VALUES (?, ?, ?, ?, ?, ?, 'cataloged')",
                    (doc_id, accession, title, year, has_fulltext, source_url)
                )
                if res.rowcount > 0:
                    new_rows_count += 1
                    
            total_processed += 1
            if limit and total_processed >= limit:
                print(f"Limit of {limit} results reached.")
                return
                
        print(f"Processed {len(rows)} rows on this page. New cataloged: {new_rows_count}")
        
        start += page_size
        if start >= total_entries or total_entries == 0:
            print("Reached end of search results.")
            break

def fetch(limit: int | None = None):
    conn = connect(cfg)
    cur = conn.cursor()
    
    # Select cataloged rows with fulltext
    cur.execute(
        "SELECT doc_id, source_url FROM documents WHERE status='cataloged' AND has_fulltext=1 ORDER BY doc_id ASC LIMIT ?",
        (limit or 1000,)
    )
    docs = cur.fetchall()
    
    client = httpx.Client(headers={"User-Agent": cfg.harvest["user_agent"]})
    broker_url = f"http://{cfg.broker['host']}:{cfg.broker['port']}"
    
    raw_dir = cfg.storage_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    
    for row in docs:
        doc_id = row["doc_id"]
        source_url = row["source_url"]
        
        tmp_path = raw_dir / f"{doc_id}.tmp"
        dest_path = raw_dir / f"{doc_id}.pdf"
        
        # Skip if file already exists (idempotent)
        if dest_path.exists():
            try:
                pdf_data = dest_path.read_bytes()
                sha256 = hashlib.sha256(pdf_data).hexdigest()
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                with conn:
                    conn.execute(
                        "UPDATE documents SET status='fetched', fetched_at=?, sha256=?, local_path=? WHERE doc_id=?",
                        (now, sha256, str(dest_path), doc_id)
                    )
                print(f"File already exists for {doc_id}. Skipping download and enqueuing OCR job...")
                try:
                    broker_resp = client.post(f"{broker_url}/enqueue", json={"type": "ocr", "doc_id": doc_id})
                    if broker_resp.status_code != 200:
                        print(f"Warning: Failed to enqueue ocr job for {doc_id} via broker (status {broker_resp.status_code})")
                except Exception as e:
                    print(f"Warning: Failed to connect to broker to enqueue job: {e}")
                continue
            except Exception as e:
                print(f"Error reading existing file for {doc_id}: {e}, will re-download.")
        
        print(f"Fetching PDF for document {doc_id} from {source_url}...")
        try:
            resp = request_with_retry(client, "GET", source_url)
            if resp.status_code != 200:
                raise Exception(f"Download failed with status {resp.status_code}")
                
            pdf_data = resp.content
            
            # Atomic write
            tmp_path.write_bytes(pdf_data)
            tmp_path.rename(dest_path)
            
            sha256 = hashlib.sha256(pdf_data).hexdigest()
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            
            with conn:
                conn.execute(
                    "UPDATE documents SET status='fetched', fetched_at=?, sha256=?, local_path=? WHERE doc_id=?",
                    (now, sha256, str(dest_path), doc_id)
                )
                
            print(f"Successfully fetched {doc_id}. Enqueuing OCR job...")
            
            # Enqueue follow-on ocr job via broker
            try:
                broker_resp = client.post(f"{broker_url}/enqueue", json={"type": "ocr", "doc_id": doc_id})
                if broker_resp.status_code != 200:
                    print(f"Warning: Failed to enqueue ocr job for {doc_id} via broker (status {broker_resp.status_code})")
            except Exception as e:
                print(f"Warning: Failed to connect to broker to enqueue job: {e}")
                
        except Exception as e:
            print(f"Error fetching document {doc_id}: {e}", file=sys.stderr)
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            with conn:
                conn.execute(
                    "UPDATE documents SET status='error', error=? WHERE doc_id=?",
                    (str(e), doc_id)
                )

def status():
    conn = connect(cfg)
    cur = conn.execute("SELECT status, COUNT(*) as count FROM documents GROUP BY status")
    rows = cur.fetchall()
    print("Document status counts:")
    for row in rows:
        print(f"  {row['status']}: {row['count']}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OpenNet Harvester CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    catalog_parser = subparsers.add_parser("catalog", help="Catalog NV* docs from OpenNet (accession prefix from config.toml)")
    catalog_parser.add_argument("--limit", type=int, default=None, help="Max results to catalog")
    
    fetch_parser = subparsers.add_parser("fetch", help="Download raw PDFs for cataloged documents")
    fetch_parser.add_argument("--limit", type=int, default=None, help="Max PDFs to fetch")
    
    status_parser = subparsers.add_parser("status", help="Show catalog status counts")
    
    args = parser.parse_args()
    
    if args.command == "catalog":
        catalog(limit=args.limit)
    elif args.command == "fetch":
        fetch(limit=args.limit)
    elif args.command == "status":
        status()
