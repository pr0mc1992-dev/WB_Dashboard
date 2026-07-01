#!/usr/bin/env python3
"""Build public static WB replenishment dashboard for Netlify.

Reads WB_API_TOKEN or separate WB_ANALYTICS_TOKEN/WB_SUPPLIES_TOKEN from environment.
Writes public/data.json and expects public/index.html to render it.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib import error, parse, request
from zoneinfo import ZoneInfo

ANALYTICS_BASE = "https://seller-analytics-api.wildberries.ru"
SUPPLIES_BASE = "https://supplies-api.wildberries.ru"
MSK = ZoneInfo("Europe/Moscow")
PUBLIC_DIR = Path("public")


@dataclass
class ProductRow:
    nm_id: int
    vendor_code: str
    title: str
    subject_name: str
    stock: int
    orders_14d: int
    order_sum: float


@dataclass
class SupplyLine:
    nm_id: int
    vendor_code: str
    supply_id: str
    supply_date: date
    status_id: int
    quantity: int


def get_tokens() -> tuple[str, str]:
    common = os.getenv("WB_API_TOKEN", "").strip()
    analytics = os.getenv("WB_ANALYTICS_TOKEN", common).strip()
    supplies = os.getenv("WB_SUPPLIES_TOKEN", common).strip()
    if not analytics:
        raise RuntimeError("Set WB_API_TOKEN or WB_ANALYTICS_TOKEN in GitHub Secrets")
    return analytics, supplies


def api_json(method: str, url: str, token: str, payload: Any | None = None, params: dict[str, Any] | None = None) -> Any:
    if params:
        url = f"{url}?{parse.urlencode(params)}"
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": token, "Content-Type": "application/json", "Accept": "application/json"}
    req = request.Request(url, data=body, headers=headers, method=method)
    for attempt in range(3):
        try:
            with request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                if resp.status == 204 or not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429 and attempt < 2:
                wait = int(exc.headers.get("X-Ratelimit-Retry") or (20 * (attempt + 1)))
                time.sleep(wait)
                continue
            raise RuntimeError(f"WB API {exc.code} for {url}: {raw[:500]}") from exc
        except error.URLError as exc:
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise RuntimeError(f"WB API connection error for {url}: {exc}") from exc
    raise RuntimeError(f"WB API failed for {url}")


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def fetch_products(token: str, start: date, end: date, limit: int = 1000) -> list[ProductRow]:
    rows: list[ProductRow] = []
    offset = 0
    while True:
        payload = {
            "selectedPeriod": {"start": start.isoformat(), "end": end.isoformat()},
            "skipDeletedNm": True,
            "limit": limit,
            "offset": offset,
        }
        data = api_json("POST", f"{ANALYTICS_BASE}/api/analytics/v3/sales-funnel/products", token, payload=payload)
        products = ((data or {}).get("data") or {}).get("products") or []
        for item in products:
            product = item.get("product") or {}
            selected = ((item.get("statistic") or {}).get("selected") or {})
            stocks = product.get("stocks") or {}
            nm_id = int(product.get("nmId") or product.get("nmID") or 0)
            if not nm_id:
                continue
            stock = stocks.get("balanceSum")
            if stock is None:
                stock = int(stocks.get("wb") or 0) + int(stocks.get("mp") or 0)
            rows.append(ProductRow(
                nm_id=nm_id,
                vendor_code=str(product.get("vendorCode") or ""),
                title=str(product.get("title") or ""),
                subject_name=str(product.get("subjectName") or ""),
                stock=int(stock or 0),
                orders_14d=int(selected.get("orderCount") or 0),
                order_sum=float(selected.get("orderSum") or 0),
            ))
        if len(products) < limit:
            break
        offset += limit
    return rows


def fetch_supplies(token: str, as_of: date, horizon_days: int = 60) -> tuple[list[SupplyLine], list[str]]:
    warnings: list[str] = []
    if not token:
        return [], ["No supplies token configured"]
    date_to = as_of + timedelta(days=horizon_days)
    try:
        supplies = paged_supplies(token, {
            "dates": [{"from": as_of.isoformat(), "till": date_to.isoformat(), "type": "supplyDate"}],
            "statusIDs": [2, 3, 4, 6],
        })
    except Exception as exc:
        warnings.append(f"Supply list with date filter failed, retrying without dates: {exc}")
        supplies = paged_supplies(token, {"statusIDs": [2, 3, 4, 6]})

    result: list[SupplyLine] = []
    for supply in supplies:
        supply_date = parse_date(supply.get("supplyDate"))
        if not supply_date or supply_date < as_of or supply_date > date_to:
            continue
        supply_id = supply.get("supplyID")
        preorder_id = supply.get("preorderID")
        lookup_id = supply_id or preorder_id
        if not lookup_id:
            continue
        is_preorder = not supply_id and bool(preorder_id)
        try:
            goods = supply_goods(token, str(lookup_id), is_preorder)
        except Exception as exc:
            warnings.append(f"Supply goods failed for {lookup_id}: {exc}")
            continue
        for item in goods:
            nm_id = int(item.get("nmID") or item.get("nmId") or 0)
            qty = int(item.get("quantity") or 0)
            if nm_id and qty > 0:
                result.append(SupplyLine(
                    nm_id=nm_id,
                    vendor_code=str(item.get("vendorCode") or ""),
                    supply_id=str(lookup_id),
                    supply_date=supply_date,
                    status_id=int(supply.get("statusID") or 0),
                    quantity=qty,
                ))
    return result, warnings


def paged_supplies(token: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    offset = 0
    while True:
        data = api_json("POST", f"{SUPPLIES_BASE}/api/v1/supplies", token, payload=payload, params={"limit": 1000, "offset": offset})
        batch = data if isinstance(data, list) else []
        result.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return result


def supply_goods(token: str, supply_id: str, is_preorder: bool) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    offset = 0
    while True:
        data = api_json("GET", f"{SUPPLIES_BASE}/api/v1/supplies/{supply_id}/goods", token, params={
            "limit": 1000,
            "offset": offset,
            "isPreorderID": str(is_preorder).lower(),
        })
        batch = data if isinstance(data, list) else []
        result.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return result


def build_rows(products: list[ProductRow], supplies: list[SupplyLine], as_of: date) -> list[dict[str, Any]]:
    min_cover_days = float(os.getenv("MIN_COVER_DAYS", "10"))
    target_cover_days = float(os.getenv("TARGET_COVER_DAYS", "30"))
    supplies_by_nm: dict[int, list[SupplyLine]] = defaultdict(list)
    for line in supplies:
        supplies_by_nm[line.nm_id].append(line)
    for lines in supplies_by_nm.values():
        lines.sort(key=lambda line: line.supply_date)

    rows: list[dict[str, Any]] = []
    for product in products:
        avg_daily = product.orders_14d / 14
        if avg_daily > 0:
            stock_days = product.stock / avg_daily
        elif product.stock > 0:
            stock_days = math.inf
        else:
            stock_days = 0.0

        next_supply = next((line for line in supplies_by_nm.get(product.nm_id, []) if line.supply_date >= as_of), None)
        days_to_supply = (next_supply.supply_date - as_of).days if next_supply else None
        next_qty = 0
        if next_supply:
            next_qty = sum(line.quantity for line in supplies_by_nm[product.nm_id] if line.supply_date == next_supply.supply_date)

        reorder_qty = 0
        if avg_daily <= 0:
            status = "Нет продаж"
            severity = "neutral"
            comment = "За 14 дней заказов нет"
        elif product.stock <= 0:
            status = "Срочно: нет остатка"
            severity = "danger"
            reorder_qty = max(0, math.ceil(avg_daily * target_cover_days - product.stock))
            comment = f"Догруз до {target_cover_days:g} дней"
        elif next_supply is None and stock_days < min_cover_days:
            status = "Срочно: <10 дней, поставок нет"
            severity = "danger"
            reorder_qty = max(0, math.ceil(avg_daily * target_cover_days - product.stock))
            comment = f"Нет поставки, догруз до {target_cover_days:g} дней"
        elif next_supply and days_to_supply is not None and stock_days < days_to_supply:
            status = "Не хватит до поставки"
            severity = "danger"
            reorder_qty = max(0, math.ceil(avg_daily * days_to_supply - product.stock))
            comment = "Догруз закрывает разрыв до поставки"
        elif stock_days < min_cover_days:
            status = "Меньше 10, поставка успевает"
            severity = "warning"
            comment = "Поставка должна успеть"
        else:
            status = "OK"
            severity = "ok"
            comment = ""

        rows.append({
            "status": status,
            "severity": severity,
            "vendorCode": product.vendor_code,
            "nmId": product.nm_id,
            "category": product.subject_name,
            "title": product.title,
            "stock": product.stock,
            "orders14d": product.orders_14d,
            "avgDaily": round(avg_daily, 2),
            "stockDays": None if stock_days == math.inf else round(stock_days, 1),
            "stockDaysText": "inf" if stock_days == math.inf else str(round(stock_days, 1)),
            "nextSupplyDate": next_supply.supply_date.isoformat() if next_supply else "",
            "daysToSupply": days_to_supply,
            "nextSupplyQty": next_qty,
            "reorderQty": reorder_qty,
            "comment": comment,
        })
    return sorted(rows, key=risk_sort)


def risk_sort(row: dict[str, Any]) -> tuple[int, float, int]:
    order = {"danger": 0, "warning": 1, "neutral": 2, "ok": 3}
    stock_days = row["stockDays"] if row["stockDays"] is not None else 999999
    return order.get(row["severity"], 9), float(stock_days), -int(row["reorderQty"])


def main() -> None:
    analytics_token, supplies_token = get_tokens()
    as_of = datetime.now(MSK).date()
    period_end = as_of - timedelta(days=1)
    period_start = period_end - timedelta(days=13)
    products = fetch_products(analytics_token, period_start, period_end)
    supplies, warnings = fetch_supplies(supplies_token, as_of)
    rows = build_rows(products, supplies, as_of)

    payload = {
        "updatedAt": datetime.now(MSK).isoformat(timespec="seconds"),
        "asOf": as_of.isoformat(),
        "periodStart": period_start.isoformat(),
        "periodEnd": period_end.isoformat(),
        "summary": {
            "skuCount": len(rows),
            "dangerCount": sum(1 for row in rows if row["severity"] == "danger"),
            "warningCount": sum(1 for row in rows if row["severity"] == "warning"),
            "totalStock": sum(row["stock"] for row in rows),
            "totalOrders14d": sum(row["orders14d"] for row in rows),
            "totalReorderQty": sum(row["reorderQty"] for row in rows),
        },
        "warnings": warnings,
        "rows": rows,
    }
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    (PUBLIC_DIR / "data.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {PUBLIC_DIR / 'data.json'} with {len(rows)} rows")


if __name__ == "__main__":
    main()
