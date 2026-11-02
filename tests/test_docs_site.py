"""The documentation site's build steps."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent


def load_hooks() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "mkdocs_hooks", ROOT / "scripts" / "mkdocs_hooks.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dashboard_links_become_plain_text():
    hooks = load_hooks()
    text = (
        "Live views: [trace one article](/under/trace), "
        "[the map](/under/lake#map), and [the code](https://example.com/x)."
    )
    assert hooks.on_page_markdown(text) == (
        "Live views: trace one article, the map, "
        "and [the code](https://example.com/x)."
    )


def test_the_build_copies_the_math_files_the_config_names(tmp_path):
    hooks = load_hooks()
    hooks.on_post_build({"site_dir": str(tmp_path)})
    config = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    named = [
        line.strip().removeprefix("- ")
        for line in config.splitlines()
        if line.strip().startswith("- assets/katex/")
    ]
    assert named
    for path in named:
        assert (tmp_path / path).is_file()
