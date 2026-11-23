"""Cross-process admission and observed seat demand in the shared factory scratch."""

from __future__ import annotations

import json
import math
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from autobuild.domain import UsagePolicy, UsageReading
from autobuild.enforcement.subscription_admission import (
    assess_lane,
    instant,
    same_window,
    window_key,
)


# How long a released reservation stays counted against its lane.
RELEASED_RESERVATION_SECONDS = 90
# How old the last good reading may be when it stands in for a failed read. It
# never exceeds the time above, so an older reading cannot hide a finished seat
# whose reservation is no longer counted.
LAST_GOOD_READING_SECONDS = RELEASED_RESERVATION_SECONDS


def reading_or_last_good(
    reading: UsageReading, last_good: UsageReading | None, policy: UsagePolicy, now: datetime
) -> UsageReading:
    """``reading``, or ``last_good`` carrying the failure's error when the read
    failed without saying anything about the account and the earlier reading is
    young enough with every limit below the warning level."""

    if not reading.unread or last_good is None or not last_good.ok or not last_good.limits:
        return reading
    try:
        age = (now - instant(last_good.fetched_at)).total_seconds()
        usable = 0 <= age < LAST_GOOD_READING_SECONDS and all(
            0 <= limit.percent < policy.warn_percent for limit in last_good.limits
        )
    except (TypeError, ValueError, OverflowError):
        return reading
    return replace(last_good, error=reading.error) if usable else reading


@dataclass(frozen=True)
class SubscriptionLease:
    lease_id: str
    lane: str
    role: str


class SubscriptionAdmissionStore:
    def __init__(self, scratch_root: Path, *, now=None, read_only: bool = False):
        self.path = scratch_root / "subscription-admission" / "factory.db"
        self._now = now or (lambda: datetime.now(UTC))
        self._read_only = read_only

    @contextmanager
    def _transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS admissions (
                id TEXT PRIMARY KEY, lane TEXT, role TEXT, pid INTEGER, expires REAL,
                windows TEXT, released INTEGER NOT NULL DEFAULT 0)""")
            db.execute("""CREATE TABLE IF NOT EXISTS samples (
                lane TEXT, role TEXT, window TEXT, delta REAL, stamp REAL)""")
            yield db
            db.commit()
        finally:
            db.close()

    @contextmanager
    def _snapshot(self):
        """Open the shared ledger without creating or changing it.

        Status must report reservations already held by other worktrees.  It
        must not initialise the database merely because an operator asked to
        inspect it, so a missing ledger is represented by ``None``.
        """

        if not self.path.is_file():
            yield None
            return
        try:
            db = sqlite3.connect(
                f"{self.path.resolve(strict=False).as_uri()}?mode=ro",
                uri=True,
                timeout=15,
            )
        except sqlite3.OperationalError:
            # A concurrent cleanup can remove the ledger between the check and
            # the read-only open.  The observed state is then no ledger.
            if not self.path.is_file():
                yield None
                return
            raise
        try:
            yield db
        finally:
            db.close()

    def _projection(self, db, readings, policy, role, now):
        rows = []
        for lane, reading in readings.items():
            pending = {}
            if db is not None:
                for pid, expires, raw, released in db.execute(
                    "SELECT pid,expires,windows,released FROM admissions WHERE lane=?",
                    (lane,),
                ):
                    if expires <= now.timestamp() and (released or not _alive(pid)):
                        continue
                    for entry in json.loads(raw):
                        key = (entry["key"], entry["resets_at"])
                        pending[key] = (
                            pending.get(key, 0.0) + entry["estimated_seat_percent"]
                        )
            # Use the largest of the last eight comparable readings per window.
            # Concurrent foreground usage can inflate a sample, so keep the
            # history bounded and never interpret these deltas as billed tokens.
            estimates = {}
            for limit in reading.limits:
                key = window_key(limit)
                if db is None:
                    estimates[key] = 1.0
                else:
                    samples = db.execute(
                        """SELECT delta FROM samples WHERE lane=? AND role=? AND window=?
                         ORDER BY stamp DESC LIMIT 8""",
                        (lane, role, key),
                    )
                    estimates[key] = max((x[0] for x in samples), default=1.0)
            rows.append(assess_lane(lane, reading, policy, now, pending, estimates))
        eligible = [r for r in rows if r["eligible"]]
        best = max(eligible, key=lambda r: r["score"]) if eligible else None
        return dict(lanes=rows, recommendation={"lane": best["lane"]} if best else None)

    def reading_or_last_good(self, reading, last_good, policy):
        return reading_or_last_good(reading, last_good, policy, self._now())

    def preview(self, readings, policy, role):
        if self._read_only:
            with self._snapshot() as db:
                return self._projection(db, readings, policy, role, self._now())
        with self._transaction() as db:
            return self._projection(db, readings, policy, role, self._now())

    def acquire(self, readings, policy, role, timeout_seconds):
        lease, _ = self.reserve_with_decision(readings, policy, role, timeout_seconds)
        return lease

    def reserve_with_decision(self, readings, policy, role, timeout_seconds):
        if self._read_only:
            raise RuntimeError("read-only admission store cannot reserve a lane")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("invalid seat duration")
        with self._transaction() as db:
            now = self._now()
            decision = self._projection(db, readings, policy, role, now)
            if not decision["recommendation"]:
                return None, decision
            lane = decision["recommendation"]["lane"]
            windows = next(
                row["windows"] for row in decision["lanes"] if row["lane"] == lane
            )
            lease = SubscriptionLease(uuid4().hex, lane, role)
            db.execute(
                "INSERT INTO admissions VALUES(?,?,?,?,?,?,0)",
                (
                    lease.lease_id,
                    lane,
                    role,
                    os.getpid(),
                    now.timestamp() + timeout_seconds + 300,
                    json.dumps(windows),
                ),
            )
            db.execute(
                "DELETE FROM admissions WHERE released=1 AND expires <= ?",
                (now.timestamp(),),
            )
            return lease, decision

    def release(self, lease, observed_reading=None):
        if self._read_only:
            raise RuntimeError("read-only admission store cannot release a lane")
        with self._transaction() as db:
            now = self._now()
            row = db.execute(
                "SELECT windows FROM admissions WHERE id=? AND released=0",
                (lease.lease_id,),
            ).fetchone()
            if row is None:
                return False
            # Keep the reservation while a new caller may still see an older
            # rounded/cached reading (including a release with no post-reading).
            db.execute(
                "UPDATE admissions SET released=1,expires=? WHERE id=?",
                (now.timestamp() + RELEASED_RESERVATION_SECONDS, lease.lease_id),
            )
            if observed_reading and observed_reading.ok:
                limits = {window_key(w): w for w in observed_reading.limits}
                for previous in json.loads(row[0]):
                    k = previous["key"]
                    latest = limits.get(k)
                    if (
                        latest is None
                        or not latest.resets_at
                        or not same_window(latest.resets_at, previous["resets_at"])
                    ):
                        continue
                    delta = latest.percent - previous["used_percent"]
                    if math.isfinite(delta) and 0 <= delta <= 100:
                        db.execute(
                            "INSERT INTO samples VALUES(?,?,?,?,?)",
                            (lease.lane, lease.role, k, delta, now.timestamp()),
                        )
            db.execute(
                "DELETE FROM samples WHERE stamp < ?",
                ((now - timedelta(days=14)).timestamp(),),
            )
            return True


def _alive(pid):
    # Never signal a process on Windows. A stuck lease after its stated timeout
    # is safer than dropping a reservation for a still-running turn.
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
