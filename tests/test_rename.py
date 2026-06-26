"""Rename-service tests: season parsing, destination planning, idempotent move."""

from pathlib import Path

import pytest
from medialab_contracts import MediaType

from medialab_orchestrator.core.errors import AppException, ErrorCode
from medialab_orchestrator.services.rename import (
    SeasonUnparseableError,
    apply_rename,
    build_movie_dest,
    build_tv_dest,
    parse_season,
    plan_rename,
)

ROOT = Path("/media/Shows")


class TestParseSeason:
    @pytest.mark.parametrize(
        ("release", "expected"),
        [
            ("Show.Name.S01.1080p.GROUP", 1),
            ("Show.Name.S03.720p.HEVC", 3),
            ("The Show Name Season 2 1080p", 2),
            ("Some.Show.S10.COMPLETE", 10),
        ],
    )
    def test_parses_single_season(self, release: str, expected: int):
        assert parse_season(release) == expected

    def test_no_season_raises(self):
        with pytest.raises(SeasonUnparseableError):
            parse_season("Movie.Name.2020.1080p.BluRay")

    def test_multi_season_pack_raises(self):
        with pytest.raises(SeasonUnparseableError):
            parse_season("Show.Name.S01-S03.1080p.GROUP")


class TestDest:
    def test_tv_dest_zero_padded(self):
        dest = build_tv_dest(media_root=ROOT, title="The Show", year=2020, season=1)
        assert dest == ROOT / "The Show (2020)" / "Season 01"

    def test_tv_dest_two_digit_season(self):
        dest = build_tv_dest(media_root=ROOT, title="The Show", year=2020, season=12)
        assert dest == ROOT / "The Show (2020)" / "Season 12"

    def test_movie_dest_unchanged(self):
        dest = build_movie_dest(media_root=Path("/media/Movies"), release_name="Foo.2021")
        assert dest == Path("/media/Movies") / "Foo.2021"


class TestPlanRename:
    def test_tv_plan(self):
        source, dest = plan_rename(
            media_type=MediaType.SHOW,
            media_root=ROOT,
            release_name="Show.Name.S02.1080p.GROUP",
            title="Show Name",
            year=2019,
        )
        assert source == ROOT / "Show.Name.S02.1080p.GROUP"
        assert dest == ROOT / "Show Name (2019)" / "Season 02"

    def test_movie_plan_no_move(self):
        root = Path("/media/Movies")
        source, dest = plan_rename(
            media_type=MediaType.MOVIE,
            media_root=root,
            release_name="Foo.2021.1080p",
            title="Foo",
            year=2021,
        )
        assert source == dest == root / "Foo.2021.1080p"

    def test_unparseable_tv_raises_app_exception(self):
        with pytest.raises(AppException) as exc:
            plan_rename(
                media_type=MediaType.SHOW,
                media_root=ROOT,
                release_name="Show.Name.NoSeason.1080p",
                title="Show Name",
                year=2019,
            )
        assert exc.value.code is ErrorCode.SEASON_UNPARSEABLE


class TestApplyRename:
    def test_moves_into_dest(self, tmp_path: Path):
        source = tmp_path / "Show.S01"
        source.mkdir()
        (source / "ep.mkv").write_text("x")
        dest = tmp_path / "Show (2020)" / "Season 01"
        apply_rename(source, dest)
        assert (dest / "ep.mkv").read_text() == "x"
        assert not source.exists()

    def test_existing_dest_is_noop(self, tmp_path: Path):
        source = tmp_path / "Show.S01"
        source.mkdir()
        dest = tmp_path / "Show (2020)" / "Season 01"
        dest.mkdir(parents=True)
        apply_rename(source, dest)
        # Source untouched: the move was skipped because dest already exists.
        assert source.exists()

    def test_same_source_dest_is_noop(self, tmp_path: Path):
        path = tmp_path / "Foo.2021"
        path.mkdir()
        apply_rename(path, path)
        assert path.exists()
