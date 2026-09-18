from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import AuthContext, get_auth
from ..database import get_db
from ..models.expense import ExpenseCreate, ExpenseOut, ExpenseUpdate

router = APIRouter(prefix="/expenses", tags=["expenses"])


@router.get("", response_model=list[ExpenseOut])
def list_expenses(
    auth: Annotated[AuthContext, Depends(get_auth)],
    category: str | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
):
    db = get_db()
    query = (
        db.table("expenses")
        .select("*")
        .eq("org_id", auth.org_id)
        .order("expense_date", desc=True)
        .range(offset, offset + limit - 1)
    )
    if category:
        query = query.eq("category", category)
    return query.execute().data


@router.post("", response_model=ExpenseOut, status_code=201)
def create_expense(body: ExpenseCreate, auth: Annotated[AuthContext, Depends(get_auth)]):
    db = get_db()
    data = body.model_dump(exclude_none=True)
    data["org_id"] = auth.org_id
    if hasattr(data.get("expense_date"), "isoformat"):
        data["expense_date"] = data["expense_date"].isoformat()
    result = db.table("expenses").insert(data).execute()
    return result.data[0]


@router.get("/{expense_id}", response_model=ExpenseOut)
def get_expense(expense_id: str, auth: Annotated[AuthContext, Depends(get_auth)]):
    db = get_db()
    result = (
        db.table("expenses")
        .select("*")
        .eq("id", expense_id)
        .eq("org_id", auth.org_id)
        .maybe_single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Expense not found.")
    return result.data


@router.patch("/{expense_id}", response_model=ExpenseOut)
def update_expense(
    expense_id: str,
    body: ExpenseUpdate,
    auth: Annotated[AuthContext, Depends(get_auth)],
):
    db = get_db()
    update_data = body.model_dump(exclude_none=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update.")
    if hasattr(update_data.get("expense_date"), "isoformat"):
        update_data["expense_date"] = update_data["expense_date"].isoformat()
    result = (
        db.table("expenses")
        .update(update_data)
        .eq("id", expense_id)
        .eq("org_id", auth.org_id)
        .execute()
    )
    if not result.data:
        raise HTTPException(status_code=404, detail="Expense not found.")
    return result.data[0]


@router.delete("/{expense_id}", status_code=204)
def delete_expense(expense_id: str, auth: Annotated[AuthContext, Depends(get_auth)]):
    db = get_db()
    db.table("expenses").delete().eq("id", expense_id).eq("org_id", auth.org_id).execute()
