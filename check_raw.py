from blofin.client import BlofinClient
import json

c = BlofinClient()
positions = c.get_positions()

print("=== RAW POSITIONS RESPONSE ===")
print(json.dumps(positions, indent=2)[:2000])