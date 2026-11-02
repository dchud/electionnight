"""The project's credits, license scope, and third-party notices."""

from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
NOTICES = (ROOT / "docs/third_party_notices.md").read_text(encoding="utf-8")
COLOPHON = (ROOT / "docs/colophon.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
VENDOR_SCRIPT = (ROOT / "scripts/vendor_assets.sh").read_text(encoding="utf-8")
GAZETTEER = (ROOT / "src/monitor/ref/gazetteer.py").read_text(encoding="utf-8")
PACKAGE_NAMES = {
    "htmx.org": ["htmx"],
    "htmx-ext-sse": ["htmx SSE extension"],
    "vega": ["Vega"],
    "vega-lite": ["Vega-Lite"],
    "vega-embed": ["Vega-Embed"],
    "d3": ["D3"],
    "topojson-client": ["TopoJSON Client"],
    "@fontsource-variable/inter": ["Inter (Fontsource)"],
    "katex": ["KaTeX", "KaTeX fonts"],
}
LICENSE_WORDING = {
    "ISC": "Permission to use, copy, modify",
    "BSD 3-Clause": "Redistribution and use",
    "MIT": "Permission is hereby granted",
    "0BSD": "for any purpose with or without fee",
}


def pinned_packages():
    found = set(
        re.findall(r"(@?[a-z][\w./-]*)@(\d+\.\d+\.\d+)", VENDOR_SCRIPT)
    )
    rows = [(row, ver) for pkg, ver in found for row in PACKAGE_NAMES[pkg]]
    tailwind = re.search(r"TAILWIND_VERSION=v([\d.]+)", VENDOR_SCRIPT)[1]
    atlas = re.search(r'US_ATLAS_VERSION = "([\d.]+)"', GAZETTEER)[1]
    return sorted(
        [*rows, ("Tailwind CSS", tailwind), ("US Atlas TopoJSON", atlas)]
    )


def table_rows():
    rows = re.findall(r"^\| (.+?) \| (.+?) \| .+? \| (.+?) \|$", NOTICES, re.M)
    return [row for row in rows if row[0] not in ("Package", "---")]


def license_text(title):
    match = re.search(
        rf"^### {re.escape(title)}\n\n```text\n(.*?)^```", NOTICES, re.M | re.S
    )
    assert match, title
    return match[1]


def test_the_pinned_list_is_read():
    assert len(pinned_packages()) == 12


@pytest.mark.parametrize("row,version", pinned_packages())
def test_every_vendored_package_has_a_notice_at_its_version(row, version):
    found = next(
        line for line in NOTICES.splitlines() if line.startswith(f"| {row} |")
    )
    assert version in found


def test_every_package_in_the_table_has_its_license_text():
    rows = table_rows()
    assert len(rows) == 15
    for name, _version, license_name in rows:
        if license_name == "SIL OFL 1.1":
            continue
        title = "KaTeX" if name == "KaTeX" else name
        assert LICENSE_WORDING[license_name] in " ".join(
            license_text(title).split()
        ), name


def test_the_font_notices_and_the_font_license_are_present():
    assert "SIL OPEN FONT LICENSE" in NOTICES
    assert "Copyright 2016 The Inter Project Authors" in NOTICES
    assert "Reserved Font Names KaTeX_AMS" in NOTICES


def test_the_readme_says_what_the_mit_license_covers():
    section = README[README.index("## License") :]
    assert "covers this project's own code and" in section
    assert "docs/third_party_notices.md" in section


def test_the_site_lists_the_colophon_and_the_notices():
    site = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert "colophon.md" in site
    assert "third_party_notices.md" in site


def test_lean_ratings_hold_no_ad_fontes_values():
    text = (ROOT / "config/lean_ratings.csv").read_text(encoding="utf-8")
    assert list(csv.DictReader(text.splitlines()))
    assert not re.search(r"ad ?fontes", text, re.I)


def test_the_colophon_names_every_race_rater():
    races = yaml.safe_load((ROOT / "config/races.yaml").read_text("utf-8"))
    used = {
        key for race in races["races"] for key in race.get("ratings") or {}
    }
    names = {
        "Cook": "Cook Political Report",
        "IE": "Inside Elections",
        "Sabato": "Sabato's Crystal Ball",
        "DDHQ": "Decision Desk HQ",
        "Fox": "Fox News",
        "RCP": "RealClearPolitics",
        "Silver": "Silver Bulletin",
        "Econ": "The Economist",
        "FPO": "FiftyPlusOne",
        "ST": "Split Ticket",
        "NBC": "NBC News",
        "MS NOW": "MS NOW",
    }
    assert used <= set(names)
    notices = " ".join(NOTICES.split())
    for key in used:
        assert names[key] in COLOPHON
        assert names[key] in notices


def test_wikipedia_fixtures_are_listed_with_their_license():
    fixtures = ROOT / "tests/fixtures/ref"
    notice = (fixtures / "README.md").read_text(encoding="utf-8")
    assert "by-sa/4.0" in notice
    for path in fixtures.glob("*.html"):
        assert f"`{path.name}`" in notice
