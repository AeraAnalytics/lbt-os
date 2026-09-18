from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ..auth import AuthContext, get_auth
from ..database import get_db
from ..services.metrics import (
    get_dashboard_metrics,
    get_revenue_forecast,
    get_revenue_trend,
    get_segment_analysis,
)

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/dashboard")
def dashboard_metrics(
    auth: Annotated[AuthContext, Depends(get_auth)],
    days: int = Query(30, ge=7, le=365),
):
    """All dashboard KPIs for the org. Single endpoint — frontend fetches once."""
    db = get_db()
    return get_dashboard_metrics(db, auth.org_id, days=days)


@router.get("/revenue-trend")
def revenue_trend(
    auth: Annotated[AuthContext, Depends(get_auth)],
    weeks: int = Query(12, ge=4, le=52),
):
    """Daily revenue data points for the last N weeks (for chart rendering)."""
    db = get_db()
    return get_revenue_trend(db, auth.org_id, weeks=weeks)


@router.get("/segments")
def segment_metrics(
    auth: Annotated[AuthContext, Depends(get_auth)],
    days: int = Query(30, ge=7, le=365),
):
    """
    Cross-tabulate lead source against conversion rate, revenue, volume, and
    average deal size. Surfaces which acquisition channels actually pay off.
    """
    db = get_db()
    return get_segment_analysis(db, auth.org_id, days=days)


@router.get("/forecast")
def revenue_forecast(
    auth: Annotated[AuthContext, Depends(get_auth)],
    lookback_weeks: int = Query(16, ge=4, le=52),
):
    """
    30/60/90-day revenue forecast using linear trend extrapolation with
    confidence intervals. Requires at least 3 weeks of historical data.
    """
    db = get_db()
    return get_revenue_forecast(db, auth.org_id, lookback_weeks=lookback_weeks)
