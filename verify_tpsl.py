from blofin.client import BlofinClient
import json

c = BlofinClient()

# Verify TP/SL are now set on positions
positions = c.get_positions()
data = positions.get('data', [])

print("=== VERIFYING TP/SL AFTER ATTACH ===")
for p in data:
    if float(p.get('positions', 0)) != 0:
        tp = p.get('tp_price', p.get('tpPrice', 'NONE'))
        sl = p.get('sl_price', p.get('slPrice', 'NONE'))
        print(f"{p['instId']}: TP={tp} SL={sl}")

# Also check orders endpoint to see if TP/SL orders exist
print("\n=== CHECKING FOR TP/SL ORDERS VIA OPEN ORDERS ===")
# Try to get open orders to verify TP/SL exist
for p in data:
    if float(p.get('positions', 0)) != 0:
        inst = p['instId']
        try:
            orders = c.get_positions(inst_id=inst)
            print(f"{inst}: {json.dumps(orders, indent=2)[:500]}")
        except Exception as e:
            print(f"{inst}: Error - {e}")