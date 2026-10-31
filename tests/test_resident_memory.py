"""Isolated checks for resident-aware shared memory behavior."""

import contextlib
import hashlib
import importlib.util
import io
import os
import sqlite3
import tempfile
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]


class FakeEmbeddingModel:
    def encode(self, _text, show_progress_bar=False):
        return np.array([1.0, 0.0], dtype=np.float32)


class DistinctEmbeddingModel:
    """Different texts get unrelated vectors, so the duplicate check stays quiet."""

    def encode(self, text, show_progress_bar=False):
        seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "little")
        return np.random.default_rng(seed).standard_normal(32).astype(np.float32)


def load_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, REPO / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_server(data_dir: str, resident_env: str = ""):
    os.environ["CLAUDE_MEMORIES_DIR"] = data_dir
    os.environ["CLAUDE_MEMORY_WARM_EMBEDDINGS_ON_STARTUP"] = "0"
    os.environ["NEBULA_RESIDENT_ID"] = resident_env
    return load_module("semantic_nebula_test_server", "source/mcp_server.py")


def check_default_resident():
    """NEBULA_RESIDENT_ID fills in when no resident_id is passed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        server = load_server(tmpdir, resident_env="${user_config.resident_id}")
        try:
            assert server.DEFAULT_RESIDENT_ID is None, "unresolved .mcpb placeholder must be ignored"
        finally:
            server.db.close()

    with tempfile.TemporaryDirectory() as tmpdir:
        server = load_server(tmpdir, resident_env="  Cari ")
        try:
            assert server.DEFAULT_RESIDENT_ID == "cari"
            server.ensure_embedding_model_ready = lambda: (DistinctEmbeddingModel(), None)

            def owner_of(result: str) -> str:
                memory_id = int(result.split("#", 1)[1].split(" ", 1)[0])
                return server.db.get_memory(memory_id)["resident_id"]

            assert owner_of(server.add_memory("defaulted to cari")) == "cari"
            explicit = server.add_memory_force("explicitly coro's", resident_id="Coro")
            assert owner_of(explicit) == "coro"

            # update_memory never applies the default: omitting keeps the owner
            explicit_id = int(explicit.split("#", 1)[1].split(" ", 1)[0])
            server.update_memory(explicit_id, importance=9)
            assert server.db.get_memory(explicit_id)["resident_id"] == "coro"

            # write_journal: explicit resident_id > author; the install default
            # (cari here) never overrides the name a resident signs with
            def journal_owner(title: str, **kwargs) -> str:
                result = server.write_journal(title=title, content=title * 3, **kwargs)
                memory_id = int(result.split("Memory: #", 1)[1].split("\n", 1)[0])
                return server.db.get_memory(memory_id)["resident_id"]

            assert journal_owner(title="explicit", author="Opus 4.7", resident_id="Valentine") == "valentine"
            assert journal_owner(title="signed name", author="Fable") == "fable"
        finally:
            server.db.close()


def check_backfill_script():
    """Journal companions get their entry's author; dry run writes nothing."""
    backfill = load_module("backfill_resident_ids", "scripts/backfill_resident_ids.py")
    with tempfile.TemporaryDirectory() as tmpdir:
        server = load_server(tmpdir)
        db_path = Path(tmpdir) / "fixture.db"
        fixture = server.MemoryDatabase(db_path)
        try:
            for entry_id, author in [
                ("001", "Sunshine"),
                ("2026-03-31_coming-home", "Debugger"),
                ("2026-05-13_meeting", "opus-4.7-birthday-eve"),
                ("2026-06-01_project-chat", "Claude (Opus 5.5, project chat)"),
            ]:
                fixture.save_journal_entry(
                    entry_id=entry_id, author=author, title=entry_id, entry_type="reflection",
                    content="x", why_it_mattered="x", tags=[], importance=5, date="2026-01-01",
                )
            pulse_ref = fixture.save_memory("s", type="journal", journal_file="entries/001.md")
            text_ref = fixture.save_memory("d | File: journal/entries/2026-03-31_coming-home.md", type="journal")
            bare_ref = fixture.save_memory("o", type="journal", journal_file="2026-05-13_meeting")
            skipped_ref = fixture.save_memory("c", type="journal", journal_file="2026-06-01_project-chat")
            owned = fixture.save_memory("v", type="journal", journal_file="entries/001.md", resident_id="valentine")
            orphan = fixture.save_memory("? | File: journal/entries/missing.md", type="journal")
            general = fixture.save_memory("not a journal memory")
        finally:
            fixture.close()

        quiet = io.StringIO()
        args = ["--db", str(db_path), "--map", "opus-4.7-birthday-eve=Valentine",
                "--skip", "Claude (Opus 5.5, project chat)"]
        with contextlib.redirect_stdout(quiet):
            assert backfill.main(args) == 0
        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM memories WHERE resident_id IS NOT NULL").fetchone()[0] == 1
        finally:
            conn.close()

        with contextlib.redirect_stdout(quiet):
            assert backfill.main(args + ["--apply"]) == 0
        conn = sqlite3.connect(db_path)
        try:
            owners = dict(conn.execute("SELECT id, resident_id FROM memories"))
        finally:
            conn.close()
        assert owners[pulse_ref] == "sunshine"
        assert owners[text_ref] == "debugger"
        assert owners[bare_ref] == "valentine"
        assert owners[skipped_ref] is None
        assert owners[owned] == "valentine"
        assert owners[orphan] is None
        assert owners[general] is None
        assert list(Path(tmpdir).glob("fixture.before-resident-backfill-*.db")), "backup missing"
        server.db.close()


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        server = load_server(tmpdir)
        try:
            columns = {
                row[1] for row in server.db.conn.execute("PRAGMA table_info(memories)")
            }
            assert "resident_id" in columns

            own_id = server.db.save_memory("own", resident_id="  Sunshine  ")
            neutral_id = server.db.save_memory("neutral")
            assert server.db.get_memory(own_id)["resident_id"] == "sunshine"
            assert server.db.get_memory(neutral_id)["resident_id"] is None

            legacy_path = Path(tmpdir) / "legacy-shape.db"
            legacy_conn = sqlite3.connect(legacy_path)
            try:
                legacy_conn.execute(
                    "CREATE TABLE memories ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, "
                    "tags TEXT NOT NULL DEFAULT '[]', type TEXT NOT NULL DEFAULT 'fact', "
                    "importance INTEGER NOT NULL DEFAULT 5, "
                    "retrieval_count INTEGER NOT NULL DEFAULT 0, last_accessed TEXT, "
                    "supersedes INTEGER, journal_file TEXT, "
                    "date TEXT NOT NULL DEFAULT (datetime('now')), embedding BLOB)"
                )
                legacy_conn.commit()
            finally:
                legacy_conn.close()
            migrated = server.MemoryDatabase(legacy_path)
            try:
                migrated_columns = {
                    row[1] for row in migrated.conn.execute("PRAGMA table_info(memories)")
                }
                assert "resident_id" in migrated_columns
            finally:
                migrated.close()

            vector = np.array([1.0, 0.0], dtype=np.float32).tobytes()
            memories = [
                {
                    "embedding": vector,
                    "retrieval_count": 0,
                    "importance": 5,
                    "tags": [],
                    "resident_id": "valentine",
                },
                {
                    "embedding": vector,
                    "retrieval_count": 0,
                    "importance": 5,
                    "tags": [],
                    "resident_id": "sunshine",
                },
            ]
            results = server._search_memories_core(
                "same semantic match",
                memories,
                top_k=2,
                embedding_model=FakeEmbeddingModel(),
                resident_id="Sunshine",
            )
            assert [item[0] for item in results] == [1, 0]
            assert abs(results[0][3] - results[1][3] - 0.02) < 1e-6

            server.ensure_embedding_model_ready = lambda: (FakeEmbeddingModel(), None)
            other_id = server.db.save_memory(
                "other-search-marker", embedding=vector, resident_id="valentine"
            )
            own_search_id = server.db.save_memory(
                "own-search-marker", embedding=vector, resident_id="sunshine"
            )
            output = server.search_memory(
                "same semantic match", top_k=2, resident_id="Sunshine"
            )
            assert output.find(f"[{own_search_id}]") < output.find(f"[{other_id}]")
            assert f"[{own_search_id}] (fact) by sunshine" in output
            assert f"[{other_id}] (fact) by valentine" in output
            assert f"[{neutral_id}] " in server.list_memories(limit=50)
            assert " by " not in server.list_memories(limit=50).split(f"[{neutral_id}]", 1)[1].split("\n", 1)[0]
        finally:
            server.db.close()

    check_default_resident()
    check_backfill_script()
    print("[OK] Semantic Nebula resident memory backend")


if __name__ == "__main__":
    main()
