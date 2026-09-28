"""Monthly and daily usage limits for free-plan email providers.

One JSON file per provider under the private state directory:
{"month": "2026-10", "used": 12, "day": "2026-10-03", "used_before_today": 9}

- `check()` refuses before a call once the month's free allowance is used, or
  once today's even share of what is left has been spent (so a single day
  cannot use a whole month).
- `spend()` records a used credit. Providers that charge only for a found
  email record only then; providers whose no-result policy is unknown record
  every call (conservative).
"""
from __future__ import annotations

import calendar
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from jobagent.runtime import atomic_json


class LimitReached(Exception):
    kind = "free_limit_reached"


class Credits:
    def __init__(self, path, monthly_limit, name):
        self.path, self.limit, self.name = Path(path), max(0, int(monthly_limit)), name

    def _load(self):
        now = datetime.now(timezone.utc)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        except (OSError, ValueError):
            data = {"month": now.strftime("%Y-%m"), "used": self.limit}  # unreadable: assume used up
        if data.get("month") != now.strftime("%Y-%m"):
            data = {"month": now.strftime("%Y-%m"), "used": 0}
        if data.get("day") != now.strftime("%Y-%m-%d"):
            data.update(day=now.strftime("%Y-%m-%d"), used_before_today=data["used"])
        return data, now

    def allowance_today(self):
        data, now = self._load()
        days_left = calendar.monthrange(now.year, now.month)[1] - now.day + 1
        return math.ceil(max(0, self.limit - data["used_before_today"]) / days_left), data

    def check(self):
        allowance, data = self.allowance_today()
        if data["used"] >= self.limit:
            raise LimitReached(f"{self.name}: free monthly allowance ({self.limit}) used")
        if data["used"] - data["used_before_today"] >= allowance:
            raise LimitReached(f"{self.name}: today's share ({allowance}) of the free allowance used")

    def spend(self, n=1):
        data, _now = self._load()
        data["used"] += n
        atomic_json(self.path, data)

    def summary(self):
        allowance, data = self.allowance_today()
        return f"{self.name} {data['used']}/{self.limit} this month, today's share {allowance}"
