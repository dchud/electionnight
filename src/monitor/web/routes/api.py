"""Chart specs and data, as JSON."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from monitor.web.charts import specs

router = APIRouter(prefix="/api")


@router.get("/spec/{chart}")
def spec(chart: str, variant: str = "") -> JSONResponse:
    builders = specs.SPECS
    key = f"{chart}:{variant}" if variant else chart
    if key not in builders:
        raise HTTPException(404, f"no chart named {key}")
    return JSONResponse(specs.cached_spec(key))


@router.get("/data/{chart}")
def data(request: Request, chart: str) -> JSONResponse:
    web = request.app.state.web
    filters = web.filters(request)
    panels = web.panels
    result: dict[str, Any]
    if chart == "stream":
        result = {
            "table": panels.stream_rows(filters),
            "rules": panels.poll_rules(filters),
        }
    elif chart == "topic_heat":
        result = {"table": panels.topic_heat(filters)}
    elif chart == "bump":
        result = {
            "table": panels.bump(filters),
            "rules": panels.poll_rules(filters),
        }
    elif chart == "tone_by_lean":
        result = {"table": panels.tone_by_lean(filters)}
    elif chart == "coverage_matrix":
        result = {"table": panels.coverage_matrix(filters)}
    elif chart == "global_press":
        result = {"table": panels.global_press(filters)}
    elif chart == "map":
        metric = request.query_params.get("metric", "attention")
        result = panels.map_data(filters, metric)
    else:
        raise HTTPException(404, f"no data named {chart}")
    return JSONResponse(_jsonable(result))


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return value
