from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException
from supabase import Client

MAX_CSV_FILE_BYTES = 2 * 1024 * 1024
MAX_CSV_ROWS = 5_000
FORMULA_PREFIXES = ("=", "+", "-", "@")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_decimal(value: str | None, field: str, required: bool = False) -> float | None:
    if value is None or value == "":
        if required:
            raise HTTPException(status_code=400, detail=f"Missing required numeric field '{field}'.")
        return None
    try:
        return float(Decimal(str(value).strip()))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid numeric value for '{field}': {value}")


async def _read_upload_bytes(upload, max_bytes: int = MAX_CSV_FILE_BYTES) -> bytes:
    file_bytes = await upload.read(max_bytes + 1)
    if len(file_bytes) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"CSV file is too large. Maximum size is {max_bytes // (1024 * 1024)} MB.",
        )
    return file_bytes


def _neutralize_formula(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    stripped = value.lstrip()
    if stripped.startswith(FORMULA_PREFIXES) and field not in {"phone"}:
        return f"'{value}"
    return value


def _csv_rows(file_bytes: bytes) -> list[dict[str, str]]:
    if b"\x00" in file_bytes:
        raise HTTPException(status_code=400, detail="CSV file must be plain text.")
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="CSV file must be UTF-8 encoded text.") from exc
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV file is missing headers.")
    rows: list[dict[str, str]] = []
    for idx, row in enumerate(reader, start=1):
        if idx > MAX_CSV_ROWS:
            raise HTTPException(status_code=400, detail=f"CSV file exceeds the {MAX_CSV_ROWS} row limit.")
        rows.append({
            (k or "").strip(): _neutralize_formula((v or "").strip(), (k or "").strip())
            for k, v in row.items()
        })
    return rows


def _log_import(db: Client, org_id: str, entity_type: str, filename: str | None, rows_imported: int, row_count: int, error: str | None = None) -> None:
    """Write to csv_import_logs. Silently skips if the table doesn't exist yet."""
    try:
        db.table("csv_import_logs").insert({
            "org_id": org_id,
            "entity_type": entity_type,
            "filename": filename,
            "rows_imported": rows_imported,
            "row_count": row_count,
            "status": "failed" if error else "success",
            "error": error,
        }).execute()
    except Exception:
        pass


def list_import_history(db: Client, org_id: str, limit: int = 30) -> list[dict[str, Any]]:
    """Return recent CSV import logs. Returns empty list if table doesn't exist yet."""
    try:
        result = (
            db.table("csv_import_logs")
            .select("*")
            .eq("org_id", org_id)
            .order("imported_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []
    except Exception:
        return []


async def import_csv_rows(db: Client, org_id: str, entity_type: str, upload_file, filename: str | None = None) -> dict[str, Any]:
    file_bytes = await _read_upload_bytes(upload_file)
    rows = _csv_rows(file_bytes)
    if not rows:
        raise HTTPException(status_code=400, detail="CSV file contains no data rows.")

    entity = entity_type.lower()
    created_at = _now_iso()
    imported = 0

    if entity == "leads":
        payload = []
        for row in rows:
            name = row.get("name")
            if not name:
                raise HTTPException(status_code=400, detail="Lead import requires a 'name' column.")
            payload.append({
                "org_id": org_id,
                "name": name,
                "email": row.get("email") or None,
                "phone": row.get("phone") or None,
                "source": row.get("source") or "manual_csv",
                "status": row.get("status") or "new",
                "service_interest": row.get("service_interest") or row.get("service") or None,
                "estimated_value": _to_decimal(row.get("estimated_value"), "estimated_value"),
                "notes": row.get("notes") or "Imported from CSV.",
                "created_at": created_at,
                "updated_at": created_at,
            })
        db.table("leads").insert(payload).execute()
        imported = len(payload)

    elif entity == "customers":
        payload = []
        for row in rows:
            name = row.get("name")
            if not name:
                raise HTTPException(status_code=400, detail="Customer import requires a 'name' column.")
            payload.append({
                "org_id": org_id,
                "name": name,
                "email": row.get("email") or None,
                "phone": row.get("phone") or None,
                "address": row.get("address") or None,
                "notes": row.get("notes") or "Imported from CSV.",
                "created_at": created_at,
                "updated_at": created_at,
            })
        db.table("customers").insert(payload).execute()
        imported = len(payload)

    elif entity == "sales":
        payload = []
        for row in rows:
            service = row.get("service")
            amount = _to_decimal(row.get("amount"), "amount", required=True)
            if not service:
                raise HTTPException(status_code=400, detail="Sales import requires a 'service' column.")
            payload.append({
                "org_id": org_id,
                "service": service,
                "amount": amount,
                "cost": _to_decimal(row.get("cost"), "cost") or 0,
                "payment_method": row.get("payment_method") or None,
                "payment_status": row.get("payment_status") or "paid",
                "source": row.get("source") or "manual_csv",
                "invoice_number": row.get("invoice_number") or None,
                "notes": row.get("notes") or "Imported from CSV.",
                "sold_at": row.get("sold_at") or created_at,
                "created_at": created_at,
                "updated_at": created_at,
            })
        db.table("sales").insert(payload).execute()
        imported = len(payload)

    elif entity == "expenses":
        payload = []
        for row in rows:
            category = row.get("category")
            amount = _to_decimal(row.get("amount"), "amount", required=True)
            description = row.get("description")
            if not category or not description:
                raise HTTPException(status_code=400, detail="Expense import requires 'category' and 'description' columns.")
            payload.append({
                "org_id": org_id,
                "category": category,
                "description": description,
                "amount": amount,
                "vendor": row.get("vendor") or None,
                "is_recurring": (row.get("is_recurring") or "").lower() in {"true", "1", "yes", "y"},
                "recurrence_period": row.get("recurrence_period") or None,
                "expense_date": row.get("expense_date") or created_at[:10],
                "created_at": created_at,
                "updated_at": created_at,
            })
        db.table("expenses").insert(payload).execute()
        imported = len(payload)
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported import type '{entity_type}'.")

    _log_import(db, org_id, entity, filename, rows_imported=imported, row_count=len(rows))
    return {"entity_type": entity, "imported": imported, "row_count": len(rows)}
