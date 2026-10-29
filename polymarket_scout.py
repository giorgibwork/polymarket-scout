#!/usr/bin/env python3
"""Polymarket Scout v2: public-data market observations + conservative paper trading.

Read-only: no wallet, private keys, API credentials, or order-placement endpoints.
Python standard library only; designed for Android/Termux.

The strategy is a hypothesis, not proven profitable. It deliberately requires local
CLOB midpoint history before opening any position and models immediate taker execution
by walking visible CLOB levels. It does not use Gamma display prices as fill prices.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
APP_VERSION = "v2.0.1"
USER_AGENT = "PolymarketScout/2.0.1 (read-only public data; paper trading only)"
DEFAULT_DB = Path.home() / ".polymarket_scout" / "scout.sqlite3"
# Keep the ledger namespace stable across this hotfix so existing v2.0 paper
# positions continue to be managed. APP_VERSION tracks code release separately.
MODEL_VERSION = "v2.0"


def utc(ts: Optional[float] = None) -> str:
    value = time.time() if ts is None else ts
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def parse_ts(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (float, int)) or (isinstance(value, str) and value.strip().replace(".", "", 1).isdigit()):
            number = float(value)
            if number > 1e12:
                number /= 1000.0
            return number if number > 0 else None
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def number(value: Any, default: float = float("nan")) -> float:
    try:
        if value is None or value == "":
            return default
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def as_bool(value: Any) -> Optional[bool]:
    if value is True or value == 1:
        return True
    if value is False or value == 0:
        return False
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes"}:
            return True
        if lowered in {"false", "0", "no"}:
            return False
    return None


def parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, list) else []
        except json.JSONDecodeError:
            return []
    return []


def parse_json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def api_json(url: str, method: str = "GET", payload: Any = None, timeout: int = 20, retries: int = 3) -> Any:
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    last_error: Optional[Exception] = None
    for attempt in range(max(1, retries)):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = response.read()
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status} from {urllib.parse.urlsplit(url).netloc}")
                return json.loads(data.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read(500).decode("utf-8", errors="replace")
            last_error = RuntimeError(f"HTTP {exc.code} from {urllib.parse.urlsplit(url).netloc}: {detail}")
            if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == retries - 1:
                raise last_error from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = RuntimeError(f"API/network error for {urllib.parse.urlsplit(url).netloc}: {exc}")
            if attempt == retries - 1:
                raise last_error from exc
        time.sleep(min(2 ** attempt, 4))
    raise last_error or RuntimeError("API request failed")


def _markets_page(limit: int, cursor: Optional[str], order: str) -> tuple[list[dict[str, Any]], Optional[str]]:
    params: dict[str, Any] = {
        "active": "true", "closed": "false", "limit": min(max(int(limit), 1), 100),
        "order": order, "ascending": "false",
    }
    if cursor:
        params["after_cursor"] = cursor
    payload = api_json(f"{GAMMA}/markets/keyset?{urllib.parse.urlencode(params)}")
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)], None
    if not isinstance(payload, dict) or not isinstance(payload.get("markets"), list):
        raise RuntimeError("Unexpected Gamma keyset response shape")
    return [x for x in payload["markets"] if isinstance(x, dict)], payload.get("next_cursor")


def fetch_markets(limit: int = 100) -> list[dict[str, Any]]:
    """Fetch active markets with keyset paging, falling back safely if unavailable."""
    limit = min(max(int(limit), 1), 300)
    last_error: Optional[Exception] = None
    # Gamma keyset pages cap at 100. Try recent-volume ordering, then documented fields.
    for order in ("volume24hr", "volume_num,liquidity_num"):
        found: list[dict[str, Any]] = []
        cursor: Optional[str] = None
        try:
            while len(found) < limit:
                page, next_cursor = _markets_page(min(100, limit - len(found)), cursor, order)
                if not page:
                    break
                found.extend(page)
                if not next_cursor or next_cursor == cursor:
                    break
                cursor = str(next_cursor)
            # De-duplicate and sort by currently reported 24-hour volume after collection.
            unique: dict[str, dict[str, Any]] = {}
            for item in found:
                key = str(item.get("id") or item.get("conditionId") or item.get("slug") or "")
                if key:
                    unique[key] = item
            result = list(unique.values())
            result.sort(key=lambda m: number(m.get("volume24hr", m.get("volume24h", 0)), 0.0), reverse=True)
            if result:
                return result[:limit]
        except Exception as exc:
            last_error = exc
            # A 422 can mean the selected ordering field is not recognized; try next.
            continue

    # Compatibility fallback: standard offset endpoint, in 100-record pages.
    try:
        found = []
        for offset in range(0, limit, 100):
            params = urllib.parse.urlencode({
                "active": "true", "closed": "false", "limit": min(100, limit - offset),
                "offset": offset, "order": "volume24hr", "ascending": "false",
            })
            payload = api_json(f"{GAMMA}/markets?{params}")
            if not isinstance(payload, list):
                raise RuntimeError("Unexpected Gamma list response shape")
            found.extend(x for x in payload if isinstance(x, dict))
            if len(payload) < min(100, limit - offset):
                break
        unique = {}
        for item in found:
            key = str(item.get("id") or item.get("conditionId") or item.get("slug") or "")
            if key:
                unique[key] = item
        result = list(unique.values())
        result.sort(key=lambda m: number(m.get("volume24hr", m.get("volume24h", 0)), 0.0), reverse=True)
        return result[:limit]
    except Exception as exc:
        raise RuntimeError(f"Could not fetch Gamma markets. Keyset error: {last_error}; fallback error: {exc}") from exc


def fetch_market_by_id(market_id: str) -> Optional[dict[str, Any]]:
    if not str(market_id).isdigit():
        return None
    payload = api_json(f"{GAMMA}/markets/{urllib.parse.quote(str(market_id), safe='')}")
    if isinstance(payload, list):
        return payload[0] if payload and isinstance(payload[0], dict) else None
    return payload if isinstance(payload, dict) else None


def _nested_game_start(raw: dict[str, Any]) -> Optional[float]:
    candidates = [raw.get("gameStartTime")]
    sports = raw.get("sports")
    if isinstance(sports, dict):
        candidates.append(sports.get("gameStartTime"))
    events = raw.get("events")
    if isinstance(events, dict):
        candidates.append(events.get("gameStartTime"))
        candidates.append(events.get("startTime"))
    elif isinstance(events, list):
        for event in events[:3]:
            if isinstance(event, dict):
                candidates.extend([event.get("gameStartTime"), event.get("startTime"), event.get("eventStartTime")])
    for candidate in candidates:
        parsed = parse_ts(candidate)
        if parsed is not None:
            return parsed
    return None


def normalize_market(raw: dict[str, Any]) -> Optional[dict[str, Any]]:
    outcomes = parse_json_list(raw.get("outcomes"))
    prices = parse_json_list(raw.get("outcomePrices"))
    tokens = parse_json_list(raw.get("clobTokenIds"))
    if len(outcomes) != 2 or len(prices) != 2 or len(tokens) != 2:
        return None
    labels = {str(label).strip().lower(): i for i, label in enumerate(outcomes)}
    if set(labels) != {"yes", "no"}:
        return None
    yes_idx, no_idx = labels["yes"], labels["no"]
    yes_price = number(prices[yes_idx], -1.0)
    no_price = number(prices[no_idx], -1.0)
    if not (0.0 <= yes_price <= 1.0 and 0.0 <= no_price <= 1.0):
        return None
    market_id = str(raw.get("id") or "").strip()
    condition_id = str(raw.get("conditionId") or raw.get("condition_id") or "").strip()
    slug = str(raw.get("slug") or "")
    if not market_id or not str(tokens[yes_idx]).strip() or not str(tokens[no_idx]).strip():
        return None
    fee_schedule = parse_json_dict(raw.get("feeSchedule", raw.get("fee_schedule")))
    fee_enabled = as_bool(raw.get("feesEnabled", raw.get("fees_enabled")))
    fee_rate = number(fee_schedule.get("rate"), float("nan"))
    fee_exponent = number(fee_schedule.get("exponent"), 1.0)
    fee_taker_only = as_bool(fee_schedule.get("takerOnly", fee_schedule.get("taker_only")))
    category = str(raw.get("category") or "")
    tags = raw.get("tags") or []
    tag_text = " ".join(
        str(x.get("slug", "") if isinstance(x, dict) else x)
        + " " + str(x.get("label", x.get("name", "")) if isinstance(x, dict) else "")
        for x in tags
    ) if isinstance(tags, list) else str(tags)
    sport_type = str(raw.get("sportsMarketType") or raw.get("sports_market_type") or "")
    is_sports = bool(sport_type) or "sport" in category.lower() or "sport" in tag_text.lower()
    game_start_ts = _nested_game_start(raw)
    if is_sports and game_start_ts is None:
        game_start_ts = parse_ts(raw.get("eventStartTime"))
    return {
        "market_id": market_id,
        "condition_id": condition_id,
        "question": str(raw.get("question") or raw.get("title") or "Untitled market").strip(),
        "slug": slug,
        "url": f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com/",
        "yes_token_id": str(tokens[yes_idx]),
        "no_token_id": str(tokens[no_idx]),
        "yes_price_gamma": yes_price,
        "no_price_gamma": no_price,
        "volume24h": number(raw.get("volume24hr", raw.get("volume24h", 0)), 0.0),
        "liquidity": number(raw.get("liquidityClob", raw.get("liquidity", 0)), 0.0),
        "active": as_bool(raw.get("active")),
        "closed": as_bool(raw.get("closed")),
        "accepting_orders": as_bool(raw.get("acceptingOrders", raw.get("accepting_orders"))),
        "enable_order_book": as_bool(raw.get("enableOrderBook", raw.get("enable_order_book"))),
        "archived": as_bool(raw.get("archived")),
        "restricted": as_bool(raw.get("restricted")),
        "end_ts": parse_ts(raw.get("endDate", raw.get("end_date"))),
        "game_start_ts": game_start_ts,
        "is_sports": is_sports,
        "category": category,
        "fees_enabled": fee_enabled,
        "fee_rate": fee_rate,
        "fee_exponent": fee_exponent,
        "fee_taker_only": fee_taker_only,
        "resolution_status": str(raw.get("umaResolutionStatuses") or raw.get("uma_resolution_status") or raw.get("resolutionStatus") or ""),
        "raw_hash": hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest(),
        "raw": raw,
    }


def market_trade_ready(m: dict[str, Any]) -> tuple[bool, str]:
    # Fail closed: unknown acceptingOrders is not sufficient proof that a book is open.
    # Gamma's "restricted" field indicates SOME jurisdictions may be restricted,
    # not that ALL are blocked. Do not auto-reject; the official geographic-check
    # endpoint is separate. Keep explicit checks for fields that directly control tradability.
    if m.get("active") is not True:
        return False, "market_not_active_or_status_unknown"
    if m.get("closed") is not False:
        return False, "market_closed_or_status_unknown"
    if m.get("accepting_orders") is not True:
        return False, "not_accepting_orders_or_unknown"
    if m.get("enable_order_book") is False:
        return False, "order_book_disabled"
    if m.get("archived") is True:
        return False, "archived"
    # "restricted" is informational; not a trade-readiness gate.
    return True, "ready"


def _parse_levels(value: Any, reverse: bool) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    if not isinstance(value, list):
        return result
    for level in value:
        if not isinstance(level, dict):
            continue
        price = number(level.get("price"), -1.0)
        size = number(level.get("size"), 0.0)
        if 0.0 < price < 1.0 and size > 0:
            result.append((price, size))
    result.sort(key=lambda x: x[0], reverse=reverse)
    return result


def normalize_book(raw: dict[str, Any], captured_ts: Optional[float] = None) -> dict[str, Any]:
    ts = parse_ts(raw.get("timestamp", raw.get("ts")))
    bids = _parse_levels(raw.get("bids"), reverse=True)
    asks = _parse_levels(raw.get("asks"), reverse=False)
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    mid = (best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None else None
    spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
    return {
        "token_id": str(raw.get("asset_id") or raw.get("token_id") or raw.get("assetId") or ""),
        "book_ts": ts,
        "captured_ts": captured_ts if captured_ts is not None else time.time(),
        "bids": bids,
        "asks": asks,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread": spread,
        "bid_depth_usd_3": sum(p * q for p, q in bids[:3]),
        "ask_depth_usd_3": sum(p * q for p, q in asks[:3]),
        "tick_size": number(raw.get("tick_size", raw.get("tickSize")), float("nan")),
        "min_order_size": number(raw.get("min_order_size", raw.get("minOrderSize")), float("nan")),
        "last_trade_price": number(raw.get("last_trade_price", raw.get("lastTradePrice")), float("nan")),
        "hash": str(raw.get("hash") or ""),
    }


def fetch_books(token_ids: list[str]) -> dict[str, dict[str, Any]]:
    clean = list(dict.fromkeys(str(t) for t in token_ids if t))
    result: dict[str, dict[str, Any]] = {}
    for start in range(0, len(clean), 400):
        chunk = clean[start:start + 400]
        payload = api_json(f"{CLOB}/books", method="POST", payload=[{"token_id": token} for token in chunk])
        if not isinstance(payload, list):
            raise RuntimeError("Unexpected CLOB /books response: expected a list")
        for raw in payload:
            if isinstance(raw, dict):
                book = normalize_book(raw)
                if book["token_id"]:
                    result[book["token_id"]] = book
    return result


def book_is_fresh(book: Optional[dict[str, Any]], now: float, max_age_seconds: int) -> bool:
    if not book or book.get("book_ts") is None:
        return False
    age = now - float(book["book_ts"])
    return -30 <= age <= max_age_seconds


def fee_for_fill(shares: float, price: float, rate: float, exponent: float = 1.0) -> float:
    """Use the current official documented fee formula per fill.

    Current Polymarket fee docs specify C * feeRate * p * (1-p). Gamma still
    exposes feeSchedule.exponent, so we preserve that field for audit metadata,
    but do not apply it to the formula unless current official docs change.
    """
    del exponent  # retained in the signature to preserve recorded market metadata
    if shares <= 0 or rate <= 0:
        return 0.0
    p = min(1.0, max(0.0, price))
    return shares * rate * p * (1.0 - p)


def walk_buy_budget(book: dict[str, Any], budget: float, rate: float, exponent: float) -> dict[str, Any]:
    remaining = budget
    shares = spent = fees = 0.0
    fills: list[dict[str, float]] = []
    for price, available in book.get("asks", []):
        if remaining <= 1e-8:
            break
        qty = min(available, remaining / price)
        if qty <= 0:
            continue
        notional = qty * price
        fee = fee_for_fill(qty, price, rate, exponent)
        shares += qty
        spent += notional
        fees += fee
        remaining -= notional
        fills.append({"price": price, "shares": qty, "notional": notional, "fee": fee})
    return {"shares": shares, "spent": spent, "fee": fees, "remaining_budget": max(0.0, remaining), "fill_fraction": spent / budget if budget > 0 else 0.0, "fills": fills}


def walk_sell_shares(book: dict[str, Any], target_shares: float, rate: float, exponent: float) -> dict[str, Any]:
    remaining = target_shares
    filled = gross = fees = 0.0
    fills: list[dict[str, float]] = []
    for price, available in book.get("bids", []):
        if remaining <= 1e-8:
            break
        qty = min(available, remaining)
        if qty <= 0:
            continue
        proceeds = qty * price
        fee = fee_for_fill(qty, price, rate, exponent)
        filled += qty
        gross += proceeds
        fees += fee
        remaining -= qty
        fills.append({"price": price, "shares": qty, "gross": proceeds, "fee": fee})
    return {"shares": filled, "gross": gross, "fee": fees, "remaining_shares": max(0.0, remaining), "fill_fraction": filled / target_shares if target_shares > 0 else 0.0, "fills": fills}


def _safe_int_bool(value: Any) -> int:
    return 1 if value is True else 0 if value is False else -1


class Scout:
    def __init__(self, db_path: Path, args: argparse.Namespace):
        self.db_path = db_path
        self.args = args
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.db_path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        self._init_db()

    def _init_db(self) -> None:
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS snapshots (
            ts INTEGER NOT NULL, market_id TEXT NOT NULL, question TEXT NOT NULL, slug TEXT,
            yes_price REAL, no_price REAL, volume24h REAL NOT NULL DEFAULT 0,
            liquidity REAL NOT NULL DEFAULT 0, change1h REAL, change24h REAL, end_date TEXT,
            PRIMARY KEY (ts, market_id)
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_market_ts ON snapshots(market_id, ts);
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL, question TEXT NOT NULL, slug TEXT, side TEXT NOT NULL,
            entry_ts INTEGER NOT NULL, entry_price REAL NOT NULL, stake REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'OPEN', exit_ts INTEGER, exit_price REAL, pnl REAL,
            exit_reason TEXT, signal_change REAL
        );
        CREATE INDEX IF NOT EXISTS idx_trades_status ON paper_trades(status);
        CREATE INDEX IF NOT EXISTS idx_trades_market_ts ON paper_trades(market_id, entry_ts);
        CREATE TABLE IF NOT EXISTS quote_snapshots (
            ts INTEGER NOT NULL, market_id TEXT NOT NULL, condition_id TEXT, token_id_yes TEXT,
            token_id_no TEXT, question TEXT, category TEXT, volume24h REAL, liquidity REAL,
            yes_bid REAL, yes_ask REAL, yes_mid REAL, yes_spread REAL,
            no_bid REAL, no_ask REAL, no_mid REAL, no_spread REAL,
            yes_bid_depth3 REAL, yes_ask_depth3 REAL, no_bid_depth3 REAL, no_ask_depth3 REAL,
            yes_book_ts REAL, no_book_ts REAL, yes_hash TEXT, no_hash TEXT,
            active INTEGER, closed INTEGER, accepting_orders INTEGER,
            end_ts REAL, game_start_ts REAL, fees_enabled INTEGER, fee_rate REAL,
            fee_exponent REAL, raw_hash TEXT, PRIMARY KEY (ts, market_id)
        );
        CREATE INDEX IF NOT EXISTS idx_quotes_market_ts ON quote_snapshots(market_id, ts);
        CREATE TABLE IF NOT EXISTS scan_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, elapsed REAL,
            fetched INTEGER, parsed INTEGER, book_count INTEGER, warm_count INTEGER,
            qualified INTEGER, opened INTEGER, managed INTEGER, rejected TEXT,
            config_hash TEXT, status TEXT, error TEXT
        );
        CREATE TABLE IF NOT EXISTS signal_log (
            ts INTEGER NOT NULL, market_id TEXT NOT NULL, signal_change REAL,
            baseline_mid REAL, current_mid REAL, baseline_age_seconds INTEGER,
            spread REAL, eligible INTEGER NOT NULL, reason TEXT,
            PRIMARY KEY (ts, market_id)
        );
        """)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(paper_trades)").fetchall()}
        additions = {
            "model_version": "TEXT", "condition_id": "TEXT", "token_id": "TEXT",
            "shares": "REAL", "remaining_shares": "REAL", "entry_spend": "REAL",
            "entry_fee": "REAL", "remaining_entry_cost": "REAL", "remaining_entry_fee": "REAL",
            "entry_bid": "REAL", "entry_ask": "REAL", "entry_mid": "REAL", "entry_spread": "REAL",
            "exit_gross": "REAL", "exit_fee": "REAL", "exit_shares": "REAL",
            "fee_rate": "REAL", "fee_exponent": "REAL", "entry_fills_json": "TEXT",
            "exit_fills_json": "TEXT", "entry_book_hash": "TEXT", "exit_book_hash": "TEXT",
            "realized_partial_pnl": "REAL", "config_json": "TEXT",
        }
        for name, sql_type in additions.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {sql_type}")
        existing_version = self.db.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()
        if not existing_version or existing_version["value"] != "2":
            # Legacy entries used Gamma displayed prices and flat friction; preserve them but exclude them from v2 metrics.
            legacy_rows = self.db.execute("SELECT id, status, exit_reason FROM paper_trades WHERE model_version IS NULL OR model_version='' ").fetchall()
            for row in legacy_rows:
                old_status = str(row["status"] or "")
                if old_status == "OPEN":
                    new_status = "LEGACY"
                elif old_status == "CLOSED":
                    new_status = "LEGACY_CLOSED"
                elif old_status == "UNRESOLVED":
                    new_status = "LEGACY_UNRESOLVED"
                else:
                    new_status = "LEGACY_" + (old_status or "UNKNOWN")
                reason = str(row["exit_reason"] or "").strip()
                note = "Legacy pre-v2 position; Gamma displayed price/flat-cost model is not comparable to v2 executable-book simulation and is excluded from v2 P&L."
                merged = (reason + " | " + note).strip(" |")
                self.db.execute("UPDATE paper_trades SET status=?, model_version='legacy-v0', exit_reason=? WHERE id=?", (new_status, merged, int(row["id"])))
            self.db.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('schema_version','2')")
            self.db.execute("INSERT OR REPLACE INTO app_meta(key,value) VALUES('migration_note','legacy trades preserved and excluded from v2 metrics')")
        self.db.commit()

    def _snapshot_quotes(self, markets: list[dict[str, Any]], books: dict[str, dict[str, Any]], now: int) -> tuple[int, dict[str, dict[str, Any]]]:
        count = 0
        by_id = {m["market_id"]: m for m in markets}
        for m in markets:
            raw_y = books.get(m["yes_token_id"], {})
            raw_n = books.get(m["no_token_id"], {})
            # Do not let stale/incomplete book data become historical signal inputs.
            y = raw_y if book_is_fresh(raw_y, now, self.args.max_book_age_seconds) else {}
            n = raw_n if book_is_fresh(raw_n, now, self.args.max_book_age_seconds) else {}
            vals = (
                now, m["market_id"], m["condition_id"], m["yes_token_id"], m["no_token_id"], m["question"], m["category"],
                m["volume24h"], m["liquidity"], y.get("best_bid"), y.get("best_ask"), y.get("mid"), y.get("spread"),
                n.get("best_bid"), n.get("best_ask"), n.get("mid"), n.get("spread"),
                y.get("bid_depth_usd_3"), y.get("ask_depth_usd_3"), n.get("bid_depth_usd_3"), n.get("ask_depth_usd_3"),
                y.get("book_ts"), n.get("book_ts"), y.get("hash"), n.get("hash"),
                _safe_int_bool(m.get("active")), _safe_int_bool(m.get("closed")), _safe_int_bool(m.get("accepting_orders")),
                m.get("end_ts"), m.get("game_start_ts"), _safe_int_bool(m.get("fees_enabled")),
                m.get("fee_rate") if math.isfinite(m.get("fee_rate", float("nan"))) else None,
                m.get("fee_exponent") if math.isfinite(m.get("fee_exponent", float("nan"))) else None, m["raw_hash"],
            )
            self.db.execute("""INSERT OR REPLACE INTO quote_snapshots
            (ts,market_id,condition_id,token_id_yes,token_id_no,question,category,volume24h,liquidity,
             yes_bid,yes_ask,yes_mid,yes_spread,no_bid,no_ask,no_mid,no_spread,
             yes_bid_depth3,yes_ask_depth3,no_bid_depth3,no_ask_depth3,yes_book_ts,no_book_ts,yes_hash,no_hash,
             active,closed,accepting_orders,end_ts,game_start_ts,fees_enabled,fee_rate,fee_exponent,raw_hash)
            VALUES (""" + ",".join("?" for _ in vals) + ")", vals)
            count += 1
        self.db.commit()
        return count, by_id

    def _historical_mid(self, market_id: str, now: int) -> tuple[Optional[float], Optional[int]]:
        lookback = int(float(self.args.lookback_hours) * 3600)
        target = now - lookback
        row = self.db.execute("SELECT ts, yes_mid FROM quote_snapshots WHERE market_id=? AND ts<=? AND yes_mid IS NOT NULL ORDER BY ts DESC LIMIT 1", (market_id, target)).fetchone()
        if row is None:
            return None, None
        age = now - int(row["ts"])
        if age < lookback or age > lookback + int(self.args.max_history_lag_minutes * 60):
            return None, age
        return float(row["yes_mid"]), age

    def _market_risk_gate(self, m: dict[str, Any], now: int) -> tuple[bool, str]:
        ready, reason = market_trade_ready(m)
        if not ready:
            return False, reason
        if m["volume24h"] < self.args.min_volume:
            return False, "volume_below_threshold"
        if m["liquidity"] < self.args.min_liquidity:
            return False, "liquidity_below_threshold"
        if m["is_sports"]:
            if m["game_start_ts"] is None:
                return False, "sports_start_time_unknown"
            if m["game_start_ts"] <= now + int(self.args.hold_hours * 3600):
                return False, "sports_event_starts_or_has_started_within_hold_window"
        if m["end_ts"] is None:
            return False, "end_date_unknown"
        if m["end_ts"] <= now + int(self.args.hold_hours * 3600):
            return False, "end_date_within_hold_window_resolution_risk"
        if m["fees_enabled"] is None:
            return False, "fee_enabled_status_unknown"
        if m["fees_enabled"] is True:
            if not math.isfinite(m["fee_rate"]) or not (0 <= m["fee_rate"] <= 1):
                return False, "fee_schedule_missing_or_invalid"
            # The current official fee formula does not use Gamma's exponent field.
            # Do not reject otherwise valid markets merely because this diagnostic
            # metadata is fractional or absent; fee_exponent defaults to 1.0.
        return True, "eligible_for_signal_test"

    def _latest_v2_trade(self, market_id: str) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM paper_trades WHERE market_id=? AND model_version=? ORDER BY entry_ts DESC LIMIT 1", (market_id, MODEL_VERSION)).fetchone()

    def _cooldown_ok(self, market_id: str, now: int) -> bool:
        row = self._latest_v2_trade(market_id)
        if row is None:
            return True
        if row["status"] in ("OPEN", "UNRESOLVED"):
            return False
        reference = int(row["exit_ts"] or row["entry_ts"])
        return now - reference >= int(self.args.cooldown_hours * 3600)

    def _append_json(self, current: Optional[str], payload: list[dict[str, Any]]) -> str:
        existing = parse_json_list(current)
        existing.extend(payload)
        return json.dumps(existing, separators=(",", ":"))

    def _settled_outcome_price(self, m: dict[str, Any], side: str) -> Optional[float]:
        # A close flag alone is not proof of final resolution. Accept only an exact
        # terminal status token (or explicit resolved boolean); substring matching
        # is unsafe because "unresolved" contains the word "resolved".
        raw = m.get("raw", {})
        status_values: list[Any] = []
        for key in ("umaResolutionStatuses", "umaResolutionStatus", "uma_resolution_status",
                    "resolutionStatus", "resolution_status"):
            value = raw.get(key)
            if isinstance(value, list):
                status_values.extend(value)
            elif isinstance(value, str):
                decoded = parse_json_list(value)
                status_values.extend(decoded if decoded else [value])
        terminal_statuses = {"resolved", "final", "finalized", "settled"}
        status_tokens = {str(value).strip().lower().replace("_", " ").replace("-", " ") for value in status_values}
        is_resolved = bool(status_tokens & terminal_statuses) or as_bool(raw.get("resolved")) is True or as_bool(raw.get("isResolved")) is True
        if not is_resolved or m.get("closed") is not True:
            return None
        prices = parse_json_list(raw.get("outcomePrices"))
        outcomes = parse_json_list(raw.get("outcomes"))
        if len(prices) != 2 or len(outcomes) != 2:
            return None
        labels = {str(label).strip().lower(): i for i, label in enumerate(outcomes)}
        idx = labels.get(side.lower())
        if idx is None:
            return None
        price = number(prices[idx], float("nan"))
        if price >= 0.999:
            return 1.0
        if price <= 0.001:
            return 0.0
        return None

    def _settle_trade(self, trade: sqlite3.Row, outcome_price: float, now: int) -> None:
        rem = number(trade["remaining_shares"], 0.0)
        remaining_cost = number(trade["remaining_entry_cost"], 0.0)
        remaining_fee = number(trade["remaining_entry_fee"], 0.0)
        payout = rem * outcome_price
        pnl_add = payout - remaining_cost - remaining_fee
        exit_shares = number(trade["exit_shares"], 0.0) + rem
        exit_gross = number(trade["exit_gross"], 0.0) + payout
        pnl_total = number(trade["pnl"], 0.0) + pnl_add
        avg_exit = exit_gross / exit_shares if exit_shares > 0 else outcome_price
        self.db.execute("""UPDATE paper_trades SET status='CLOSED', exit_ts=?, exit_price=?, exit_gross=?, exit_shares=?,
            pnl=?, remaining_shares=0, remaining_entry_cost=0, remaining_entry_fee=0, exit_reason=? WHERE id=?""",
            (now, avg_exit, exit_gross, exit_shares, pnl_total, f"settled from resolved market outcome at {outcome_price:.0f}", int(trade["id"])))

    def _manage_positions(self, by_id: dict[str, dict[str, Any]], books: dict[str, dict[str, Any]], now: int) -> tuple[int, int, list[str]]:
        rows = self.db.execute("SELECT * FROM paper_trades WHERE model_version=? AND status IN ('OPEN','UNRESOLVED') ORDER BY entry_ts", (MODEL_VERSION,)).fetchall()
        managed = unresolved = 0
        notes: list[str] = []
        for trade in rows:
            m = by_id.get(str(trade["market_id"]))
            if m is None:
                try:
                    raw = fetch_market_by_id(str(trade["market_id"]))
                    m = normalize_market(raw) if raw else None
                    if m:
                        by_id[m["market_id"]] = m
                except Exception:
                    # Never infer expiry from absence in the volume-ranked discovery page.
                    m = None
            if m and m.get("closed") is True:
                settled = self._settled_outcome_price(m, str(trade["side"]))
                if settled is not None:
                    self._settle_trade(trade, settled, now)
                    managed += 1
                    notes.append(f"#{trade['id']} resolved at {settled:.0f}")
                else:
                    self.db.execute("UPDATE paper_trades SET status='UNRESOLVED', exit_reason=? WHERE id=?",
                        ("market closed; final settlement not positively verified; excluded from closed P&L", int(trade["id"])))
                    unresolved += 1
                continue
            if m and (m.get("active") is False or m.get("accepting_orders") is False):
                self.db.execute("UPDATE paper_trades SET status='UNRESOLVED', exit_reason=? WHERE id=?",
                    ("market is no longer accepting orders and no verified final settlement was available", int(trade["id"])))
                unresolved += 1
                continue
            held = now - int(trade["entry_ts"])
            if held < int(self.args.hold_hours * 3600):
                # If previously unresolved because data was temporarily stale, restore only when tradability is verified.
                if trade["status"] == "UNRESOLVED" and m and market_trade_ready(m)[0]:
                    self.db.execute("UPDATE paper_trades SET status='OPEN', exit_reason=NULL WHERE id=?", (int(trade["id"]),))
                continue
            token = str(trade["token_id"] or "")
            book = books.get(token)
            if not book_is_fresh(book, now, self.args.max_book_age_seconds):
                notes.append(f"#{trade['id']} exit pending: no fresh book")
                continue
            if m and not market_trade_ready(m)[0]:
                self.db.execute("UPDATE paper_trades SET status='UNRESOLVED', exit_reason=? WHERE id=?",
                    ("market not trade-ready; no verified final settlement", int(trade["id"])))
                unresolved += 1
                continue
            remaining = number(trade["remaining_shares"], 0.0)
            if remaining <= 1e-8:
                self.db.execute("UPDATE paper_trades SET status='CLOSED', exit_ts=? WHERE id=?", (now, int(trade["id"])))
                managed += 1
                continue
            rate = number(trade["fee_rate"], 0.0)
            exponent = number(trade["fee_exponent"], 1.0)
            execution = walk_sell_shares(book, remaining, rate, exponent)
            if execution["shares"] <= 1e-8:
                notes.append(f"#{trade['id']} exit pending: no visible bids")
                continue
            fraction = execution["shares"] / remaining
            entry_cost_alloc = number(trade["remaining_entry_cost"], 0.0) * fraction
            entry_fee_alloc = number(trade["remaining_entry_fee"], 0.0) * fraction
            pnl_add = execution["gross"] - execution["fee"] - entry_cost_alloc - entry_fee_alloc
            new_rem = max(0.0, remaining - execution["shares"])
            new_cost = max(0.0, number(trade["remaining_entry_cost"], 0.0) - entry_cost_alloc)
            new_entry_fee = max(0.0, number(trade["remaining_entry_fee"], 0.0) - entry_fee_alloc)
            new_pnl = number(trade["pnl"], 0.0) + pnl_add
            new_exit_gross = number(trade["exit_gross"], 0.0) + execution["gross"]
            new_exit_fee = number(trade["exit_fee"], 0.0) + execution["fee"]
            new_exit_shares = number(trade["exit_shares"], 0.0) + execution["shares"]
            avg_exit = new_exit_gross / new_exit_shares if new_exit_shares else None
            old_fills = parse_json_list(trade["exit_fills_json"])
            old_fills.extend(execution["fills"])
            done = new_rem <= max(1e-8, number(book.get("min_order_size"), 0.0) * 0.00001)
            self.db.execute("""UPDATE paper_trades SET status=?, exit_ts=?, exit_price=?, exit_gross=?, exit_fee=?, exit_shares=?,
                pnl=?, remaining_shares=?, remaining_entry_cost=?, remaining_entry_fee=?, exit_fills_json=?, exit_book_hash=?,
                exit_reason=? WHERE id=?""", (
                "CLOSED" if done else "OPEN", now if done else None, avg_exit, new_exit_gross, new_exit_fee, new_exit_shares,
                new_pnl, 0.0 if done else new_rem, 0.0 if done else new_cost, 0.0 if done else new_entry_fee,
                json.dumps(old_fills, separators=(",", ":")), book.get("hash", ""),
                f"held {held/3600:.2f}h; visible-bid book walk" if done else f"partial exit {execution['shares']:.4f}; waiting for more visible bids",
                int(trade["id"])))
            managed += 1
            if not done:
                notes.append(f"#{trade['id']} partial exit; {new_rem:.4f} shares remain")
        self.db.commit()
        return managed, unresolved, notes

    def _entry_candidates(self, markets: list[dict[str, Any]], books: dict[str, dict[str, Any]], now: int) -> tuple[list[dict[str, Any]], dict[str, int]]:
        counters: dict[str, int] = {}
        candidates: list[dict[str, Any]] = []
        for m in markets:
            yes_book = books.get(m["yes_token_id"])
            no_book = books.get(m["no_token_id"])
            reason = ""
            eligible, reason = self._market_risk_gate(m, now)
            if not eligible:
                counters[reason] = counters.get(reason, 0) + 1
                self.db.execute("INSERT OR REPLACE INTO signal_log(ts,market_id,eligible,reason) VALUES(?,?,0,?)", (now, m["market_id"], reason))
                continue
            if not book_is_fresh(yes_book, now, self.args.max_book_age_seconds) or yes_book.get("mid") is None:
                reason = "yes_book_missing_stale_or_one_sided"
                counters[reason] = counters.get(reason, 0) + 1
                self.db.execute("INSERT OR REPLACE INTO signal_log(ts,market_id,eligible,reason) VALUES(?,?,0,?)", (now, m["market_id"], reason))
                continue
            if yes_book["spread"] is None or yes_book["spread"] > self.args.max_spread:
                reason = "yes_spread_above_threshold"
                counters[reason] = counters.get(reason, 0) + 1
                self.db.execute("INSERT OR REPLACE INTO signal_log(ts,market_id,eligible,reason,spread) VALUES(?,?,0,?,?)", (now, m["market_id"], reason, yes_book.get("spread")))
                continue
            baseline, age = self._historical_mid(m["market_id"], now)
            if baseline is None:
                reason = "warmup_or_history_gap"
                counters[reason] = counters.get(reason, 0) + 1
                self.db.execute("INSERT OR REPLACE INTO signal_log(ts,market_id,eligible,reason,current_mid,baseline_age_seconds,spread) VALUES(?,?,0,?,?,?,?)",
                    (now, m["market_id"], reason, yes_book["mid"], age, yes_book["spread"]))
                continue
            change = float(yes_book["mid"]) - baseline
            if abs(change) < self.args.min_move:
                reason = "movement_below_threshold"
                counters[reason] = counters.get(reason, 0) + 1
                self.db.execute("INSERT OR REPLACE INTO signal_log(ts,market_id,signal_change,baseline_mid,current_mid,baseline_age_seconds,spread,eligible,reason) VALUES(?,?,?,?,?,?,?,?,?)",
                    (now, m["market_id"], change, baseline, yes_book["mid"], age, yes_book["spread"], 0, reason))
                continue
            side = "YES" if change > 0 else "NO"
            token = m["yes_token_id"] if side == "YES" else m["no_token_id"]
            entry_book = yes_book if side == "YES" else no_book
            if not book_is_fresh(entry_book, now, self.args.max_book_age_seconds) or entry_book.get("best_ask") is None or entry_book.get("best_bid") is None:
                reason = "selected_token_book_missing_or_stale"
                counters[reason] = counters.get(reason, 0) + 1
                self.db.execute("INSERT OR REPLACE INTO signal_log(ts,market_id,signal_change,baseline_mid,current_mid,baseline_age_seconds,eligible,reason) VALUES(?,?,?,?,?,?,0,?)",
                    (now, m["market_id"], change, baseline, yes_book["mid"], age, reason))
                continue
            if entry_book["spread"] is None or entry_book["spread"] > self.args.max_spread:
                reason = "selected_token_spread_above_threshold"
                counters[reason] = counters.get(reason, 0) + 1
                continue
            entry_mid = entry_book.get("mid")
            if entry_mid is None or not (self.args.min_contract_price <= entry_mid <= self.args.max_contract_price):
                reason = "selected_token_mid_outside_price_bounds"
                counters[reason] = counters.get(reason, 0) + 1
                continue
            if not self._cooldown_ok(m["market_id"], now):
                reason = "cooldown_or_existing_position"
                counters[reason] = counters.get(reason, 0) + 1
                continue
            fee_rate = float(m["fee_rate"]) if m["fees_enabled"] is True else 0.0
            fee_exp = float(m["fee_exponent"]) if m["fees_enabled"] is True else 1.0
            execution = walk_buy_budget(entry_book, self.args.stake, fee_rate, fee_exp)
            if execution["fill_fraction"] < self.args.min_entry_fill_fraction or execution["shares"] <= 0:
                reason = "insufficient_visible_ask_depth_for_stake"
                counters[reason] = counters.get(reason, 0) + 1
                continue
            min_size = number(entry_book.get("min_order_size"), 0.0)
            if min_size > 0 and execution["shares"] < min_size:
                reason = "below_minimum_order_size"
                counters[reason] = counters.get(reason, 0) + 1
                continue
            score = abs(change)
            candidate = {"market": m, "side": side, "token_id": token, "entry_book": entry_book,
                "signal_change": change, "baseline_mid": baseline, "baseline_age": age,
                "execution": execution, "fee_rate": fee_rate, "fee_exponent": fee_exp, "score": score}
            candidates.append(candidate)
            self.db.execute("INSERT OR REPLACE INTO signal_log(ts,market_id,signal_change,baseline_mid,current_mid,baseline_age_seconds,spread,eligible,reason) VALUES(?,?,?,?,?,?,?,?,?)",
                (now, m["market_id"], change, baseline, yes_book["mid"], age, yes_book["spread"], 1, "qualified_for_paper_entry"))
        self.db.commit()
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates, counters

    def _open_candidates(self, candidates: list[dict[str, Any]], now: int) -> list[dict[str, Any]]:
        open_count = self.db.execute("SELECT COUNT(*) FROM paper_trades WHERE model_version=? AND status IN ('OPEN','UNRESOLVED')", (MODEL_VERSION,)).fetchone()[0]
        capacity = max(0, int(self.args.max_open) - int(open_count))
        opened: list[dict[str, Any]] = []
        config = {
            "model_version": MODEL_VERSION, "lookback_hours": self.args.lookback_hours,
            "min_move": self.args.min_move, "min_volume": self.args.min_volume,
            "min_liquidity": self.args.min_liquidity, "stake": self.args.stake,
            "hold_hours": self.args.hold_hours, "max_spread": self.args.max_spread,
            "fee_model": "market feeSchedule; current official formula shares*rate*p*(1-p) per taker fill; exponent stored for audit only",
            "entry_model": "walk visible asks", "exit_model": "walk visible bids, partial fills allowed",
        }
        config_json = json.dumps(config, sort_keys=True, separators=(",", ":"))
        for c in candidates[:capacity]:
            m, book, ex = c["market"], c["entry_book"], c["execution"]
            spent, shares, fee = ex["spent"], ex["shares"], ex["fee"]
            if shares <= 0 or spent <= 0:
                continue
            avg_price = spent / shares
            cur = self.db.execute("""INSERT INTO paper_trades
            (market_id,question,slug,side,entry_ts,entry_price,stake,status,signal_change,model_version,
             condition_id,token_id,shares,remaining_shares,entry_spend,entry_fee,remaining_entry_cost,remaining_entry_fee,
             entry_bid,entry_ask,entry_mid,entry_spread,fee_rate,fee_exponent,entry_fills_json,exit_fills_json,
             entry_book_hash,realized_partial_pnl,config_json,pnl)
            VALUES (?,?,?,?,?,?,?,'OPEN',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""", (
                m["market_id"], m["question"], m["slug"], c["side"], now, avg_price, self.args.stake,
                c["signal_change"], MODEL_VERSION, m["condition_id"], c["token_id"], shares, shares,
                spent, fee, spent, fee, book.get("best_bid"), book.get("best_ask"), book.get("mid"), book.get("spread"),
                c["fee_rate"], c["fee_exponent"], json.dumps(ex["fills"], separators=(",", ":")), "[]",
                book.get("hash"), 0.0, config_json))
            opened.append({"id": cur.lastrowid, **c, "avg_price": avg_price})
        self.db.commit()
        return opened

    def scan_once(self) -> None:
        started = time.time()
        now = int(time.time())
        rejected: dict[str, int] = {}
        raw_markets = fetch_markets(self.args.limit)
        markets: list[dict[str, Any]] = []
        for raw in raw_markets:
            m = normalize_market(raw)
            if m is not None:
                markets.append(m)
        # Save and inspect active paper positions even if a market falls out of the discovery page.
        open_rows = self.db.execute("SELECT token_id FROM paper_trades WHERE model_version=? AND status IN ('OPEN','UNRESOLVED') AND token_id IS NOT NULL", (MODEL_VERSION,)).fetchall()
        tokens = []
        for m in markets:
            tokens.extend([m["yes_token_id"], m["no_token_id"]])
        tokens.extend(str(r["token_id"]) for r in open_rows if r["token_id"])
        books = fetch_books(tokens) if tokens else {}
        snap_count, by_id = self._snapshot_quotes(markets, books, now)
        managed, unresolved, manage_notes = self._manage_positions(by_id, books, now)
        candidates, rejected = self._entry_candidates(markets, books, now)
        opened = self._open_candidates(candidates, now)
        elapsed = time.time() - started
        config_for_hash = json.dumps({k: getattr(self.args, k, None) for k in ("limit", "lookback_hours", "max_history_lag_minutes", "min_move", "min_volume", "min_liquidity", "stake", "max_open", "hold_hours", "max_spread")}, sort_keys=True)
        config_hash = hashlib.sha256(config_for_hash.encode()).hexdigest()[:16]
        self.db.execute("INSERT INTO scan_runs(ts,elapsed,fetched,parsed,book_count,warm_count,qualified,opened,managed,rejected,config_hash,status,error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, elapsed, len(raw_markets), len(markets), len(books), self.db.execute("SELECT COUNT(*) FROM signal_log WHERE ts=? AND reason='warmup_or_history_gap'", (now,)).fetchone()[0], len(candidates), len(opened), managed, json.dumps(rejected, sort_keys=True), config_hash, "OK", None))
        cutoff = now - int(self.args.snapshot_retention_days * 86400)
        self.db.execute("DELETE FROM quote_snapshots WHERE ts<?", (cutoff,))
        # Signal diagnostics older than the retention window are similarly bounded.
        self.db.execute("DELETE FROM signal_log WHERE ts<?", (cutoff,))
        self.db.commit()

        v2_closed = self.db.execute("SELECT COUNT(*) FROM paper_trades WHERE model_version=? AND status='CLOSED'", (MODEL_VERSION,)).fetchone()[0]
        v2_open = self.db.execute("SELECT COUNT(*) FROM paper_trades WHERE model_version=? AND status='OPEN'", (MODEL_VERSION,)).fetchone()[0]
        v2_unresolved = self.db.execute("SELECT COUNT(*) FROM paper_trades WHERE model_version=? AND status='UNRESOLVED'", (MODEL_VERSION,)).fetchone()[0]
        pnl = self.db.execute("SELECT COALESCE(SUM(pnl),0) FROM paper_trades WHERE model_version=? AND status='CLOSED'", (MODEL_VERSION,)).fetchone()[0]
        wins = self.db.execute("SELECT COUNT(*) FROM paper_trades WHERE model_version=? AND status='CLOSED' AND pnl>0", (MODEL_VERSION,)).fetchone()[0]
        unresolved_legacy = self.db.execute("SELECT COUNT(*) FROM paper_trades WHERE model_version LIKE 'legacy-%' AND status LIKE 'LEGACY%' ").fetchone()[0]
        print("\n" + "=" * 78)
        print(f"POLYMARKET SCOUT {APP_VERSION} | ledger model {MODEL_VERSION} | {utc(now)}")
        print(f"Gamma markets fetched: {len(raw_markets)} | binary markets parsed: {len(markets)} | CLOB books: {len(books)} | quote rows: {snap_count} | {elapsed:.1f}s")
        print(f"V2 paper ledger: {v2_open} open | {v2_unresolved} unresolved | {v2_closed} closed | closed net P&L ${pnl:+.2f} | win rate {(wins/v2_closed*100 if v2_closed else 0):.1f}%")
        print(f"Legacy records preserved but excluded from v2 metrics: {unresolved_legacy}")
        print(f"Read-only mode: no real orders. Signal requires locally observed CLOB YES midpoint history ({self.args.lookback_hours:g}h); no Gamma oneHourPriceChange trigger.")
        print(f"Paper execution: visible ask/bid book-walk, market feeSchedule, partial exits; stake ${self.args.stake:.2f}; hold {self.args.hold_hours:g}h.")
        print(f"Qualified signals: {len(candidates)} | opened this scan: {len(opened)} | positions managed: {managed} | unresolved transitions: {unresolved}")
        if opened:
            print("\nNEW PAPER ENTRIES (simulated taker fills from visible asks)")
            for c in opened:
                ex, m = c["execution"], c["market"]
                print(f"  #{c['id']} BUY {c['side']} | avg fill {c['avg_price']:.4f} | shares {ex['shares']:.4f} | spread {c['entry_book']['spread']:.4f} | entry fee ${ex['fee']:.4f} | signal {c['signal_change']*100:+.2f}pp")
                print(f"     {m['question'][:130]}\n     {m['url']}")
        else:
            print("\nNEW PAPER ENTRIES: none. On early scans this is expected while a local CLOB history baseline is being collected.")
        if rejected:
            print("\nTOP REJECTION REASONS")
            for reason, count in sorted(rejected.items(), key=lambda item: item[1], reverse=True)[:8]:
                print(f"  {reason}: {count}")
        if manage_notes:
            print("\nPOSITION NOTES")
            for note in manage_notes[:8]:
                print("  " + note)
        print(f"\nDatabase: {self.db_path}\nSnapshots retained: {self.args.snapshot_retention_days} days")
        print("Reminder: correct execution simulation is not evidence of a profitable signal.")
        print("=" * 78)

    def status(self) -> None:
        rows = self.db.execute("SELECT status, COUNT(*) AS n FROM paper_trades GROUP BY status ORDER BY status").fetchall()
        print("Paper ledger by status:")
        for r in rows:
            print(f"  {r['status']}: {r['n']}")
        closed = self.db.execute("SELECT * FROM paper_trades WHERE model_version=? AND status='CLOSED' ORDER BY exit_ts DESC", (MODEL_VERSION,)).fetchall()
        open_rows = self.db.execute("SELECT * FROM paper_trades WHERE model_version=? AND status='OPEN' ORDER BY entry_ts", (MODEL_VERSION,)).fetchall()
        unresolved = self.db.execute("SELECT * FROM paper_trades WHERE model_version=? AND status='UNRESOLVED' ORDER BY entry_ts", (MODEL_VERSION,)).fetchall()
        total_pnl = sum(number(r["pnl"], 0.0) for r in closed)
        wins = sum(1 for r in closed if number(r["pnl"], 0.0) > 0)
        expectancy = total_pnl / len(closed) if closed else 0.0
        print(f"\nV2 CLOSED P&L: ${total_pnl:+.4f} | closed trades {len(closed)} | win rate {(wins/len(closed)*100 if closed else 0):.1f}% | average closed P&L ${expectancy:+.4f}")
        print(f"These results use visible-book assumptions; unresolved/legacy entries are not silently counted as wins or losses.")
        if open_rows:
            print("\nOPEN POSITIONS")
            for r in open_rows:
                print(f"  #{r['id']} {r['side']} entry avg {number(r['entry_price'],0):.4f} | shares left {number(r['remaining_shares'],0):.4f} | entry fee ${number(r['entry_fee'],0):.4f} | {utc(r['entry_ts'])} | {r['question'][:80]}")
        if unresolved:
            print("\nUNRESOLVED V2 POSITIONS (not counted as closed P&L)")
            for r in unresolved:
                print(f"  #{r['id']} {r['side']} remaining shares {number(r['remaining_shares'],0):.4f} | {r['exit_reason']} | {r['question'][:80]}")
        legacy = self.db.execute("SELECT COUNT(*) FROM paper_trades WHERE model_version LIKE 'legacy-%'").fetchone()[0]
        print(f"\nLegacy pre-v2 trades preserved for audit and excluded from metrics: {legacy}")
        latest = self.db.execute("SELECT ts, fetched, parsed, book_count, qualified, opened, rejected, status FROM scan_runs ORDER BY ts DESC LIMIT 1").fetchone()
        if latest:
            print(f"Latest scan: {utc(latest['ts'])} | fetched {latest['fetched']} | parsed {latest['parsed']} | books {latest['book_count']} | qualified {latest['qualified']} | opened {latest['opened']} | status {latest['status']}")
            try:
                rejects = json.loads(latest["rejected"] or "{}")
                if rejects:
                    print("Latest rejection counts: " + ", ".join(f"{k}={v}" for k, v in sorted(rejects.items(), key=lambda i: i[1], reverse=True)[:5]))
            except json.JSONDecodeError:
                pass
        print(f"\nDB: {self.db_path}")

    def export_csv(self, target: Path) -> None:
        rows = self.db.execute("SELECT * FROM paper_trades ORDER BY entry_ts").fetchall()
        target.parent.mkdir(parents=True, exist_ok=True)
        fields = list(rows[0].keys()) if rows else [x[1] for x in self.db.execute("PRAGMA table_info(paper_trades)").fetchall()]
        with target.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows([dict(r) for r in rows])
        print(f"Exported {len(rows)} paper trades to {target}")

    def close(self) -> None:
        self.db.close()


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Polymarket Scout v2: read-only market-data observations + paper execution")
    p.add_argument("--version", action="version", version=f"Polymarket Scout {APP_VERSION} (ledger model {MODEL_VERSION})")
    p.add_argument("command", choices=["scan", "run", "status", "export", "self-test"], nargs="?", default="scan")
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"SQLite database path (default: {DEFAULT_DB})")
    p.add_argument("--limit", type=int, default=100, help="Markets to inspect (1-300 via keyset pagination)")
    p.add_argument("--interval", type=int, default=300, help="Seconds between run-mode scans (minimum 60)")
    p.add_argument("--lookback-hours", type=float, default=1.0, help="Required local CLOB midpoint history horizon")
    p.add_argument("--max-history-lag-minutes", type=float, default=15.0, help="Maximum allowed age beyond lookback for the historical baseline")
    p.add_argument("--min-move", type=float, default=0.03, help="Absolute YES midpoint movement threshold, 0.03 = 3 percentage points")
    p.add_argument("--min-volume", type=float, default=5000.0, help="Minimum reported 24h volume (USD)")
    p.add_argument("--min-liquidity", type=float, default=1000.0, help="Minimum reported CLOB liquidity (USD)")
    p.add_argument("--stake", type=float, default=10.0, help="Simulated notional budget per paper entry (USD)")
    p.add_argument("--max-open", type=int, default=5, help="Maximum simultaneous v2 paper positions")
    p.add_argument("--hold-hours", type=float, default=6.0, help="Minimum holding horizon before attempting paper exit")
    p.add_argument("--cooldown-hours", type=float, default=6.0, help="Cooldown before a second trade in the same market")
    p.add_argument("--max-spread", type=float, default=0.03, help="Maximum quoted bid-ask spread, in probability points")
    p.add_argument("--min-entry-fill-fraction", type=float, default=0.98, help="Minimum visible ask depth that must cover entry notional")
    p.add_argument("--min-contract-price", type=float, default=0.05, help="Minimum entry-token midpoint")
    p.add_argument("--max-contract-price", type=float, default=0.95, help="Maximum entry-token midpoint")
    p.add_argument("--max-book-age-seconds", type=int, default=180, help="Maximum allowed CLOB book timestamp age")
    p.add_argument("--snapshot-retention-days", type=int, default=30, help="Local quote and signal history retention in days")
    p.add_argument("--export-path", type=Path, default=Path.home() / "polymarket_scout_trades.csv", help="CSV export destination")
    return p


def self_test() -> int:
    import tempfile
    from types import SimpleNamespace

    raw = {
        "id": "123", "conditionId": "0xabc", "question": "Will example happen?", "slug": "example-market",
        "active": True, "closed": False, "acceptingOrders": True, "enableOrderBook": True,
        "archived": False, "restricted": False,
        "outcomes": '["Yes", "No"]', "outcomePrices": '["0.62", "0.38"]',
        "clobTokenIds": '["token-yes", "token-no"]', "volume24hr": "25000", "liquidityClob": "5000",
        "endDate": "2030-01-01T00:00:00Z", "feesEnabled": True,
        "feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True},
    }
    m = normalize_market(raw)
    assert m is not None and m["yes_token_id"] == "token-yes" and m["no_token_id"] == "token-no"
    assert market_trade_ready(m) == (True, "ready")
    assert market_trade_ready({**m, "accepting_orders": None})[0] is False
    # Regression: Gamma's "restricted" field indicates SOME jurisdictions may be restricted,
    # not that ALL are blocked. Must not auto-reject on restricted=True.
    assert market_trade_ready({**m, "restricted": True}) == (True, "ready")
    assert market_trade_ready({**m, "restricted": False}) == (True, "ready")
    assert parse_ts("2030-01-01T00:00:00Z") is not None and parse_ts("bad") is None
    assert fee_for_fill(100, 0.5, 0.05, 1) == 1.25
    # Current official fee docs specify p*(1-p); retained metadata exponent does not alter the formula.
    assert fee_for_fill(100, 0.5, 0.05, 0.5) == 1.25
    book = normalize_book({"asset_id":"x", "timestamp": int(time.time()*1000), "bids":[{"price":"0.49","size":"50"},{"price":"0.48","size":"50"}], "asks":[{"price":"0.51","size":"10"},{"price":"0.52","size":"50"}], "tick_size":"0.01", "min_order_size":"1", "hash":"h"})
    assert book["best_bid"] == 0.49 and book["best_ask"] == 0.51 and abs(book["spread"]-0.02)<1e-8
    buy = walk_buy_budget(book, 10, 0.05, 1)
    assert buy["spent"] > 9.99 and buy["fill_fraction"] >= 0.999 and buy["shares"] > 0
    sell = walk_sell_shares(book, 20, 0.05, 1)
    assert sell["shares"] == 20 and sell["gross"] > 0 and sell["fee"] > 0
    with tempfile.TemporaryDirectory(prefix="scout-v2-test-") as tmp:
        db = Path(tmp) / "scout.sqlite3"
        # Simulate existing v1 database: the migration must preserve but exclude it.
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE paper_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, market_id TEXT NOT NULL, question TEXT NOT NULL, slug TEXT, side TEXT NOT NULL, entry_ts INTEGER NOT NULL, entry_price REAL NOT NULL, stake REAL NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN', exit_ts INTEGER, exit_price REAL, pnl REAL, exit_reason TEXT, signal_change REAL)")
        conn.execute("INSERT INTO paper_trades(market_id,question,slug,side,entry_ts,entry_price,stake,status) VALUES('legacy','legacy Q','legacy','YES',100,0.5,10,'OPEN')")
        conn.commit(); conn.close()
        args = make_parser().parse_args(["status", "--db", str(db)])
        scout = Scout(db, args)
        try:
            # The current fee formula ignores the metadata exponent; 0.5 must not
            # cause a valid fee-enabled market to be rejected.
            fractional_exponent = normalize_market({**raw, "feeSchedule": {"rate": 0.05, "exponent": 0.5, "takerOnly": True}})
            assert fractional_exponent is not None
            assert scout._market_risk_gate(fractional_exponent, int(time.time()))[0] is True
            # Never treat the substring "resolved" inside "unresolved" as final.
            unresolved_market = normalize_market({**raw, "closed": True, "resolved": False,
                "umaResolutionStatuses": ["unresolved"], "outcomePrices": '["1", "0"]'})
            assert unresolved_market is not None
            assert scout._settled_outcome_price(unresolved_market, "YES") is None
            resolved_market = normalize_market({**raw, "closed": True, "umaResolutionStatuses": ["resolved"],
                "outcomePrices": '["1", "0"]'})
            assert resolved_market is not None
            assert scout._settled_outcome_price(resolved_market, "YES") == 1.0
            row = scout.db.execute("SELECT status,model_version FROM paper_trades WHERE market_id='legacy'").fetchone()
            assert row["status"] == "LEGACY" and row["model_version"] == "legacy-v0"
            # Exercise quote persistence, entry insertion, visible-bid exit, and net P&L end to end.
            now = int(time.time())
            test_market = normalize_market({**raw, "id": "456", "conditionId": "0xdef", "slug": "synthetic-v2",
                "question": "Synthetic v2 market", "clobTokenIds": '["yes-v2", "no-v2"]',
                "outcomes": '["Yes", "No"]', "outcomePrices": '["0.62", "0.38"]'})
            assert test_market is not None
            yes_entry = normalize_book({"asset_id":"yes-v2", "timestamp":now*1000,
                "bids":[{"price":"0.60","size":"100"}], "asks":[{"price":"0.62","size":"100"}],
                "tick_size":"0.01", "min_order_size":"1", "hash":"entry-hash"})
            no_entry = normalize_book({"asset_id":"no-v2", "timestamp":now*1000,
                "bids":[{"price":"0.37","size":"100"}], "asks":[{"price":"0.39","size":"100"}],
                "tick_size":"0.01", "min_order_size":"1", "hash":"no-hash"})
            scout._snapshot_quotes([test_market], {"yes-v2":yes_entry, "no-v2":no_entry}, now)
            assert scout.db.execute("SELECT yes_mid FROM quote_snapshots WHERE market_id='456'").fetchone()[0] == 0.61
            candidate = {"market": test_market, "side":"YES", "token_id":"yes-v2", "entry_book":yes_entry,
                "signal_change":0.05, "baseline_mid":0.56, "baseline_age":3600,
                "execution":walk_buy_budget(yes_entry, 10, 0.05, 1), "fee_rate":0.05, "fee_exponent":1, "score":0.05}
            opened = scout._open_candidates([candidate], now)
            assert len(opened) == 1
            exit_ts = now + 6*3600
            yes_exit = normalize_book({"asset_id":"yes-v2", "timestamp":exit_ts*1000,
                "bids":[{"price":"0.70","size":"100"}], "asks":[{"price":"0.71","size":"100"}],
                "tick_size":"0.01", "min_order_size":"1", "hash":"exit-hash"})
            managed = scout._manage_positions({"456":test_market}, {"yes-v2":yes_exit}, exit_ts)
            row = scout.db.execute("SELECT status,pnl,remaining_shares FROM paper_trades WHERE model_version=?", (MODEL_VERSION,)).fetchone()
            assert managed[0] == 1 and row["status"] == "CLOSED" and row["pnl"] > 0 and row["remaining_shares"] == 0
        finally:
            scout.close()
    print("SELF-TEST PASS: token mapping, trade-readiness gates, fee schedule, order-book walk, timestamps, legacy DB migration and synthetic P&L.")
    print("LIVE API connectivity is not tested by self-test; use `scan` for that.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = make_parser().parse_args(argv)
    if args.command == "self-test":
        return self_test()
    if not (1 <= args.limit <= 300):
        print("--limit must be between 1 and 300", file=sys.stderr); return 2
    if args.interval < 60 or args.lookback_hours <= 0 or args.max_history_lag_minutes < 0 or args.stake <= 0 or args.hold_hours <= 0:
        print("Invalid settings: interval >=60s; positive stake/holding/lookback; non-negative history lag.", file=sys.stderr); return 2
    if args.min_move < 0 or args.min_volume < 0 or args.min_liquidity < 0 or args.max_spread <= 0 or not (0 < args.min_entry_fill_fraction <= 1):
        print("Invalid threshold settings.", file=sys.stderr); return 2
    if not (0 < args.min_contract_price < args.max_contract_price < 1) or args.max_book_age_seconds <= 0 or args.snapshot_retention_days < 1:
        print("Invalid price bounds, book freshness, or snapshot retention.", file=sys.stderr); return 2
    if args.command == "status" or args.command == "export":
        scout = Scout(args.db, args)
        try:
            if args.command == "status": scout.status()
            else: scout.export_csv(args.export_path)
        finally:
            scout.close()
        return 0
    scout = Scout(args.db, args)
    try:
        if args.command == "scan":
            try:
                scout.scan_once()
                return 0
            except (RuntimeError, json.JSONDecodeError, OSError, sqlite3.Error, ValueError) as exc:
                now = int(time.time())
                try:
                    scout.db.execute("INSERT INTO scan_runs(ts,status,error) VALUES(?,?,?)", (now, "FAILED", str(exc)[:1000]))
                    scout.db.commit()
                except sqlite3.Error:
                    pass
                print(f"[{utc(now)}] SCAN FAILED: {exc}. Paper ledger preserved; this run will exit non-zero.", file=sys.stderr)
                return 1
        print(f"Starting app {APP_VERSION} (ledger model {MODEL_VERSION}), read-only paper scanner every {args.interval}s. Ctrl+C stops it; database is retained.")
        while True:
            try:
                scout.scan_once()
            except (RuntimeError, json.JSONDecodeError, OSError, sqlite3.Error, ValueError) as exc:
                now = int(time.time())
                try:
                    scout.db.execute("INSERT INTO scan_runs(ts,status,error) VALUES(?,?,?)", (now, "FAILED", str(exc)[:1000]))
                    scout.db.commit()
                except sqlite3.Error:
                    pass
                print(f"[{utc(now)}] SCAN FAILED: {exc}. Ledger preserved; retrying next interval.", file=sys.stderr)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped. Paper ledger and local observations preserved.")
        return 130
    finally:
        scout.close()


if __name__ == "__main__":
    raise SystemExit(main())
