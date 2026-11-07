"""Adapted from R5 live_exec/order_loop.py (2026-11-07).

一批委托的生命周期：先落盘再发送、并行轮询、每笔独立超时撤单、剩余数量有限次重发、崩溃后续跑。

与策略无关：请求内容由调用方的 make_request 决定（必须对同一 leg/attempt/quantity 给出同一个请求）。
状态文件只记"发了什么、网关回了什么"，不记资金。任何不确定都抛 Stop，绝不换号重发不确定的单。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .client import (
    REPRICE,
    RETRY_LATER,
    RETRY_NOW_NEW_ID,
    SKIP,
    SKIP_OWNED,
    Refused,
    Transport,
    is_terminal,
)
from contextlib import contextmanager
from datetime import datetime, timedelta, time
from ..events import CN

NO_IDENTITY = "NO_AUTHORITATIVE_ORDER"


class Wait(Exception):
    """This leg lacks a fresh executable quote."""


class Skip(Exception):
    """Confirmed no executable residual for this leg."""


class Stop(Exception):
    """当天剩余动作全部停止，等人处理。"""


class UnresolvedCancel(Stop):
    """Retain this order and its reservations until its terminal is proven."""


@dataclass(frozen=True)
class Leg:
    key: str  # 计划内唯一，例如 "03B"
    code: str
    side: str  # BUY / SELL
    quantity: int  # 本阶段需要成交的总量
    wait_seconds: float
    max_attempts: int
    deadline: datetime
    deferrable: bool = False


@dataclass
class Policy:
    poll_seconds: float = 2.0
    replay_limit: int = 3  # 超时/5xx/断连时同一请求最多再发几次
    replay_pause: float = 2.0
    unknown_limit: float = 300.0  # 一直没有柜台身份超过这么久就停
    cancel_grace: float = 60.0  # 撤单后这么久还不终态就停（网关撤单确认窗口 15 秒）
    cancel_spacing: float = 1.0  # 相邻撤单最少间隔
    refresh_failure_limit: float = 60.0


@dataclass
class LegResult:
    leg: Leg
    filled: int = 0
    intents: list = field(default_factory=list)
    status: str = "PENDING"  # FILLED / INCOMPLETE / SKIPPED
    reason: str = ""
    requested: int | None = None


class StateFile:
    """execution_state.json：每个请求在发出前写入；原子替换，崩溃不留半个文件。"""

    def __init__(self, path, identity):
        self.path = Path(path)
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if self.data.get("identity") != identity:
                raise Stop("State file belongs to another plan/day")
        else:
            self.data = {"identity": identity, "legs": {}, "requests": {}}
            self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        with open(temp, "w", encoding="utf-8") as stream:
            json.dump(self.data, stream, ensure_ascii=False, indent=1, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.path)

    def attempts(self, leg):
        return self.data["legs"].setdefault(leg.key, [])

    def put_request(self, leg, request):
        rid = request["request_id"]
        known = self.data["requests"].get(rid)
        if known is not None and known["request"] != request:
            raise Stop(f"Request {rid} differs from the persisted one")
        if known is None:
            self.data["requests"][rid] = {
                "request": request,
                "intent_id": None,
                "last": None,
                "refused": None,
            }
            self.attempts(leg).append(rid)
            self.save()
        return self.data["requests"][rid]

    def note(self, rid, **values):
        self.data["requests"][rid].update(values)
        self.save()


@contextmanager
def exclusive_run(folder):
    """The kernel releases the lock after a crash, including on mounted Windows drives."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "execution.lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            if stream.seek(0, 2) == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise Stop("Another executor owns this run") from exc
        else:
            import fcntl

            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Stop("Another executor owns this run") from exc
        yield


def session_open(now):
    local = now.astimezone(CN)
    return local.weekday() < 5 and (
        time(9, 30) <= local.time() < time(11, 30) or time(13) <= local.time() < time(14, 57)
    )


class OrderLoop:
    """Nonblocking per-leg scheduling; one persisted request per attempt."""

    def __init__(
        self,
        client,
        state,
        make_request,
        *,
        now,
        sleep,
        policy=None,
        retry_seconds=60,
        quote_age=60,
        cancel_check=None,
        submit_check=None,
        single_attempt=False,
    ):
        self.client, self.state, self.make_request = client, state, make_request
        self.now, self.sleep = now, sleep
        self.policy = policy or Policy()
        self.retry_seconds = retry_seconds
        self.quote_age = quote_age
        self.cancel_check = cancel_check
        self.submit_check = submit_check
        self.single_attempt = single_attempt
        self._last_cancel = None

    def submit(self, entry, deadline):
        req = entry["request"]
        rid = req["request_id"]
        for attempt in range(self.policy.replay_limit + 1):
            if self.now() >= deadline or not session_open(self.now()):
                raise Stop(f"{rid}: submission window closed, reconcile recorded request")
            age = (self.now() - datetime.fromisoformat(entry["facts"]["source_at"])).total_seconds()
            if not 0 <= age <= self.quote_age:
                raise Stop(f"{rid}: recorded quote expired; reconcile without a fresh request")
            # Every POST may be the first actual dispatch (even a transport replay).
            # Recheck local announcements and any one-order authorization here.
            if self.submit_check is not None:
                self.submit_check(req)
            if self.now() >= deadline or not session_open(self.now()):
                raise Stop(f"{rid}: submission window closed after final checks")
            try:
                answer = self.client.submit(req)
            except Transport:
                if attempt == self.policy.replay_limit:
                    raise Stop(f"{rid}: unknown submission; retain request identity") from None
                self.sleep(self.policy.replay_pause)
                continue
            except Refused as exc:
                self.state.note(
                    rid, refused=dict(status=exc.status, reason=exc.reason, action=exc.action)
                )
                return
            if not isinstance(answer, dict) or not answer.get("intent_id"):
                raise Stop(f"{rid}: missing intent identity")
            self.state.note(
                rid,
                intent_id=answer["intent_id"],
                sent_at=self.now().isoformat(),
                last={"intent_id": answer["intent_id"], "status": answer.get("status")},
            )
            return

    def refresh(self, entry):
        rid = entry["request"]["request_id"]
        try:
            intent = self.client.refresh(entry["intent_id"])
        except Transport:
            first = entry.get("refresh_failing_since") or self.now().isoformat()
            self.state.note(rid, refresh_failing_since=first)
            if (
                self.now() - datetime.fromisoformat(first)
            ).total_seconds() >= self.policy.refresh_failure_limit:
                raise Stop(f"{rid}: refresh outcome unknown") from None
            return None
        req = entry["request"]
        if (
            intent.get("intent_id") != entry["intent_id"]
            or intent.get("request_id") != rid
            or intent.get("strategy_id") != "v12"
            or intent.get("security") != req["stock_code"]
            or intent.get("side") != ("BUY" if req["direction"] == 23 else "SELL")
            or intent.get("quantity") != req["volume"]
            or type(intent.get("filled")) is not int
            or not 0 <= intent["filled"] <= req["volume"]
        ):
            raise Stop(f"{rid}: inconsistent intent identity or fill")
        if intent["filled"] < (entry.get("last") or {}).get("filled", 0):
            raise Stop(f"{rid}: cumulative fill went backwards")
        keep = (
            "intent_id",
            "strategy_id",
            "request_id",
            "security",
            "side",
            "quantity",
            "state",
            "filled",
            "fees_confirmed",
            "final_fee_cents",
            "reason",
        )
        self.state.note(
            rid, last={k: intent[k] for k in keep if k in intent}, refresh_failing_since=None
        )
        return intent

    def service(self, entry, *, wait, deadline):
        """Return the terminal view; only a known terminal releases a leg."""
        intent = self.refresh(entry)
        if intent is None or is_terminal(intent):
            return intent
        now = self.now()
        rid = entry["request"]["request_id"]
        age = (now - datetime.fromisoformat(entry["sent_at"])).total_seconds()
        if entry.get("cancel_requested_at"):
            cancel_age = (now - datetime.fromisoformat(entry["cancel_requested_at"])).total_seconds()
            if cancel_age >= self.policy.cancel_grace:
                raise UnresolvedCancel(f"{rid}: cancellation unconfirmed")
        elif intent.get("reason") == NO_IDENTITY:
            if age >= self.policy.unknown_limit or now >= deadline:
                raise Stop(f"{rid}: broker identity unknown")
        elif intent.get("state") in ("DONE", "SELL_SETTLEMENT_PENDING"):
            if age >= wait + self.policy.unknown_limit:
                raise Stop(f"{rid}: waiting for settlement or fee confirmation")
        elif age >= wait or now >= deadline:
            if not session_open(now):
                if now >= deadline:
                    raise Stop(f"{rid}: cancellation outside session, unresolved")
                return None
            if self.cancel_check is not None and not self.cancel_check(entry["request"]["stock_code"]):
                self.state.note(rid, cancel_hold_reason="QDII_CANCEL_STATE_UNVERIFIED")
                raise UnresolvedCancel(f"{rid}: QDII cancellation eligibility unverified")
            if self._last_cancel:
                wait_more = self.policy.cancel_spacing - (now - self._last_cancel).total_seconds()
                if wait_more > 0:
                    self.sleep(wait_more)
            if not session_open(self.now()):
                raise UnresolvedCancel(f"{rid}: cancellation session ended")
            self._last_cancel = self.now()
            self.state.note(rid, cancel_requested_at=self._last_cancel.isoformat())
            try:
                self.client.cancel(entry["intent_id"])
            except (Transport, Refused):
                confirmed = self.refresh(entry)
                if confirmed and is_terminal(confirmed):
                    return confirmed
                raise UnresolvedCancel(f"{rid}: cancellation unresolved; inspect before retry") from None
        return None

    def run(self, legs):
        results = {leg.key: LegResult(leg) for leg in legs}
        retry_at, working, repriced, settled = {}, {}, set(), {}
        domestic_sells = [r for r in results.values() if r.leg.side == "SELL" and not r.leg.deferrable]
        while any(r.status == "PENDING" for r in results.values()):
            if self.submit_check is not None:
                self.submit_check(None)  # also invalidate a plan while waiting/working
            for r in results.values():
                leg = r.leg
                if r.status != "PENDING":
                    continue
                if leg.key in working:
                    entry = working[leg.key]
                    try:
                        answer = self.service(entry, wait=leg.wait_seconds, deadline=leg.deadline)
                    except UnresolvedCancel as exc:
                        if not leg.deferrable:
                            raise
                        r.status, r.reason = "UNRESOLVED", str(exc)
                        del working[leg.key]
                        continue
                    if answer is None:
                        continue
                    del working[leg.key]
                    r.intents.append(entry["intent_id"])
                    r.filled += answer["filled"]
                    settled[leg.key] = settled.get(leg.key, 0) + 1
                    if r.requested is not None and r.filled >= r.requested:
                        r.status = "FILLED"
                        continue
                    if answer["filled"] == entry["request"]["volume"]:
                        r.reason = entry["facts"]["size_reason"]
                        r.status = "SATISFIED" if r.reason == "TARGET_AT_LIMIT" else "INCOMPLETE"
                        continue
                    if self.single_attempt or (not leg.deferrable and settled[leg.key] >= leg.max_attempts):
                        r.status, r.reason = "INCOMPLETE", "NOT_FILLED_IN_TIME"
                        continue
                    retry_at[leg.key] = self.now() + timedelta(
                        seconds=self.retry_seconds if leg.deferrable else 0
                    )
                if self.now() >= leg.deadline:
                    r.status, r.reason = "INCOMPLETE", r.reason or "DEADLINE"
                    continue
                if leg.side == "BUY" and any(s.status == "PENDING" for s in domestic_sells):
                    continue
                if leg.side == "BUY" and any(
                    s.status not in ("FILLED", "SATISFIED") for s in domestic_sells
                ):
                    r.status, r.reason = "SKIPPED", "DOMESTIC_SELL_INCOMPLETE"
                    continue
                if not session_open(self.now()) or (
                    leg.key in retry_at and self.now() < retry_at[leg.key]
                ):
                    continue
                number = len(self.state.attempts(leg)) + 1
                try:
                    remaining = None if r.requested is None else r.requested - r.filled
                    request, facts = self.make_request(leg, number, r.filled, remaining)
                except Wait as exc:
                    r.reason = str(exc)
                    retry_at[leg.key] = self.now() + timedelta(seconds=self.retry_seconds)
                    continue
                except Skip as exc:
                    r.reason = str(exc)
                    r.status = "SATISFIED" if r.reason == "TARGET_REACHED" else "INCOMPLETE"
                    continue
                if self.now() >= leg.deadline or not session_open(self.now()):
                    continue
                entry = self.state.put_request(leg, request)
                rid = request["request_id"]
                self.state.note(rid, facts=facts)
                self.submit(entry, leg.deadline)
                if not entry["refused"]:
                    if r.requested is None:
                        r.requested = facts["desired_quantity"]
                    working[leg.key] = entry
                    r.reason = ""
                    continue
                refusal = entry["refused"]
                r.reason = refusal["reason"]
                action = refusal["action"]
                if self.single_attempt:
                    r.status = "SKIPPED"  # no new ID after any confirmed trial refusal
                    continue
                if action == REPRICE and leg.key not in repriced:
                    repriced.add(leg.key)
                    if leg.deferrable:
                        retry_at[leg.key] = self.now() + timedelta(seconds=self.retry_seconds)
                elif action in (RETRY_NOW_NEW_ID, RETRY_LATER):
                    pause = self.retry_seconds if leg.deferrable or action == RETRY_LATER else 3
                    retry_at[leg.key] = self.now() + timedelta(seconds=pause)
                elif action in (SKIP, SKIP_OWNED, REPRICE, RETRY_LATER):
                    r.status = "SKIPPED"
                    if action == SKIP_OWNED:
                        r.reason = "NOT_OWNED:" + leg.code
                else:
                    raise Stop(f"{leg.code}: {r.reason}")
            if any(r.status == "PENDING" for r in results.values()):
                delays = [self.policy.poll_seconds]
                instant = self.now()
                delays.extend(
                    (r.leg.deadline - instant).total_seconds()
                    for r in results.values()
                    if r.status == "PENDING" and r.leg.deadline > instant
                )
                self.sleep(min(delays))
        return results

    def recover_identity(self, rid, entry):
        """Find the already submitted request among working and completed events."""
        matches = [item for item in self.client.intents() if item.get("request_id") == rid]
        if not matches:
            day = self.state.data["identity"]["day"].replace("-", "")
            facts = self.client.events(day)
            if facts.get("event_day") != day:
                raise Stop("Recovery day mismatch")
            rejected = [
                row
                for row in facts.get("local_rejections", [])
                if row.get("request_id") == rid and row.get("submission_outcome") == "NOT_SUBMITTED"
            ]
            if len(rejected) == 1:
                self.state.note(rid, refused={"action": "RECOVERED_REFUSAL", "reason": "NOT_SUBMITTED"})
                return
            references = {
                row["intent_id"]
                for row in facts["orders"] + facts["trades"]
                if row.get("request_id") == rid
                or row.get("order_remark") == entry["request"]["order_remark"]
            }
            matches = [self.client.order(iid) for iid in references]
            matches = [
                item
                for item in matches
                if item.get("request_id") == rid and item.get("strategy_id") == "v12"
            ]
        if len(matches) != 1 or not matches[0].get("intent_id"):
            raise Stop("Outcome unknown; no new submission during recovery")
        self.state.note(rid, intent_id=matches[0]["intent_id"], sent_at=self.now().isoformat())

    def drain(self, deadline):
        """Settle verified outstanding requests independently. Never submit an order here."""
        pending = {
            rid: entry
            for rid, entry in self.state.data["requests"].items()
            if not entry["refused"] and not (entry["last"] and is_terminal(entry["last"]))
        }
        unresolved = []
        while pending:
            for rid, entry in list(pending.items()):
                try:
                    if not entry["intent_id"]:
                        self.recover_identity(rid, entry)
                    if entry["refused"] or self.service(entry, wait=0, deadline=deadline) is not None:
                        del pending[rid]
                except (Stop, Transport, Refused) as exc:
                    unresolved.append(f"{rid}: {exc}")
                    del pending[rid]
            if pending:
                self.sleep(self.policy.poll_seconds)
        if unresolved:
            raise Stop("Unresolved on recovery: " + " | ".join(unresolved))
