"""Pure watchdog decision logic; process cancellation stays in the supervisor."""
from __future__ import annotations

from .models import WatchdogAction, WorkerSession


class Watchdog:
    def check(self, now: float, sessions: list[WorkerSession]) -> list[WatchdogAction]:
        return [
            WatchdogAction("timeout", session.id, session.node_id, "worker deadline exceeded")
            for session in sessions
            if session.status == "running" and session.deadline > 0 and now > session.deadline
        ]
