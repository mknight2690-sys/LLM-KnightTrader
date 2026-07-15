from __future__ import annotations

from blofin.client import BlofinClient
from trader.state import load_state
from trader.tpsl import resolve_mark_price, attach_tpsl_safe


def _live_tpsl_rows(client: BlofinClient, inst: str) -> list[dict]:
    pending = client.get_orders_tpsl_pending(inst_id=inst)
    rows = pending.get("data") or []
    live = []
    for r in rows:
        if str(r.get("state") or "").lower() == "live":
            live.append(r)
    return live


def main() -> None:
    client = BlofinClient()
    _state = load_state()
    account = client.parse_account_snapshot(force=True)
    positions = account.get("positions") or []

    print("positions", len(positions))
    attached: list[dict] = []

    for pos in positions:
        inst = str(pos.get("instId") or "")
        raw = float(pos.get("size") or pos.get("positions") or 0)
        if not inst or abs(raw) <= 0:
            continue

        live_rows = _live_tpsl_rows(client, inst)
        if live_rows:
            continue

        side = "buy" if raw > 0 else "sell"
        contracts = client._format_order_size(inst, abs(raw))
        mark = resolve_mark_price(client, inst, account=account)
        if mark <= 0:
            continue

        print("Attaching", inst, "side", side, "contracts", contracts, "mark", mark)
        resp = attach_tpsl_safe(
            client,
            inst_id=inst,
            side=side,
            contracts=contracts,
            mark=mark,
            tp_pct=5.0,
            sl_pct=2.0,
            leverage=int(float(pos.get("leverage") or 3)),
            account=account,
            max_attempts=8,
        )
        tpsl_id = ((resp.get("data") or {}) if isinstance(resp.get("data"), dict) else {}).get("tpslId")
        live_after = _live_tpsl_rows(client, inst)
        print("  attach_code", resp.get("code"), "tpslId", tpsl_id, "live_after", len(live_after))
        attached.append({"inst": inst, "code": resp.get("code"), "tpslId": tpsl_id, "live_after": len(live_after)})

    print("attached_summary")
    for row in attached:
        print(row)


if __name__ == "__main__":
    main()

