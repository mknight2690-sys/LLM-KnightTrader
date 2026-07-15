from blofin.client import BlofinClient

c = BlofinClient()

# Check one position and try to attach TP/SL
positions = c.get_positions()
data = positions.get('data', [])

# Find a position to test
test_pos = None
for p in data:
    if float(p.get('positions', 0)) != 0:
        test_pos = p
        break

if test_pos:
    inst = test_pos['instId']
    side = 'buy' if test_pos['positionSide'] == 'long' else 'sell'
    if test_pos['positionSide'] == 'net':
        # For net mode, determine from averagePrice vs markPrice
        if float(test_pos['markPrice']) > float(test_pos['averagePrice']):
            side = 'buy'
        else:
            side = 'sell'
    
    contracts = test_pos['positions']
    mark = float(test_pos['markPrice'])
    
    print(f"Testing TP/SL on {inst}")
    print(f"  side: {side}")
    print(f"  contracts: {contracts}")
    print(f"  mark: {mark}")
    print(f"  entry: {test_pos['averagePrice']}")
    
    # Try 2% TP, 1% SL
    tp = mark * 1.02 if side == 'buy' else mark * 0.98
    sl = mark * 0.99 if side == 'buy' else mark * 1.01
    
    print(f"  Attempting TP={tp:.6f} SL={sl:.6f}")
    
    resp = c.attach_tpsl(inst, None, 'sell' if side == 'buy' else 'buy', contracts, tp, sl)
    print(f"  Response: {resp}")
else:
    print("No positions found")