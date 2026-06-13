# palimpsest/review.py
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional

from palimpsest.config import load, Config
from palimpsest.db import connect

def get_initials() -> str:
    """Prompt the user for reviewer initials once per session."""
    while True:
        try:
            initials = input("Enter reviewer initials: ").strip()
            if initials:
                return initials
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            sys.exit(0)

def get_clear_context(conn, doc_id: str, page_no: int, char_start: Optional[int], char_end: Optional[int]) -> str:
    """Extract context around a clear text entity from pages table."""
    if char_start is None or char_end is None:
        return ""
    try:
        cur = conn.execute("SELECT text FROM pages WHERE doc_id = ? AND page_no = ?", (doc_id, page_no))
        row = cur.fetchone()
        if not row or not row["text"]:
            return ""
        text = row["text"]
        start = max(0, char_start - 150)
        end = min(len(text), char_end + 150)
        return text[start:end]
    except Exception:
        return ""

def log_decision_to_audit(cfg: Config, review_id: int, norm: str, decision: str, decided_by: str, decided_at: str):
    """Append decision to {root}/db/review_audit.jsonl without leaking plaintext norm."""
    audit_dir = cfg.storage_root / "db"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_file = audit_dir / "review_audit.jsonl"
    
    norm_hash = hashlib.sha256(norm.encode("utf-8")).hexdigest()
    record = {
        "review_id": review_id,
        "norm_hash": norm_hash,
        "decision": decision,
        "decided_by": decided_by,
        "decided_at": decided_at
    }
    
    with open(audit_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

def handle_people(cfg: Config, list_only: bool):
    """Handle the people queue (interactive review or list dump)."""
    conn = connect(cfg)
    
    if list_only:
        # Non-interactive dump
        try:
            cur = conn.execute("""
                SELECT rq.review_id, rq.entity_id, rq.reason, rq.status, rq.decided_by, rq.decided_at,
                       e.text, e.norm, e.doc_id, e.page_no, e.living_status
                FROM review_queue rq
                JOIN entities e ON rq.entity_id = e.entity_id
                ORDER BY rq.review_id ASC
            """)
            rows = cur.fetchall()
            if not rows:
                print("No items found in review queue.")
                return
                
            print(f"{'ID':<6} | {'Status':<10} | {'Living Status':<20} | {'Text':<20} | {'Doc/Page':<12} | {'Decided By':<10} | {'Decided At':<20} | {'Reason'}")
            print("-" * 120)
            for row in rows:
                decided_by = row["decided_by"] or "N/A"
                decided_at = row["decided_at"] or "N/A"
                print(f"{row['review_id']:<6} | {row['status']:<10} | {row['living_status']:<20} | {row['text']:<20} | {row['doc_id']}/{row['page_no']:<7} | {decided_by:<10} | {decided_at:<20} | {row['reason']}")
        finally:
            conn.close()
        return

    # Interactive review
    cur = conn.execute("""
        SELECT rq.review_id, rq.entity_id, rq.reason, rq.status,
               e.doc_id, e.page_no, e.kind, e.text, e.norm
        FROM review_queue rq
        JOIN entities e ON rq.entity_id = e.entity_id
        WHERE rq.status = 'pending'
        ORDER BY rq.review_id ASC
    """)
    items = cur.fetchall()
    if not items:
        print("No pending items in review queue.")
        conn.close()
        return

    print(f"Found {len(items)} pending review queue items.")
    initials = None

    for item in items:
        review_id = item["review_id"]
        entity_id = item["entity_id"]
        reason = item["reason"]
        doc_id = item["doc_id"]
        page_no = item["page_no"]
        kind = item["kind"]
        real_text = item["text"]
        norm = item["norm"]
        pseudonym = f"PERSON-{entity_id:04d}"

        # Fetch all occurrences of this normalized entity
        cur_occ = conn.execute("""
            SELECT e.doc_id, e.page_no, d.title, d.accession
            FROM entities e
            LEFT JOIN documents d ON e.doc_id = d.doc_id
            WHERE e.norm = ? AND e.kind = 'person'
        """, (norm,))
        occurrences = cur_occ.fetchall()

        # Fetch triggering gap candidates
        cur_gap = conn.execute("""
            SELECT gc.gap_id, gc.score,
                   r.context_before, r.context_after, r.doc_id AS r_doc_id, r.page_no AS r_page_no
            FROM gap_candidates gc
            JOIN redactions r ON gc.redaction_id = r.redaction_id
            WHERE gc.clear_entity_id = ?
        """, (entity_id,))
        gap_cands = cur_gap.fetchall()

        # Co-occurring dates on pages where the entity occurs
        dates_by_page = {}
        for occ in occurrences:
            o_doc_id = occ["doc_id"]
            o_page_no = occ["page_no"]
            cur_date = conn.execute("""
                SELECT text FROM entities
                WHERE doc_id = ? AND page_no = ? AND kind = 'date'
            """, (o_doc_id, o_page_no))
            dates = [r["text"] for r in cur_date.fetchall()]
            if dates:
                dates_by_page[f"{o_doc_id}/{o_page_no}"] = list(set(dates))

        print("\n" + "=" * 80)
        print(f"Review ID:  {review_id}")
        print(f"Pseudonym:  {pseudonym}")
        print(f"Real Name:  {real_text}")
        print(f"Norm Name:  {norm}")
        print(f"Kind:       {kind}")
        print(f"Reason:     {reason}")
        print("-" * 80)
        print("Occurrences:")
        for occ in occurrences:
            title = occ["title"] or "No Title"
            accession = occ["accession"] or "N/A"
            purl = f"https://www.osti.gov/opennet/servlets/purl/{occ['doc_id']}.pdf"
            page_str = f"{occ['doc_id']}/{occ['page_no']}"
            dates_str = f" | Co-occurring Dates: {', '.join(dates_by_page[page_str])}" if page_str in dates_by_page else ""
            print(f"  - Page {occ['page_no']} in Doc {occ['doc_id']} (Accession: {accession}) | Title: {title}{dates_str}")
            print(f"    PURL: {purl}")

        if gap_cands:
            print("-" * 80)
            print("Triggering Gap Candidates:")
            for gc in gap_cands:
                print(f"  - Gap ID {gc['gap_id']} (Score: {gc['score']:.4f})")
                print(f"    Redacted Page: Doc {gc['r_doc_id']}, Page {gc['r_page_no']}")
                ctx_before = gc["context_before"] or ""
                ctx_after = gc["context_after"] or ""
                print(f"    Context: ... {ctx_before} [ REDACTED ] {ctx_after} ...")

        print("=" * 80)

        # Prompt for initials if not yet prompted
        if initials is None:
            initials = get_initials()

        # Decision loop
        while True:
            try:
                choice = input("[a]pprove as deceased_historical / [d]eny / [s]kip / [q]uit: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print("\nQuitting loop.")
                conn.close()
                return

            if choice in ("a", "approve"):
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                with conn:
                    # Update all entities sharing this norm
                    conn.execute(
                        "UPDATE entities SET living_status = 'deceased_historical' WHERE norm = ? AND kind = 'person'",
                        (norm,)
                    )
                    # Approve pending review_queue rows for this norm
                    conn.execute("""
                        UPDATE review_queue 
                        SET status = 'approved', decided_by = ?, decided_at = ?
                        WHERE entity_id IN (SELECT entity_id FROM entities WHERE norm = ?) AND status = 'pending'
                    """, (initials, now, norm))
                    # Fallback update for the specific review_id just in case
                    conn.execute(
                        "UPDATE review_queue SET status = 'approved', decided_by = ?, decided_at = ? WHERE review_id = ?",
                        (initials, now, review_id)
                    )
                log_decision_to_audit(cfg, review_id, norm, "approved", initials, now)
                print(f"Approved as deceased_historical. Updated living status for all occurrences of norm '{norm}'.")
                break
            elif choice in ("d", "deny"):
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                with conn:
                    # Update all entities sharing this norm to potentially_living
                    conn.execute(
                        "UPDATE entities SET living_status = 'potentially_living' WHERE norm = ? AND kind = 'person'",
                        (norm,)
                    )
                    # Deny pending review_queue rows for this norm
                    conn.execute("""
                        UPDATE review_queue 
                        SET status = 'denied', decided_by = ?, decided_at = ?
                        WHERE entity_id IN (SELECT entity_id FROM entities WHERE norm = ?) AND status = 'pending'
                    """, (initials, now, norm))
                    # Fallback update for the specific review_id just in case
                    conn.execute(
                        "UPDATE review_queue SET status = 'denied', decided_by = ?, decided_at = ? WHERE review_id = ?",
                        (initials, now, review_id)
                    )
                log_decision_to_audit(cfg, review_id, norm, "denied", initials, now)
                print(f"Denied approval. Set living status to potentially_living for norm '{norm}'.")
                break
            elif choice in ("s", "skip"):
                print("Skipped.")
                break
            elif choice in ("q", "quit"):
                print("Quitting.")
                conn.close()
                return
            else:
                print("Invalid option. Please enter a, d, s, or q.")

    conn.close()

def handle_gaps(cfg: Config):
    """Handle the gap candidate interactive verification queue."""
    conn = connect(cfg)
    
    cur = conn.execute("""
        SELECT gc.gap_id, gc.redaction_id, gc.clear_entity_id, gc.score,
               gc.score_cosine, gc.score_anchor, gc.score_kind, gc.method, gc.status,
               r.doc_id AS r_doc_id, r.page_no AS r_page_no, r.kind AS r_kind, r.label AS r_label,
               r.context_before AS r_ctx_before, r.context_after AS r_ctx_after,
               e.doc_id AS e_doc_id, e.page_no AS e_page_no, e.text AS e_text, e.norm AS e_norm,
               e.char_start AS e_char_start, e.char_end AS e_char_end
        FROM gap_candidates gc
        JOIN redactions r ON gc.redaction_id = r.redaction_id
        JOIN entities e ON gc.clear_entity_id = e.entity_id
        WHERE gc.status = 'candidate'
        ORDER BY gc.score DESC
    """)
    items = cur.fetchall()
    if not items:
        print("No pending gap candidates to review.")
        conn.close()
        return

    print(f"Found {len(items)} pending gap candidates.")
    initials = None

    for item in items:
        gap_id = item["gap_id"]
        score = item["score"]
        score_cosine = item["score_cosine"]
        score_anchor = item["score_anchor"]
        score_kind = item["score_kind"]
        method = item["method"]
        r_doc_id = item["r_doc_id"]
        r_page_no = item["r_page_no"]
        r_kind = item["r_kind"]
        r_label = item["r_label"] or "N/A"
        r_ctx_before = item["r_ctx_before"] or ""
        r_ctx_after = item["r_ctx_after"] or ""
        
        e_doc_id = item["e_doc_id"]
        e_page_no = item["e_page_no"]
        e_text = item["e_text"]
        e_norm = item["e_norm"]
        e_char_start = item["e_char_start"]
        e_char_end = item["e_char_end"]
        
        # PURLs
        r_purl = f"https://www.osti.gov/opennet/servlets/purl/{r_doc_id}.pdf"
        e_purl = f"https://www.osti.gov/opennet/servlets/purl/{e_doc_id}.pdf"
        
        # Clear entity context
        clear_context = get_clear_context(conn, e_doc_id, e_page_no, e_char_start, e_char_end)
        
        print("\n" + "=" * 80)
        print(f"Gap Candidate ID: {gap_id}")
        print(f"Score:            {score:.4f} (Cosine: {score_cosine:.4f}, Anchor: {score_anchor:.4f}, Kind: {score_kind:.4f})")
        print(f"Method:           {method}")
        print("-" * 80)
        print("Redacted Page Context:")
        print(f"  Doc {r_doc_id}, Page {r_page_no} (Type: {r_kind}, Label: {r_label})")
        print(f"  Context: ... {r_ctx_before} [ REDACTED ] {r_ctx_after} ...")
        print(f"  PURL: {r_purl}")
        print("-" * 80)
        print("Corroborating Clear Page Context:")
        print(f"  Doc {e_doc_id}, Page {e_page_no} (Entity: {e_text} / norm: {e_norm})")
        if clear_context:
            print(f"  Context: ... {clear_context} ...")
        else:
            print(f"  Context: [No surrounding page text cached, entity text: '{e_text}']")
        print(f"  PURL: {e_purl}")
        print("=" * 80)

        # Prompt for initials if not yet prompted
        if initials is None:
            initials = get_initials()

        # Decision loop
        while True:
            try:
                choice = input("[v]erify / [r]eject / [s]kip / [q]uit: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                print("\nQuitting loop.")
                conn.close()
                return

            if choice in ("v", "verify"):
                notes = None
                try:
                    notes = input("Optional note: ").strip()
                except (KeyboardInterrupt, EOFError):
                    pass
                notes = notes if notes else None
                
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                with conn:
                    conn.execute("""
                        UPDATE gap_candidates
                        SET status = 'verified', reviewed_by = ?, reviewed_at = ?, notes = ?
                        WHERE gap_id = ?
                    """, (initials, now, notes, gap_id))
                print(f"Verified gap candidate {gap_id}.")
                break
            elif choice in ("r", "reject"):
                notes = None
                try:
                    notes = input("Optional note: ").strip()
                except (KeyboardInterrupt, EOFError):
                    pass
                notes = notes if notes else None
                
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                with conn:
                    conn.execute("""
                        UPDATE gap_candidates
                        SET status = 'rejected', reviewed_by = ?, reviewed_at = ?, notes = ?
                        WHERE gap_id = ?
                    """, (initials, now, notes, gap_id))
                print(f"Rejected gap candidate {gap_id}.")
                break
            elif choice in ("s", "skip"):
                print("Skipped.")
                break
            elif choice in ("q", "quit"):
                print("Quitting.")
                conn.close()
                return
            else:
                print("Invalid option. Please enter v, r, s, or q.")
                
    conn.close()

def handle_audit(cfg: Config):
    """Print the human decisions audit log from {root}/db/review_audit.jsonl."""
    audit_file = cfg.storage_root / "db" / "review_audit.jsonl"
    if not audit_file.exists():
        print("No audit records found.")
        return
        
    print(f"=== Review Audit Log: {audit_file} ===")
    try:
        with open(audit_file, "r", encoding="utf-8") as f:
            count = 0
            for line in f:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    review_id = record.get("review_id")
                    norm_hash = record.get("norm_hash")
                    decision = record.get("decision")
                    decided_by = record.get("decided_by")
                    decided_at = record.get("decided_at")
                    print(f"[{decided_at}] Review {review_id} by {decided_by}: {decision.upper()} (hash: {norm_hash})")
                    count += 1
                except Exception as e:
                    print(f"Error parsing line: {line.strip()} ({e})")
            if count == 0:
                print("No valid records found in the audit file.")
    except Exception as e:
        print(f"Failed to read audit log: {e}")

def apply_heuristic(cfg: Config):
    """Apply birth-year and document-date safety heuristic to classify pending reviews."""
    conn = connect(cfg)
    current_year = datetime.datetime.now().year
    
    # Fetch pending review queue items
    cur = conn.execute("""
        SELECT rq.review_id, rq.entity_id, e.doc_id, e.page_no, e.text, e.norm, d.year AS doc_year
        FROM review_queue rq
        JOIN entities e ON rq.entity_id = e.entity_id
        LEFT JOIN documents d ON e.doc_id = d.doc_id
        WHERE rq.status = 'pending'
    """)
    items = cur.fetchall()
    if not items:
        print("No pending items in review queue to classify.")
        conn.close()
        return
        
    print(f"Running safety heuristic on {len(items)} pending review items...")
    
    approved_count = 0
    denied_count = 0
    
    birth_patterns = [
        re.compile(r'\b(?:born|b\.|birth(?:\s+date)?)\s*[:\-\s]?\s*(\d{4})\b', re.IGNORECASE),
        re.compile(r'\(\s*(?:b\.\s*)?(\d{4})\s*-\s*(?:\d{4}|present)?\s*\)', re.IGNORECASE)
    ]
    
    for item in items:
        review_id = item["review_id"]
        entity_id = item["entity_id"]
        doc_id = item["doc_id"]
        page_no = item["page_no"]
        text = item["text"]
        norm = item["norm"]
        doc_year = item["doc_year"]
        
        # Try to find birth year from the page text
        birth_year = None
        cur_page = conn.execute("SELECT text FROM pages WHERE doc_id = ? AND page_no = ?", (doc_id, page_no))
        page_row = cur_page.fetchone()
        if page_row and page_row["text"]:
            page_text = page_row["text"]
            for pat in birth_patterns:
                m = pat.search(page_text)
                if m:
                    birth_year = int(m.group(1))
                    break
                    
        # Apply heuristic
        is_deceased = False
        reason = ""
        
        if doc_year is not None:
            doc_age = current_year - doc_year
            if doc_age > 75:
                is_deceased = True
                reason = f"Document year {doc_year} is > 75 years old ({doc_age} years)"
            elif birth_year is not None:
                age_at_doc = doc_year - birth_year
                if age_at_doc > 100:
                    is_deceased = True
                    reason = f"Subject age at document date ({doc_year} - birth year {birth_year}) is {age_at_doc} > 100"
                    
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if is_deceased:
            with conn:
                conn.execute(
                    "UPDATE entities SET living_status = 'deceased_historical' WHERE norm = ? AND kind = 'person'",
                    (norm,)
                )
                conn.execute("""
                    UPDATE review_queue 
                    SET status = 'approved', decided_by = 'HEURISTIC', decided_at = ?
                    WHERE entity_id IN (SELECT entity_id FROM entities WHERE norm = ?) AND status = 'pending'
                """, (now, norm))
                conn.execute(
                    "UPDATE review_queue SET status = 'approved', decided_by = 'HEURISTIC', decided_at = ? WHERE review_id = ?",
                    (now, review_id)
                )
            log_decision_to_audit(cfg, review_id, norm, "approved", "HEURISTIC", now)
            print(f"[APPROVE] {text} ({norm}) - {reason}")
            approved_count += 1
        else:
            with conn:
                conn.execute(
                    "UPDATE entities SET living_status = 'potentially_living' WHERE norm = ? AND kind = 'person' AND living_status = 'unknown'",
                    (norm,)
                )
            denied_count += 1
            
    print(f"Heuristic classification completed: approved {approved_count} as deceased_historical, updated {denied_count} to potentially_living.")
    conn.close()

def main():
    parser = argparse.ArgumentParser(description="Palimpsest Human-in-the-Loop Review CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # people sub-command
    people_parser = subparsers.add_parser("people", help="Review pending name disclosures or dump list")
    people_parser.add_argument("--list", action="store_true", help="Dump queue in non-interactive list mode")
    
    # gaps sub-command
    subparsers.add_parser("gaps", help="Interactive review of gap candidates")
    
    # audit sub-command
    subparsers.add_parser("audit", help="Show decision logs")
    
    # heuristic sub-command
    subparsers.add_parser("heuristic", help="Apply birth-year/document-date safety heuristic to pending reviews")
    
    args = parser.parse_args()
    
    cfg = load()
    
    if args.command == "people":
        handle_people(cfg, args.list)
    elif args.command == "gaps":
        handle_gaps(cfg)
    elif args.command == "audit":
        handle_audit(cfg)
    elif args.command == "heuristic":
        apply_heuristic(cfg)

if __name__ == "__main__":
    main()
