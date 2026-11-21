"""Pre-generated conversation trails.

A trail is one whole email conversation (opener, recipient, subject and every message in order),
written ahead of time by the LLM and stored one per line (JSON Lines) in a plain text file:

    {"id": "t00001", "opener": "a@x.com", "recipient": "b@gmail.com", "subject": "...", "topic": "...",
     "messages": [{"turn": 1, "from": "a@x.com", "body": "..."}, {"turn": 2, "from": "b@gmail.com", ...}]}

`generate_trails` appends trails until a stock of unsent emails is reached. The engine consumes them
(see Engine._reserve_trail / _compose); which trail is unused, active or done lives in the DB table
`trail_state`, never in the file, so the file stays append-only and safe to regenerate or edit."""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .content import DEFAULT_TOPICS, ContentError, validate

log = logging.getLogger("warmup.trails")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class GenerationStopped(Exception):
    """The LLM stayed unavailable too long. Everything written so far is intact; run `generate` again."""


@dataclass
class Trail:
    id: str
    opener: str
    recipient: str
    subject: str
    topic: str
    messages: list[dict]

    @property
    def total(self) -> int:
        return len(self.messages)


def parse_trail(rec) -> Trail | str:
    """A Trail, or the reason the record is unusable."""
    if not isinstance(rec, dict):
        return "not an object"
    tid, a, b, subj = rec.get("id"), rec.get("opener"), rec.get("recipient"), rec.get("subject")
    if not (isinstance(tid, str) and tid.strip()):
        return "missing id"
    for v in (a, b):
        if not (isinstance(v, str) and EMAIL.match(v)):
            return f"bad address {v!r}"
    a, b = a.lower(), b.lower()
    if a == b:
        return "opener and recipient are the same"
    if not (isinstance(subj, str) and 3 <= len(subj) <= 120 and "\n" not in subj):
        return "bad subject"
    msgs = rec.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return "no messages"
    out = []
    for i, m in enumerate(msgs, 1):
        if not isinstance(m, dict) or m.get("turn") != i:
            return f"message {i}: turns must be 1..n in order"
        want = a if i % 2 == 1 else b
        if str(m.get("from", "")).lower() != want:
            return f"message {i}: must be from {want} (senders alternate)"
        body = m.get("body")
        if not isinstance(body, str):
            return f"message {i}: no body"
        why = validate(body)
        if why:
            return f"message {i}: {why}"
        out.append({"turn": i, "from": want, "body": body})
    return Trail(tid.strip(), a, b, subj.strip(), str(rec.get("topic", "")), out)


class TrailStore:
    """Read side (and appender) for the trails file. Safe to use while `generate` is writing."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.trails: dict[str, Trail] = {}
        self.order: list[str] = []
        self.skipped = 0
        self._sig: tuple | None = None

    def refresh(self) -> bool:
        """Re-read the file if it changed. A last line without its newline may be mid-write: ignored
        until it is complete."""
        try:
            st = self.path.stat()
        except FileNotFoundError:
            changed = bool(self.trails)
            self.trails, self.order, self.skipped, self._sig = {}, [], 0, None
            return changed
        sig = (st.st_mtime_ns, st.st_size)
        if sig == self._sig:
            return False
        lines = self.path.read_text(encoding="utf-8").split("\n")[:-1]
        trails: dict[str, Trail] = {}
        order: list[str] = []
        skipped = 0
        for n, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                t = parse_trail(json.loads(line))
            except json.JSONDecodeError:
                t = "not valid JSON"
            if isinstance(t, str):
                skipped += 1
                log.warning("%s line %d skipped: %s", self.path.name, n, t)
            elif t.id in trails:
                skipped += 1
                log.warning("%s line %d skipped: duplicate id %s", self.path.name, n, t.id)
            else:
                trails[t.id] = t
                order.append(t.id)
        self.trails, self.order, self.skipped, self._sig = trails, order, skipped, sig
        return True

    def get(self, tid: str | None) -> Trail | None:
        return self.trails.get(tid) if tid else None

    def next_id(self) -> str:
        nums = [int(m.group(1)) for t in self.trails if (m := re.fullmatch(r"t(\d+)", t))]
        return f"t{(max(nums) if nums else 0) + 1:05d}"

    def append(self, rec: dict) -> Trail:
        t = parse_trail(rec)
        if isinstance(t, str):
            raise ValueError(f"refusing to write an invalid trail: {t}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size:
            with open(self.path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                needs_nl = f.read(1) != b"\n"  # a crash may have left a partial line: terminate it
        else:
            needs_nl = False
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(("\n" if needs_nl else "") + json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self.refresh()
        return t


# ---------------------------------------------------------------- state helpers (DB)
def used_ids(db) -> set[str]:
    return {r["trail_id"] for r in db.q("select trail_id from trail_state")}


def eligible(t: Trail, accounts) -> bool:
    return t.opener in accounts and t.recipient in accounts


def unsent(store: TrailStore, db, accounts) -> list[Trail]:
    used = used_ids(db)
    return [store.trails[i] for i in store.order if i not in used and eligible(store.trails[i], accounts)]


def plan_turns(n: int, lo: int, hi: int, rng: random.Random) -> list[int]:
    """Thread lengths in [lo, hi] that sum to exactly n."""
    if not any(k * lo <= n <= k * hi for k in range(1, n // max(lo, 1) + 1)):
        raise ValueError(f"cannot split {n} emails into threads of {lo} to {hi} emails each")
    plan: list[int] = []
    left = n
    while left > 0:
        choices = [t for t in range(lo, hi + 1) if left - t == 0 or left - t >= lo]
        if not choices:                      # remainder is too small: give the last thread back and re-split
            left += plan.pop()
            continue
        t = rng.choice(choices)
        plan.append(t)
        left -= t
    return plan


def render(t: Trail, names: dict[str, str] | None = None) -> str:
    names = names or {}
    out = [f"[{t.id}] {t.subject}   ({t.topic})"]
    for m in t.messages:
        out.append(f"  --- {m['turn']}. {names.get(m['from'], m['from'])} <{m['from']}>")
        out.extend("      " + ln for ln in m["body"].splitlines())
    return "\n".join(out)


def report(store: TrailStore, db, cfg, show: int = 0) -> str:
    store.refresh()
    accounts = {a.email: a for a in [cfg.primary, *cfg.peers]}
    states = {r["status"]: r["c"] for r in db.q("select status, count(*) c from trail_state group by status")}
    free = unsent(store, db, accounts)
    ineligible = [t for t in store.trails.values() if not eligible(t, accounts)]
    lines = [f"Trails file: {store.path} ({'exists' if store.path.exists() else 'MISSING, run generate'})",
             f"Valid threads: {len(store.trails)} ({sum(t.total for t in store.trails.values())} emails), "
             f"invalid lines skipped: {store.skipped}",
             f"Unsent and usable: {len(free)} threads, {sum(t.total for t in free)} emails "
             f"(low-stock alert below {cfg.trails_low_stock})",
             f"Used so far: {states or 'none'}"]
    if ineligible:
        lines.append(f"Not usable with the current accounts: {len(ineligible)} threads "
                     f"({sum(t.total for t in ineligible)} emails)")
    per: dict[str, int] = {}
    for t in free:
        peer = t.recipient if t.opener == cfg.primary.email else t.opener
        per[peer] = per.get(peer, 0) + t.total
    if per:
        lines.append("Unsent emails per peer: " + ", ".join(f"{k} {v}" for k, v in sorted(per.items())))
    names = {a.email: a.name for a in accounts.values()}
    for t in free[:show]:
        lines += ["", render(t, names)]
    return "\n".join(lines)


# ---------------------------------------------------------------- generation
def _topics_sidecar(store: TrailStore) -> Path:
    return store.path.with_name(store.path.name + ".topics")


def load_topics(cfg, content, store: TrailStore, progress=print) -> list[str]:
    """Config topics plus LLM-suggested ones, saved beside the trails so a resumed run reuses them."""
    side = _topics_sidecar(store)
    if side.exists():
        topics = [ln.strip() for ln in side.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if topics:
            return topics
    topics = list(cfg.topics or DEFAULT_TOPICS)
    try:
        extra = content.expand_topics(40)
        progress(f"Added {len(extra)} LLM-suggested topics to the {len(topics)} configured ones")
        topics += extra
    except Exception as e:  # noqa: BLE001  (not fatal: the configured topics still work)
        progress(f"Could not expand topics ({e}); using the {len(topics)} configured ones")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    side.write_text("\n".join(topics) + "\n", encoding="utf-8")
    return topics


def generate_trails(cfg, db, content, store: TrailStore, count: int, rng: random.Random | None = None,
                    sleep=time.sleep, max_wait_s: float = 1800, progress=print) -> int:
    """Append conversations until at least `count` unsent, usable emails are in stock. Returns the
    number of emails written. Idempotent and resumable: re-running only tops up the difference."""
    rng = rng or random.Random()
    accounts = {a.email: a for a in [cfg.primary, *cfg.peers]}
    prim = cfg.primary
    store.refresh()
    free = unsent(store, db, accounts)
    have = sum(t.total for t in free)
    need = count - have
    if need <= 0:
        progress(f"Stock is already {have} unsent emails (target {count}). Nothing to do.")
        return 0
    progress(f"Stock {have}, target {count}: generating {need} emails")

    topics = load_topics(cfg, content, store, progress)
    pending_topics: list[str] = []

    def next_topic() -> str:
        if not pending_topics:
            pending_topics.extend(rng.sample(topics, len(topics)))
        return pending_topics.pop()

    load = {p.email: 0 for p in cfg.peers}
    for t in free:
        load[t.recipient if t.opener == prim.email else t.opener] += t.total
    total_open = (cfg.open_share + cfg.peer_open_share) or 1.0
    p_primary_opens = cfg.open_share / total_open

    recent: deque = deque(maxlen=150)
    for t in store.trails.values():
        recent.append((t.subject, t.messages[0]["body"]))
        recent.extend(("", m["body"]) for m in t.messages[1:])
    content.recent = lambda: list(recent)

    def attempt(fn):
        waited, delay = 0.0, 30.0
        while True:
            try:
                return fn()
            except ContentError as e:
                if waited >= max_wait_s:
                    raise GenerationStopped(str(e)) from e
                progress(f"  LLM not usable ({e}); waiting {delay:.0f}s then retrying")
                sleep(delay)
                waited += delay
                delay = min(delay * 2, 300.0)

    made = 0
    for turns in plan_turns(need, max(cfg.min_turns, 1), max(cfg.max_turns, cfg.min_turns, 1), rng):
        peer = min(cfg.peers, key=lambda p: (load[p.email], rng.random()))
        a, b = (prim, peer) if rng.random() < p_primary_opens else (peer, prim)
        topic = next_topic()
        first = [True]

        def make_thread(a=a, b=b, turns=turns, topic=topic, first=first):
            # a retry after a duplicate gets a fresh topic instead of asking for the same one again
            t = topic if first[0] else next_topic()
            first[0] = False
            return t, content.thread(a, b, turns, t)
        topic, (subject, bodies) = attempt(make_thread)
        msgs = [{"turn": k + 1, "from": (a if k % 2 == 0 else b).email, "body": body}
                for k, body in enumerate(bodies)]
        for k, body in enumerate(bodies):
            recent.append((subject if k == 0 else "", body))
        store.append({"id": store.next_id(), "opener": a.email, "recipient": b.email, "subject": subject,
                      "topic": topic, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "messages": msgs})
        load[peer.email] += turns
        made += turns
        progress(f"[{made}/{need}] {a.email} -> {b.email}, {turns} emails: {subject}")
    progress(f"Done: wrote {made} emails. Unsent stock is now {have + made}.")
    return made
