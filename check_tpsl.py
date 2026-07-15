from blofin.client import BlofinClient
import json

c = BlofinClient()
positions = c.get_positions()

print("=== LIVE POSITIONS WITH TP/SL STATUS ===")
for p in positions:
    if p.get('size', 0) != 0:
        tp = p.get('tp_price', 'NONE')
        sl = p.get('sl_price', 'NONE')
        print(f"{p['instId']}: side={p['side']} size={p['size']} entry={p['entry']} TP={tp} SL={sl}")

print()
print("=== CHECKING OPEN ORDERS FOR TP/SL ===")
for p in positions:
    if p.get('size', 0) != 0:
        orders = c.get_orders(instId=p['instId'], instType='SWAP')
        tp_orders = [o for o in orders if o.get('ordType') == 'take_profit']
        sl_orders = [o for o in orders if o.get('ordType') == 'stop_loss']
        print(f"{p['instId']}: TP_orders={len(tp_orders)} SL_orders={len(sl_orders)}")
        if tp_orders:
            for o in tp_orders:
                print(f"  TP: {o.get('tpTriggerPx', o.get('triggerPx', 'N/A'))} @ {o.get('px', 'N/A')}")
        if sl_orders:
            for o in sl_orders:
                print(f"  SL: {o.get('slTriggerPx', o.get('triggerPx', 'N/A'))} @ {o.get('px', 'N/A')}")