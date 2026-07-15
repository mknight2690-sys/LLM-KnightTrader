from blofin.client import BlofinClient

c = BlofinClient()

# Verify the TP/SL was actually set on XRP-USDT
positions = c.get_positions()
data = positions.get('data', [])

for p in data:
    if p['instId'] == 'XRP-USDT':
        print(f"XRP-USDT after attach:")
        print(f"  tp_price: {p.get('tp_price', 'NONE')}")
        print(f"  sl_price: {p.get('sl_price', 'NONE')}")
        print(f"  tpPrice: {p.get('tpPrice', 'NONE')}")
        print(f"  slPrice: {p.get('slPrice', 'NONE')}")
        break

# Now try to attach TP/SL on all positions that don't have them
print("\n=== Attaching TP/SL on all unprotected positions ===")
for p in data:
    if float(p.get('positions', 0)) == 0:
        continue
    
    tp = p.get('tp_price', p.get('tpPrice', None))
    sl = p.get('sl_price', p.get('slPrice', None))
    
    if tp is not None and sl is not None:
        print(f"{p['instId']}: Already has TP/SL")
        continue
    
    inst = p['instId']
    pos_side = p['positionSide']
    if pos_side == 'long':
        side = 'buy'
    elif pos_side == 'short':
        side = 'sell'
    else:
        # net mode - determine from entry vs mark
        if float(p['markPrice']) > float(p['averagePrice']):
            side = 'buy'
        else:
            side = 'sell'
    
    contracts = p['positions']
    mark = float(p['markPrice'])
    
    # 2% TP, 1% SL
    if side == 'buy':
        tp_price = mark * 1.02
        sl_price = mark * 0.99
        close_side = 'sell'
    else:
        tp_price = mark * 0.98
        sl_price = mark * 1.01
        close_side = 'buy'
    
    print(f"Setting TP/SL on {inst}: side={side} tp={tp_price:.6f} sl={sl_price:.6f}")
    resp = c.attach_tpsl(inst, None, close_side, contracts, tp_price, sl_price)
    print(f"  Response: {resp}")