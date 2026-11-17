from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Query

from .database import Database


DAY_TOTALS = """
    SELECT date(started_at, 'unixepoch') AS day, SUM(price_cents) AS gross, 0 AS refunds,
           COUNT(*) AS orders
    FROM rentals WHERE started_at >= ? AND started_at < ? GROUP BY day
    UNION ALL
    SELECT date(recorded_at, 'unixepoch') AS day, 0 AS gross, SUM(amount_cents) AS refunds,
           0 AS orders
    FROM refunds WHERE recorded_at >= ? AND recorded_at < ? GROUP BY day
"""


def midnight_utc(day: date) -> int:
    return int(datetime.combine(day, time.min, timezone.utc).timestamp())


def create_reports_router(database: Database) -> APIRouter:
    router = APIRouter()

    @router.get("/stats/revenue-chart")
    def revenue_chart(days: Annotated[int, Query(ge=1, le=365)] = 7):
        today = datetime.now(timezone.utc).date()
        start_day = today - timedelta(days=days - 1)
        start = midnight_utc(start_day)
        end = midnight_utc(today + timedelta(days=1))
        points = {
            (start_day + timedelta(days=i)).isoformat(): [0, 0, 0]
            for i in range(days)
        }
        with database.connect() as connection:
            for row in connection.execute(DAY_TOTALS, (start, end, start, end)):
                point = points[row["day"]]
                point[0] += row["gross"]
                point[1] += row["refunds"]
                point[2] += row["orders"]
        chart = [
            {"date": day, "earned": gross / 100, "refunds": returned / 100,
             "net": (gross - returned) / 100, "orders": count}
            for day, (gross, returned, count) in points.items()
        ]
        return {"chart": chart, "total": sum(p[0] - p[1] for p in points.values()) / 100}

    @router.get("/dashboard/profit-loss")
    def profit_loss(days: Annotated[int, Query(ge=0, le=365)] = 0):
        today = datetime.now(timezone.utc).date()
        start = midnight_utc(today - timedelta(days=days - 1)) if days else 0
        with database.connect() as connection:
            sales = connection.execute(
                """SELECT COALESCE(SUM(price_cents), 0) AS gross, COUNT(*) AS orders
                    FROM rentals WHERE started_at >= ?""", (start,),
            ).fetchone()
            refunds = connection.execute(
                """SELECT COALESCE(SUM(amount_cents), 0) AS amount, COUNT(*) AS count
                    FROM refunds WHERE recorded_at >= ?""", (start,),
            ).fetchone()
            costs = connection.execute(
                """SELECT COALESCE(SUM(purchase_cents), 0) AS purchase,
                          COALESCE(SUM(expense_cents), 0) AS expenses FROM accounts"""
            ).fetchone()
        net = sales["gross"] - refunds["amount"]
        total_cost = costs["purchase"] + costs["expenses"]
        profit = net - total_cost
        return {
            "gross_revenue": sales["gross"] / 100,
            "completed_orders": sales["orders"],
            "refunds": {"amount": refunds["amount"] / 100, "count": refunds["count"]},
            "net_revenue": net / 100,
            "costs": {"purchase_cost": costs["purchase"] / 100, "extra_expenses": costs["expenses"] / 100},
            "net_profit": profit / 100,
            "roi_percent": round(100 * profit / total_cost, 2) if total_cost else None,
        }

    return router
