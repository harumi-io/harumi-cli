"""Minimal smoke test — no third-party dependencies required.

Good for a first `harumi run` to confirm your CLI, auth, and notebook id
are all wired up correctly before trying a real solver script.

    harumi run demos/hello_world.py --notebook <NOTEBOOK_ID> --mode interactive
"""

import platform
import sys


def fib(n: int) -> int:
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a


print(f"Hello from Harumi! Python {sys.version.split()[0]} on {platform.system()}")
print(f"First 10 Fibonacci numbers: {[fib(i) for i in range(10)]}")
