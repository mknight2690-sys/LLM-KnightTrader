"""Wrapper to launch trader.agent as a persistent process.

Windows subprocess + `-c` causes the parent Python to exit before the trader
main loop stabilizes. Running via a script file avoids this.
"""
from trader.agent import main

if __name__ == "__main__":
    main()
