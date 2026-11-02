"""Build steps for the documentation site.

The site has no dashboard, so links to dashboard pages become plain text. Math
is drawn with the dashboard's vendored KaTeX, so the site loads it from itself.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

KATEX_DIR = (
    Path(__file__).resolve().parent.parent
    / "src/monitor/web/static/vendor/katex"
)
DASHBOARD_LINK_RE = re.compile(r"\[([^\]]+)\]\(/under[^)]*\)")


def on_page_markdown(markdown: str, **_kwargs: Any) -> str:
    return DASHBOARD_LINK_RE.sub(r"\1", markdown)


def on_post_build(config: Any, **_kwargs: Any) -> None:
    target = Path(config["site_dir"]) / "assets" / "katex"
    shutil.copytree(KATEX_DIR, target, dirs_exist_ok=True)
