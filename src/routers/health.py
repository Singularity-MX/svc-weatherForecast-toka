from fastapi import APIRouter

from ..cache import SEASONAL_CACHE, SHORT_TERM_CACHE
from ..config import get_settings
from ..schemas import HealthResponse, RateLimitInfo

router = APIRouter(tags=["Health"])


@router.get("/health", response_model=HealthResponse, summary="Service status", operation_id="health")
def health() -> HealthResponse:
    """Quick check that the service is up. Doesn't call any provider and isn't rate limited."""
    s = get_settings()
    return HealthResponse(
        service=s.service_name, version=s.service_version, api_version=s.api_version, port=s.port, ensemble_models=list(s.ensemble_models),
        rate_limit=RateLimitInfo(enabled=s.rate_limit_enabled, units=s.rate_limit_units, window_s=s.rate_limit_window_s),
        cache={"short_term": len(SHORT_TERM_CACHE), "seasonal": len(SEASONAL_CACHE)})
