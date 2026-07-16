from blofin.client import BlofinClient
import json

c = BlofinClient()
positions = c.get_positions()
data = positions.get('data', [])

print("=== LIVE POSITIONS WITH TP/SL STATUS ===")
for p in data:
    size = float(p.get('size', p.get('positions', 0)))
    if size == 0:
        continue
    tp = p.get('tp_price', p.get('tpPrice', p.get('tpTriggerPrice', 'NONE')))
    sl = p.get('sl_price', p.get('slPrice', p.get('slTriggerPrice', 'NONE')))
    print(f"{p['instId']}: side={p.get('side', p.get('positionSide', '?'))} size={size} entry={p.get('averagePrice', p.get('entry', '?'))} TP={tp} SL={sl}")

print()
print("=== CHECKING OPEN ORDERS FOR TP/SL ===")
for p in data:
    size = float(p.get('size', p.get('positions', 0)))
    if size == 0:
        continue
    inst = p['instId']
    orders = c.get_orders_tpsl_pending(inst_id=inst)
    rows = orders.get('data') or []
    tp_orders = [o for o in rows if o.get('side') and 'take_profit' in str(o.get('side', '')).lower()] or [o for o in rows if o.get('tpTriggerPrice')]
    sl_orders = [o for o in rows if o.get('side') and 'stop_loss' in str(o.get('side', '')).lower()] or [o for o in rows if o.get('slTriggerPrice')]
    # Better: just show any tpsl orders
    print(f"{inst}: pending_tpsl_orders={len(rows)}")
    for o in rows[:4]:
        print(f"  tpslId={o.get('tpslId')} side={o.get('side')} state={o.get('state')} tp={o.get('tpTriggerPrice')} sl={o.get('slTriggerPrice')}")
