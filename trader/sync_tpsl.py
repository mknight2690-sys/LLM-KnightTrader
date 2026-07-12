"""Sync TP/SL orders to current open positions.

Philosophy:
- Exchange truth = live pending TPSL orders.
- Sync does three things:
  1) Cancel orphan TP/SL orders that no longer match any open position.
  2) Cancel duplicate TP/SL orders for the same position side.
  3) Attach missing TP/SL for positions that legitimately have no live trigger.

Net mode assumption: one TP/SL per position close side.
"""
from __future__ import annotations

import logging
from typing import Any

from blofin.client import BlofinClient
from trader.tpsl import attach_tpsl_safe, resolve_mark_price

log_event = logging.getLogger("llm_knighttrader.sync_tpsl").info


def _pos_close_side(pos: dict[str, Any]) -> str:
    raw = float(pos.get("size") or pos.get("positions") or 0)
    return "sell" if raw > 0 else "buy"


def _live_tpsl_rows(client: BlofinClient, inst_id: str) -> list[dict[str, Any]]:
    try:
        pending = client.get_orders_tpsl_pending(inst_id=inst_id)
    except Exception as exc:  # pragma: no cover - defensive
        log_event("sync_tpsl", "Pending TPSL fetch failed", str(exc))
        return []
    rows = pending.get("data") or []
    return [r for r in rows if str(r.get("state") or "").lower() == "live"]


def _cancel_all_tpsl(client: BlofinClient, inst: str, tpsl_ids: list[str]) -> None:
    """Cancel in small chunks until nothing remains."""
    while tpsl_ids:
        chunk = tpsl_ids[:10]
        tpsl_ids = tpsl_ids[10:]
        try:
            resp = client.cancel_tpsl(inst_id=inst, tpsl_ids=chunk)
            code = str(resp.get("code") or "")
            msg = str(resp.get("msg") or "")
            if code != "0" and code != "0.0":
                log_event("sync_tpsl", f"Cancellation error {inst}", f"{code}: {msg}")
        except Exception as exc:
            log_event("sync_tpsl", f"Cancellation exception {inst}", str(exc))


def sync_tpsl(
    client: BlofinClient,
    account: dict[str, Any],
    *,
    tp_pct: float = 5.0,
    sl_pct: float = 2.0,
) -> dict[str, Any]:
    """Reconcile live pending TPSL orders with current open positions.

    Returns a summary dict with counts and actions taken.
    """
    cancelled: list[str] = []
    attached: list[dict[str, Any]] = []

    # 1) Build position map: inst_id -> close_side
    positions = [p for p in (account.get("positions") or []) if float(p.get("size") or p.get("positions") or 0) != 0]
    needed: dict[str, str] = {}
    for pos in positions:
        inst = str(pos.get("instId") or "")
        if not inst:
            continue
        needed[inst] = _pos_close_side(pos)

    # 2) Build live TPSL map: inst_id -> list of live rows, deduped by tpslId
    live_map: dict[str, list[dict[str, Any]]] = {}
    seen_ids: set[str] = set()

    for inst in list(needed.keys()):
        for row in _live_tpsl_rows(client, inst):
            key = str(row.get("tpslId") or "")
            if not key or key in seen_ids:
                continue
            seen_ids.add(key)
            live_map.setdefault(inst, []).append(row)

    # Global scan for orphans
    try:
        pending_all = client.get_orders_tpsl_pending(page_index=1, page_size=500)
        for r in (pending_all.get("data") or []):
            if str(r.get("state") or "").lower() != "live":
                continue
            key = str(r.get("tpslId") or "")
            if not key or key in seen_ids:
                continue
            seen_ids.add(key)
            inst = str(r.get("instId") or "")
            live_map.setdefault(inst, []).append(r)
    except Exception as exc:
        log_event("sync_tpsl", "Global pending TPSL fetch failed", str(exc))

    # 3) Cancel extras and duplicates
    for inst, rows in list(live_map.items()):
        if not rows:
            live_map.pop(inst, None)
            continue

        needed_side = needed.get(inst)

        by_side: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            side = str(r.get("side") or "").lower()
            by_side.setdefault(side, []).append(r)

        for side, side_rows in list(by_side.items()):
            to_cancel = [str(r.get("tpslId")) for r in side_rows if r.get("tpslId")]

            # Cancel if no matching position or wrong close side
            if inst not in needed or needed[inst] != side:
                if to_cancel:
                    _cancel_all_tpsl(client, inst, to_cancel)
                    log_event(
                        "sync_tpsl",
                        f"Cancelled {len(to_cancel)} orphan/extra TPSL {inst} side={side}",
                        ",".join(to_cancel[:20]),
                    )
                    cancelled.extend(to_cancel)
                by_side.pop(side, None)
                continue

            # Duplicates: keep the last, cancel the rest
            if len(side_rows) > 1:
                cancel_rows = side_rows[:-1]
                to_cancel = [str(r.get("tpslId")) for r in cancel_rows if r.get("tpslId")]
                if to_cancel:
                    _cancel_all_tpsl(client, inst, to_cancel)
                    log_event(
                        "sync_tpsl",
                        f"Cancelled {len(to_cancel)} duplicate TPSL {inst} side={side}",
                        ",".join(to_cancel[:20]),
                    )
                    cancelled.extend(to_cancel)

        # Keep only the best candidate per side after cancellation
        remaining_rows = []
        for side, side_rows in by_side.items():
            if side_rows:
                remaining_rows.append(side_rows[-1])
        live_map[inst] = remaining_rows

    # 4) One final verification sweep for any stragglers the API didn't cancel cleanly
    for inst in list(live_map.keys()):
        rows = _live_tpsl_rows(client, inst)
        if rows:
            ids = [str(r.get("tpslId")) for r in rows if r.get("tpslId")]
            if ids:
                _cancel_all_tpsl(client, inst, ids)
                log_event("sync_tpsl", f"Final sweep cancelled {len(ids)} stragglers {inst}", ",".join(ids[:20]))
                cancelled.extend(ids)
            live_map[inst] = []

    # 5) Attach missing
    for pos in positions:
        inst = str(pos.get("instId") or "")
        if not inst:
            continue
        side = _pos_close_side(pos)
        rows = live_map.get(inst) or []
        has_match = any(str(r.get("side") or "").lower() == side for r in rows)
        if has_match:
            continue

        contracts = client._format_order_size(inst, abs(float(pos.get("size") or pos.get("positions") or 0)))
        mark = resolve_mark_price(client, inst, account=account)
        if mark <= 0:
            continue

        leverage = int(float(pos.get("leverage") or 3))
        log_event(
            "sync_tpsl",
            f"Attaching missing TP/SL {inst} side={side} contracts={contracts} mark={mark:.6f}",
        )
        resp = attach_tpsl_safe(
            client,
            inst_id=inst,
            side=side,
            contracts=contracts,
            mark=mark,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            leverage=leverage,
            account=account,
        )
        attached.append({"instId": inst, "side": side, "code": resp.get("code"), "tpsl_id": (((resp.get("data") or {}) or {}).get("tpslId") if isinstance(resp.get("data"), dict) else None)})

    return {
        "positions": len(positions),
        "live_tpsl_total": sum(len(v) for v in live_map.values()),
        "orphan_cancelled": len(cancelled),
        "missing_attached": len(attached),
        "orphan_tpsl_ids": cancelled[:50],
        "attached": attached[:50],
    }
