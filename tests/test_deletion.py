"""Deletion planning (pure) and execution (tmp_path + mocked clients)."""

import json
from pathlib import Path

import pytest
from medialab_contracts import MediaType

from medialab_orchestrator.core.errors import AppException
from medialab_orchestrator.services import deletion as deletion_module
from medialab_orchestrator.services.deletion import DeletionService, plan_deletion
from medialab_orchestrator.store import JobStatus, JobStore

HASH = "e" * 40


@pytest.fixture
def media(tmp_path: Path, mocker) -> Path:
    mocker.patch.object(deletion_module.config, "media_mount_path", str(tmp_path))
    (tmp_path / "Movies").mkdir()
    (tmp_path / "Shows").mkdir()
    return tmp_path


@pytest.fixture
def service(store, torrent_client, jellyfin_client) -> DeletionService:
    return DeletionService(
        store=store, torrent_client=torrent_client, jellyfin_client=jellyfin_client
    )


def _job(store: JobStore, status: JobStatus, media_type=MediaType.MOVIE, **fields):
    job = store.create_job(
        torrent_hash=HASH, release_name="Movie.2021.1080p", media_type=media_type, tmdb_id=1
    )
    return store.update_job(job.id, status=status, **fields)


class TestPlan:
    def test_downloading_job_removes_torrent_and_download_folder(self, store, media):
        job = _job(store, JobStatus.DOWNLOADING)
        plan = plan_deletion(job)
        assert plan.torrent is True
        assert plan.download_folder == str(media / "Movies" / "Movie.2021.1080p")
        assert plan.placed_paths == []
        assert plan.scan_path is None
        assert plan.refused is None

    def test_mid_pipeline_after_seed_removal_skips_torrent(self, store, media):
        job = _job(store, JobStatus.RENAME, seeding_removed_at="t", source_path="Movie.2021-GRP")
        plan = plan_deletion(job)
        assert plan.torrent is False
        assert plan.download_folder == str(media / "Movies" / "Movie.2021-GRP")

    def test_done_job_uses_recorded_placed_paths(self, store, media):
        placed = [str(media / "Shows" / "Show (2019)" / "Season 01" / "Show S01E01.mkv")]
        job = _job(
            store,
            JobStatus.DONE,
            MediaType.SHOW,
            seeding_removed_at="t",
            dest_path=str(media / "Shows" / "Show (2019)"),
            placed_paths=json.dumps(placed),
        )
        plan = plan_deletion(job)
        assert plan.placed_paths == placed
        assert plan.scan_path == str(media / "Shows" / "Show (2019)")
        assert plan.refused is None

    def test_done_movie_without_record_falls_back_to_its_folder(self, store, media):
        job = _job(
            store,
            JobStatus.DONE,
            seeding_removed_at="t",
            dest_path=str(media / "Movies" / "Movie (2021)"),
        )
        plan = plan_deletion(job)
        assert plan.placed_paths == [str(media / "Movies" / "Movie (2021)")]
        assert plan.scan_path == str(media / "Movies")

    def test_done_show_without_record_is_refused(self, store, media):
        job = _job(
            store,
            JobStatus.DONE,
            MediaType.SHOW,
            seeding_removed_at="t",
            dest_path=str(media / "Shows" / "Show (2019)"),
        )
        plan = plan_deletion(job)
        assert plan.refused is not None
        assert "Show (2019)" in plan.refused

    def test_deleted_job_is_refused(self, store, media):
        job = _job(store, JobStatus.DELETED)
        assert plan_deletion(job).refused == "already deleted"


class TestExecute:
    async def test_placed_movie_is_removed_and_jellyfin_told(
        self, service, store, media, torrent_client, jellyfin_client
    ):
        folder = media / "Movies" / "Movie (2021)"
        folder.mkdir()
        (folder / "Movie (2021).mkv").write_text("x")
        job = _job(
            store,
            JobStatus.DONE,
            seeding_removed_at="t",
            dest_path=str(folder),
            placed_paths=json.dumps([str(folder / "Movie (2021).mkv")]),
        )
        result = await service.execute(job)
        assert result.status is JobStatus.DELETED
        assert result.deleted_at is not None
        assert not folder.exists()  # emptied parent pruned, library root kept
        assert (media / "Movies").exists()
        torrent_client.remove_transfer.assert_not_awaited()
        jellyfin_client.scan.assert_awaited_once_with(
            path=str(media / "Movies"), update_type="Deleted"
        )

    async def test_downloading_job_deletes_torrent_with_files_and_folder(
        self, service, store, media, torrent_client, jellyfin_client
    ):
        folder = media / "Movies" / "Movie.2021.1080p"
        folder.mkdir()
        (folder / "part.mkv").write_text("x")
        job = _job(store, JobStatus.DOWNLOADING)
        result = await service.execute(job)
        assert result.status is JobStatus.DELETED
        torrent_client.remove_transfer.assert_awaited_once_with(HASH, delete_files=True)
        assert not folder.exists()
        jellyfin_client.scan.assert_not_awaited()

    async def test_show_episode_removed_and_only_empty_season_pruned(
        self, service, store, media, jellyfin_client
    ):
        season = media / "Shows" / "Show (2019)" / "Season 01"
        season.mkdir(parents=True)
        (season / "Show S01E01.mkv").write_text("x")
        (season / "Show S01E02.mkv").write_text("keep")
        job = _job(
            store,
            JobStatus.DONE,
            MediaType.SHOW,
            seeding_removed_at="t",
            dest_path=str(media / "Shows" / "Show (2019)"),
            placed_paths=json.dumps([str(season / "Show S01E01.mkv")]),
        )
        await service.execute(job)
        assert not (season / "Show S01E01.mkv").exists()
        assert (season / "Show S01E02.mkv").exists()
        assert season.exists()

    async def test_refused_plan_raises_409_and_touches_nothing(
        self, service, store, media, jellyfin_client
    ):
        job = _job(
            store,
            JobStatus.DONE,
            MediaType.SHOW,
            seeding_removed_at="t",
            dest_path=str(media / "Shows" / "Show (2019)"),
        )
        with pytest.raises(AppException) as exc:
            await service.execute(job)
        assert exc.value.status_code == 409
        assert store.get_job_by_id(job.id).status is JobStatus.DONE
        jellyfin_client.scan.assert_not_awaited()

    async def test_second_delete_is_refused(self, service, store, media):
        job = _job(store, JobStatus.DOWNLOADING)
        await service.execute(job)
        with pytest.raises(AppException):
            await service.execute(store.get_job_by_id(job.id))

    async def test_never_deletes_outside_media_root(self, service, store, media, tmp_path):
        outside = tmp_path.parent / "elsewhere.txt"
        job = _job(
            store,
            JobStatus.DONE,
            seeding_removed_at="t",
            dest_path=str(media / "Movies" / "M"),
            placed_paths=json.dumps([str(outside)]),
        )
        with pytest.raises(AppException):
            await service.execute(job)


class TestLegacySourcePath:
    def test_host_path_in_source_path_is_not_joined_into_the_plan(self, store, media):
        # Jobs from before the content_path fix stored the downloader's host root here.
        job = _job(
            store,
            JobStatus.DONE,
            seeding_removed_at="t",
            source_path="F:\Media\Movies",
            dest_path=str(media / "Movies" / "Movie (2021)"),
        )
        plan = plan_deletion(job)
        # Falls back to the release name; the host path never reaches the plan.
        assert plan.download_folder == str(media / "Movies" / "Movie.2021.1080p")
        assert "F:" not in plan.download_folder

    def test_plain_folder_name_is_used(self, store, media):
        job = _job(store, JobStatus.DOWNLOADING, source_path="Movie.2021-GRP")
        assert plan_deletion(job).download_folder == str(media / "Movies" / "Movie.2021-GRP")
