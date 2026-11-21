"""Glue code: groups the vertices into cells, runs each horizon per cell (cached) and
keeps going if one horizon fails."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..cache import SEASONAL_CACHE, SHORT_TERM_CACHE
from ..config import get_settings
from ..http_client import UpstreamError
from ..schemas import CellForecast, CellLocation, SeasonalHorizon, ShortTermHorizon
from . import seasonal, short_term

log = logging.getLogger("toka.forecast")

Cell = Tuple[float, float]


def cell_of(lat: float, lon: float) -> Cell:
    d = get_settings().cache_decimals
    return round(lat, d), round(lon, d)


def group_by_cell(points: Sequence[Tuple[float, float]]) -> Dict[Cell, List[int]]:
    # cell -> vertex numbers (1-based) that fall inside it, in the order they came
    cells: Dict[Cell, List[int]] = {}
    for i, (lat, lon) in enumerate(points, start=1):
        cells.setdefault(cell_of(lat, lon), []).append(i)
    return cells


def _short_term(cell: Cell):
    return SHORT_TERM_CACHE.get_or_compute(cell, get_settings().ttl_short_term_s, lambda: short_term.calculate(*cell))


def _seasonal(cell: Cell):
    return SEASONAL_CACHE.get_or_compute(cell, get_settings().ttl_seasonal_s, lambda: seasonal.calculate(*cell))


_FACTORIES: Dict[str, Callable] = {"short_term": _short_term, "seasonal": _seasonal}
_EMPTY = {"short_term": ShortTermHorizon, "seasonal": SeasonalHorizon}


def compute_cell(cell: Cell, vertices: List[int], horizons: Sequence[str]) -> CellForecast:
    with ThreadPoolExecutor(max_workers=len(horizons)) as ex:
        futures = {h: ex.submit(_FACTORIES[h], cell) for h in horizons}

    fields: Dict[str, object] = {}
    elevation: Optional[float] = None
    tz: Optional[str] = None
    for h, fut in futures.items():
        try:
            result = fut.result()
            if h == "short_term":
                fields[h], elevation, tz = result
            else:
                fields[h] = result
        except UpstreamError as e:
            log.warning("horizon %s failed at %s: %s", h, cell, e)
            fields[h] = _EMPTY[h](available=False, error=str(e))
        except Exception as e:  # a bug in one horizon shouldn't kill the other one
            log.exception("internal error in horizon %s", h)
            fields[h] = _EMPTY[h](available=False, error=f"internal error: {e.__class__.__name__}")

    return CellForecast(vertices=vertices,
                        location=CellLocation(latitude=cell[0], longitude=cell[1], elevation_m=elevation, timezone=tz),
                        **fields)


def compute_polygon(points: List[Tuple[float, float]], horizons: Sequence[str]) -> List[CellForecast]:
    groups = group_by_cell(points)
    workers = max(1, min(get_settings().max_workers, len(groups)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda item: compute_cell(item[0], item[1], horizons), groups.items()))

    blocks = [getattr(r, h) for r in results for h in horizons]
    if all(b is not None and not b.available for b in blocks):
        errors = sorted({b.error for b in blocks if b and b.error})
        raise UpstreamError("providers", None, "; ".join(errors)[:600])
    return results
