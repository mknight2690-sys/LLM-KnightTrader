"""BloFin REST client for LLM KnightTrader."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any
from uuid import uuid4

from config import BLOFIN_BASE, BLOFIN_BROKER_ID
from credentials import BlofinCredentials, load_blofin_credentials
from blofin.account_cache import get_account_snapshot
from blofin.market_cache import get_cached_candles, get_cached_tickers, set_cached_candles, set_cached_tickers
from blofin.market_cache import get_cached_instruments, set_cached_instruments

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://blofin.com",
    "Referer": "https://blofin.com/",
    "Content-Type": "application/json",
}

SCAN_COINS = [
    ("SOL", "SOL-USDT"),
    ("XRP", "XRP-USDT"),
    ("ETH", "ETH-USDT"),
    ("DOGE", "DOGE-USDT"),
    ("SUI", "SUI-USDT"),
    ("AVAX", "AVAX-USDT"),
    ("LINK", "LINK-USDT"),
    ("BTC", "BTC-USDT"),
]


class BlofinClient:
    def __init__(self, credentials: BlofinCredentials | None = None) -> None:
        self.credentials = credentials or load_blofin_credentials()
        self.broker_id = BLOFIN_BROKER_ID
        self._position_mode: str | None = None
        self._instruments_cache: list[dict[str, Any]] | None = None
        self._instruments_cache_ts: float = 0.0

    def get_position_mode(self) -> str:
        resp = self.request("GET", "/api/v1/account/position-mode")
        if resp.get("code") != "0":
            return self._position_mode or "net_mode"
        mode = (resp.get("data") or {}).get("positionMode", "net_mode")
        self._position_mode = mode
        return mode

    def ensure_net_position_mode(self) -> str:
        """BloFin account uses net (one-way) mode — orders need positionSide=net."""
        if self._position_mode in ("net_mode", "net"):
            return self._position_mode
        from blofin.account_cache import is_rate_limited

        if is_rate_limited():
            self._position_mode = "net_mode"
            return self._position_mode
        mode = self.get_position_mode()
        if mode != "net_mode":
            resp = self.request(
                "POST",
                "/api/v1/account/set-position-mode",
                body={"positionMode": "net_mode"},
            )
            if resp.get("code") == "0":
                mode = (resp.get("data") or {}).get("positionMode", "net_mode")
        self._position_mode = mode
        return mode

    def position_side_for_order(self, side: str) -> str:
        mode = self._position_mode or self.get_position_mode()
        if mode == "long_short_mode":
            return "long" if side.lower() == "buy" else "short"
        return "net"

    def _sign(self, method: str, sign_path: str, body_str: str) -> tuple[str, str, str]:
        ts = str(int(time.time() * 1000))
        nonce = str(uuid4())
        prehash = f"{sign_path}{method.upper()}{ts}{nonce}{body_str}"
        hex_sig = hmac.new(
            self.credentials.secret_key.encode(),
            prehash.encode(),
            hashlib.sha256,
        ).hexdigest()
        signature = base64.b64encode(hex_sig.encode()).decode()
        return ts, nonce, signature

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        params = params or {}
        query = "?" + urllib.parse.urlencode(params) if params else ""
        sign_path = path + query
        body_str = json.dumps(body, separators=(",", ":")) if body else ""
        url = f"{BLOFIN_BASE}{sign_path}"
        ts, nonce, signature = self._sign(method, sign_path, body_str)
        headers = dict(DEFAULT_HEADERS)
        headers.update(
            {
                "ACCESS-KEY": self.credentials.api_key,
                "ACCESS-SIGN": signature,
                "ACCESS-TIMESTAMP": ts,
                "ACCESS-NONCE": nonce,
                "ACCESS-PASSPHRASE": self.credentials.passphrase,
            }
        )
        req = urllib.request.Request(
            url,
            data=body_str.encode() if body_str else None,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"code": str(exc.code), "msg": raw[:300], "error": True}
            if exc.code == 429 or data.get("error_code") == 1015:
                from blofin.account_cache import note_rate_limit

                retry = float(data.get("retry_after") or 60)
                note_rate_limit(retry)
            return data

    def get_balance(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/account/balance", params={"accountType": "futures"})

    def get_positions(self, inst_id: str | None = None) -> dict[str, Any]:
        params: dict[str, str] = {}
        if inst_id:
            params["instId"] = inst_id

        account_resp = self.request("GET", "/api/v1/account/positions", params=params or None)
        from blofin.account_cache import response_is_rate_limited

        if response_is_rate_limited(account_resp):
            return account_resp
        if account_resp.get("code") == "0":
            if account_resp.get("data"):
                return account_resp
            trade_resp = self.request("GET", "/api/v1/trade/positions", params=params or None)
            if trade_resp.get("code") == "0" and trade_resp.get("data"):
                return trade_resp
            return account_resp

        trade_resp = self.request("GET", "/api/v1/trade/positions", params=params or None)
        if trade_resp.get("code") == "0":
            return trade_resp
        return account_resp

    def get_candles(self, inst_id: str, bar: str = "1m", limit: str = "30") -> list[list[str]]:
        if bar == "1m" and limit == "20":
            cached = get_cached_candles(inst_id)
            if cached is not None:
                return cached
        resp = self.request(
            "GET",
            "/api/v1/market/candles",
            params={"instId": inst_id, "bar": bar, "limit": limit},
        )
        if resp.get("code") != "0":
            raise RuntimeError(f"candles error: {json.dumps(resp)[:200]}")
        rows = list(reversed(resp.get("data") or []))
        if bar == "1m" and limit == "20":
            set_cached_candles(inst_id, rows)
        return rows

    def get_tickers(self, inst_id: str | None = None, *, allow_stale: bool = False) -> list[dict[str, Any]]:
        from blofin.market_cache import fetch_public_tickers

        if inst_id is None:
            cached = get_cached_tickers(allow_stale=allow_stale)
            if cached is not None:
                return cached
            try:
                return fetch_public_tickers()
            except Exception:
                stale = get_cached_tickers(allow_stale=True)
                if stale:
                    return stale
        params: dict[str, str] = {}
        if inst_id:
            params["instId"] = inst_id
        resp = self.request("GET", "/api/v1/market/tickers", params=params or None)
        rate_limited = resp.get("error_code") == 1015 or resp.get("status") == 429
        if resp.get("code") != "0" or rate_limited:
            if inst_id is None:
                stale = get_cached_tickers(allow_stale=True)
                if stale:
                    return stale
            raise RuntimeError(f"tickers error: {json.dumps(resp)[:200]}")
        rows = list(resp.get("data") or [])
        if inst_id is None:
            set_cached_tickers(rows)
        return rows

    def place_market_order(
        self,
        inst_id: str,
        side: str,
        size: str,
        position_side: str | None = None,
        *,
        reduce_only: bool = False,
        margin_mode: str = "cross",
    ) -> dict[str, Any]:
        self.ensure_net_position_mode()
        pos_side = position_side or self.position_side_for_order(side)
        body = {
            "instId": inst_id,
            "marginMode": margin_mode,
            "positionSide": pos_side,
            "side": side,
            "orderType": "market",
            "size": size,
            "reduceOnly": "true" if reduce_only else "false",
            "brokerId": self.broker_id,
        }
        return self.request("POST", "/api/v1/trade/order", body=body)

    def attach_tpsl(
        self,
        inst_id: str,
        position_side: str | None,
        close_side: str,
        size: str,
        tp: float,
        sl: float,
    ) -> dict[str, Any]:
        pos_side = position_side or self.position_side_for_order(
            "sell" if close_side == "sell" else "buy"
        )
        body = {
            "instId": inst_id,
            "marginMode": "cross",
            "positionSide": pos_side,
            "side": close_side,
            "size": size,
            "tpTriggerPrice": str(tp),
            "tpOrderPrice": "-1",
            "tpTriggerPriceType": "last",
            "slTriggerPrice": str(sl),
            "slOrderPrice": "-1",
            "slTriggerPriceType": "last",
            "brokerId": self.broker_id,
        }
        return self.request("POST", "/api/v1/trade/order-tpsl", body=body)

    def get_orders_tpsl_pending(
        self,
        *,
        inst_id: str | None = None,
        inst_type: str = "SWAP",
        page_index: int | None = None,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        """List pending TP/SL (TPSL) orders.

        Endpoint returns list in resp["data"].
        """
        params: dict[str, str] = {}
        if inst_id:
            params["instId"] = inst_id
        if inst_type:
            params["instType"] = inst_type
        if page_index is not None:
            params["pageIndex"] = str(page_index)
        if page_size is not None:
            params["pageSize"] = str(page_size)
        return self.request("GET", "/api/v1/trade/orders-tpsl-pending", params=params or None)

    def get_order_tpsl_detail(
        self,
        *,
        inst_id: str,
        tpsl_id: str,
    ) -> dict[str, Any]:
        """Fetch a specific TPSL order detail by tpslId."""
        params = {"instId": inst_id, "tpslId": tpsl_id}
        return self.request("GET", "/api/v1/trade/order-tpsl-detail", params=params)

    def cancel_tpsl(
        self,
        *,
        inst_id: str | None = None,
        tpsl_ids: list[str] | None = None,
        client_order_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Cancel TPSL orders.

        BloFin requires either tpslId or clientOrderId per row.
        """
        body: list[dict[str, Any]] = []
        if tpsl_ids:
            for tid in tpsl_ids:
                body.append({"instId": inst_id, "tpslId": str(tid), "clientOrderId": ""})
        if client_order_ids:
            for cid in client_order_ids:
                body.append({"instId": inst_id, "tpslId": "", "clientOrderId": str(cid)})
        if not body:
            return {"code": "1", "msg": "no tpsl identifiers provided", "data": []}
        return self.request("POST", "/api/v1/trade/cancel-tpsl", body=body)

    def set_leverage(
        self,
        inst_id: str,
        leverage: int | str,
        *,
        margin_mode: str = "cross",
        position_side: str | None = None,
    ) -> dict[str, Any]:
        pos_side = position_side or "net"
        body: dict[str, Any] = {
            "instId": inst_id,
            "leverage": str(leverage),
            "marginMode": margin_mode,
            "positionSide": pos_side,
        }
        return self.request("POST", "/api/v1/account/set-leverage", body=body)

    def parse_account_snapshot(self, *, force: bool = False) -> dict[str, Any]:
        return get_account_snapshot(force=force)

    def order_rejected(self, resp: dict[str, Any]) -> tuple[bool, str]:
        if resp.get("code") == "0":
            return False, ""
        rows = resp.get("data") or []
        if isinstance(rows, list) and rows:
            row = rows[0]
            code = str(row.get("code", ""))
            msg = str(row.get("msg", resp.get("msg", "")))
            if code not in ("0", ""):
                return True, f"{code}: {msg}"
        if resp.get("code") not in ("0", None):
            return True, str(resp.get("msg", resp.get("code")))
        return False, ""

    def _format_order_size(self, inst: str, size: float) -> str:
        """Format contract size respecting min lot (fixes 0.1 → '0' bug with int())."""
        import math

        min_size = 1.0
        for row in self.get_instruments():
            if row.get("instId") == inst:
                min_size = float(row.get("minSize") or row.get("lotSize") or 1.0)
                break
        size = abs(float(size))
        if size <= 0:
            return "0"
        if min_size >= 1:
            return str(max(1, int(round(size))))
        decimals = max(0, -int(round(math.log10(min_size)))) if min_size > 0 else 2
        fmt = f"{{:.{decimals}f}}"
        return fmt.format(size).rstrip("0").rstrip(".") or str(min_size)

    def close_position(self, pos: dict[str, Any]) -> dict[str, Any]:
        """Close a net-mode position; retries without reduceOnly on BloFin 102022."""
        inst = str(pos.get("instId") or "")
        raw_size = float(pos.get("size") or pos.get("positions") or 0)
        size = abs(raw_size)
        if not inst or size <= 0:
            return {"code": "1", "msg": "invalid position", "data": []}

        close_side = "sell" if raw_size > 0 else "buy"
        pos_side = "net"
        margin_mode = str(pos.get("marginMode") or "cross")
        size_str = self._format_order_size(inst, size)

        resp = self.place_market_order(
            inst,
            close_side,
            size_str,
            pos_side,
            reduce_only=True,
            margin_mode=margin_mode,
        )
        rejected, err = self.order_rejected(resp)
        if rejected and ("102022" in err or "reduce" in err.lower() or "size" in err.lower()):
            resp = self.place_market_order(
                inst,
                close_side,
                size_str,
                pos_side,
                reduce_only=False,
                margin_mode=margin_mode,
            )
        return resp

    def get_instruments(self, inst_type: str = "SWAP") -> list[dict[str, Any]]:
        now = time.time()
        if self._instruments_cache and now - self._instruments_cache_ts < 3600:
            return self._instruments_cache
        resp = self.request("GET", "/api/v1/market/instruments", params={"instType": inst_type})
        if resp.get("code") != "0":
            return self._instruments_cache or []
        rows = resp.get("data") or []
        if rows:
            self._instruments_cache = rows
            self._instruments_cache_ts = now
            set_cached_instruments(rows)
        return rows

    def list_usdt_swap_ids(self, limit: int | None = None) -> list[str]:
        rows = self.get_instruments("SWAP")
        ids = []
        for row in rows:
            inst = str(row.get("instId") or "")
            state = str(row.get("state") or "live").lower()
            if inst.endswith("-USDT") and state in ("live", "trading", ""):
                ids.append(inst)
        ids.sort()
        if not ids:
            ids = [inst for _, inst in SCAN_COINS]
        if limit is not None:
            return ids[:limit]
        return ids

    def tradable_usdt_swap_set(self, *, skip_fetch: bool = False) -> set[str]:
        if self._instruments_cache:
            return {
                str(row.get("instId") or "")
                for row in self._instruments_cache
                if str(row.get("instId") or "").endswith("-USDT")
                and str(row.get("state") or "live").lower() in ("live", "trading", "")
            }
        if skip_fetch:
            return set()
        from blofin.account_cache import is_rate_limited

        if is_rate_limited():
            return set()
        return set(self.list_usdt_swap_ids())

    @staticmethod
    def _score_momentum(closes: list[float], vols: list[float]) -> dict[str, Any]:
        c1 = (closes[-1] - closes[-2]) / closes[-2] * 100
        c5 = (closes[-1] - closes[-6]) / closes[-6] * 100
        vol_ratio = vols[-1] / max(sum(vols[-5:]) / 5, 1e-9)
        score = 0
        if c1 > 0.10:
            score += 2
        elif c1 > 0.04:
            score += 1
        elif c1 < -0.10:
            score -= 2
        elif c1 < -0.04:
            score -= 1
        if c5 > 0.25:
            score += 2
        elif c5 > 0.12:
            score += 1
        elif c5 < -0.25:
            score -= 2
        elif c5 < -0.12:
            score -= 1
        if vol_ratio > 2.0:
            score = int(score * 1.5)
        elif vol_ratio > 1.5:
            score = int(score * 1.2)
        side = None
        if c1 > 0 and c5 > 0:
            side = "buy"
        elif c1 < 0 and c5 < 0:
            side = "sell"
        return {
            "side": side,
            "score": score,
            "c1_pct": round(c1, 3),
            "c5_pct": round(c5, 3),
            "vol_ratio": round(vol_ratio, 2),
            "price": closes[-1],
        }

    def scan_momentum(self, extra_inst_ids: list[str] | None = None) -> list[dict[str, Any]]:
        pairs = list(SCAN_COINS)
        seen = {inst for _, inst in pairs}
        for inst in extra_inst_ids or []:
            if inst not in seen:
                pairs.append((inst.split("-")[0], inst))
                seen.add(inst)

        setups: list[dict[str, Any]] = []
        for coin, inst_id in pairs:
            try:
                rows = self.get_candles(inst_id, "1m", "20")
                closes = [float(r[4]) for r in rows]
                vols = [float(r[5]) for r in rows]
                scored = self._score_momentum(closes, vols)
                setups.append({"coin": coin, "instId": inst_id, **scored})
            except Exception as exc:
                setups.append({"coin": coin, "instId": inst_id, "error": str(exc)[:120]})
        setups.sort(key=lambda x: abs(x.get("score", 0)), reverse=True)
        return setups

    @staticmethod
    def _score_ticker(row: dict[str, Any]) -> dict[str, Any] | None:
        inst_id = str(row.get("instId") or "")
        last = float(row.get("last") or 0)
        open24 = float(row.get("open24h") or 0)
        high24 = float(row.get("high24h") or last)
        low24 = float(row.get("low24h") or last)
        if last <= 0 or open24 <= 0:
            return None
        chg = (last - open24) / open24 * 100
        span = max(high24 - low24, last * 0.0001)
        range_pos = (last - low24) / span
        vol = float(row.get("volCurrency24h") or row.get("vol24h") or 0)
        score = 0
        if chg > 3:
            score += 3
        elif chg > 1:
            score += 1
        elif chg < -3:
            score -= 3
        elif chg < -1:
            score -= 1
        if range_pos > 0.85:
            score += 1
        elif range_pos < 0.15:
            score -= 1
        if vol > 5_000_000:
            score = int(score * 1.5)
        elif vol > 500_000:
            score = int(score * 1.2)
        side = "buy" if chg > 0.5 and range_pos > 0.4 else "sell" if chg < -0.5 and range_pos < 0.6 else None
        return {
            "coin": inst_id.split("-")[0],
            "instId": inst_id,
            "side": side,
            "score": score,
            "c1_pct": round(chg, 3),
            "c5_pct": round(chg, 3),
            "vol_ratio": round(vol, 2),
            "price": last,
            "range_pos": round(range_pos, 3),
            "source": "tickers",
        }

    def scan_all_tradable(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Scan every live USDT swap via one bulk tickers request (+ hourly instruments list)."""
        meta: dict[str, Any] = {"source": "bulk_tickers"}
        tickers: list[dict[str, Any]] | None = None
        try:
            tickers = self.get_tickers()
            meta["tickers_ok"] = True
            meta["stale"] = False
        except Exception as exc:
            tickers = get_cached_tickers(allow_stale=True)
            if not tickers:
                raise
            meta["tickers_ok"] = False
            meta["stale"] = True
            meta["error"] = str(exc)[:200]

        tradable = self.tradable_usdt_swap_set(skip_fetch=meta.get("stale", False))

        setups: list[dict[str, Any]] = []
        for row in tickers:
            inst_id = str(row.get("instId") or "")
            if not inst_id.endswith("-USDT"):
                continue
            if tradable and inst_id not in tradable:
                continue
            scored = self._score_ticker(row)
            if scored:
                setups.append(scored)

        setups.sort(key=lambda x: abs(x.get("score", 0)), reverse=True)
        meta["tradable_count"] = len(tradable) if tradable else len(setups)
        meta["scored_count"] = len(setups)
        meta["source"] = "bulk_tickers"
        return setups, meta

    def scan_momentum_bulk(self, inst_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """Score instruments from one tickers REST call."""
        want = set(inst_ids) if inst_ids else None
        tickers = self.get_tickers(allow_stale=True)
        setups: list[dict[str, Any]] = []
        for row in tickers:
            inst_id = str(row.get("instId") or "")
            if not inst_id.endswith("-USDT"):
                continue
            if want is not None and inst_id not in want:
                continue
            scored = self._score_ticker(row)
            if scored:
                setups.append(scored)
        setups.sort(key=lambda x: abs(x.get("score", 0)), reverse=True)
        return setups
