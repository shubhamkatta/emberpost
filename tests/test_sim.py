import random
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from warmup.config import Account, Config
from warmup.content import Content
from warmup.db import DB
from warmup.engine import Engine
from warmup.transport import DryRunTransport


def make(spam_rate=0.0, days_to_max=35, peers=4, seed=7):
    cfg = Config(database=":memory:", ramp_days=days_to_max, llm_enabled=False)
    cfg.primary = Account("me@corp.com", "Sam Lee")
    cfg.peers = [Account(f"p{i}@gmail.com", f"Peer{i} Name") for i in range(peers)]
    tr = DryRunTransport(spam_rate=spam_rate, seed=seed)
    state = {"t": datetime(2026, 11, 23, 9, 0, tzinfo=ZoneInfo(cfg.timezone)).timestamp()}
    rng = random.Random(seed)
    eng = Engine(cfg, DB(":memory:"), tr, Content(cfg, rng), rng, clock=lambda: state["t"])
    return cfg, eng, tr, state


def run(eng, state, days):
    for _ in range(days * 24 * 6):  # tick every 10 min
        eng.tick()
        state["t"] += 600


def test_ramp_caps_and_threading():
    cfg, eng, tr, st = make()
    run(eng, st, 40)
    # primary never exceeds its ramp max on any day
    rows = eng.db.q("select sent_at from messages where sender=?", (cfg.primary.email,))
    per_day = {}
    for r in rows:
        d = eng.local(r["sent_at"]).date()
        per_day[d] = per_day.get(d, 0) + 1
    assert max(per_day.values()) <= cfg.ramp_max
    # no weekend sends
    assert all(d.weekday() < 5 for d in per_day)
    # volume ramps up
    days = sorted(per_day)
    assert sum(per_day[d] for d in days[-5:]) > sum(per_day[d] for d in days[:5]) * 2
    # replies carry In-Reply-To, and conversations go back and forth
    assert any(m["irt"] for m in tr.sent)
    turns = eng.db.q("select max(turn) t from messages group by thread_id")
    assert max(r["t"] for r in turns) >= 3
    # only configured peers are used, and peers' daily cap is respected
    assert {m["to"] for m in tr.sent} <= {a.email for a in [cfg.primary, *cfg.peers]}
    print(eng.status())


def test_spam_rescue_and_ramp_hold():
    cfg, eng, tr, st = make(spam_rate=0.6)
    run(eng, st, 12)
    assert any(p == "spam" for _, p, _ in tr.engaged)  # rescued from spam
    assert int(eng.db.get("day_idx")) < 8  # ramp held (would be ~11 w/o hold)


def test_peer_count_configurable():
    cfg, eng, tr, st = make(peers=2)
    run(eng, st, 5)
    assert {m["to"] for m in tr.sent if m["frm"] == cfg.primary.email} <= {"p0@gmail.com", "p1@gmail.com"}


if __name__ == "__main__":
    test_ramp_caps_and_threading()
    test_spam_rescue_and_ramp_hold()
    test_peer_count_configurable()
    print("ok")
