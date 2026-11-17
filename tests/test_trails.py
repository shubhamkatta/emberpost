import contextlib
import io
import json
import os
import random
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

from test_config import _alerter  # noqa: E402
from test_sim import make, run  # noqa: E402

from warmup.cli import main  # noqa: E402
from warmup.config import Config  # noqa: E402
from warmup.content import Content, ContentError  # noqa: E402
from warmup.db import DB  # noqa: E402
from warmup.engine import Engine  # noqa: E402
from warmup.trails import (  # noqa: E402
    GenerationStopped,
    TrailStore,
    eligible,
    generate_trails,
    parse_trail,
    plan_turns,
    report,
    unsent,
)
from warmup.validate import validate  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
VOCAB = ("river lantern ticket harbour window orchard compass velvet anchor meadow ladder copper "
         "garden pebble saddle thunder canvas bridge falcon market pocket silver tunnel violet wagon "
         "yellow zephyr basket candle dragon engine forest glacier hammer island jungle kettle lemon "
         "mirror needle ocean planet quartz rocket shadow timber umbrella voyage willow yonder").split()


def sentence(rng, n=35):
    return " ".join(rng.choices(VOCAB, k=n)) + " zzmark."


def rec(tid, a, b, turns, seed, topic="a topic"):
    rng = random.Random(seed)
    return {"id": tid, "opener": a, "recipient": b, "subject": " ".join(rng.sample(VOCAB, 4)), "topic": topic,
            "messages": [{"turn": i + 1, "from": a if i % 2 == 0 else b, "body": "Hi,\n\n" + sentence(rng) + "\n\nSam"}
                         for i in range(turns)]}


def llm_stub(rng, fail_after=None):
    state = {"n": 0}

    def ask(prompt):
        state["n"] += 1
        if fail_after is not None and state["n"] > fail_after:
            raise RuntimeError("Please run /login")
        if "distinct, specific" in prompt:
            return "\n".join(f"- asking whether {' '.join(rng.sample(VOCAB, 4))} is ready {i}" for i in range(12))
        body = "Hi,\n\n" + sentence(rng) + "\n\nSam"
        if "complete work email conversation" in prompt:
            n = int(re.search(r"exactly (\d+) messages", prompt).group(1))
            return json.dumps({"subject": " ".join(rng.sample(VOCAB, 4)),
                               "messages": ["Hi,\n\n" + sentence(rng) + "\n\nSam" for _ in range(n)]})
        if "first line 'Subject:" in prompt:
            return "Subject: " + " ".join(rng.sample(VOCAB, 4)) + "\n\n" + body
        return body
    ask.state = state
    return ask


def strict_content(cfg, rng, fail_after=None):
    c = Content(cfg, rng, strict=True)
    c.client = True
    c._ask = llm_stub(rng, fail_after)
    return c


def mk(tmp, peers=3, seed=7, fallback="live", reply_rate=1.0, low=0):
    cfg, _, tr, st = make(peers=peers, seed=seed)
    cfg.trails_enabled, cfg.trails_file = True, str(Path(tmp) / "trails.jsonl")
    cfg.trails_fallback, cfg.reply_rate, cfg.trails_low_stock = fallback, reply_rate, low
    rng = random.Random(seed)
    eng = Engine(cfg, DB(":memory:"), tr, Content(cfg, rng), rng, clock=lambda: st["t"])
    return cfg, eng, tr, st


def populate(cfg, n, turns=4, start=1):
    store = TrailStore(cfg.trails_file)
    peers = [p.email for p in cfg.peers]
    for i in range(n):
        peer = peers[i % len(peers)]
        a, b = (cfg.primary.email, peer) if i % 3 != 2 else (peer, cfg.primary.email)  # 2/3 primary opens
        store.append(rec(f"t{start + i:05d}", a, b, turns, seed=1000 + start + i))
    return store


# ============================================================ planner
def test_plan_turns_sums_exactly_within_bounds():
    for n in range(2, 90):
        for seed in range(5):
            plan = plan_turns(n, 2, 5, random.Random(seed))
            assert sum(plan) == n and all(2 <= t <= 5 for t in plan), (n, plan)
    assert sum(plan_turns(500, 2, 5, random.Random(1))) == 500


def test_plan_turns_rejects_impossible_splits_instead_of_hanging():
    for n, lo, hi in [(1, 2, 5), (3, 2, 2), (7, 4, 4)]:
        try:
            plan_turns(n, lo, hi, random.Random(0))
        except ValueError:
            continue
        raise AssertionError((n, lo, hi))


# ============================================================ file format and store
def test_store_skips_bad_lines_ignores_partial_line_and_repairs_on_append():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.jsonl"
        good = rec("t00001", "a@x.com", "b@gmail.com", 3, 1)
        bad_order = rec("t00002", "a@x.com", "b@gmail.com", 3, 2)
        bad_order["messages"][1]["from"] = "a@x.com"            # senders must alternate
        dup = rec("t00001", "a@x.com", "c@gmail.com", 2, 3)
        partial = json.dumps(rec("t00009", "a@x.com", "b@gmail.com", 2, 9))[:60]       # crash mid-write
        p.write_text("\n".join([json.dumps(good), "{not json", json.dumps(bad_order), json.dumps(dup), partial]),
                     encoding="utf-8")
        s = TrailStore(p)
        s.refresh()
        assert list(s.trails) == ["t00001"] and s.skipped == 3
        s.append(rec("t00002", "a@x.com", "b@gmail.com", 2, 4))          # starts on a fresh line
        assert list(s.trails) == ["t00001", "t00002"]
        assert all(ln.strip() for ln in p.read_text().split("\n")[:-1]) and p.read_text().endswith("\n")
        assert s.next_id() == "t00003"
        try:
            s.append(bad_order)
            raise AssertionError("an invalid trail must never be written")
        except ValueError:
            pass


def test_parse_trail_rules():
    ok = rec("t1", "a@x.com", "b@gmail.com", 3, 1)
    assert parse_trail(ok).total == 3
    for mutate, why in [(lambda r: r.update(opener="nope"), "bad address"),
                        (lambda r: r.update(recipient="a@x.com"), "same"),
                        (lambda r: r["messages"][0].update(body="[Name] short"), "message 1"),
                        (lambda r: r["messages"][1].update(turn=5), "turns"),
                        (lambda r: r.update(messages=[]), "no messages"),
                        (lambda r: r.update(subject="Hi\nBcc: x@y.com"), "subject")]:
        r = json.loads(json.dumps(ok))
        mutate(r)
        out = parse_trail(r)
        assert isinstance(out, str) and why in out, (why, out)


def test_eligibility_follows_the_configured_accounts():
    t = parse_trail(rec("t1", "me@corp.com", "p0@gmail.com", 2, 1))
    assert eligible(t, {"me@corp.com": 1, "p0@gmail.com": 1})
    assert not eligible(t, {"me@corp.com": 1})


# ============================================================ generation
def test_generate_writes_exact_stock_is_balanced_and_idempotent():
    with tempfile.TemporaryDirectory() as d:
        cfg, eng, _, _ = mk(d, peers=4)
        rng = random.Random(3)
        store = TrailStore(cfg.trails_file)
        n = generate_trails(cfg, eng.db, strict_content(cfg, rng), store, 60, rng, sleep=lambda s: None,
                            progress=lambda m: None)
        assert n == 60
        fresh = TrailStore(cfg.trails_file)
        fresh.refresh()
        assert sum(t.total for t in fresh.trails.values()) == 60 and fresh.skipped == 0
        accounts = {a.email for a in [cfg.primary, *cfg.peers]}
        peers_seen, openers = set(), set()
        for t in fresh.trails.values():
            assert {t.opener, t.recipient} <= accounts and cfg.primary.email in (t.opener, t.recipient)
            assert 2 <= t.total <= 5
            peers_seen.add(t.recipient if t.opener == cfg.primary.email else t.opener)
            openers.add(t.opener == cfg.primary.email)
            assert all("zzmark" in m["body"] for m in t.messages), "only LLM text may be stored, never templates"
        assert openers == {True, False}, "both directions are generated"
        assert len(peers_seen) >= 3, f"peers should be spread out: {peers_seen}"
        assert len({t.subject for t in fresh.trails.values()}) == len(fresh.trails)
        assert Path(cfg.trails_file + ".topics").exists()
        # top-up semantics
        assert generate_trails(cfg, eng.db, strict_content(cfg, rng), store, 60, rng, progress=lambda m: None) == 0
        assert generate_trails(cfg, eng.db, strict_content(cfg, rng), store, 85, rng, sleep=lambda s: None,
                               progress=lambda m: None) == 25
        assert sum(t.total for t in unsent(store, eng.db, {a: 1 for a in accounts})) == 85


def test_generation_survives_an_llm_outage_and_resumes_without_templates():
    with tempfile.TemporaryDirectory() as d:
        cfg, eng, _, _ = mk(d)
        rng = random.Random(4)
        store = TrailStore(cfg.trails_file)
        slept = []
        try:
            generate_trails(cfg, eng.db, strict_content(cfg, rng, fail_after=6), store, 40, rng,
                            sleep=slept.append, max_wait_s=120, progress=lambda m: None)
            raise AssertionError("expected GenerationStopped")
        except GenerationStopped:
            pass
        assert slept, "should have waited and retried before giving up"
        part = TrailStore(cfg.trails_file)
        part.refresh()
        have = sum(t.total for t in part.trails.values())
        assert 0 < have < 40 and part.skipped == 0, "only whole, valid conversations are on disk"
        assert generate_trails(cfg, eng.db, strict_content(cfg, rng), part, 40, rng, sleep=lambda s: None,
                               progress=lambda m: None) == 40 - have
        done = TrailStore(cfg.trails_file)
        done.refresh()
        assert sum(t.total for t in done.trails.values()) == 40
        assert all("zzmark" in m["body"] for t in done.trails.values() for m in t.messages)


def test_strict_content_never_falls_back_to_templates():
    cfg = Config()
    from warmup.config import Account
    a, b = Account("a@x.com", "Sam Lee"), Account("b@gmail.com", "Jamie Rivera")
    c = Content(cfg, random.Random(1), strict=True)           # no LLM available at all
    for call in (lambda: c.opening(a, b), lambda: c.reply(b, a, [("Sam Lee", "hello")], final=False)):
        try:
            call()
            raise AssertionError("must raise instead of returning a template")
        except ContentError:
            pass
    c.client = True
    c._ask = lambda prompt: "Subject: Hi\n\nHi [Name], this is a body that has a placeholder inside it, sorry."
    try:
        c.opening(a, b)
        raise AssertionError("invalid LLM output must not be accepted or replaced by a template")
    except ContentError as e:
        assert "rejected" in str(e)


def test_expand_topics_filters_junk_and_sidecar_is_reused():
    cfg = Config(topics=["scheduling a demo for a small studio"])
    c = Content(cfg, random.Random(1))
    c.client = True
    c._ask = lambda p: ("1. asking whether the onboarding call can move to Thursday\n"
                        "- following up on the team pilot plan\n"
                        "short\nasking about the $29 plan for a team of three\nconfirming [date] for the setup call\n"
                        "scheduling a demo for a small studio\n"
                        "* asking whether the onboarding call can move to Thursday\n"
                        "see https://example.com for details on setup steps\n")
    assert c.expand_topics() == ["asking whether the onboarding call can move to Thursday",
                                 "following up on the team pilot plan"]
    with tempfile.TemporaryDirectory() as d:
        cfg2, eng, _, _ = mk(d)
        store = TrailStore(cfg2.trails_file)
        calls = {"n": 0}
        c2 = strict_content(cfg2, random.Random(2))
        inner = c2._ask

        def counting(p):
            calls["n"] += "distinct, specific" in p
            return inner(p)
        c2._ask = counting
        from warmup.trails import load_topics
        t1 = load_topics(cfg2, c2, store, progress=lambda m: None)
        t2 = load_topics(cfg2, c2, store, progress=lambda m: None)
        assert t1 == t2 and calls["n"] == 1 and len(t1) > len(cfg2.topics or [])


# ============================================================ the engine consuming trails
def test_engine_sends_trail_text_verbatim_in_order_and_tracks_state():
    with tempfile.TemporaryDirectory() as d:
        cfg, eng, tr, st = mk(d)
        store = populate(cfg, 25)
        run(eng, st, 25)
        threads = eng.db.q("select * from threads where trail_id is not null")
        assert len(threads) >= 6
        assert len({t["trail_id"] for t in threads}) == len(threads), "a trail was used twice"
        for t in threads:
            trail = store.trails[t["trail_id"]]
            assert (t["a"], t["b"]) == (trail.opener, trail.recipient)
            msgs = eng.db.q("select * from messages where thread_id=? order by turn", (t["id"],))
            assert msgs and all(m["source"] == "trail" for m in msgs)
            for m in msgs:
                want = trail.messages[m["turn"] - 1]
                assert m["body"] == want["body"] and m["sender"] == want["from"], (trail.id, m["turn"])
        # consumption is a prefix of the file, per direction
        prim = cfg.primary.email
        for primary_opens in (True, False):
            order = [i for i in store.order if (store.trails[i].opener == prim) == primary_opens]
            used = [t["trail_id"] for t in threads if (t["a"] == prim) == primary_opens]
            assert sorted(used) == order[:len(used)], (primary_opens, used, order[:len(used)])
        # state table agrees with what was sent
        for r in eng.db.q("select * from trail_state"):
            sent = eng.db.q("select max(turn) m from messages m join threads t on t.id=m.thread_id "
                            "where t.trail_id=?", (r["trail_id"],))[0]["m"]
            if r["status"] == "reserved":
                assert sent is None
            else:
                assert r["last_turn"] == sent
                assert (r["status"] == "done") == (sent == store.trails[r["trail_id"]].total)
        assert eng.db.q("select 1 from trail_state where status='done'"), "at least one conversation completed"
        unused_emails = sum(t.total for t in unsent(store, eng.db, eng.accounts))
        assert eng.trail_stock() == unused_emails
        assert "Trails:" in eng.status()


def test_replies_stop_when_the_other_side_does_not_answer_but_state_stays_truthful():
    with tempfile.TemporaryDirectory() as d:
        cfg, eng, tr, st = mk(d, reply_rate=0.0)       # nobody ever replies
        populate(cfg, 10)
        run(eng, st, 10)
        rows = eng.db.q("select * from trail_state")
        assert rows and all(r["status"] == "active" and r["last_turn"] == 1 for r in rows if r["status"] != "reserved")


def test_fallback_live_when_the_file_runs_out():
    with tempfile.TemporaryDirectory() as d:
        cfg, eng, tr, st = mk(d, fallback="live")
        populate(cfg, 2)                                 # tiny stock, will be exhausted on day 1-2
        run(eng, st, 12)
        src = {r["source"] for r in eng.db.q("select distinct source from messages")}
        assert {"trail", "template"} <= src, src         # trails first, then live generation (templates: LLM off here)
        assert len(eng.db.q("select 1 from trail_state")) == 2, "each trail used exactly once, not recycled"


def test_fallback_stop_sends_nothing_new_once_the_file_is_exhausted():
    with tempfile.TemporaryDirectory() as d:
        cfg, eng, tr, st = mk(d, fallback="stop")
        populate(cfg, 3)
        run(eng, st, 15)
        src = {r["source"] for r in eng.db.q("select distinct source from messages")}
        assert src == {"trail"}, src
        assert len(eng.db.q("select * from threads")) == 3
        assert all(r["status"] == "done" for r in eng.db.q("select * from trail_state")), "started threads still finish"


def test_trails_for_accounts_no_longer_configured_are_ignored():
    with tempfile.TemporaryDirectory() as d:
        cfg, eng, tr, st = mk(d)
        store = TrailStore(cfg.trails_file)
        store.append(rec("t00001", cfg.primary.email, "retired@gmail.com", 3, 1))      # peer no longer configured
        store.append(rec("t00002", cfg.primary.email, cfg.peers[0].email, 3, 2))
        run(eng, st, 6)
        used = {r["trail_id"] for r in eng.db.q("select trail_id from trail_state")}
        assert used == {"t00002"}


def test_low_stock_alert_names_the_remedy():
    with tempfile.TemporaryDirectory() as d:
        cfg, eng, tr, st = mk(d, low=100)
        populate(cfg, 3)
        sent = []
        eng.alerter = _alerter(cfg, eng.db, st, sent)
        run(eng, st, 3)
        low = [p["text"] for u, p in sent if u != "GET" and "running low" in p["text"]]
        assert low and "generate" in low[0]
        os.environ.pop("ALERT_WEBHOOK_URL", None)


class Crash(BaseException):
    pass


def test_crash_between_send_and_record_keeps_trail_state_consistent():
    with tempfile.TemporaryDirectory() as d:
        cfg, eng, tr, st = mk(d)
        store = populate(cfg, 20)
        real, n = eng._finish, {"n": 0}

        def finish(*a, **k):
            n["n"] += 1
            if n["n"] == 4:
                raise Crash()
            return real(*a, **k)
        eng._finish = finish
        try:
            run(eng, st, 10)
            raise AssertionError("expected the simulated crash")
        except Crash:
            pass
        rng = random.Random(1)
        eng2 = Engine(cfg, eng.db, tr, Content(cfg, rng), rng, clock=lambda: st["t"])
        run(eng2, st, 15)
        assert not eng2.db.q("select 1 from jobs where status='sending'")
        mids = [m["mid"] for m in tr.sent]
        assert len(mids) == len(set(mids))
        for r in eng2.db.q("select * from trail_state where status!='reserved'"):
            sent = eng2.db.q("select max(turn) m from messages m join threads t on t.id=m.thread_id "
                             "where t.trail_id=?", (r["trail_id"],))[0]["m"]
            assert r["last_turn"] == sent, (r["trail_id"], r["last_turn"], sent)
            assert (r["status"] == "done") == (sent == store.trails[r["trail_id"]].total)


# ============================================================ CLI, config, report
def _cfg_file(d, **trails):
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    raw["database"] = str(Path(d) / "w.db")
    raw["llm"]["enabled"] = False                      # tests must never call a real LLM
    raw["trails"] = {"enabled": True, "file": str(Path(d) / "trails.jsonl"), **trails}
    p = Path(d) / "config.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p, raw


def test_cli_trails_report_and_generate_refuses_without_an_llm():
    with tempfile.TemporaryDirectory() as d:
        p, raw = _cfg_file(d)
        cwd = os.getcwd()
        os.chdir(d)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                assert main(["trails", "-c", str(p)]) == 0
            assert "MISSING" in out.getvalue()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = main(["generate", "--count", "10", "-c", str(p), "--dry-run"])
            assert rc == 2 and "never written from templates" in err.getvalue()
            assert not Path(raw["trails"]["file"]).exists()
        finally:
            os.chdir(cwd)


def test_report_shows_stock_and_previews():
    with tempfile.TemporaryDirectory() as d:
        cfg, eng, _, _ = mk(d)
        store = populate(cfg, 6)
        text = report(store, eng.db, cfg, show=1)
        assert "Unsent and usable: 6 threads, 24 emails" in text and "[t00001]" in text and "Unsent emails per peer" in text


def test_trails_config_validation():
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    assert validate(raw, Config()).errors == []
    for mutate, expect in [(lambda r: r["trails"].update(fallback="maybe"), "'live' or 'stop'"),
                           (lambda r: r["trails"].update(low_stock_alert=-1), "at least 0"),
                           (lambda r: r["trails"].update(enabled="yes"), "true or false"),
                           (lambda r: r["trails"].update(fil="x.jsonl"), "unknown setting 'trails.fil'")]:
        r = yaml.safe_load((ROOT / "config.example.yaml").read_text())
        mutate(r)
        errs = validate(r, Config()).errors
        assert any(expect in e for e in errs), (expect, errs)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
