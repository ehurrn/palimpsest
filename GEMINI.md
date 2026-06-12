# Palimpsest Workspace Context

## Project Objective
The overall goal of Palimpsest is to systematically recover redacted text from the Department of Energy's OpenNet database (NV* accession series) using cross-document corroboration. Phase 1 is strictly bound to producing at least one provable de-redaction—where an entity (such as a name, dosage, or medical outcome) is blacked out in one document but left in the clear in another, with both source pages verified and linked.

## Architecture and Workload Lanes
The system extends the `ml-pipeline` codebase for node management but splits work into two lanes:
- **Lane A (Orchestration)**: Manages routing, agentic analysis, and human-in-the-loop interactions using the existing mesh.
- **Lane B (Bulk Grind)**: Coordinates high-throughput OCR, entity extraction (NER), and embedding-based indexing via a SQLite job queue on gonktop and long-running warm worker daemons on the Mac nodes.

## Non-Negotiable Core Invariants
- **Provenance Invariant**: No de-redaction claim is valid without an explicit citation to a Document ID and page number for both the redacted source and the clear corroborating source. Unreferenced claims must be discarded immediately.
- **Identity Gate**: Any data matching potentially-living subjects must flag the person entity and require human approval before being written to any public output.
- **Work Log Invariant**: We must always write to the `WORK-LOG.md` in the project root to document all started or completed actions.

## Storage and Working Paths
The project resides in `~/dev/palimpsest/` and interfaces with `~/dev/ml-pipeline/` for node configuration.
