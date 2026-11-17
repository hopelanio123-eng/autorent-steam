from decimal import Decimal
import sqlite3
from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, StringConstraints

from .database import Database


Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class LotInput(BaseModel):
    title: Text
    game: Text
    minutes: Annotated[int, Field(ge=1, le=43200)]
    price: Annotated[Decimal, Field(ge=0, le=10_000_000, decimal_places=2)]
    is_enabled: bool = True

    def fields(self) -> tuple:
        return self.title, self.game, self.minutes, int(self.price * 100), self.is_enabled


def load_lot(connection: sqlite3.Connection, lot_id: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM lots WHERE id = ?", (lot_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Лот не найден")
    return row


def lot_view(row: sqlite3.Row, available: int | None = None) -> dict:
    return {
        "id": row["id"], "title": row["title"], "game": row["game"], "minutes": row["minutes"],
        "price": row["price_cents"] / 100, "is_enabled": bool(row["is_enabled"]), "revision": row["revision"],
        "available": available,
    }


def ensure_game(connection: sqlite3.Connection, game: str):
    if connection.execute("SELECT 1 FROM accounts WHERE game = ? AND archived = 0 LIMIT 1", (game,)).fetchone() is None:
        raise HTTPException(422, "Добавьте аккаунт для этой игры, чтобы создать или изменить лот")


def create_lots_router(database: Database) -> APIRouter:
    router = APIRouter(prefix="/lots")

    @router.get("")
    def list_lots():
        with database.connect() as connection:
            rows = connection.execute(
                """WITH available AS (
                      SELECT accounts.game, COUNT(*) AS count
                      FROM accounts
                      LEFT JOIN rentals ON rentals.account_id = accounts.id AND rentals.closed_at IS NULL
                      WHERE accounts.archived = 0 AND accounts.disabled = 0 AND rentals.id IS NULL
                      GROUP BY accounts.game
                   )
                   SELECT lots.*, COALESCE(available.count, 0) AS available
                   FROM lots LEFT JOIN available ON available.game = lots.game ORDER BY lots.id DESC"""
            )
            return {"lots": [lot_view(row, row["available"]) for row in rows]}

    @router.post("", status_code=201)
    def create_lot(lot: LotInput):
        with database.connect(write=True) as connection:
            ensure_game(connection, lot.game)
            cursor = connection.execute(
                "INSERT INTO lots(title, game, minutes, price_cents, is_enabled) VALUES (?, ?, ?, ?, ?)", lot.fields(),
            )
            return lot_view(load_lot(connection, cursor.lastrowid))

    @router.put("/{lot_id}")
    def edit_lot(lot_id: int, lot: LotInput):
        with database.connect(write=True) as connection:
            existing = load_lot(connection, lot_id)
            if existing["game"] != lot.game:
                ensure_game(connection, lot.game)
            connection.execute(
                """UPDATE lots SET title = ?, game = ?, minutes = ?, price_cents = ?, is_enabled = ?,
                    revision = revision + 1 WHERE id = ?""", (*lot.fields(), lot_id),
            )
            return lot_view(load_lot(connection, lot_id))

    @router.delete("/{lot_id}")
    def delete_lot(lot_id: int):
        with database.connect(write=True) as connection:
            load_lot(connection, lot_id)
            connection.execute("DELETE FROM lots WHERE id = ?", (lot_id,))
        return {"success": True}

    return router
