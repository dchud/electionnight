import csv
import io
import json
import zipfile
from pathlib import Path

import pytest

from monitor.ref import gazetteer
from monitor.ref.gazetteer import (
    build_geo,
    check_topology,
    legacy_counties,
    normalize_counties,
    normalize_places,
    normalize_states,
    place_name,
    read_gazetteer,
    ref_size_bytes,
)
from monitor.settings import Paths, Settings

FIXTURES = Path(__file__).parent / "fixtures" / "ref" / "gazetteer"
TOPOLOGY = {
    "type": "Topology",
    "objects": {"counties": {}, "states": {}, "nation": {}},
}


def as_zip(name: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.write(FIXTURES / name, arcname=name)
    return buffer.getvalue()


def test_reads_pipe_delimited_file():
    rows = read_gazetteer(as_zip("counties.txt"))
    assert rows[0]["USPS"] == "AK"
    assert {row["GEOID"] for row in rows} == {"02013", "35013", "09110"}


def test_reads_padded_tab_delimited_file():
    rows = read_gazetteer(as_zip("legacy_counties.txt"))
    fairfield = next(row for row in rows if row["GEOID"] == "09001")
    assert fairfield["NAME"] == "Fairfield County"
    assert fairfield["INTPTLONG"].startswith("-73.")


def test_rejects_zip_without_single_text_file():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.txt", "x")
        archive.writestr("b.txt", "y")
    with pytest.raises(ValueError):
        read_gazetteer(buffer.getvalue())


def test_normalize_states():
    rows = normalize_states(read_gazetteer(as_zip("state.txt")))
    assert {"usps": "DC", "fips": "11", "name": "District of Columbia"} in rows


def test_normalize_counties_keeps_accents_and_columns():
    rows = normalize_counties(read_gazetteer(as_zip("counties.txt")))
    dona_ana = next(row for row in rows if row["fips"] == "35013")
    assert dona_ana["name"] == "Doña Ana County"
    assert dona_ana["state_usps"] == "NM"
    assert float(dona_ana["lat"]) > 30
    assert int(dona_ana["aland"]) > 0


def test_normalize_places_strips_legal_area_words():
    rows = normalize_places(read_gazetteer(as_zip("place.txt")))
    names = {row["geoid"]: row["name"] for row in rows}
    assert names["0100124"] == "Abbeville"
    assert names["0236400"] == "Juneau"
    assert names["1571550"] == "Honolulu"
    assert names["2148006"] == "Louisville"
    assert names["3209700"] == "Carson City"
    assert names["4752006"] == "Nashville"
    assert names["7277500"] == "San Sebastián"


@pytest.mark.parametrize(
    ("raw", "lsad", "expected"),
    [
        ("Atlanta city", "25", "Atlanta"),
        ("Town of city", "25", "Town of"),
        (
            "Augusta-Richmond County consolidated government (balance)",
            "00",
            "Augusta",
        ),
        ("Indianapolis city (balance)", "00", "Indianapolis"),
        ("Lexington-Fayette urban county", "UC", "Lexington"),
        ("Princeton", "00", "Princeton"),
    ],
)
def test_place_name(raw, lsad, expected):
    assert place_name(raw, lsad) == expected


def test_legacy_counties_only_adds_connecticut():
    rows = legacy_counties(read_gazetteer(as_zip("legacy_counties.txt")))
    assert [row["fips"] for row in rows] == ["09001"]


def test_check_topology_rejects_wrong_shape():
    good = json.dumps(TOPOLOGY).encode()
    check_topology(good, {"counties", "states"})
    with pytest.raises(ValueError):
        check_topology(good, {"districts"})
    with pytest.raises(ValueError):
        check_topology(b'{"type": "FeatureCollection"}', {"counties"})


def test_build_geo_writes_files_only(tmp_path, monkeypatch, capsys):
    settings = Settings(paths=Paths.under(tmp_path))
    topo = tmp_path / "topo"
    monkeypatch.setattr(gazetteer, "TOPO_DIR", topo)
    fixtures = {
        "state_national": "state.txt",
        "counties_national": "counties.txt",
        "place_national": "place.txt",
    }

    def fake_download(client, url):
        assert url.startswith("https://")
        if url == gazetteer.LEGACY_COUNTIES_URL:
            return as_zip("legacy_counties.txt")
        if url.startswith(gazetteer.US_ATLAS_BASE):
            assert "@3.0.1/" in url
            return json.dumps(TOPOLOGY).encode()
        name = next(v for k, v in fixtures.items() if k in url)
        return as_zip(name)

    monkeypatch.setattr(gazetteer, "_download", fake_download)
    build_geo(settings)
    ref = settings.paths.ref
    with (ref / "counties.csv").open(encoding="utf-8") as handle:
        counties = list(csv.DictReader(handle))
    assert {row["fips"] for row in counties} == {
        "02013",
        "35013",
        "09110",
        "09001",
    }
    with (ref / "places.csv").open(encoding="utf-8") as handle:
        assert len(list(csv.DictReader(handle))) == 7
    with (ref / "states.csv").open(encoding="utf-8") as handle:
        assert len(list(csv.DictReader(handle))) == 3
    assert (topo / "states-albers-10m.json").exists()
    assert (topo / "counties-albers-10m.json").exists()
    assert not settings.paths.catalog.exists()
    assert not settings.paths.data.exists()
    assert "states 3" in capsys.readouterr().out


def test_build_geo_stops_when_over_the_size_limit(tmp_path, monkeypatch):
    settings = Settings(paths=Paths.under(tmp_path))
    monkeypatch.setattr(gazetteer, "TOPO_DIR", tmp_path / "topo")
    monkeypatch.setattr(gazetteer, "MAX_REF_BYTES", 1)

    def fake_download(client, url):
        if url.startswith(gazetteer.US_ATLAS_BASE):
            return json.dumps(TOPOLOGY).encode()
        if url == gazetteer.LEGACY_COUNTIES_URL:
            return as_zip("legacy_counties.txt")
        for key, name in (
            ("state", "state.txt"),
            ("counties", "counties.txt"),
            ("place", "place.txt"),
        ):
            if key in url:
                return as_zip(name)
        raise AssertionError(url)

    monkeypatch.setattr(gazetteer, "_download", fake_download)
    with pytest.raises(RuntimeError, match="150 MB"):
        build_geo(settings)
    assert ref_size_bytes(settings.paths.ref) > 1
