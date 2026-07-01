import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ANALYTICS_BASE = "https://seller-analytics-api.wildberries.ru"
STATISTICS_BASE = "https://statistics-api.wildberries.ru"
MARKETPLACE_BASE = "https://marketplace-api.wildberries.ru"
SUPPLIES_BASE = "https://supplies-api.wildberries.ru"
CONTENT_BASE = "https://content-api.wildberries.ru"
DAYS = int(os.getenv("WB_DAYS", "14"))
LOW_STOCK_DAYS = float(os.getenv("WB_LOW_STOCK_DAYS", "10"))
RESTOCK_DAYS_DEFAULT = int(os.getenv("WB_RESTOCK_DAYS", "14"))
SUPPLY_LOOKAHEAD_DAYS = int(os.getenv("WB_SUPPLY_LOOKAHEAD_DAYS", "90"))
PUBLIC_DIR = Path(os.getenv("PUBLIC_DIR", "public"))
DATA_PATH = PUBLIC_DIR / "data.json"


@dataclass
class Product:
    nm_id: int
    vendor_code: str
    name: str
    category: str
    orders: int
    stock: int


def yesterday_utc() -> date:
    return datetime.now(timezone.utc).date() - timedelta(days=1)


def api_json(method: str, url: str, token: str, payload: dict[str, Any] | None = None) -> Any:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": token, "Content-Type": "application/json", "Accept": "application/json"}
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=70) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 + attempt * 3)
                continue
            raise RuntimeError(f"WB API {exc.code} for {url}: {raw}") from exc
        except urllib.error.URLError:
            if attempt < 3:
                time.sleep(2 + attempt * 3)
                continue
            raise


def as_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def first_text(data: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def normalize_products_payload(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("data", "products", "items", "cards"):
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = normalize_products_payload(value)
            if nested:
                return nested
    return []


def stat_value(stat: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        if key in stat:
            return as_int(stat.get(key))
    return 0


def stock_value(product: dict[str, Any]) -> int:
    stocks = product.get("stocks")
    if isinstance(stocks, dict):
        return as_int(
            stocks.get("quantity")
            or stocks.get("quantityFull")
            or stocks.get("stock")
            or stocks.get("qty")
            or stocks.get("balanceQty")
        )
    return as_int(product.get("quantity") or product.get("quantityFull") or product.get("stock") or product.get("qty") or product.get("remain"))


def fetch_products(token: str, period_start: date, period_end: date) -> list[Product]:
    products: list[Product] = []
    offset = 0
    limit = 1000

    while True:
        payload = {
            "selectedPeriod": {"start": period_start.isoformat(), "end": period_end.isoformat()},
            "nmIds": [],
            "brandNames": [],
            "subjectIds": [],
            "tagIds": [],
            "skipDeletedNm": False,
            "limit": limit,
            "offset": offset,
        }
        data = api_json("POST", f"{ANALYTICS_BASE}/api/analytics/v3/sales-funnel/products", token, payload=payload)
        rows = normalize_products_payload(data)
        if not rows:
            break

        for row in rows:
            if not isinstance(row, dict):
                continue
            product_data = row.get("product") if isinstance(row.get("product"), dict) else row
            statistic = row.get("statistic") if isinstance(row.get("statistic"), dict) else {}
            selected = statistic.get("selected") if isinstance(statistic.get("selected"), dict) else row
            nm_id = as_int(product_data.get("nmID") or product_data.get("nmId") or product_data.get("nm_id"))
            if not nm_id:
                continue
            products.append(Product(
                nm_id=nm_id,
                vendor_code=first_text(product_data, ("vendorCode", "supplierArticle", "article", "sa_name")),
                name=first_text(product_data, ("title", "name", "brandName", "subjectName")),
                category=first_text(product_data, ("subjectName", "object", "category", "categoryName")),
                orders=stat_value(selected, ("orderCount", "orders", "ordersCount", "orderedUnits")),
                stock=stock_value(product_data),
            ))

        if len(rows) < limit:
            break
        offset += limit
        time.sleep(21)

    return products


def extract_tag_names(card: dict[str, Any]) -> list[str]:
    names: list[str] = []

    def add(value: Any) -> None:
        if value is None:
            return
        text = str(value).strip()
        if text and text not in names:
            names.append(text)

    for key in ("tags", "tagNames", "tagsNames", "labels", "labelsNames"):
        value = card.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    add(item.get("name") or item.get("tagName") or item.get("title") or item.get("label"))
                else:
                    add(item)
        elif isinstance(value, dict):
            add(value.get("name") or value.get("tagName") or value.get("title") or value.get("label"))
        elif isinstance(value, str):
            for part in value.replace(";", ",").split(","):
                add(part)
    return names


def fetch_card_tags(token: str) -> tuple[dict[int, dict[str, Any]], list[str]]:
    result: dict[int, dict[str, Any]] = {}
    warnings: list[str] = []
    cursor: dict[str, Any] = {"limit": 100}
    seen: set[tuple[Any, Any]] = set()

    while True:
        payload = {
            "settings": {
                "cursor": cursor,
                "filter": {"withPhoto": -1},
            }
        }
        try:
            data = api_json("POST", f"{CONTENT_BASE}/content/v2/get/cards/list", token, payload=payload)
        except Exception as exc:
            warnings.append("Не удалось получить ярлыки карточек WB. Проверьте, что токен имеет доступ к разделу Контент/Карточки товаров.")
            break

        cards = data.get("cards") if isinstance(data, dict) else []
        if not isinstance(cards, list) or not cards:
            break

        for card in cards:
            if not isinstance(card, dict):
                continue
            nm_id = as_int(card.get("nmID") or card.get("nmId"))
            if not nm_id:
                continue
            result[nm_id] = {
                "vendorCode": first_text(card, ("vendorCode", "supplierArticle", "article")),
                "title": first_text(card, ("title", "imtName", "subjectName")),
                "category": first_text(card, ("subjectName", "object", "parentName")),
                "managerTags": extract_tag_names(card),
            }

        api_cursor = data.get("cursor") if isinstance(data, dict) else {}
        updated_at = api_cursor.get("updatedAt") if isinstance(api_cursor, dict) else None
        nm_id = api_cursor.get("nmID") if isinstance(api_cursor, dict) else None
        marker = (updated_at, nm_id)
        if len(cards) < 100 or not updated_at or marker in seen:
            break
        seen.add(marker)
        cursor = {"limit": 100, "updatedAt": updated_at, "nmID": nm_id}

    return result, warnings


def fetch_current_stocks(token: str) -> tuple[dict[int, int], list[str]]:
    stocks_by_nm: dict[int, int] = {}
    warnings: list[str] = []
    url = f"{STATISTICS_BASE}/api/v1/supplier/stocks?dateFrom=2019-06-20"
    try:
        data = api_json("GET", url, token)
    except Exception:
        warnings.append("Не удалось получить остатки WB в штуках. Проверьте, что токен имеет доступ к категории Статистика.")
        return stocks_by_nm, warnings

    rows = data if isinstance(data, list) else normalize_products_payload(data)
    for row in rows:
        if not isinstance(row, dict):
            continue
        nm_id = as_int(row.get("nmId") or row.get("nmID"))
        if not nm_id:
            continue
        stocks_by_nm[nm_id] = stocks_by_nm.get(nm_id, 0) + as_int(row.get("quantity"))
    return stocks_by_nm, warnings

def supply_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("result", "supplies", "data", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def fetch_supply_goods(token: str, supply_id: int, is_preorder_id: bool) -> list[dict[str, Any]]:
    goods: list[dict[str, Any]] = []
    offset = 0
    limit = 1000
    preorder_flag = "true" if is_preorder_id else "false"
    while True:
        query = urllib.parse.urlencode({"limit": limit, "offset": offset, "isPreorderID": preorder_flag})
        data = api_json("GET", f"{SUPPLIES_BASE}/api/v1/supplies/{supply_id}/goods?{query}", token)
        rows = supply_rows(data)
        goods.extend(rows)
        if len(rows) < limit:
            break
        offset += limit
        time.sleep(2.1)
    return goods


def fetch_supplies(token: str) -> tuple[dict[int, list[dict[str, Any]]], list[str]]:
    supplies_by_nm: dict[int, list[dict[str, Any]]] = {}
    warnings: list[str] = []
    today = datetime.now(timezone.utc).date()
    cutoff = today + timedelta(days=SUPPLY_LOOKAHEAD_DAYS)
    offset = 0
    limit = 1000
    active_supply_statuses = [2, 3, 4, 6]

    while True:
        query = urllib.parse.urlencode({"limit": limit, "offset": offset})
        payload = {
            "dates": [{"from": today.isoformat(), "till": cutoff.isoformat(), "type": "supplyDate"}],
            "statusIDs": active_supply_statuses,
        }
        try:
            data = api_json("POST", f"{SUPPLIES_BASE}/api/v1/supplies?{query}", token, payload=payload)
        except Exception as exc:
            warnings.append(f"Не удалось получить поставки WB через API поставок: {str(exc)[:220]}. Проверьте, что токен WB_API_TOKEN имеет доступ к категории Supplies/Поставки.")
            return supplies_by_nm, warnings

        supplies = supply_rows(data)
        if not supplies:
            break

        for supply in supplies:
            supply_id = as_int(supply.get("supplyID"))
            preorder_id = as_int(supply.get("preorderID"))
            lookup_id = supply_id or preorder_id
            if not lookup_id:
                continue
            supply_date = parse_date(first_text(supply, ("supplyDate", "createDate", "updatedDate")))
            if supply_date and (supply_date < today or supply_date > cutoff):
                continue

            try:
                goods = fetch_supply_goods(token, lookup_id, not bool(supply_id))
            except Exception as exc:
                warnings.append(f"Не удалось получить состав поставки {lookup_id}: {str(exc)[:180]}")
                continue

            for item in goods:
                nm_id = as_int(item.get("nmID") or item.get("nmId"))
                if not nm_id:
                    continue
                qty = as_int(item.get("quantity"))
                ready_qty = as_int(item.get("readyForSaleQuantity"))
                qty_to_supply = max(qty - ready_qty, 0)
                if qty_to_supply <= 0:
                    continue
                supplies_by_nm.setdefault(nm_id, []).append({
                    "supplyId": str(supply_id or preorder_id or ""),
                    "date": supply_date.isoformat() if supply_date else "",
                    "quantity": qty_to_supply,
                })
            time.sleep(2.1)

        if len(supplies) < limit:
            break
        offset += limit
        time.sleep(2.1)

    return supplies_by_nm, warnings

def parse_date(value: str) -> date | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%d.%m.%Y"):
        try:
            return datetime.strptime(text[:26] if "%f" in fmt else text[:20], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def build_rows(products: list[Product], supplies: dict[int, list[dict[str, Any]]], card_meta: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    today = datetime.now(timezone.utc).date()
    rows: list[dict[str, Any]] = []
    for product in products:
        if product.orders == 0 and product.stock == 0:
            continue
        meta = card_meta.get(product.nm_id, {})
        manager_tags = list(meta.get("managerTags") or [])
        manager_text = ", ".join(manager_tags) if manager_tags else "Без ярлыка"
        avg_daily_sales = product.orders / DAYS if DAYS else 0
        days_left = math.inf if avg_daily_sales <= 0 and product.stock > 0 else (product.stock / avg_daily_sales if avg_daily_sales > 0 else 0)
        item_supplies = sorted(supplies.get(product.nm_id, []), key=lambda x: x.get("date") or "9999-99-99")
        next_supply = item_supplies[0] if item_supplies else None
        next_supply_date = next_supply.get("date") if next_supply else ""
        days_to_supply = None
        if next_supply_date:
            supply_date = parse_date(next_supply_date)
            if supply_date:
                days_to_supply = max((supply_date - today).days, 0)

        counted_supplies: list[dict[str, Any]] = []
        for supply in item_supplies:
            supply_date = parse_date(str(supply.get("date") or ""))
            if not supply_date:
                continue
            supply_days = max((supply_date - today).days, 0)
            if math.isinf(days_left) or supply_days <= days_left:
                counted_supplies.append(supply)

        supply_qty = sum(as_int(x.get("quantity")) for x in counted_supplies)
        target_days = RESTOCK_DAYS_DEFAULT
        required_qty = max(math.ceil(avg_daily_sales * target_days - product.stock - supply_qty), 0)

        needs_restock = days_left < LOW_STOCK_DAYS
        gap_to_supply = days_to_supply is not None and days_left < days_to_supply
        status = "critical" if needs_restock and not counted_supplies else "warning" if needs_restock or gap_to_supply else "ok"

        rows.append({
            "status": status,
            "statusText": "Нужен догруз" if status == "critical" else "Проверить" if status == "warning" else "ОК",
            "managerTags": manager_tags,
            "managerText": manager_text,
            "vendorCode": product.vendor_code or meta.get("vendorCode") or "",
            "nmId": product.nm_id,
            "name": product.name or meta.get("title") or "",
            "category": product.category or meta.get("category") or "",
            "stock": product.stock,
            "orders14d": product.orders,
            "avgDailySales": round(avg_daily_sales, 2),
            "daysLeft": None if math.isinf(days_left) else round(days_left, 1),
            "nextSupplyDate": next_supply_date,
            "nextSupplyQty": supply_qty,
            "daysToSupply": days_to_supply,
            "requiredQty": required_qty,
        })
    rows.sort(key=lambda row: (0 if row["status"] == "critical" else 1 if row["status"] == "warning" else 2, row["daysLeft"] if row["daysLeft"] is not None else 999999))
    return rows


def main() -> None:
    token = os.getenv("WB_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Secret WB_API_TOKEN is empty")

    period_end = yesterday_utc()
    period_start = period_end - timedelta(days=DAYS - 1)
    warnings: list[str] = []

    products = fetch_products(token, period_start, period_end)
    stock_by_nm, stock_warnings = fetch_current_stocks(token)
    warnings.extend(stock_warnings)
    if stock_by_nm:
        for product in products:
            product.stock = stock_by_nm.get(product.nm_id, 0)
    card_meta, tag_warnings = fetch_card_tags(token)
    warnings.extend(tag_warnings)
    supplies, supply_warnings = fetch_supplies(token)
    warnings.extend(supply_warnings)
    rows = build_rows(products, supplies, card_meta)

    managers = sorted({tag for row in rows for tag in row.get("managerTags", [])})
    without_tags = sum(1 for row in rows if not row.get("managerTags"))
    total_required = sum(as_int(row.get("requiredQty")) for row in rows)
    critical = sum(1 for row in rows if row.get("status") == "critical")
    warning = sum(1 for row in rows if row.get("status") == "warning")

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "period": {"from": period_start.isoformat(), "to": period_end.isoformat(), "days": DAYS},
        "thresholds": {"lowStockDays": LOW_STOCK_DAYS, "restockDaysDefault": RESTOCK_DAYS_DEFAULT, "supplyLookaheadDays": SUPPLY_LOOKAHEAD_DAYS},
        "summary": {
            "totalItems": len(rows),
            "criticalItems": critical,
            "warningItems": warning,
            "okItems": len(rows) - critical - warning,
            "totalRequiredQty": total_required,
            "managerCount": len(managers),
            "withoutManagerTags": without_tags,
        },
        "managers": managers,
        "warnings": warnings,
        "rows": rows,
    }

    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {DATA_PATH} with {len(rows)} rows")


if __name__ == "__main__":
    main()
