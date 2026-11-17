import sqlite3
from time import time
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, StringConstraints

from .database import Database


class Settings(BaseModel):
    buyer_portal_enabled: bool = True
    buyer_portal_ttl_hours: Annotated[int, Field(ge=0, le=720)] = 0
    buyer_portal_help: Annotated[str, StringConstraints(strip_whitespace=True, max_length=1500)] = ""
    rental_reminder_minutes: Annotated[int, Field(ge=0, le=1440)] = 30


def read_settings(connection: sqlite3.Connection) -> Settings:
    return Settings.model_validate(dict(connection.execute("SELECT * FROM settings WHERE id = 1").fetchone()))


def create_settings_router(database: Database) -> APIRouter:
    router = APIRouter()

    @router.get("/settings", response_model=Settings)
    def get_settings():
        with database.connect() as connection:
            return read_settings(connection)

    @router.post("/settings", response_model=Settings)
    def update_settings(settings: Settings):
        with database.connect(write=True) as connection:
            connection.execute(
                """UPDATE settings SET buyer_portal_enabled = :buyer_portal_enabled,
                    buyer_portal_ttl_hours = :buyer_portal_ttl_hours, buyer_portal_help = :buyer_portal_help,
                    rental_reminder_minutes = :rental_reminder_minutes WHERE id = 1""",
                settings.model_dump(),
            )
        return settings

    @router.get("/notifications")
    def notifications():
        with database.connect() as connection:
            settings = read_settings(connection)
            now = int(time())
            rows = connection.execute(
                """WITH due AS (
                    SELECT rentals.id AS rental_id, rentals.expires_at, accounts.login,
                        COALESCE(rentals.game_at_start, accounts.game) AS game,
                        CASE WHEN rentals.expires_at <= ? THEN 'overdue' ELSE 'due' END AS kind
                    FROM rentals JOIN accounts ON rentals.account_id = accounts.id
                    WHERE rentals.closed_at IS NULL AND rentals.expires_at <= ?
                )
                SELECT due.*, notification_reads.rental_id IS NOT NULL AS is_read
                FROM due LEFT JOIN notification_reads
                    ON due.rental_id = notification_reads.rental_id
                    AND due.kind = notification_reads.kind AND due.expires_at = notification_reads.expires_at
                ORDER BY due.expires_at""",
                (now, now + settings.rental_reminder_minutes * 60),
            )
            result = [dict(row) for row in rows]
        return {"notifications": result, "unread": sum(not record["is_read"] for record in result)}

    @router.post("/notifications/{rental_id}/{kind}/read")
    def read_notification(rental_id: int, kind: Literal["due", "overdue"], expires_at: int):
        with database.connect(write=True) as connection:
            settings = read_settings(connection)
            now = int(time())
            row = connection.execute("SELECT expires_at, closed_at FROM rentals WHERE id = ?", (rental_id,)).fetchone()
            expected_kind = "overdue" if expires_at <= now else "due"
            if (row is None or row["closed_at"] is not None or row["expires_at"] != expires_at
                    or expected_kind != kind or expires_at > now + settings.rental_reminder_minutes * 60):
                raise HTTPException(404, "Уведомление больше не актуально")
            connection.execute(
                """INSERT INTO notification_reads(rental_id, kind, expires_at) VALUES (?, ?, ?)
                   ON CONFLICT DO NOTHING""",
                (rental_id, kind, expires_at),
            )
        return {"success": True}

    return router
