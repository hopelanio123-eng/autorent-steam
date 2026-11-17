from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import re
from secrets import token_urlsafe
import sqlite3
from time import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from .database import Database
from .settings import read_settings


PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def issue_link(connection: sqlite3.Connection, rental_id: int) -> str:
    settings = read_settings(connection)
    if not settings.buyer_portal_enabled:
        raise HTTPException(409, "Портал аренды выключен в настройках")
    ttl = settings.buyer_portal_ttl_hours
    link_expires_at = int(time()) + 3600 * ttl if ttl else None
    token = token_urlsafe(32)
    connection.execute(
        """INSERT INTO buyer_links(rental_id, token_digest, link_expires_at) VALUES (?, ?, ?)
            ON CONFLICT(rental_id) DO UPDATE SET token_digest = excluded.token_digest,
                link_expires_at = excluded.link_expires_at""",
        (rental_id, sha256(token.encode("ascii")).digest(), link_expires_at),
    )
    return "/b/" + token


def create_buyer_router(database: Database, web_root: Path) -> APIRouter:
    router = APIRouter()

    @router.get("/b/{token}", include_in_schema=False)
    def buyer_page(token: str):
        return FileResponse(web_root / "buyer.html", headers=PRIVATE_HEADERS)

    @router.get("/b/api/{token}/info", include_in_schema=False)
    def buyer_info(token: str):
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
            raise HTTPException(404, "Ссылка не найдена", headers=PRIVATE_HEADERS)
        with database.connect() as connection:
            settings = read_settings(connection)
            row = connection.execute(
                """SELECT rentals.expires_at, rentals.closed_at, rentals.order_id,
                          COALESCE(rentals.game_at_start, accounts.game) AS game, buyer_links.link_expires_at
                    FROM buyer_links JOIN rentals ON rentals.id = buyer_links.rental_id
                    JOIN accounts ON accounts.id = rentals.account_id
                    WHERE buyer_links.token_digest = ?""",
                (sha256(token.encode("ascii")).digest(),),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "Ссылка не найдена", headers=PRIVATE_HEADERS)
        now = int(time())
        if (not settings.buyer_portal_enabled or row["closed_at"] is not None or row["expires_at"] <= now
                or (row["link_expires_at"] is not None and row["link_expires_at"] <= now)):
            raise HTTPException(410, "Срок доступа закончился", headers=PRIVATE_HEADERS)
        return JSONResponse({
            "game": row["game"], "order_id": row["order_id"],
            "expires_at": datetime.fromtimestamp(row["expires_at"], timezone.utc).isoformat(),
            "help": settings.buyer_portal_help,
        }, headers=PRIVATE_HEADERS)

    return router
