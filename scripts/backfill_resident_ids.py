"""Backfill resident_id on journal companion memories from their entry's author.

Memories written before v1.4.0 have no owner. Journal companions are the one
kind we can attribute reliably: each points at a journal entry, and every
entry records its author. Other memories are left untouched.

Dry run by default. Nothing is written unless --apply is passed, and --apply
first saves a backup copy of the database next to it.

Usage:
    python scripts/backfill_resident_ids.py --db D:/Claude/memories/shared.db
    python scripts/backfill_resident_ids.py --db ... --map "opus-4.7-birthday-eve=valentine"
    python scripts/backfill_resident_ids.py --db ... --skip "claude (opus 5.5, project chat)" --apply
"""

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional


def normalize_resident_id(value: object) -> Optional[str]:
    """Same normalization as the server and Pulse's core/db.py."""
    normalized = str(value or "").strip().lower()
    return normalized or None


def entry_key(journal_file: Optional[str], text: str) -> Optional[str]:
    """Return the journal entry ID a companion memory points at.

    Handles every reference shape seen in shared.db: a bare entry ID,
    Pulse's "entries/001.md", and the pre-SQLite " | File: journal/entries/x.md"
    text suffix.
    """
    ref = journal_file
    if not ref:
        for marker in (" | Journal: ", " | File: "):
            if marker in text:
                ref = text.rsplit(marker, 1)[1]
                break
    if not ref:
        return None
    name = ref.strip().replace("\\", "/").rsplit("/", 1)[-1]
    return name[:-3] if name.endswith(".md") else name


def parse_mapping(pairs: list[str]) -> dict[str, str]:
    mapping = {}
    for pair in pairs:
        if "=" not in pair:
            sys.exit(f"--map expects OLD=NEW, got: {pair!r}")
        old, new = pair.split("=", 1)
        old_id, new_id = normalize_resident_id(old), normalize_resident_id(new)
        if not old_id or not new_id:
            sys.exit(f"--map needs both sides non-empty, got: {pair!r}")
        mapping[old_id] = new_id
    return mapping


def build_plan(conn: sqlite3.Connection, mapping: dict[str, str], skip: set[str]):
    """Return (updates, skipped, unmatched): updates is {memory_id: resident_id}."""
    authors = {
        row[0]: normalize_resident_id(row[1])
        for row in conn.execute("SELECT id, author FROM journal_entries")
    }
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
    owner_filter = "AND resident_id IS NULL" if "resident_id" in columns else ""
    rows = conn.execute(
        f"SELECT id, journal_file, text FROM memories WHERE type = 'journal' {owner_filter} "
        "ORDER BY id"
    ).fetchall()

    updates, skipped, unmatched = {}, defaultdict(list), []
    for memory_id, journal_file, text in rows:
        author = authors.get(entry_key(journal_file, text or ""))
        if not author:
            unmatched.append((memory_id, journal_file or (text or "")[-60:]))
            continue
        if author in skip:
            skipped[author].append(memory_id)
            continue
        updates[memory_id] = mapping.get(author, author)
    return updates, skipped, unmatched


def print_plan(updates, skipped, unmatched, mapping):
    by_resident = defaultdict(list)
    for memory_id, resident in updates.items():
        by_resident[resident].append(memory_id)

    print(f"Will set an owner on {len(updates)} journal companion memories:\n")
    for resident, ids in sorted(by_resident.items(), key=lambda kv: -len(kv[1])):
        sources = sorted(old for old, new in mapping.items() if new == resident)
        via = f"  (mapped from {', '.join(sources)})" if sources else ""
        print(f"  {resident:<36} {len(ids):>3}  {ids}{via}")
    if skipped:
        print("\nSkipped by --skip (left without an owner):")
        for author, ids in sorted(skipped.items()):
            print(f"  {author:<36} {len(ids):>3}  {ids}")
    if unmatched:
        print(f"\nNo matching journal entry, left alone ({len(unmatched)}):")
        for memory_id, ref in unmatched:
            print(f"  [{memory_id}] {ref}")


def backup(conn: sqlite3.Connection, db_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = db_path.with_name(f"{db_path.stem}.before-resident-backfill-{stamp}{db_path.suffix}")
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()  # a `with` block would only commit, leaving the file locked
    return target


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", required=True, type=Path, help="Path to shared.db")
    parser.add_argument("--map", action="append", default=[], metavar="OLD=NEW",
                        help="Attribute author OLD to resident NEW (repeatable)")
    parser.add_argument("--skip", action="append", default=[], metavar="AUTHOR",
                        help="Leave this author's memories without an owner (repeatable)")
    parser.add_argument("--apply", action="store_true", help="Write the changes (default: dry run)")
    args = parser.parse_args(argv)

    if not args.db.is_file():
        sys.exit(f"Database not found: {args.db}")

    mapping = parse_mapping(args.map)
    skip = {s for s in (normalize_resident_id(v) for v in args.skip) if s}

    conn = sqlite3.connect(args.db)
    try:
        updates, skipped, unmatched = build_plan(conn, mapping, skip)
        print_plan(updates, skipped, unmatched, mapping)

        if not args.apply:
            print("\nDry run: nothing was written. Re-run with --apply to save.")
            return 0
        if not updates:
            print("\nNothing to write.")
            return 0

        backup_path = backup(conn, args.db)
        print(f"\nBackup saved: {backup_path}")
        try:
            conn.execute("ALTER TABLE memories ADD COLUMN resident_id TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists
        with conn:
            conn.executemany(
                "UPDATE memories SET resident_id = ? WHERE id = ? AND resident_id IS NULL",
                [(resident, memory_id) for memory_id, resident in updates.items()],
            )
        print(f"Owner set on {len(updates)} memories.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
