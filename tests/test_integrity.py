"""Tests for markbot.session.integrity — WAL, checksums, and archiving."""

from datetime import datetime, timedelta

import pytest

from markbot.session.integrity import SessionIntegrity


@pytest.fixture
def integrity(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    return SessionIntegrity(sessions_dir)


class TestWAL:
    def test_write_wal(self, integrity, tmp_path):
        session_path = tmp_path / "sessions" / "test.jsonl"
        wal_path = integrity.write_wal(session_path, '{"test": true}')
        assert wal_path.exists()
        assert wal_path.suffix == ".wal"
        assert wal_path.read_text(encoding="utf-8") == '{"test": true}'

    def test_commit_wal(self, integrity, tmp_path):
        session_path = tmp_path / "sessions" / "test.jsonl"
        wal_path = integrity.write_wal(session_path, '{"test": true}')
        assert wal_path.exists()
        integrity.commit_wal(session_path)
        assert not wal_path.exists()

    def test_commit_wal_nonexistent(self, integrity, tmp_path):
        session_path = tmp_path / "sessions" / "test.jsonl"
        # Should not raise
        integrity.commit_wal(session_path)

    def test_recover_from_wal(self, integrity, tmp_path):
        session_path = tmp_path / "sessions" / "test.jsonl"
        integrity.write_wal(session_path, '{"recovered": true}')
        data = integrity.recover_from_wal(session_path)
        assert data == '{"recovered": true}'

    def test_recover_no_wal(self, integrity, tmp_path):
        session_path = tmp_path / "sessions" / "test.jsonl"
        data = integrity.recover_from_wal(session_path)
        assert data is None


class TestChecksum:
    def test_compute_checksum(self, integrity):
        checksum = integrity.compute_checksum("hello world")
        assert isinstance(checksum, str)
        assert len(checksum) == 64  # SHA-256 hex

    def test_checksum_deterministic(self, integrity):
        c1 = integrity.compute_checksum("test data")
        c2 = integrity.compute_checksum("test data")
        assert c1 == c2

    def test_checksum_different_data(self, integrity):
        c1 = integrity.compute_checksum("data1")
        c2 = integrity.compute_checksum("data2")
        assert c1 != c2

    def test_write_and_verify_checksum(self, integrity, tmp_path):
        session_path = tmp_path / "sessions" / "test.jsonl"
        data = '{"test": true}'
        session_path.write_text(data, encoding="utf-8")
        integrity.write_checksum(session_path, data)
        assert integrity.verify_checksum(session_path) is True

    def test_verify_checksum_mismatch(self, integrity, tmp_path):
        session_path = tmp_path / "sessions" / "test.jsonl"
        session_path.write_text('{"original": true}', encoding="utf-8")
        integrity.write_checksum(session_path, '{"original": true}')
        # Corrupt the file
        session_path.write_text('{"corrupted": true}', encoding="utf-8")
        assert integrity.verify_checksum(session_path) is False

    def test_verify_no_checksum_file(self, integrity, tmp_path):
        session_path = tmp_path / "sessions" / "test.jsonl"
        session_path.write_text('{"test": true}', encoding="utf-8")
        # No checksum file - should return True
        assert integrity.verify_checksum(session_path) is True


class TestArchive:
    def test_archive_session(self, integrity, tmp_path):
        session_path = tmp_path / "sessions" / "test.jsonl"
        session_path.write_text('{"data": true}', encoding="utf-8")
        archive_path = integrity.archive_session(session_path)
        assert archive_path is not None
        assert archive_path.exists()
        assert not session_path.exists()
        assert "archive" in str(archive_path)

    def test_archive_nonexistent(self, integrity, tmp_path):
        session_path = tmp_path / "sessions" / "missing.jsonl"
        result = integrity.archive_session(session_path)
        assert result is None

    def test_archive_with_sidecars(self, integrity, tmp_path):
        session_path = tmp_path / "sessions" / "test.jsonl"
        session_path.write_text('{"data": true}', encoding="utf-8")
        # Create sidecar files
        cs_path = session_path.with_suffix(".sha256")
        cs_path.write_text("abc123", encoding="utf-8")
        wal_path = session_path.with_suffix(".wal")
        wal_path.write_text("wal data", encoding="utf-8")

        archive_path = integrity.archive_session(session_path)
        assert archive_path is not None
        assert not cs_path.exists()
        assert not wal_path.exists()

    def test_cleanup_archive(self, integrity, tmp_path):
        # Create an old archive entry
        archive_dir = integrity.archive_dir
        archive_dir.mkdir(parents=True, exist_ok=True)
        old_file = archive_dir / "old_session.jsonl"
        old_file.write_text("{}")
        # Set mtime to old time
        old_time = (datetime.now() - timedelta(days=100)).timestamp()
        import os
        os.utime(old_file, (old_time, old_time))

        removed = integrity.cleanup_archive()
        assert removed == 1
        assert not old_file.exists()

    def test_cleanup_archive_keeps_recent(self, integrity, tmp_path):
        archive_dir = integrity.archive_dir
        archive_dir.mkdir(parents=True, exist_ok=True)
        recent_file = archive_dir / "recent_session.jsonl"
        recent_file.write_text("{}")

        removed = integrity.cleanup_archive()
        assert removed == 0
        assert recent_file.exists()
