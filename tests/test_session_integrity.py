"""Tests for markbot.session.integrity — WAL, checksums, and archiving."""

import json
from pathlib import Path

from markbot.session.integrity import SessionIntegrity


class TestSessionIntegrity:
    def test_write_and_commit_wal(self, tmp_path: Path):
        si = SessionIntegrity(tmp_path)
        session_path = tmp_path / "test.jsonl"
        data = '{"role": "user", "content": "hello"}'

        wal_path = si.write_wal(session_path, data)
        assert wal_path.exists()
        assert wal_path.suffix == ".wal"

        si.commit_wal(session_path)
        assert not wal_path.exists()

    def test_recover_from_wal(self, tmp_path: Path):
        si = SessionIntegrity(tmp_path)
        session_path = tmp_path / "test.jsonl"
        data = '{"role": "user", "content": "hello"}'

        si.write_wal(session_path, data)
        recovered = si.recover_from_wal(session_path)
        assert recovered == data

    def test_recover_no_wal(self, tmp_path: Path):
        si = SessionIntegrity(tmp_path)
        session_path = tmp_path / "nonexistent.jsonl"
        assert si.recover_from_wal(session_path) is None

    def test_checksum_roundtrip(self, tmp_path: Path):
        si = SessionIntegrity(tmp_path)
        session_path = tmp_path / "test.jsonl"
        data = "test session data"

        session_path.write_text(data, encoding="utf-8")
        si.write_checksum(session_path, data)

        assert si.verify_checksum(session_path) is True

    def test_checksum_mismatch(self, tmp_path: Path):
        si = SessionIntegrity(tmp_path)
        session_path = tmp_path / "test.jsonl"

        session_path.write_text("original data", encoding="utf-8")
        si.write_checksum(session_path, "original data")

        session_path.write_text("tampered data", encoding="utf-8")
        assert si.verify_checksum(session_path) is False

    def test_checksum_missing_file_ok(self, tmp_path: Path):
        si = SessionIntegrity(tmp_path)
        session_path = tmp_path / "no_checksum.jsonl"
        session_path.write_text("data", encoding="utf-8")
        assert si.verify_checksum(session_path) is True

    def test_archive_session(self, tmp_path: Path):
        si = SessionIntegrity(tmp_path)
        session_path = tmp_path / "old_session.jsonl"
        session_path.write_text("old data", encoding="utf-8")

        archive_path = si.archive_session(session_path)
        assert archive_path is not None
        assert not session_path.exists()
        assert archive_path.exists()

    def test_archive_nonexistent(self, tmp_path: Path):
        si = SessionIntegrity(tmp_path)
        session_path = tmp_path / "nonexistent.jsonl"
        assert si.archive_session(session_path) is None

    def test_cleanup_archive(self, tmp_path: Path):
        si = SessionIntegrity(tmp_path, archive_ttl_days=0)
        archive_dir = tmp_path / "archive"
        archive_dir.mkdir()

        old_file = archive_dir / "old_session.jsonl"
        old_file.write_text("old data", encoding="utf-8")

        import os
        import time
        old_time = time.time() - 100 * 86400
        os.utime(old_file, (old_time, old_time))

        removed = si.cleanup_archive()
        assert removed == 1
        assert not old_file.exists()
