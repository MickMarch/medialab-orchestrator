"""WishlistStore unit tests: idempotent upsert, no-op remove, ordering, schema coexistence."""

import sqlite3
from pathlib import Path

from medialab_contracts import MediaType, WishlistAddRequest

from medialab_orchestrator.store import JobStore, WishlistStore

DUNE_ID = 438631


def _request(title: str = "Dune", **overrides) -> WishlistAddRequest:
    fields = {"title": title, "year": "2021", "poster_path": "/dune.jpg", "overview": "Sand."}
    fields.update(overrides)
    return WishlistAddRequest(**fields)


class TestAdd:
    def test_add_returns_the_stored_item(self, wishlist: WishlistStore):
        item = wishlist.add(MediaType.MOVIE, DUNE_ID, _request())
        assert item.tmdb_id == DUNE_ID
        assert item.media_type is MediaType.MOVIE
        assert item.title == "Dune"
        assert item.year == "2021"
        assert item.poster_path == "/dune.jpg"
        assert item.overview == "Sand."
        assert item.in_library is False

    def test_add_is_idempotent_and_keeps_added_at(self, wishlist: WishlistStore):
        first = wishlist.add(MediaType.MOVIE, DUNE_ID, _request())
        second = wishlist.add(MediaType.MOVIE, DUNE_ID, _request(title="Dune: Part One"))
        assert len(wishlist.list()) == 1
        assert second.added_at == first.added_at
        assert second.title == "Dune: Part One"

    def test_same_id_different_media_type_is_a_separate_row(self, wishlist: WishlistStore):
        wishlist.add(MediaType.MOVIE, DUNE_ID, _request())
        wishlist.add(MediaType.SHOW, DUNE_ID, _request())
        assert len(wishlist.list()) == 2


class TestRemove:
    def test_remove_deletes_only_the_matching_row(self, wishlist: WishlistStore):
        wishlist.add(MediaType.MOVIE, DUNE_ID, _request())
        wishlist.add(MediaType.SHOW, DUNE_ID, _request())
        wishlist.remove(MediaType.MOVIE, DUNE_ID)
        assert wishlist.keys(MediaType.MOVIE) == set()
        assert wishlist.keys(MediaType.SHOW) == {DUNE_ID}

    def test_remove_absent_is_a_noop(self, wishlist: WishlistStore):
        wishlist.remove(MediaType.MOVIE, DUNE_ID)
        assert wishlist.list() == []


class TestList:
    def test_newest_first(self, wishlist: WishlistStore):
        for tmdb_id in (1, 2, 3):
            wishlist.add(MediaType.MOVIE, tmdb_id, _request(title=str(tmdb_id)))
        assert [item.tmdb_id for item in wishlist.list()] == [3, 2, 1]

    def test_filter_by_media_type(self, wishlist: WishlistStore):
        wishlist.add(MediaType.MOVIE, 1, _request())
        wishlist.add(MediaType.SHOW, 2, _request())
        assert [item.tmdb_id for item in wishlist.list(MediaType.SHOW)] == [2]

    def test_keys_are_scoped_to_media_type(self, wishlist: WishlistStore):
        wishlist.add(MediaType.MOVIE, 1, _request())
        wishlist.add(MediaType.MOVIE, 2, _request())
        wishlist.add(MediaType.SHOW, 3, _request())
        assert wishlist.keys(MediaType.MOVIE) == {1, 2}


class TestSchema:
    def test_table_creation_on_an_existing_db_leaves_pipeline_job_intact(self, tmp_path: Path):
        db_path = str(tmp_path / "orchestrator.db")
        jobs = JobStore(db_path=db_path)
        job = jobs.create_job(release_name="r", media_type=MediaType.MOVIE, tmdb_id=DUNE_ID)

        wishlist = WishlistStore(db_path=db_path)
        wishlist.add(MediaType.MOVIE, DUNE_ID, _request())
        WishlistStore(db_path=db_path)

        assert jobs.get_job_by_id(job.id).tmdb_id == DUNE_ID
        assert len(wishlist.list()) == 1
        with sqlite3.connect(db_path) as conn:
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert {"pipeline_job", "wishlist_item"} <= tables
