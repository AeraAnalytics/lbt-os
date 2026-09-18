from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import AuthContext, get_auth
from ..database import get_db
from ..models.sale import SaleCreate, SaleOut, SaleUpdate

router = APIRouter(prefix="/sales", tags=["sales"])


@router.get("", response_model=list[SaleOut])
def list_sales(
    auth: Annotated[AuthContext, Depends(get_auth)],
    payment_status: str | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
):
    db = get_db()
    query = (
        db.table("sales")
        .select("*")
        .eq("org_id", auth.org_id)
        .order("sold_at", desc=True)
        .range(offset, offset + limit - 1)
    )
    if payment_status:
        query = query.eq("payment_status", payment_status)
    return query.execute().data


@router.post("", response_model=SaleOut, status_code=201)
def create_sale(body: SaleCreate, auth: Annotated[AuthContext, Depends(get_auth)]):
    from datetime import datetime, timezone
    db = get_db()
    data = body.model_dump(exclude_none=True)
    data["org_id"] = auth.org_id
    if "sold_at" in data and hasattr(data["sold_at"], "isoformat"):
        data["sold_at"] = data["sold_at"].isoformat()
    result = db.table("sales").insert(data).execute()
    sale = result.data[0]

    # Sync lifetime_value, total_orders, last_purchase_at on linked customer
    customer_id = sale.get("customer_id")
    if customer_id:
        try:
            existing = (
                db.table("customers")
                .select("lifetime_value, total_orders")
                .eq("id", customer_id)
                .eq("org_id", auth.org_id)
                .maybe_single()
                .execute()
            )
            if existing.data:
                current_ltv = float(existing.data.get("lifetime_value") or 0)
                current_orders = int(existing.data.get("total_orders") or 0)
                db.table("customers").update({
                    "lifetime_value":   round(current_ltv + float(sale.get("amount") or 0), 2),
                    "total_orders":     current_orders + 1,
                    "last_purchase_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", customer_id).eq("org_id", auth.org_id).execute()
        except Exception:
            pass  # best-effort — don't fail the sale creation

    return sale


@router.get("/{sale_id}", response_model=SaleOut)
def get_sale(sale_id: str, auth: Annotated[AuthContext, Depends(get_auth)]):
    db = get_db()
    result = (
        db.table("sales")
        .select("*")
        .eq("id", sale_id)
        .eq("org_id", auth.org_id)
        .maybe_single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Sale not found.")
    return result.data


@router.patch("/{sale_id}", response_model=SaleOut)
def update_sale(
    sale_id: str,
    body: SaleUpdate,
    auth: Annotated[AuthContext, Depends(get_auth)],
):
    db = get_db()
    update_data = body.model_dump(exclude_none=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update.")
    for k, v in update_data.items():
        if hasattr(v, "isoformat"):
            update_data[k] = v.isoformat()
    result = (
        db.table("sales")
        .update(update_data)
        .eq("id", sale_id)
        .eq("org_id", auth.org_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Sale not found.")
    return result.data[0]


@router.delete("/{sale_id}", status_code=204)
def delete_sale(sale_id: str, auth: Annotated[AuthContext, Depends(get_auth)]):
    db = get_db()
    db.table("sales").delete().eq("id", sale_id).eq("org_id", auth.org_id).execute()
