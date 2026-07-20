"""JobStore unit tests: surrogate id, nullable hash, stamp, update by id."""

import pytest
from medialab_contracts import MediaType

from medialab_orchestrator.store import JobNotFoundError, JobStatus, JobStore, PipelineJob

HASH = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"
RELEASE = "Show.Name.S01.1080p.GROUP"


def _create(store: JobStore, **overrides) -> PipelineJob:
    kwargs = {
        "release_name": RELEASE,
        "media_type": MediaType.SHOW,
        "tmdb_id": 1234,
    }
    kwargs.update(overrides)
    return store.create_job(**kwargs)


class TestCreate:
    def test_create_returns_job_at_default_status(self, store: JobStore):
        job = _create(store)
        assert isinstance(job.id, str) and len(job.id) > 0
        assert job.status is JobStatus.DOWNLOAD_SUBMITTED
        assert job.media_type is MediaType.SHOW
        assert job.tmdb_id == 1234
        assert job.attempts == 0
        assert job.created_at == job.updated_at

    def test_create_has_no_hash_by_default(self, store: JobStore):
        # The hash is not known at creation for a .torrent-URL download; it is
        # stamped later (readback or completion webhook).
        job = _create(store)
        assert job.torrent_hash is None

    def test_ids_are_unique(self, store: JobStore):
        a = _create(store)
        b = _create(store)
        assert a.id != b.id


class TestStampHash:
    def test_stamp_sets_lowercased_hash(self, store: JobStore):
        job = _create(store)
        stamped = store.stamp_hash(job.id, HASH)
        assert stamped.torrent_hash == HASH.lower()

    def test_stamped_hash_is_findable(self, store: JobStore):
        job = _create(store)
        store.stamp_hash(job.id, HASH)
        assert store.get_job_by_hash(HASH).id == job.id

    def test_stamp_missing_id_raises(self, store: JobStore):
        with pytest.raises(JobNotFoundError):
            store.stamp_hash("no-such-id", HASH)

    def test_duplicate_hash_rejected(self, store: JobStore):
        a = _create(store)
        b = _create(store)
        store.stamp_hash(a.id, HASH)
        with pytest.raises(Exception):  # noqa: B017 - sqlite IntegrityError
            store.stamp_hash(b.id, HASH)


class TestLookup:
    def test_get_by_hash_case_insensitive(self, store: JobStore):
        job = _create(store)
        store.stamp_hash(job.id, HASH)
        found = store.get_job_by_hash(HASH.lower())
        assert found.torrent_hash == HASH.lower()

    def test_get_by_id(self, store: JobStore):
        created = _create(store)
        assert store.get_job_by_id(created.id).id == created.id

    def test_missing_hash_raises(self, store: JobStore):
        with pytest.raises(JobNotFoundError):
            store.get_job_by_hash("deadbeef")

    def test_missing_id_raises(self, store: JobStore):
        with pytest.raises(JobNotFoundError):
            store.get_job_by_id("no-such-id")


class TestList:
    def test_empty(self, store: JobStore):
        assert store.list_jobs() == []

    def test_newest_first(self, store: JobStore):
        first = _create(store)
        second = _create(store)
        ids = [j.id for j in store.list_jobs()]
        assert ids == [second.id, first.id]

    def test_filter_by_status(self, store: JobStore):
        a = _create(store)
        _create(store)
        store.update_job(a.id, status=JobStatus.DONE)
        done = store.list_jobs(status=JobStatus.DONE)
        assert len(done) == 1
        assert done[0].id == a.id


class TestUpdate:
    def test_update_status_and_fields(self, store: JobStore):
        job = _create(store)
        updated = store.update_job(
            job.id,
            status=JobStatus.RESOLVE_META,
            resolved_title="Show Name",
            resolved_year=2020,
        )
        assert updated.status is JobStatus.RESOLVE_META
        assert updated.resolved_title == "Show Name"
        assert updated.resolved_year == 2020

    def test_update_bumps_updated_at(self, store: JobStore):
        created = _create(store)
        updated = store.update_job(created.id, attempts=1)
        assert updated.updated_at >= created.updated_at
        assert updated.created_at == created.created_at

    def test_update_unknown_column_raises(self, store: JobStore):
        job = _create(store)
        with pytest.raises(ValueError):
            store.update_job(job.id, bogus="x")

    def test_update_immutable_column_raises(self, store: JobStore):
        job = _create(store)
        with pytest.raises(ValueError):
            store.update_job(job.id, created_at="x")

    def test_update_missing_id_raises(self, store: JobStore):
        with pytest.raises(JobNotFoundError):
            store.update_job("no-such-id", status=JobStatus.DONE)


class TestPersistence:
    def test_survives_reconnect(self, tmp_path):
        db = str(tmp_path / "jobs.db")
        store_a = JobStore(db_path=db)
        _create(store_a)
        store_b = JobStore(db_path=db)
        assert store_b.list_jobs()[0].release_name == RELEASE
