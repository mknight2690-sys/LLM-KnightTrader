from __future__ import annotations

from blofin.client import BlofinClient
from trader.sync_tpsl import sync_tpsl
from blofin.account_cache import get_account_snapshot


def main() -> None:
    client = BlofinClient()
    account = get_account_snapshot(force=True)
    summary = sync_tpsl(client, account)
    print(summary)


if __name__ == "__main__":
    main()
