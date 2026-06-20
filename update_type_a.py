import re

with open("palimpsest/scorers/type_a.py", "r") as f:
    content = f.read()

new_top = """    def top(self, conn: sqlite3.Connection, limit: int = 20, doc_ids: list[str] | None = None) -> list[Candidate]:
        \"\"\"Return top-N gap candidates ordered by score DESC.\"\"\"
        if doc_ids is not None and not doc_ids:
            return []

        base = (
            "SELECT gc.gap_id, gc.redaction_id, gc.clear_entity_id, gc.score, "
            "gc.method, r.doc_id AS red_doc_id, r.page_no AS red_page_no, "
            "e.doc_id AS ent_doc_id, e.page_no AS ent_page_no, "
            "e.kind, e.norm "
            "FROM gap_candidates gc "
            "JOIN redactions r ON gc.redaction_id = r.redaction_id "
            "JOIN entities e ON gc.clear_entity_id = e.entity_id "
            "WHERE gc.status = 'candidate' "
        )

        if doc_ids is None:
            base += "ORDER BY gc.score DESC LIMIT ?"
            rows = conn.execute(base, [limit]).fetchall()
            return [self._row_to_candidate(row) for row in rows]

        chunk_size = 400
        all_rows = []
        for i in range(0, len(doc_ids), chunk_size):
            chunk = doc_ids[i:i+chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            query = base + f"AND (r.doc_id IN ({placeholders}) OR e.doc_id IN ({placeholders})) "
            query += "ORDER BY gc.score DESC LIMIT ?"
            params = chunk + chunk + [limit]
            all_rows.extend(conn.execute(query, params).fetchall())

        seen = set()
        deduped = []
        for row in sorted(all_rows, key=lambda r: r["score"], reverse=True):
            if row["gap_id"] not in seen:
                seen.add(row["gap_id"])
                deduped.append(row)
                if len(deduped) >= limit:
                    break

        return [self._row_to_candidate(row) for row in deduped]"""

content = re.sub(r'    def top\(self, conn: sqlite3\.Connection, limit: int = 20, doc_ids: list\[str\] \| None = None\) -> list\[Candidate\]:.*?return \[self\._row_to_candidate\(row\) for row in rows\]', new_top, content, flags=re.DOTALL)

with open("palimpsest/scorers/type_a.py", "w") as f:
    f.write(content)
