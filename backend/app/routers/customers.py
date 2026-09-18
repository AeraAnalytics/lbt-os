from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import AuthContext, get_auth
from ..database import get_db
from ..models.customer import CustomerCreate, CustomerOut, CustomerUpdate

router = APIRouter(prefix="/customers", tags=["customers"])


@router.get("", response_model=list[CustomerOut])
def list_customers(
    auth: Annotated[AuthContext, Depends(get_auth)],
    tag: str | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
):
    db = get_db()
    query = (
        db.table("customers")
        .select("*")
        .eq("org_id", auth.org_id)
        .order("last_purchase_at", desc=True, nullsfirst=False)
        .range(offset, offset + limit - 1)
    )
    if tag:
        query = query.contains("tags", [tag])
    return query.execute().data


@router.post("", response_model=CustomerOut, status_code=201)
def create_customer(body: CustomerCreate, auth: Annotated[AuthContext, Depends(get_auth)]):
    db = get_db()
    data = body.model_dump(exclude_none=True)
    data["org_id"] = auth.org_id
    result = db.table("customers").insert(data).execute()
    return result.data[0]


@router.get("/{customer_id}", response_model=CustomerOut)
def get_customer(customer_id: str, auth: Annotated[AuthContext, Depends(get_auth)]):
    db = get_db()
    result = (
        db.table("customers")
        .select("*")
        .eq("id", customer_id)
        .eq("org_id", auth.org_id)
        .maybe_single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Customer not found.")
    return result.data


@router.patch("/{customer_id}", response_model=CustomerOut)
def update_customer(
    customer_id: str,
    body: CustomerUpdate,
    auth: Annotated[AuthContext, Depends(get_auth)],
):
    db = get_db()
    update_data = body.model_dump(exclude_none=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update.")
    result = (
        db.table("customers")
        .update(update_data)
        .eq("id", customer_id)
        .eq("org_id", auth.org_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Customer not found.")
    return result.data[0]


@router.delete("/{customer_id}", status_code=204)
def delete_customer(customer_id: str, auth: Annotated[AuthContext, Depends(get_auth)]):
    db = get_db()
    db.table("customers").delete().eq("id", customer_id).eq("org_id", auth.org_id).execute()
