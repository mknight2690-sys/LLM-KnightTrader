"""TP/SL attachment using live mark price — avoids stale-scan trigger rejections."""

from __future__ import annotations

from typing import Any

from activity_log import log_event


def resolve_mark_price(
    client: Any,
    inst_id: str,
    *,
    account: dict[str, Any] | None = None,
    fallback: float = 0.0,
) -> float:
    """Best available mark: open position → account snapshot → candles → fallback."""
    if account:
        for pos in account.get("positions") or []:
            if str(pos.get("instId") or "") != inst_id:
                continue
            mark = float(pos.get("mark") or pos.get("markPrice") or 0)
            if mark > 0:
                return mark
            entry = float(pos.get("entry") or pos.get("avgPx") or pos.get("averagePrice") or 0)
            if entry > 0:
                return entry

    try:
        from blofin.account_cache import get_account_snapshot

        snap = get_account_snapshot(force=True)
        for pos in snap.get("positions") or []:
            if str(pos.get("instId") or "") != inst_id:
                continue
            mark = float(pos.get("mark") or pos.get("markPrice") or 0)
            if mark > 0:
                return mark
    except Exception:
        pass

    try:
        rows = client.get_candles(inst_id, "1m", "2")
        if rows:
            px = float(rows[-1][4])
            if px > 0:
                return px
    except Exception:
        pass

    return float(fallback or 0)


def compute_tpsl_triggers(
    side: str,
    mark: float,
    *,
    tp_pct: float = 2.0,
    sl_pct: float = 1.0,
    leverage: int = 3,
) -> tuple[float, float, str]:
    """Return (tp, sl, close_side) with triggers valid vs latest mark."""
    if mark <= 0:
        raise ValueError("mark price required for TP/SL")

    tp_r = max(abs(float(tp_pct)) / 100.0, 0.005)
    sl_r = max(abs(float(sl_pct)) / 100.0, 0.005)
    if int(leverage) > 10:
        sl_r = min(sl_r, 0.015)

    # ATR-based TP/SL like backtest: no fixed buffer that overrides intended %.
    # Exchange validates triggers vs last price; use the exact intended distances.
    if side == "buy":
        close_side = "sell"
        tp = mark * (1 + tp_r)
        sl = mark * (1 - sl_r)
    else:
        close_side = "buy"
        tp = mark * (1 - tp_r)
        sl = mark * (1 + sl_r)

    return tp, sl, close_side


def attach_tpsl_safe(
    client: Any,
    *,
    inst_id: str,
    side: str,
    contracts: str,
    mark: float,
    tp_pct: float = 2.0,
    sl_pct: float = 1.0,
    leverage: int = 3,
    account: dict[str, Any] | None = None,
    max_attempts: int = 4,
) -> dict[str, Any]:
    """Attach TP/SL; refresh mark and nudge triggers on BloFin rejections."""
    mark_px = float(mark or 0)
    if mark_px <= 0:
        mark_px = resolve_mark_price(client, inst_id, account=account)

    last: dict[str, Any] = {"code": "1", "msg": "no mark price"}
    tpsl_id: str | None = None
    verified_effective = False
    for attempt in range(max_attempts):
        if mark_px <= 0:
            mark_px = resolve_mark_price(client, inst_id, account=account)
        if mark_px <= 0:
            break

        tp, sl, close_side = compute_tpsl_triggers(
            side, mark_px, tp_pct=tp_pct, sl_pct=sl_pct, leverage=leverage
        )
        last = client.attach_tpsl(inst_id, None, close_side, contracts, tp, sl)
        if isinstance(last, dict):
            tpsl_id = (((last.get("data") or {}) or {}).get("tpslId") if isinstance(last.get("data"), dict) else None) or None
        if str(last.get("code")) in ("0", "0.0"):
            log_event(
                "trade",
                f"TP/SL attached {inst_id}",
                f"mark={mark_px:.6f} tp={tp:.6f} sl={sl:.6f}",
                {"instId": inst_id, "attempt": attempt + 1},
            )
            # Verify acceptance is effective (BloFin can quickly cancel TPSL if triggers invalid)
            if tpsl_id:
                try:
                    detail = client.get_order_tpsl_detail(inst_id=inst_id, tpsl_id=str(tpsl_id))
                    state = (detail.get("data") or {}).get("state")
                    if state and str(state).lower() not in ("effective", "live"):
                        log_event(
                            "trade",
                            f"TP/SL not effective after attach; retry",
                            f"inst={inst_id} tpslId={tpsl_id} state={state}",
                            {"attempt": attempt + 1},
                        )
                        # Nudge mark slightly and retry.
                        mark_px = mark_px * (0.998 if side == "buy" else 1.002)
                        continue
                    verified_effective = True
                except Exception:
                    # If detail verification fails, keep exchange code=0 as acceptance signal.
                    pass
            last["_tpsl_effective"] = verified_effective
            return last

        _, err = client.order_rejected(last)
        err_l = (err or str(last.get("msg") or "")).lower()
        log_event(
            "trade",
            f"TP/SL retry {inst_id}",
            f"attempt {attempt + 1}: {err_l[:160]}",
            {"mark": mark_px, "tp": tp, "sl": sl},
        )

        # BloFin commonly responds with:
        # - "TP trigger price should be higher than the latest trading price"
        # - "TP trigger price should be lower than the latest trading price"
        # - Similar wording for SL
        #
        # We fix this by nudging the base price (mark_px) in the direction that
        # moves the relevant trigger across the constraint.
        #
        # For buy:
        #   TP = mark * (1 + ...), SL = mark * (1 - ...)
        # For sell:
        #   TP = mark * (1 - ...), SL = mark * (1 + ...)
        if "tp trigger price" in err_l or "tp trigger" in err_l:
            if "higher" in err_l:
                # Need tpTrigger > lastTradePx
                mark_px = (resolve_mark_price(client, inst_id, account=account) or mark_px) * (1.005 if side == "buy" else 0.995)
            elif "lower" in err_l:
                mark_px = (resolve_mark_price(client, inst_id, account=account) or mark_px) * (0.995 if side == "buy" else 1.005)
        elif "sl trigger price" in err_l or "sl trigger" in err_l:
            if "higher" in err_l:
                mark_px = (resolve_mark_price(client, inst_id, account=account) or mark_px) * 1.005
            elif "lower" in err_l:
                mark_px = (resolve_mark_price(client, inst_id, account=account) or mark_px) * 0.995
        else:
            # Generic fallback: refresh mark, otherwise a tiny nudge.
            mark_px = resolve_mark_price(client, inst_id, account=account) or (mark_px * (1.001 if side == "buy" else 0.999))

    if isinstance(last, dict):
        last["_tpsl_effective"] = False
    return last
