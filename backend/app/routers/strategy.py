"""
Strategy router — AI strategist + competitive intelligence endpoints.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from ..auth import AuthContext, get_auth, require_plan
from ..database import get_db
from ..limiter import enforce_user_limit, limiter
from ..services.strategy import (
    get_proactive_briefing,
    run_competitive_analysis,
    run_strategy_session,
    search_competitors,
)

router = APIRouter(prefix="/strategy", tags=["strategy"])


class StrategyAskBody(BaseModel):
    question: str
    conversation_history: list[dict] = []


class CompetitorSearchBody(BaseModel):
    extra_query: str = ""
    max_results: int = 8


class CompetitorAnalysisBody(BaseModel):
    competitor_urls: list[str]


def _sanitize_history(history: list[dict]) -> list[dict]:
    """
    fix #2: strip any injected system/tool roles and cap content length.
    Only 'user' and 'assistant' turns are passed to the LLM.
    """
    return [
        {"role": h["role"], "content": str(h.get("content", ""))[:2000]}
        for h in history
        if isinstance(h, dict) and h.get("role") in ("user", "assistant")
    ]


# ---------- /strategy/briefing ----------

@router.get("/briefing")
@limiter.limit("30/hour")                       # fix #5: rate-limit briefing (no LLM but DB-heavy)
def strategy_briefing(
    request: Request,                           # required by slowapi
    auth: Annotated[AuthContext, Depends(get_auth)],
):
    """
    Proactive briefing — what the owner needs to know right now before
    starting a strategy session. Surfaces urgent signals without being asked.
    """
    db = get_db()
    org_result = (
        db.table("organizations")
        .select("name, industry, city, state")
        .eq("id", auth.org_id)
        .single()
        .execute()
    )
    org = org_result.data or {}
    org_name = org.get("name") or "Your Business"
    industry = org.get("industry") or "general"

    return get_proactive_briefing(db, auth.org_id, org_name, industry, plan=auth.plan)


# ---------- /strategy/ask ----------

@router.post("/ask")
@limiter.limit("20/hour")                       # fix #5: rate-limit LLM calls
def strategy_ask(
    request: Request,                           # required by slowapi
    body: StrategyAskBody,
    auth: Annotated[AuthContext, Depends(require_plan("pro"))],  # fix #3: Pro+ only
    _user_limit: Annotated[None, Depends(enforce_user_limit("20/hour"))],  # TW-078: per-verified-user
):
    """
    AI strategist — answer a strategic question using the org's live business data.
    Supports conversation history for multi-turn sessions.
    """
    db = get_db()
    org_result = (
        db.table("organizations")
        .select("name, industry, city, state")
        .eq("id", auth.org_id)
        .single()
        .execute()
    )
    org = org_result.data or {}
    org_name = org.get("name") or "Your Business"
    industry = org.get("industry") or "general"

    return run_strategy_session(
        db=db,
        org_id=auth.org_id,
        org_name=org_name,
        industry=industry,
        question=body.question,
        plan=auth.plan,
        conversation_history=_sanitize_history(body.conversation_history),  # fix #2
    )


# ---------- /strategy/search-competitors ----------

@router.post("/search-competitors")
@limiter.limit("10/hour")                       # fix #5: each call hits DuckDuckGo
async def strategy_search_competitors(
    request: Request,                           # required by slowapi
    body: CompetitorSearchBody,
    auth: Annotated[AuthContext, Depends(require_plan("pro"))],  # fix #3: Pro+ only
    _user_limit: Annotated[None, Depends(enforce_user_limit("10/hour"))],  # TW-078: per-verified-user
):
    """
    Search the web for competitors in the org's industry and location.
    Returns raw search results — call /analyze-competitors to run AI analysis.
    """
    db = get_db()
    org_result = (
        db.table("organizations")
        .select("name, industry, city, state")
        .eq("id", auth.org_id)
        .single()
        .execute()
    )
    org = org_result.data or {}
    org_name = org.get("name") or "Your Business"
    industry = org.get("industry") or "general"
    location = ", ".join(filter(None, [org.get("city"), org.get("state")])) or ""

    results = await search_competitors(
        industry=industry,
        location=location,
        org_name=org_name,
        extra_query=body.extra_query,
        max_results=body.max_results,
    )

    return {
        "results": results,
        "query_context": {
            "industry": industry,
            "location": location,
            "org_name": org_name,
        },
    }


# ---------- /strategy/analyze-competitors ----------

@router.post("/analyze-competitors")
@limiter.limit("10/hour")                       # fix #5: fetches pages + LLM call
async def strategy_analyze_competitors(
    request: Request,                           # required by slowapi
    body: CompetitorAnalysisBody,
    auth: Annotated[AuthContext, Depends(require_plan("pro"))],  # fix #3: Pro+ only
    _user_limit: Annotated[None, Depends(enforce_user_limit("10/hour"))],  # TW-078: per-verified-user
):
    """
    Fetch competitor pages and run AI comparative analysis.
    Returns pricing position, service gaps, and ranked strategic moves.
    SSRF-protected: URLs are validated against private/internal network ranges.
    """
    if not body.competitor_urls:
        return {"error": "No competitor URLs provided."}

    db = get_db()
    org_result = (
        db.table("organizations")
        .select("name, industry, city, state")
        .eq("id", auth.org_id)
        .single()
        .execute()
    )
    org = org_result.data or {}
    org_name = org.get("name") or "Your Business"
    industry = org.get("industry") or "general"
    location = ", ".join(filter(None, [org.get("city"), org.get("state")])) or ""

    return await run_competitive_analysis(
        db=db,
        org_id=auth.org_id,
        org_name=org_name,
        industry=industry,
        location=location,
        competitor_urls=body.competitor_urls,
        plan=auth.plan,
    )
