"""Census Gazetteer files and `us-atlas` map files.

`build_geo` writes `states.csv`, `counties.csv`, and `places.csv` under
`settings.paths.ref` and two pre-projected TopoJSON files under
`src/monitor/web/static/topo/`. It writes files only, never the lake.

Connecticut's Gazetteer entries are planning regions, while `us-atlas` and
most news text use its eight historical counties. The historical counties are
appended from the last Gazetteer that listed them, so county mentions resolve
and join to the map. `02261` (Valdez-Cordova) is on the map only: the
Gazetteer splits it into `02063` and `02066`.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import zipfile
from collections.abc import Iterable
from pathlib import Path

import httpx

from monitor.logs import get
from monitor.settings import Settings

log = get(__name__)

GAZETTEER_YEAR = 2026
GAZETTEER_BASE = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    f"{GAZETTEER_YEAR}_Gazetteer/{GAZETTEER_YEAR}_Gaz_"
)
GAZETTEER_FILES = {
    "state": f"{GAZETTEER_BASE}state_national.zip",
    "counties": f"{GAZETTEER_BASE}counties_national.zip",
    "place": f"{GAZETTEER_BASE}place_national.zip",
}
LEGACY_COUNTIES_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "2021_Gazetteer/2021_Gaz_counties_national.zip"
)
LEGACY_COUNTY_STATES = {"CT"}

US_ATLAS_VERSION = "3.0.1"
US_ATLAS_BASE = f"https://cdn.jsdelivr.net/npm/us-atlas@{US_ATLAS_VERSION}/"
TOPO_FILES = {
    "states-albers-10m.json": {"states", "nation"},
    "counties-albers-10m.json": {"counties", "states", "nation"},
}
TOPO_DIR = Path(__file__).resolve().parent.parent / "web" / "static" / "topo"

STATE_COLUMNS = ["usps", "fips", "name"]
COUNTY_COLUMNS = ["fips", "name", "state_usps", "lat", "lon", "aland"]
PLACE_COLUMNS = ["geoid", "name", "state_usps", "lat", "lon", "aland"]

MAX_REF_BYTES = 150 * 1024 * 1024

# Legal-area words the Gazetteer appends to place names, keyed by LSAD code.
PLACE_SUFFIXES = {
    "21": " borough",
    "25": " city",
    "37": " municipality",
    "43": " town",
    "47": " village",
    "53": " city and borough",
    "55": " comunidad",
    "57": " CDP",
    "62": " zona urbana",
    "CN": " corporation",
}
CONSOLIDATED = re.compile(
    r"\s+(?:(?:unified|consolidated|metro|metropolitan) government"
    r"|urban county|\(balance\))",
)
COMMON_NAMES = {
    "Athens-Clarke County": "Athens",
    "Augusta-Richmond County": "Augusta",
    "Butte-Silver Bow": "Butte",
    "Indianapolis city": "Indianapolis",
    "Lexington-Fayette": "Lexington",
    "Louisville/Jefferson County": "Louisville",
    "Macon-Bibb County": "Macon",
    "Milford city": "Milford",
    "Nashville-Davidson": "Nashville",
    "Urban Honolulu": "Honolulu",
}


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def read_gazetteer(data: bytes) -> list[dict[str, str]]:
    """Parse a Gazetteer zip. Files use `|` or tabs, with padded fields."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = [n for n in archive.namelist() if n.endswith(".txt")]
        if len(names) != 1:
            raise ValueError(f"expected one text file, found {names}")
        text = _decode(archive.read(names[0]))
    lines = text.splitlines()
    delimiter = "|" if "|" in lines[0] else "\t"
    rows = []
    for values in csv.reader(
        lines, delimiter=delimiter, quoting=csv.QUOTE_NONE
    ):
        rows.append([value.strip() for value in values])
    header = rows[0]
    return [dict(zip(header, row, strict=False)) for row in rows[1:] if row]


def place_name(raw: str, lsad: str) -> str:
    """Drop the legal-area words so a name matches how news text writes it."""
    suffix = PLACE_SUFFIXES.get(lsad)
    name = raw
    if suffix and name.endswith(suffix):
        name = name[: -len(suffix)]
    name = CONSOLIDATED.split(name)[0]
    return COMMON_NAMES.get(name, name)


def normalize_states(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {"usps": row["USPS"], "fips": row["GEOID"], "name": row["NAME"]}
        for row in rows
    ]


def normalize_counties(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "fips": row["GEOID"],
            "name": row["NAME"],
            "state_usps": row["USPS"],
            "lat": row["INTPTLAT"],
            "lon": row["INTPTLONG"],
            "aland": row["ALAND"],
        }
        for row in rows
    ]


def normalize_places(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "geoid": row["GEOID"],
            "name": place_name(row["NAME"], row.get("LSAD", "")),
            "state_usps": row["USPS"],
            "lat": row["INTPTLAT"],
            "lon": row["INTPTLONG"],
            "aland": row["ALAND"],
        }
        for row in rows
    ]


def legacy_counties(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    return normalize_counties(
        row for row in rows if row["USPS"] in LEGACY_COUNTY_STATES
    )


def write_csv(
    path: Path, columns: list[str], rows: Iterable[dict[str, str]]
) -> int:
    temporary = path.with_suffix(path.suffix + ".tmp")
    count = 0
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    os.replace(temporary, path)
    return count


def check_topology(data: bytes, objects: set[str]) -> None:
    payload = json.loads(data)
    missing = objects - set(payload.get("objects", {}))
    if payload.get("type") != "Topology" or missing:
        raise ValueError(f"unexpected TopoJSON, missing {sorted(missing)}")


def ref_size_bytes(ref_dir: Path) -> int:
    return sum(
        path.stat().st_size for path in ref_dir.rglob("*") if path.is_file()
    )


def _download(client: httpx.Client, url: str) -> bytes:
    response = client.get(url)
    response.raise_for_status()
    return response.content


def build_geo(settings: Settings) -> None:
    ref = settings.paths.ref
    raw = ref / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    TOPO_DIR.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        tables: dict[str, list[dict[str, str]]] = {}
        for kind, url in GAZETTEER_FILES.items():
            data = _download(client, url)
            (raw / url.rsplit("/", 1)[1]).write_bytes(data)
            tables[kind] = read_gazetteer(data)
        legacy_data = _download(client, LEGACY_COUNTIES_URL)
        (raw / LEGACY_COUNTIES_URL.rsplit("/", 1)[1]).write_bytes(legacy_data)
        for name, objects in TOPO_FILES.items():
            data = _download(client, US_ATLAS_BASE + name)
            check_topology(data, objects)
            target = TOPO_DIR / name
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(data)
            os.replace(temporary, target)
    states = normalize_states(tables["state"])
    counties = normalize_counties(tables["counties"])
    counties += legacy_counties(read_gazetteer(legacy_data))
    places = normalize_places(tables["place"])
    n_states = write_csv(ref / "states.csv", STATE_COLUMNS, states)
    n_counties = write_csv(ref / "counties.csv", COUNTY_COLUMNS, counties)
    n_places = write_csv(ref / "places.csv", PLACE_COLUMNS, places)
    size = ref_size_bytes(ref)
    if size > MAX_REF_BYTES:
        raise RuntimeError(f"{ref} is {size} bytes, over the 150 MB limit")
    print(
        f"states {n_states}, counties {n_counties}, places {n_places}; "
        f"{ref} {size / 1_048_576:.1f} MB; "
        f"us-atlas {US_ATLAS_VERSION} written to {TOPO_DIR}"
    )
