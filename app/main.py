from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
import sqlite3
import sys
from time import time
from typing import Annotated, Literal

from fastapi import APIRouter, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .account_csv import MAX_BYTES, decode_accounts, encode_accounts
from .buyer_portal import create_buyer_router, issue_link
from .database import Database
from .lots import create_lots_router, load_lot
from .reports import create_reports_router
from .settings import create_settings_router


WEB_ROOT = Path(__file__).with_name("web")
PROJECT_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
DEFAULT_DATA = PROJECT_ROOT / "data" / "autorent.sqlite3"
STATUSES = ("free", "busy", "expired", "disabled")


def validate_login(login: str) -> str:
    if login in {".", ".."} or any(ord(char) < 32 or ord(char) == 127 for char in login):
        raise ValueError("Логин содержит недопустимые символы")
    return login


Login = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=80, pattern=r"^[^/\\]+$"),
    AfterValidator(validate_login),
]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
OptionalName = Annotated[str, StringConstraints(strip_whitespace=True, max_length=120)]
Money = Annotated[Decimal, Field(ge=0, le=10_000_000, decimal_places=2)]

ACCOUNTS = """
    SELECT accounts.*, rentals.id AS rental_id, rentals.expires_at, rentals.order_id,
        (SELECT COUNT(*) FROM rentals AS r WHERE r.account_id = accounts.id) AS rentals_count,
        (SELECT COALESCE(SUM(r.price_cents - COALESCE(refunds.amount_cents, 0)), 0)
            FROM rentals AS r LEFT JOIN refunds ON refunds.rental_id = r.id
            WHERE r.account_id = accounts.id) AS earned
    FROM accounts
    LEFT JOIN rentals ON rentals.account_id = accounts.id AND rentals.closed_at IS NULL
    WHERE accounts.archived = 0
"""

RENTALS = """
    SELECT rentals.*, accounts.login, COALESCE(rentals.game_at_start, accounts.game) AS game,
        COALESCE(rentals.platform_at_start, accounts.platform) AS platform,
        COALESCE(refunds.amount_cents, 0) AS refunded_cents
    FROM rentals JOIN accounts ON accounts.id = rentals.account_id
    LEFT JOIN refunds ON refunds.rental_id = rentals.id
"""


class AccountFields(BaseModel):
    game: Name
    platform: Name = "steam"
    account_name: OptionalName = ""
    game_subgroup: OptionalName = ""
    purchase_cost: Money = Decimal("0")
    extra_expenses: Money = Decimal("0")

    def columns(self) -> dict:
        return {
            "game": self.game, "platform": self.platform, "account_name": self.account_name,
            "game_subgroup": self.game_subgroup,
            "purchase_cents": int(self.purchase_cost * 100), "expense_cents": int(self.extra_expenses * 100),
        }


class AccountCreate(AccountFields):
    login: Login


class PlatformUpdate(BaseModel):
    platform: Name


class RefundRequest(BaseModel):
    amount: Money


class AssignRental(BaseModel):
    login: Login | None = None
    logins: list[Login] = Field(default_factory=list, max_length=50)
    hours: Annotated[Decimal, Field(gt=0, le=720)] = Decimal("1")
    price: Money = Decimal("0")
    order_id: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    marketplace: Literal["manual", "funpay", "playerok"] = "manual"
    lot_id: Annotated[int, Field(ge=1)] | None = None
    lot_revision: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def validate_logins(self):
        if bool(self.login) == bool(self.logins):
            raise ValueError("Укажите один логин или список логинов")
        if len(set(self.logins)) != len(self.logins):
            raise ValueError("В списке есть повторяющиеся логины")
        if self.hours * 3600 < 1:
            raise ValueError("Длительность должна быть не меньше секунды")
        if (self.lot_id is None) != (self.lot_revision is None):
            raise ValueError("Укажите идентификатор и версию лота")
        return self

    def selected_logins(self) -> list[str]:
        return [self.login] if self.login is not None else self.logins


def timestamp(value: int | None) -> str | None:
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def account_view(row: sqlite3.Row, now: int) -> dict:
    if row["disabled"]:
        status = "disabled"
    elif row["expires_at"] is None:
        status = "free"
    else:
        status = "expired" if row["expires_at"] <= now else "busy"
    return {
        "id": row["id"], "login": row["login"], "account_name": row["account_name"],
        "game": row["game"], "platform": row["platform"], "status": status,
        "game_subgroup": row["game_subgroup"],
        "purchase_cost": row["purchase_cents"] / 100, "extra_expenses": row["expense_cents"] / 100,
        "is_active": row["rental_id"] is not None, "expires_at": timestamp(row["expires_at"]),
        "id_order": row["order_id"], "rentals_count": row["rentals_count"],
        "total_earned": row["earned"] / 100,
    }


def rental_view(row: sqlite3.Row, now: int) -> dict:
    remaining = max(0, row["expires_at"] - now)
    minutes = (remaining + 59) // 60
    label = f"{minutes // 60} ч {minutes % 60:02d} мин" if remaining else "Expired"
    return {
        "id": row["id"], "login": row["login"], "game": row["game"],
        "platform": row["platform"], "marketplace": row["marketplace"],
        "id_order": row["order_id"], "price": row["price_cents"] / 100,
        "refund_amount": row["refunded_cents"] / 100,
        "lot_title": row["lot_title"],
        "rented_at": timestamp(row["started_at"]), "expires_at": timestamp(row["expires_at"]),
        "closed_at": timestamp(row["closed_at"]), "time_remaining": label,
        "status": "closed" if row["closed_at"] is not None else ("busy" if remaining else "expired"),
    }


def find_account(connection: sqlite3.Connection, login: str) -> sqlite3.Row:
    row = connection.execute(ACCOUNTS + " AND accounts.login = ?", (login,)).fetchone()
    if row is None:
        raise HTTPException(404, "Аккаунт не найден")
    return row


def insert_account(connection: sqlite3.Connection, account: AccountCreate, *, skip_duplicates: bool = False) -> bool:
    statement = """
        INSERT INTO accounts (login, game, platform, account_name, game_subgroup, purchase_cents, expense_cents)
        VALUES (:login, :game, :platform, :account_name, :game_subgroup, :purchase_cents, :expense_cents)
    """
    if skip_duplicates:
        statement += " ON CONFLICT (login) WHERE archived = 0 DO NOTHING"
    return connection.execute(statement, {"login": account.login, **account.columns()}).rowcount == 1


def update_account(database: Database, login: str, values: dict):
    with database.connect(write=True) as connection:
        row = find_account(connection, login)
        setters = ", ".join(f"{column} = ?" for column in values)
        connection.execute(f"UPDATE accounts SET {setters} WHERE id = ?", (*values.values(), row["id"]))
        return account_view(find_account(connection, login), int(time()))


def ensure_unrented(row: sqlite3.Row):
    if row["rental_id"] is not None:
        raise HTTPException(409, f"Сначала закройте аренду аккаунта {row['login']}")


def open_rental_id(connection: sqlite3.Connection, login: str, expected_rental_id: int | None) -> int:
    account = find_account(connection, login)
    if account["rental_id"] is None:
        raise HTTPException(404, "Открытая аренда не найдена")
    if expected_rental_id is not None and account["rental_id"] != expected_rental_id:
        raise HTTPException(409, "Аренда уже изменилась. Обновите список")
    return account["rental_id"]


def create_app(db_path: Path) -> FastAPI:
    database = Database(db_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        database.initialize()
        yield

    app = FastAPI(title="AutoRent Steam API", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])

    @app.middleware("http")
    async def same_origin_changes(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            expected = f"{request.url.scheme}://{request.url.netloc}"
            if origin != expected:
                return JSONResponse({"detail": "Запрос с другого адреса"}, status_code=403)
        return await call_next(request)

    router = APIRouter(prefix="/api")

    @router.get("/accounts")
    def list_accounts(
        game: str | None = None,
        status_filter: Literal["free", "busy", "expired", "disabled"] | None = None,
        q: Annotated[str, Query(max_length=120)] = "",
    ):
        with database.connect() as connection:
            now = int(time())
            accounts = [account_view(row, now) for row in connection.execute(ACCOUNTS + " ORDER BY accounts.id DESC")]
        filtered = [
            account for account in accounts
            if (game is None or account["game"].casefold() == game.casefold())
            and (status_filter is None or account["status"] == status_filter)
            and (not q or any(q.casefold() in account[key].casefold() for key in ("login", "account_name", "game", "game_subgroup")))
        ]
        return {"accounts": filtered, "total": len(filtered)}

    @router.post("/accounts", status_code=201)
    def create_account(account: AccountCreate):
        try:
            with database.connect(write=True) as connection:
                insert_account(connection, account)
                return account_view(find_account(connection, account.login), int(time()))
        except sqlite3.IntegrityError as error:
            raise HTTPException(409, "Аккаунт с таким логином уже есть") from error

    @router.get("/accounts/games")
    def account_games():
        with database.connect() as connection:
            games = connection.execute("SELECT DISTINCT game FROM accounts WHERE archived = 0 ORDER BY game")
            return {"games": [row["game"] for row in games]}

    @router.get("/accounts/export-csv")
    def export_accounts():
        payload = encode_accounts(list_accounts()["accounts"])
        return Response(payload, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="accounts.csv"'})

    @router.post("/accounts/upload-csv")
    def import_accounts(file: Annotated[UploadFile, File()]):
        data = file.file.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise HTTPException(413, "CSV больше 2 МБ")
        accounts, errors = decode_accounts(data, AccountCreate)
        with database.connect(write=True) as connection:
            added = sum(insert_account(connection, account, skip_duplicates=True) for account in accounts)
        return {"added": added, "skipped": len(accounts) - added,
                "errors": len(errors), "error_messages": errors[:20]}

    @router.put("/accounts/{login}")
    def edit_account(login: str, account: AccountFields):
        return update_account(database, login, account.columns())

    @router.post("/accounts/{login}/set-game")
    def set_game(login: str, game: Name, subgroup: OptionalName = ""):
        return update_account(database, login, {"game": game, "game_subgroup": subgroup})

    @router.post("/accounts/{login}/set-platform")
    def set_platform(login: str, request: PlatformUpdate):
        return update_account(database, login, {"platform": request.platform})

    @router.post("/accounts/{login}/set-cost")
    def set_cost(login: str, cost: Money):
        return update_account(database, login, {"purchase_cents": int(cost * 100)})

    @router.post("/accounts/{login}/set-expenses")
    def set_expenses(login: str, expenses: Money):
        return update_account(database, login, {"expense_cents": int(expenses * 100)})

    @router.post("/accounts/{login}/toggle-disabled")
    def toggle_account(login: str):
        with database.connect(write=True) as connection:
            row = find_account(connection, login)
            ensure_unrented(row)
            connection.execute("UPDATE accounts SET disabled = 1 - disabled WHERE id = ?", (row["id"],))
            return account_view(find_account(connection, login), int(time()))

    @router.delete("/accounts/{login}")
    def archive_account(login: str):
        with database.connect(write=True) as connection:
            row = find_account(connection, login)
            ensure_unrented(row)
            connection.execute("UPDATE accounts SET archived = 1 WHERE id = ?", (row["id"],))
        return {"success": True}

    @router.get("/accounts/{login}/rental-history")
    def account_history(login: str):
        with database.connect() as connection:
            account = find_account(connection, login)
            rows = connection.execute(RENTALS + " WHERE rentals.account_id = ? ORDER BY rentals.id DESC", (account["id"],))
            rentals = [rental_view(row, int(time())) for row in rows]
        return {"rentals": rentals, "total": len(rentals)}

    @router.get("/rentals/available-accounts")
    def available_accounts(game: str | None = None):
        return list_accounts(status_filter="free", game=game)

    @router.get("/rentals")
    def list_rentals(include_closed: bool = False, q: Annotated[str, Query(max_length=120)] = ""):
        query = RENTALS if include_closed else RENTALS + " WHERE rentals.closed_at IS NULL"
        with database.connect() as connection:
            now = int(time())
            rentals = [
                rental_view(row, now) for row in connection.execute(query + " ORDER BY rentals.id DESC")
                if not q or any(q.casefold() in str(row[key] or "").casefold() for key in ("login", "game", "order_id"))
            ]
        return {"rentals": rentals, "total": len(rentals)}

    @router.post("/rentals/manual-assign", status_code=201)
    def assign_rental(request: AssignRental):
        logins = request.selected_logins()
        price, remainder = divmod(int(request.price * 100), len(logins))
        with database.connect(write=True) as connection:
            now = int(time())
            expiry = now + int(request.hours * 3600)
            lot = None
            if request.lot_id is not None:
                lot = load_lot(connection, request.lot_id)
                if not lot["is_enabled"] or lot["revision"] != request.lot_revision:
                    raise HTTPException(409, "Лот изменился. Откройте его заново")
                expiry = now + lot["minutes"] * 60
                price, remainder = lot["price_cents"], 0
            assigned = []
            for index, login in enumerate(logins):
                row = find_account(connection, login)
                ensure_unrented(row)
                if row["disabled"]:
                    raise HTTPException(409, f"Аккаунт {login} отключён")
                if lot is not None and row["game"] != lot["game"]:
                    raise HTTPException(409, f"Аккаунт {login} относится к другой игре")
                cursor = connection.execute(
                    """INSERT INTO rentals (account_id, order_id, marketplace, started_at, expires_at,
                                            price_cents, game_at_start, platform_at_start, lot_title)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (row["id"], request.order_id or None, request.marketplace, now, expiry,
                     price + (index < remainder), row["game"], row["platform"], lot["title"] if lot is not None else None),
                )
                saved = connection.execute(RENTALS + " WHERE rentals.id = ?", (cursor.lastrowid,)).fetchone()
                assigned.append(rental_view(saved, now))
        return {"assigned": assigned, "errors": []}

    @router.post("/rentals/{login}/add-time")
    def add_time(login: str, minutes: Annotated[int, Query(ge=-43200, le=43200)], rental_id: int | None = None):
        with database.connect(write=True) as connection:
            current_id = open_rental_id(connection, login, rental_id)
            connection.execute("UPDATE rentals SET expires_at = expires_at + ? WHERE id = ?", (minutes * 60, current_id))
        return {"success": True}

    @router.post("/rentals/{login}/set-time")
    def set_time(login: str, minutes: Annotated[int, Query(gt=0, le=43200)], rental_id: int | None = None):
        with database.connect(write=True) as connection:
            current_id = open_rental_id(connection, login, rental_id)
            connection.execute("UPDATE rentals SET expires_at = ? WHERE id = ?", (int(time()) + minutes * 60, current_id))
        return {"success": True}

    @router.post("/rentals/{login}/free")
    def close_rental(login: str, rental_id: int | None = None):
        with database.connect(write=True) as connection:
            current_id = open_rental_id(connection, login, rental_id)
            connection.execute("UPDATE rentals SET closed_at = ? WHERE id = ?", (int(time()), current_id))
        return {"success": True}

    @router.post("/rentals/{rental_id}/buyer-link")
    def new_buyer_link(rental_id: int):
        with database.connect(write=True) as connection:
            row = connection.execute(
                "SELECT expires_at, closed_at FROM rentals WHERE id = ?", (rental_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Аренда не найдена")
            if row["closed_at"] is not None or row["expires_at"] <= int(time()):
                raise HTTPException(409, "Ссылка доступна только для действующей аренды")
            return {"path": issue_link(connection, rental_id)}

    @router.post("/orders/{rental_id}/refund")
    def record_refund(rental_id: int, request: RefundRequest):
        amount = int(request.amount * 100)
        with database.connect(write=True) as connection:
            row = connection.execute(RENTALS + " WHERE rentals.id = ?", (rental_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Аренда не найдена")
            if row["closed_at"] is None:
                raise HTTPException(409, "Сначала закройте аренду")
            if row["refunded_cents"]:
                raise HTTPException(409, "Возврат уже записан")
            if not 0 < amount <= row["price_cents"]:
                raise HTTPException(422, "Сумма возврата должна быть больше нуля и не больше суммы аренды")
            connection.execute(
                "INSERT INTO refunds (rental_id, amount_cents, recorded_at) VALUES (?, ?, ?)",
                (rental_id, amount, int(time())),
            )
        return {"refund_amount": amount / 100}

    @router.get("/dashboard/stats")
    def dashboard():
        with database.connect() as connection:
            now = int(time())
            counts = dict.fromkeys(STATUSES, 0)
            for row in connection.execute(ACCOUNTS):
                counts[account_view(row, now)["status"]] += 1
            revenue = connection.execute(
                """SELECT COUNT(*) AS rentals,
                    COALESCE(SUM(price_cents - COALESCE(refunds.amount_cents, 0)), 0) AS cents
                    FROM rentals LEFT JOIN refunds ON refunds.rental_id = rentals.id"""
            ).fetchone()
        return {
            "accounts": {"total": sum(counts.values()), **counts},
            "revenue": {"total_earned": revenue["cents"] / 100, "total_rentals": revenue["rentals"]},
        }

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(WEB_ROOT / "index.html")

    router.include_router(create_reports_router(database))
    router.include_router(create_settings_router(database))
    router.include_router(create_lots_router(database))
    app.include_router(router)
    app.include_router(create_buyer_router(database, WEB_ROOT))
    app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")
    return app


app = create_app(Path(os.getenv("AUTORENT_DB", str(DEFAULT_DATA))))
