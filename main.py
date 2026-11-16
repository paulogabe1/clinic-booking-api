"""A small clinic booking service.

The web page and the API run together. The API checks a shared key, reads and
writes booking data in SQLite, and protects a slot from being booked twice.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, Response

#region 1. Database and service startup
ROOT = Path(__file__).resolve().parent
DATABASE_PATH = os.environ.get("DATABASE_PATH", str(ROOT / "booking.db"))
STATIC_DIR = ROOT / "static"

app = FastAPI(title="Clinic Booking API", version="1.0.0")


@contextmanager
def database():
    # Each request gets its own database connection, which is closed afterward.
    connection = sqlite3.connect(DATABASE_PATH, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
    finally:
        connection.close()


def initialize_database():
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    with database() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS slots (
                id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                start TEXT NOT NULL,
                end TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bookings (
                id TEXT PRIMARY KEY,
                slot_id TEXT NOT NULL REFERENCES slots(id),
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('confirmed', 'cancelled')),
                created_at TEXT NOT NULL
            );
            -- The database itself rejects a second active booking for a slot.
            CREATE UNIQUE INDEX IF NOT EXISTS one_confirmed_booking_per_slot
                ON bookings(slot_id) WHERE status = 'confirmed';
            CREATE INDEX IF NOT EXISTS bookings_by_email_created
                ON bookings(email, created_at, id);
            CREATE TABLE IF NOT EXISTS idempotency (
                -- Save the result so a retry can receive the original answer.
                key TEXT PRIMARY KEY,
                request_hash TEXT NOT NULL,
                response_status INTEGER NOT NULL,
                response_body TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rate_attempts (
                -- Keep attempts, including failed ones, to limit rapid retries.
                email TEXT NOT NULL,
                attempted_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS rate_attempts_by_email_time
                ON rate_attempts(email, attempted_at);
            """
        )
        if connection.execute("SELECT COUNT(*) FROM slots").fetchone()[0] == 0:
            now = datetime.now(timezone.utc)
            samples = [
                (
                    "demo-1",
                    "Dr Ade",
                    (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0).isoformat(),
                    (now + timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0).isoformat(),
                ),
                (
                    "demo-2",
                    "Dr Ade",
                    (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0).isoformat(),
                    (now + timedelta(days=1)).replace(hour=10, minute=30, second=0, microsecond=0).isoformat(),
                ),
                (
                    "demo-3",
                    "Dr Okafor",
                    (now + timedelta(days=2)).replace(hour=14, minute=0, second=0, microsecond=0).isoformat(),
                    (now + timedelta(days=2)).replace(hour=14, minute=30, second=0, microsecond=0).isoformat(),
                ),
            ]
            connection.executemany(
                "INSERT INTO slots(id, provider, start, end) VALUES (?, ?, ?, ?)",
                samples,
            )


@app.on_event("startup")
def startup():
    # Refuse to start without a key, so the API cannot accidentally be left open.
    if not os.environ.get("API_KEY"):
        raise RuntimeError("API_KEY must be configured")
    initialize_database()
#endregion


#region 2. API key and error responses
def error(message: str, status: int, headers: dict[str, str] | None = None):
    return JSONResponse({"error": message}, status_code=status, headers=headers)


def api_key_valid(value: str | None) -> bool:
    expected = os.environ.get("API_KEY", "")
    return bool(expected) and value is not None and hmac.compare_digest(value, expected)


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    # Tell API tools that every API request needs the X-API-Key header.
    schema["components"]["securitySchemes"] = {
        "ApiKeyAuth": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
        }
    }
    schema["security"] = [{"ApiKeyAuth": []}]
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    # Let people open the web page, but require the key for API routes.
    if request.url.path != "/" and not request.url.path.startswith("/static/"):
        if not api_key_valid(request.headers.get("X-API-Key")):
            return error("invalid or missing API key", 401)
    return await call_next(request)
#endregion


#region 3. Shared data helpers
def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed
    except ValueError:
        return None


def booking_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "slot_id": row["slot_id"],
        "name": row["name"],
        "email": row["email"],
        "status": row["status"],
        "created_at": row["created_at"],
    }
#endregion


#region 4. Web page and schedule setup
@app.get("/")
def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/reset")
async def reset(request: Request):
    """Replace the whole schedule and clear bookings (use for setup or testing)."""
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        return error("invalid JSON body", 400)
    slots = payload.get("slots") if isinstance(payload, dict) else None
    if not isinstance(slots, list):
        return error("slots must be an array", 400)
    seen_ids = set()
    clean_slots = []
    for slot in slots:
        if not isinstance(slot, dict):
            return error("each slot must be an object", 400)
        slot_id, provider = slot.get("id"), slot.get("provider")
        start, end = parse_iso(slot.get("start")), parse_iso(slot.get("end"))
        if not all(isinstance(value, str) and value.strip() for value in (slot_id, provider)):
            return error("slot id and provider are required", 400)
        if start is None or end is None:
            return error("slot start and end must be valid date-times", 400)
        if slot_id in seen_ids:
            return error("slot ids must be unique", 400)
        seen_ids.add(slot_id)
        clean_slots.append(
            (
                slot_id,
                provider,
                start.astimezone(timezone.utc).isoformat(),
                end.astimezone(timezone.utc).isoformat(),
            )
        )
    with database() as connection:
        # Do the replacement as one unit so a partial reset cannot be left behind.
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM bookings")
        connection.execute("DELETE FROM slots")
        connection.execute("DELETE FROM idempotency")
        connection.execute("DELETE FROM rate_attempts")
        connection.executemany(
            "INSERT INTO slots(id, provider, start, end) VALUES (?, ?, ?, ?)",
            clean_slots,
        )
        connection.execute("COMMIT")
    return Response(status_code=200)


@app.post("/admin/clear-bookings")
def clear_bookings(
    admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    """Clear bookings while leaving the appointment schedule in place."""
    expected_admin_key = os.environ.get("ADMIN_KEY", "")
    if not expected_admin_key or admin_key is None or not hmac.compare_digest(
        admin_key, expected_admin_key
    ):
        return error("invalid or missing admin key", 401)
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM bookings")
        connection.execute("DELETE FROM idempotency")
        connection.execute("DELETE FROM rate_attempts")
        connection.execute("COMMIT")
    return {"ok": True, "message": "bookings cleared"}
#endregion


#region 5. Show available appointments
@app.get("/slots")
def list_slots(provider: str | None = None):
    """Show appointments in time order, with whether each can still be booked."""
    with database() as connection:
        rows = connection.execute(
            """
            SELECT s.id, s.provider, s.start, s.end,
                   NOT EXISTS (
                       SELECT 1 FROM bookings b
                       WHERE b.slot_id = s.id AND b.status = 'confirmed'
                   ) AS available
            FROM slots s
            WHERE (? IS NULL OR s.provider = ?)
            ORDER BY s.start, s.id
            """,
            (provider, provider),
        ).fetchall()
    return {
        "slots": [
            {
                "id": row["id"],
                "provider": row["provider"],
                "start": row["start"],
                "end": row["end"],
                "available": bool(row["available"]),
            }
            for row in rows
        ]
    }
#endregion


#region 6. Helpers for safe retries and booking pages
def canonical_hash(payload: Any) -> str:
    # Make the same request look the same even if its JSON fields arrive in a new order.
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def cursor_encode(created_at: str, booking_id: str) -> str:
    # Pack the last booking's date and reference into a token for the next page.
    raw = json.dumps([created_at, booking_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def cursor_decode(cursor: str) -> tuple[str, str] | None:
    # Unpack the token; if it cannot be read, the caller reports an invalid page link.
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if (
            isinstance(decoded, list)
            and len(decoded) == 2
            and all(isinstance(item, str) for item in decoded)
        ):
            return decoded[0], decoded[1]
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    return None
#endregion


#region 7. Create a booking safely
@app.post("/bookings")
async def create_booking(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Book a slot; safely handle retries and limit rapid attempts by email."""
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        return error("invalid JSON body", 400)
    fingerprint = canonical_hash(payload)
    raw_email = payload.get("email") if isinstance(payload, dict) else None
    normalized_email = raw_email.strip().lower() if isinstance(raw_email, str) else None
    timestamp = datetime.now(timezone.utc).isoformat()
    now_epoch = datetime.now(timezone.utc).timestamp()

    with database() as connection:
        # Finish one booking change before another starts, even if requests arrive together.
        connection.execute("BEGIN IMMEDIATE")
        if idempotency_key:
            previous = connection.execute(
                "SELECT request_hash, response_status, response_body FROM idempotency WHERE key = ?",
                (idempotency_key,),
            ).fetchone()
            if previous:
                # Same key and same request? Return the saved answer, not a new booking.
                if previous["request_hash"] != fingerprint:
                    connection.execute("COMMIT")
                    return error("idempotency key was already used with a different body", 422)
                response = JSONResponse(
                    json.loads(previous["response_body"]),
                    status_code=previous["response_status"],
                )
                connection.execute("COMMIT")
                return response

        if normalized_email:
            # Count this email's attempts from the last minute, even failed requests.
            cutoff = now_epoch - 60
            count = connection.execute(
                "SELECT COUNT(*) FROM rate_attempts WHERE email = ? AND attempted_at > ?",
                (normalized_email, cutoff),
            ).fetchone()[0]
            if count >= 5:
                oldest = connection.execute(
                    "SELECT MIN(attempted_at) FROM rate_attempts WHERE email = ? AND attempted_at > ?",
                    (normalized_email, cutoff),
                ).fetchone()[0]
                retry_after = max(1, int(oldest + 60 - now_epoch + 0.999))
                connection.execute("COMMIT")
                return error(
                    "too many booking attempts",
                    429,
                    {"Retry-After": str(retry_after)},
                )
            connection.execute(
                "INSERT INTO rate_attempts(email, attempted_at) VALUES (?, ?)",
                (normalized_email, now_epoch),
            )
            connection.execute("DELETE FROM rate_attempts WHERE attempted_at <= ?", (cutoff,))

        response_body: dict[str, Any]
        response_status = 400
        if not isinstance(payload, dict):
            response_body = {"error": "request body must be an object"}
        else:
            slot_id, name, email = (
                payload.get("slot_id"),
                payload.get("name"),
                payload.get("email"),
            )
            if not all(isinstance(value, str) and value.strip() for value in (slot_id, name, email)):
                response_body = {"error": "slot_id, name and email are required"}
            else:
                clean_name = name.strip()
                clean_email = email.strip().lower()
                valid_email = re.fullmatch(
                    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+",
                    clean_email,
                ) is not None
                if not valid_email:
                    response_body = {"error": "email is invalid"}
                else:
                    slot = connection.execute(
                        "SELECT id, start FROM slots WHERE id = ?", (slot_id,)
                    ).fetchone()
                    if slot is None:
                        response_status = 404
                        response_body = {"error": "slot not found"}
                    else:
                        start = parse_iso(slot["start"])
                        if start is None or start <= datetime.now(timezone.utc):
                            response_body = {"error": "slot has already started"}
                        else:
                            already_booked = connection.execute(
                                "SELECT 1 FROM bookings WHERE slot_id = ? AND status = 'confirmed'",
                                (slot_id,),
                            ).fetchone()
                            if already_booked:
                                response_status = 409
                                response_body = {"error": "slot already booked"}
                            else:
                                booking = {
                                    "id": str(uuid.uuid4()),
                                    "slot_id": slot_id,
                                    "name": clean_name,
                                    "email": clean_email,
                                    "status": "confirmed",
                                    "created_at": timestamp,
                                }
                                connection.execute(
                                    """
                                    INSERT INTO bookings(id, slot_id, name, email, status, created_at)
                                    VALUES (?, ?, ?, ?, 'confirmed', ?)
                                    """,
                                    (
                                        booking["id"],
                                        slot_id,
                                        clean_name,
                                        clean_email,
                                        timestamp,
                                    ),
                                )
                                response_status = 201
                                response_body = booking
        if idempotency_key:
            connection.execute(
                """
                INSERT INTO idempotency(key, request_hash, response_status, response_body)
                VALUES (?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    fingerprint,
                    response_status,
                    json.dumps(response_body, separators=(",", ":")),
                ),
            )
        connection.execute("COMMIT")
    return JSONResponse(response_body, status_code=response_status)
#endregion


#region 8. Find, list, and cancel bookings
@app.get("/bookings")
def list_bookings(
    email: str | None = None,
    limit: str = "10",
    cursor: str | None = None,
):
    """Show someone's bookings a page at a time; the next_cursor opens the next page."""
    if email is None:
        return error("email is required", 400)
    if not limit.isdigit() or not 1 <= int(limit) <= 50:
        return error("limit must be between 1 and 50", 400)
    page_size = int(limit)
    cursor_values = cursor_decode(cursor) if cursor else None
    if cursor and cursor_values is None:
        return error("cursor is invalid", 400)
    normalized_email = email.strip().lower()
    with database() as connection:
        if cursor_values:
            rows = connection.execute(
                """
                SELECT * FROM bookings
                WHERE email = ? AND (created_at, id) > (?, ?)
                ORDER BY created_at, id LIMIT ?
                """,
                (normalized_email, cursor_values[0], cursor_values[1], page_size + 1),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM bookings WHERE email = ?
                ORDER BY created_at, id LIMIT ?
                """,
                (normalized_email, page_size + 1),
            ).fetchall()
    has_more = len(rows) > page_size
    page = rows[:page_size]
    next_cursor = (
        cursor_encode(page[-1]["created_at"], page[-1]["id"])
        if has_more and page
        else None
    )
    return {"bookings": [booking_dict(row) for row in page], "next_cursor": next_cursor}


@app.get("/bookings/{booking_id}")
def get_booking(booking_id: str):
    """Find one booking using its reference number."""
    with database() as connection:
        row = connection.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if row is None:
        return error("booking not found", 404)
    return booking_dict(row)


@app.delete("/bookings/{booking_id}")
def cancel_booking(booking_id: str):
    """Cancel a booking so another patient can take its slot."""
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        if row is None:
            connection.execute("COMMIT")
            return error("booking not found", 404)
        if row["status"] == "cancelled":
            connection.execute("COMMIT")
            return error("booking already cancelled", 409)
        connection.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,)
        )
        cancelled = dict(row)
        cancelled["status"] = "cancelled"
        connection.execute("COMMIT")
    return booking_dict(cancelled)
#endregion
