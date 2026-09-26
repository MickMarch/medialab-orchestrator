"""Rename planning to Jellyfin's layout (pure, no disk) and the mover (tmp_path)."""

import shutil
from pathlib import Path

import pytest
from medialab_contracts import MediaType

from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.services import rename as rename_module
from medialab_orchestrator.services.rename import (
    EpisodeUnparseableError,
    MediaFile,
    RenameIncompleteError,
    RenamePlan,
    apply_plan,
    episode_stem,
    list_files,
    parse_episode,
    plan_rename,
    sanitize_title,
    title_dir,
)

SHOWS = Path("/media/Shows")
MOVIES = Path("/media/Movies")
GB = 1_000_000_000


def _files(source: Path, *names: str, size: int = GB) -> list[MediaFile]:
    return [MediaFile(path=source / n, size=size) for n in names]


def _moves(plan: RenamePlan) -> dict[Path, Path]:
    return dict(plan.moves)


class TestTitle:
    @pytest.mark.parametrize(
        ("raw", "clean"),
        [
            ("Mission: Impossible", "Mission Impossible"),
            ('What/If? "Quoted" <b>', "WhatIf Quoted b"),
            ("  Two   Spaces ", "Two Spaces"),
            ("Ends with dot.", "Ends with dot"),
            ("Plain Title", "Plain Title"),
        ],
    )
    def test_sanitize(self, raw: str, clean: str):
        assert sanitize_title(raw) == clean

    def test_title_dir_with_and_without_year(self):
        assert title_dir("The Show", 2020) == "The Show (2020)"
        assert title_dir("The Show", 0) == "The Show"


class TestParseEpisode:
    @pytest.mark.parametrize(
        ("name", "season", "episodes"),
        [
            ("Show.Name.S01E02.1080p.WEB-DL.x264-GROUP.mkv", 1, [2]),
            ("Show Name - 1x03 - Title.mkv", 1, [3]),
            ("Show.Name.S01E01E02.1080p.mkv", 1, [1, 2]),
            ("Show.Name.S01E01-E02.1080p.mkv", 1, [1, 2]),
            ("show.name.s00e05.special.mkv", 0, [5]),
            ("Show (2019) - S10E12 - Finale.mkv", 10, [12]),
        ],
    )
    def test_parses(self, name: str, season: int, episodes: list[int]):
        assert parse_episode(name) == (season, episodes)

    @pytest.mark.parametrize(
        "name", ["Show.Name.S01.1080p.GROUP.mkv", "Movie.Name.2020.1080p.mkv", "sample.mkv"]
    )
    def test_unparseable(self, name: str):
        with pytest.raises(EpisodeUnparseableError):
            parse_episode(name)

    def test_episode_stem(self):
        assert episode_stem("The Show", 1, [2]) == "The Show S01E02"
        assert episode_stem("The Show", 1, [1, 2]) == "The Show S01E01-E02"
        assert episode_stem("The Show", 0, [5]) == "The Show S00E05"


class TestPlanShow:
    def test_season_pack_places_every_episode(self):
        source = SHOWS / "Show.Name.S01.1080p.GROUP"
        plan = plan_rename(
            media_type=MediaType.SHOW,
            media_root=SHOWS,
            release_name=source.name,
            title="Show Name",
            year=2019,
            files=_files(source, "Show.Name.S01E01.1080p.mkv", "Show.Name.S01E02.1080p.mkv"),
        )
        season = SHOWS / "Show Name (2019)" / "Season 01"
        assert plan.source == source
        assert plan.scan_dir == SHOWS / "Show Name (2019)"
        assert _moves(plan) == {
            source / "Show.Name.S01E01.1080p.mkv": season / "Show Name S01E01.mkv",
            source / "Show.Name.S01E02.1080p.mkv": season / "Show Name S01E02.mkv",
        }

    def test_multi_season_nested_pack_places_by_each_files_season(self):
        source = SHOWS / "Show.S01-S02"
        files = [
            MediaFile(path=source / "S01" / "Show.S01E01.mkv", size=GB),
            MediaFile(path=source / "S02" / "Show.S02E01.mkv", size=GB),
        ]
        plan = plan_rename(
            media_type=MediaType.SHOW,
            media_root=SHOWS,
            release_name=source.name,
            title="Show",
            year=2019,
            files=files,
        )
        moves = _moves(plan)
        assert moves[source / "S01" / "Show.S01E01.mkv"].parent.name == "Season 01"
        assert moves[source / "S02" / "Show.S02E01.mkv"].parent.name == "Season 02"

    def test_multi_episode_and_specials(self):
        source = SHOWS / "Show.S01"
        plan = plan_rename(
            media_type=MediaType.SHOW,
            media_root=SHOWS,
            release_name=source.name,
            title="Show",
            year=2019,
            files=_files(source, "Show.S01E01-E02.mkv", "Show.S00E01.Pilot.mkv"),
        )
        moves = _moves(plan)
        assert moves[source / "Show.S01E01-E02.mkv"].name == "Show S01E01-E02.mkv"
        assert moves[source / "Show.S00E01.Pilot.mkv"] == (
            SHOWS / "Show (2019)" / "Season 00" / "Show S00E01.mkv"
        )

    def test_subtitles_follow_their_video_keeping_language_suffix(self):
        source = SHOWS / "Show.S01"
        plan = plan_rename(
            media_type=MediaType.SHOW,
            media_root=SHOWS,
            release_name=source.name,
            title="Show",
            year=2019,
            files=[
                MediaFile(path=source / "Show.S01E01.1080p.mkv", size=GB),
                MediaFile(path=source / "Show.S01E01.1080p.en.srt", size=10),
                MediaFile(path=source / "Show.S01E01.1080p.srt", size=10),
            ],
        )
        moves = _moves(plan)
        season = SHOWS / "Show (2019)" / "Season 01"
        assert moves[source / "Show.S01E01.1080p.en.srt"] == season / "Show S01E01.en.srt"
        assert moves[source / "Show.S01E01.1080p.srt"] == season / "Show S01E01.srt"

    def test_non_media_files_are_not_moved(self):
        source = SHOWS / "Show.S01"
        plan = plan_rename(
            media_type=MediaType.SHOW,
            media_root=SHOWS,
            release_name=source.name,
            title="Show",
            year=2019,
            files=_files(source, "Show.S01E01.mkv", "Show.S01E01.nfo", "cover.jpg", "RARBG.txt"),
        )
        assert [src.name for src, _ in plan.moves] == ["Show.S01E01.mkv"]

    def test_any_unparseable_episode_fails_the_whole_job(self):
        source = SHOWS / "Show.S01"
        with pytest.raises(AppException) as exc:
            plan_rename(
                media_type=MediaType.SHOW,
                media_root=SHOWS,
                release_name=source.name,
                title="Show",
                year=2019,
                files=_files(source, "Show.S01E01.mkv", "Show.Bonus.Featurette.mkv"),
            )
        assert exc.value.code is ErrorCode.EPISODE_UNPARSEABLE
        assert "Show.Bonus.Featurette.mkv" in exc.value.detail

    def test_single_file_torrent(self):
        single = SHOWS / "Show.S01E05.1080p.mkv"
        plan = plan_rename(
            media_type=MediaType.SHOW,
            media_root=SHOWS,
            release_name=single.name,
            title="Show",
            year=2019,
            files=[MediaFile(path=single, size=GB)],
        )
        assert _moves(plan) == {single: SHOWS / "Show (2019)" / "Season 01" / "Show S01E05.mkv"}

    def test_title_is_sanitised_in_paths(self):
        source = SHOWS / "Show.S01"
        plan = plan_rename(
            media_type=MediaType.SHOW,
            media_root=SHOWS,
            release_name=source.name,
            title="Mission: Impossible",
            year=2019,
            files=_files(source, "Show.S01E01.mkv"),
        )
        dest = next(iter(_moves(plan).values()))
        assert (
            dest
            == SHOWS / "Mission Impossible (2019)" / "Season 01" / "Mission Impossible S01E01.mkv"
        )


class TestPlanMovie:
    def test_largest_video_is_the_main_file_others_are_extras(self):
        source = MOVIES / "Movie.2021.1080p-GRP"
        plan = plan_rename(
            media_type=MediaType.MOVIE,
            media_root=MOVIES,
            release_name=source.name,
            title="Movie",
            year=2021,
            files=[
                MediaFile(path=source / "Movie.2021.1080p-GRP.mkv", size=8 * GB),
                MediaFile(path=source / "Sample" / "sample.mkv", size=50_000_000),
                MediaFile(path=source / "Movie.2021.1080p-GRP.en.srt", size=10),
                MediaFile(path=source / "Movie.2021.1080p-GRP.nfo", size=10),
            ],
        )
        movie_dir = MOVIES / "Movie (2021)"
        assert plan.scan_dir == movie_dir
        assert _moves(plan) == {
            source / "Movie.2021.1080p-GRP.mkv": movie_dir / "Movie (2021).mkv",
            source / "Movie.2021.1080p-GRP.en.srt": movie_dir / "Movie (2021).en.srt",
            source / "Sample" / "sample.mkv": movie_dir / "extras" / "sample.mkv",
        }

    def test_single_file_movie(self):
        single = MOVIES / "Movie.2021.1080p.mkv"
        plan = plan_rename(
            media_type=MediaType.MOVIE,
            media_root=MOVIES,
            release_name=single.name,
            title="Movie",
            year=2021,
            files=[MediaFile(path=single, size=GB)],
        )
        assert _moves(plan) == {single: MOVIES / "Movie (2021)" / "Movie (2021).mkv"}

    def test_no_video_files_plans_nothing_but_still_names_the_scan_dir(self):
        source = MOVIES / "Movie.2021"
        plan = plan_rename(
            media_type=MediaType.MOVIE,
            media_root=MOVIES,
            release_name=source.name,
            title="Movie",
            year=2021,
            files=_files(source, "readme.txt"),
        )
        assert plan.moves == ()
        assert plan.scan_dir == MOVIES / "Movie (2021)"


class TestListFiles:
    def test_recursive_with_sizes(self, tmp_path: Path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "a.mkv").write_bytes(b"12345")
        (tmp_path / "sub" / "b.srt").write_bytes(b"1")
        found = {f.path.relative_to(tmp_path): f.size for f in list_files(tmp_path)}
        assert found == {Path("a.mkv"): 5, Path("sub/b.srt"): 1}

    def test_single_file_and_missing_source(self, tmp_path: Path):
        single = tmp_path / "only.mkv"
        single.write_bytes(b"xyz")
        assert list_files(single) == [MediaFile(path=single, size=3)]
        assert list_files(tmp_path / "nope") == []


class TestApplyPlan:
    def _plan(self, tmp_path: Path) -> RenamePlan:
        source = tmp_path / "Show.S01"
        source.mkdir()
        (source / "Show.S01E01.mkv").write_text("ep1")
        (source / "Show.S01E01.en.srt").write_text("sub")
        (source / "junk.txt").write_text("junk")
        return plan_rename(
            media_type=MediaType.SHOW,
            media_root=tmp_path,
            release_name=source.name,
            title="Show",
            year=2019,
            files=list_files(source),
        )

    def test_moves_files_and_deletes_the_emptied_source(self, tmp_path: Path):
        plan = self._plan(tmp_path)
        apply_plan(plan)
        season = tmp_path / "Show (2019)" / "Season 01"
        assert (season / "Show S01E01.mkv").read_text() == "ep1"
        assert (season / "Show S01E01.en.srt").read_text() == "sub"
        assert not plan.source.exists()

    def test_partial_destination_is_replaced_from_source(self, tmp_path: Path):
        plan = self._plan(tmp_path)
        season = tmp_path / "Show (2019)" / "Season 01"
        season.mkdir(parents=True)
        (season / "Show S01E01.mkv").write_text("partial-copy-of-different-size")
        apply_plan(plan)
        # A destination of a different size is an interrupted copy: redone.
        assert (season / "Show S01E01.mkv").read_text() == "ep1"
        assert not plan.source.exists()

    def test_missing_source_file_is_skipped(self, tmp_path: Path):
        plan = self._plan(tmp_path)
        (plan.source / "Show.S01E01.en.srt").unlink()
        apply_plan(plan)
        assert (tmp_path / "Show (2019)" / "Season 01" / "Show S01E01.mkv").exists()

    def test_unplanned_video_left_in_source_fails_loudly(self, tmp_path: Path):
        plan = self._plan(tmp_path)
        (plan.source / "unplanned.mkv").write_text("left")
        with pytest.raises(RenameIncompleteError) as exc:
            apply_plan(plan)
        assert "unplanned.mkv" in str(exc.value)
        assert (plan.source / "unplanned.mkv").exists()

    def test_rerun_is_a_noop(self, tmp_path: Path):
        plan = self._plan(tmp_path)
        apply_plan(plan)
        apply_plan(plan)
        assert (tmp_path / "Show (2019)" / "Season 01" / "Show S01E01.mkv").read_text() == "ep1"


class TestSourceRootName:
    @pytest.mark.parametrize(
        ("content_path", "expected"),
        [
            ("F:\\Media\\Movies\\Foo (2021) [5.1]", "Foo (2021) [5.1]"),
            ("/media/Shows/Show.S01/", "Show.S01"),
            ("F:\\Media\\Movies\\single.mkv", "single.mkv"),
        ],
    )
    def test_basename_from_either_separator(self, content_path: str, expected: str):
        from medialab_orchestrator.services.rename import source_root_name

        assert source_root_name(content_path) == expected


class TestInterruptedMove:
    """A move that copied the file but could not remove a locked source."""

    def _plan(self, tmp_path: Path) -> RenamePlan:
        source = tmp_path / "Movie.2021-GRP"
        source.mkdir()
        (source / "Movie.2021-GRP.mkv").write_text("full copy")
        return plan_rename(
            media_type=MediaType.MOVIE,
            media_root=tmp_path,
            release_name=source.name,
            title="Movie",
            year=2021,
            files=list_files(source),
        )

    def test_same_size_leftover_source_is_removed(self, tmp_path: Path):
        plan = self._plan(tmp_path)
        dest = tmp_path / "Movie (2021)" / "Movie (2021).mkv"
        dest.parent.mkdir(parents=True)
        dest.write_text("full copy")
        apply_plan(plan)
        assert dest.read_text() == "full copy"
        assert not plan.source.exists()

    def test_different_size_destination_is_replaced_from_source(self, tmp_path: Path):
        plan = self._plan(tmp_path)
        dest = tmp_path / "Movie (2021)" / "Movie (2021).mkv"
        dest.parent.mkdir(parents=True)
        dest.write_text("partial")
        apply_plan(plan)
        assert dest.read_text() == "full copy"
        assert not plan.source.exists()

    def test_video_left_behind_raises_rename_incomplete(self, tmp_path: Path, monkeypatch):
        plan = self._plan(tmp_path)

        def locked_move(src, dst):
            # Windows: the copy lands, then the locked source cannot be removed.
            shutil.copy2(src, dst)
            raise PermissionError(13, "Permission denied", src)

        monkeypatch.setattr(rename_module.shutil, "move", locked_move)
        with pytest.raises(RenameIncompleteError) as exc:
            apply_plan(plan)
        assert "Movie.2021-GRP.mkv" in str(exc.value)
        assert (tmp_path / "Movie (2021)" / "Movie (2021).mkv").exists()
        assert (plan.source / "Movie.2021-GRP.mkv").exists()


class TestUsableRootName:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("Movie.2021-GRP", "Movie.2021-GRP"),
            ("F:\Media\Movies", None),
            ("/media/Movies", None),
            ("", None),
            (None, None),
        ],
    )
    def test_only_a_bare_name_is_usable(self, value, expected):
        from medialab_orchestrator.services.rename import usable_root_name

        assert usable_root_name(value) == expected
