"""Time source for persisted timestamps (epoch seconds)."""

import time


def now():
    return int(time.time())
