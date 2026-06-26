"""JobStore unit tests: insert, lookup, list, update, idempotency-relevant edges."""

import pytest
from medialab_contracts import MediaType

from medialab_orchestrator.store import JobNotFoundError, JobStatus, JobStore, PipelineJob

HASH = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"
RELEASE = "Show.Name.S01.1080p.GROUP"


def _create(store: JobStore, **overrides) -> PipelineJob:
    kwargs = {
        "torrent_hash": HASH,
        "release_name": RELEASE,
        "media_type": MediaType.SHOW,
        "tmdb_id": 1234,
    }
    kwargs.update(overrides)
    return store.create_job(**kwargs)


class TestCreate:
    def test_create_returns_job_at_default_status(self, store: JobStore):
        job = _create(store)
        assert job.id > 0
        assert job.status is JobStatus.DOWNLOAD_SUBMITTED
        assert job.media_type is MediaType.SHOW
        assert job.tmdb_id == 1234
        assert job.attempts == 0
        assert job.created_at == job.updated_at

    def test_hash_stored_lowercase(self, store: JobStore):
        job = _create(store)
        assert job.torrent_hash == HASH.lower()

    def test_duplicate_hash_rejected(self, store: JobStore):
        _create(store)
        with pytest.raises(Exception):  # noqa: B017 - sqlite IntegrityError
            _create(store)


class TestLookup:
    def test_get_by_hash_case_insensitive(self, store: JobStore):
        _create(store)
        job = store.get_job_by_hash(HASH.lower())
        assert job.torrent_hash == HASH.lower()

    def test_get_by_id(self, store: JobStore):
        created = _create(store)
        assert store.get_job_by_id(created.id).id == created.id

    def test_missing_hash_raises(self, store: JobStore):
        with pytest.raises(JobNotFoundError):
            store.get_job_by_hash("deadbeef")

    def test_missing_id_raises(self, store: JobStore):
        with pytest.raises(JobNotFoundError):
            store.get_job_by_id(999)


class TestList:
    def test_empty(self, store: JobStore):
        assert store.list_jobs() == []

    def test_newest_first(self, store: JobStore):
        first = _create(store)
        second = _create(store, torrent_hash="b" * 40)
        ids = [j.id for j in store.list_jobs()]
        assert ids == [second.id, first.id]

    def test_filter_by_status(self, store: JobStore):
        _create(store)
        _create(store, torrent_hash="b" * 40)
        store.update_job(HASH, status=JobStatus.DONE)
        done = store.list_jobs(status=JobStatus.DONE)
        assert len(done) == 1
        assert done[0].torrent_hash == HASH.lower()


class TestUpdate:
    def test_update_status_and_fields(self, store: JobStore):
        _create(store)
        updated = store.update_job(
            HASH,
            status=JobStatus.RESOLVE_META,
            resolved_title="Show Name",
            resolved_year=2020,
        )
        assert updated.status is JobStatus.RESOLVE_META
        assert updated.resolved_title == "Show Name"
        assert updated.resolved_year == 2020

    def test_update_bumps_updated_at(self, store: JobStore):
        created = _create(store)
        updated = store.update_job(HASH, attempts=1)
        assert updated.updated_at >= created.updated_at
        assert updated.created_at == created.created_at

    def test_update_unknown_column_raises(self, store: JobStore):
        _create(store)
        with pytest.raises(ValueError):
            store.update_job(HASH, bogus="x")

    def test_update_immutable_column_raises(self, store: JobStore):
        _create(store)
        with pytest.raises(ValueError):
            store.update_job(HASH, created_at="x")

    def test_update_missing_hash_raises(self, store: JobStore):
        with pytest.raises(JobNotFoundError):
            store.update_job("deadbeef", status=JobStatus.DONE)


class TestPersistence:
    def test_survives_reconnect(self, tmp_path):
        db = str(tmp_path / "jobs.db")
        store_a = JobStore(db_path=db)
        _create(store_a)
        store_b = JobStore(db_path=db)
        assert store_b.get_job_by_hash(HASH).release_name == RELEASE
