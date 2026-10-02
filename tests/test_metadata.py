"""Title/year extraction from torrent-downloader TMDB-detail bodies."""

from medialab_contracts import MediaType

from medialab_orchestrator.services.metadata import extract_title_year

_MOVIE = {"status": "success", "data": {"title": "Dune", "release_date": "2021-10-22"}}
_SHOW = {"status": "success", "data": {"name": "Show", "first_air_date": "2019-03-01"}}


def test_movie_title_year_from_data():
    assert extract_title_year(MediaType.MOVIE, _MOVIE) == ("Dune", 2021)


def test_show_title_year_from_data():
    assert extract_title_year(MediaType.SHOW, _SHOW) == ("Show", 2019)


def test_missing_data_degrades_to_empty():
    assert extract_title_year(MediaType.MOVIE, {"status": "error", "data": None}) == ("", 0)


def test_non_dict_body_degrades():
    assert extract_title_year(MediaType.MOVIE, None) == ("", 0)


def test_missing_date_yields_zero_year():
    body = {"data": {"title": "No Date"}}
    assert extract_title_year(MediaType.MOVIE, body) == ("No Date", 0)


class TestOrphanTitle:
    def test_parses_title_and_year_from_the_release_name(self):
        from medialab_orchestrator.services.metadata import parse_release_title_year

        assert parse_release_title_year("The.Mummy.2026.1080p.WEB-DL.x264-GRP") == (
            "The Mummy",
            2026,
        )
        assert parse_release_title_year("Mystery.Show.S02E05.720p") == ("Mystery Show", 0)

    def test_unparseable_name_falls_back_to_the_name_itself(self):
        from medialab_orchestrator.services.metadata import parse_release_title_year

        title, year = parse_release_title_year("x")
        assert title == "x" and year == 0
