from blofin.client import BlofinClient

c = BlofinClient()

# Verify the TP/SL was actually set on all positions
positions = c.get_positions()
data = positions.get('data', [])

for p in data:
    if float(p.get('positions', 0)) == 0:
        continue
    
    inst = p['instId']
    pos_side = p['positionSide']
    raw = float(p['positions'])
    
    if raw > 0:
        side = 'buy'
        close_side = 'sell'
    else:
        side = 'sell'
        close_side = 'buy'
    
    contracts = c._format_order_size(inst, abs(raw))
    mark = float(p['markPrice'])
    
    # 2% TP, 1% SL
    if side == 'buy':
        tp_price = mark * 1.02
        sl_price = mark * 0.99
    else:
        tp_price = mark * 0.98
        sl_price = mark * 1.01
    
    print(f"Setting TP/SL on {inst}: side={close_side} contracts={contracts} tp={tp_price:.6f} sl={sl_price:.6f}")
    resp = c.attach_tpsl(inst, None, close_side, contracts, tp_price, sl_price)
    print(f"  Response: {resp}")
