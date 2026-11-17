import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_sim import make, run  # noqa: E402

from warmup.cli import acquire_lock  # noqa: E402
from warmup.config import Account, Config  # noqa: E402
from warmup.content import Content, parse_opening, validate  # noqa: E402
from warmup.engine import Engine  # noqa: E402
from warmup.transport import AuthError, DryRunTransport  # noqa: E402


class Crash(BaseException):
    """Simulates the process dying (not caught by `except Exception`)."""


def _tick_until_crash(eng, st, max_ticks=24 * 6 * 5):
    for _ in range(max_ticks):
        try:
            eng.tick()
        except Crash:
            return True
        st["t"] += 600
    return False


def _assert_consistent(eng, tr):
    mids = [m["mid"] for m in tr.sent]
    assert len(mids) == len(set(mids)), "same Message-ID delivered twice"
    rec = [r["message_id"] for r in eng.db.q("select message_id from messages")]
    assert sorted(rec) == sorted(mids), "recorded messages differ from delivered messages"
    assert not eng.db.q("select 1 from jobs where status='sending'")


def test_crash_after_send_is_recorded_not_resent():
    cfg, eng, tr, st = make()
    real, boom = eng._finish, {"n": 0}

    def finish(*a, **k):
        boom["n"] += 1
        if boom["n"] == 3:
            raise Crash()
        return real(*a, **k)

    eng._finish = finish
    assert _tick_until_crash(eng, st)
    sent_before = len(tr.sent)
    eng2 = Engine(cfg, eng.db, tr, Content(cfg, random.Random(1)), random.Random(1), clock=lambda: st["t"])
    eng2.tick()
    assert len(tr.sent) >= sent_before and len(tr.sent) - sent_before <= 3  # recovery sent nothing twice
    run(eng2, st, 3)
    _assert_consistent(eng2, tr)


def test_crash_before_delivery_resends_same_message_id():
    cfg, eng, tr, st = make()
    real, boom = tr.send, {"n": 0}

    def send(*a, **k):
        boom["n"] += 1
        if boom["n"] == 2:
            raise Crash()  # died before the mail left
        return real(*a, **k)

    tr.send = send
    assert _tick_until_crash(eng, st)
    stuck = eng.db.q("select * from jobs where status='sending'")
    assert len(stuck) == 1
    import json
    mid = json.loads(stuck[0]["payload"])["mid"]
    eng2 = Engine(cfg, eng.db, tr, Content(cfg, random.Random(1)), random.Random(1), clock=lambda: st["t"])
    run(eng2, st, 3)
    _assert_consistent(eng2, tr)
    assert mid in [m["mid"] for m in tr.sent]  # the interrupted mail went out under its original id


def test_lock_blocks_second_process():
    with tempfile.TemporaryDirectory() as d:
        db = str(Path(d) / "w.db")
        first = acquire_lock(db)
        assert first not in (False, None)
        assert acquire_lock(db) is False
        first.close()
        again = acquire_lock(db)
        assert again not in (False, None)
        again.close()


class AuthFlaky(DryRunTransport):
    def __init__(self, bad_email):
        super().__init__()
        self.bad, self.down = bad_email, True

    def send(self, sender, *a, **k):
        if self.down and sender.email == self.bad:
            raise AuthError("SMTP login rejected: 535")
        return super().send(sender, *a, **k)

    def check(self, account):
        return "SMTP: 535" if (self.down and account.email == self.bad) else ""


def test_broken_account_persists_then_recovers():
    cfg, eng, tr0, st = make()
    tr = AuthFlaky(cfg.primary.email)
    eng = Engine(cfg, eng.db, tr, Content(cfg, random.Random(3)), random.Random(3), clock=lambda: st["t"])
    run(eng, st, 2)
    assert eng.is_broken(cfg.primary.email)
    assert not eng.db.q("select 1 from jobs where status='failed'"), "auth failure must not burn attempts"
    assert not [m for m in tr.sent if m["frm"] == cfg.primary.email]
    # survives a restart
    eng2 = Engine(cfg, eng.db, tr, Content(cfg, random.Random(3)), random.Random(3), clock=lambda: st["t"])
    assert eng2.is_broken(cfg.primary.email) and "BROKEN" in eng2.status()
    # login fixed: re-enabled after the cooldown, and sending resumes
    tr.down = False
    run(eng2, st, 4)
    assert not eng2.is_broken(cfg.primary.email)
    assert [m for m in tr.sent if m["frm"] == cfg.primary.email]


def test_validate_rejects_bad_llm_output():
    good = "Hi Jamie,\n\nCould we find twenty minutes on Thursday to walk through the onboarding steps?\n\nSam"
    assert validate(good) is None
    for bad in ["Hi [Name],\n\nPlease review the document I will send you tomorrow morning at nine.\n\nSam",
                "Hi Jamie,\n\nHave a look at https://example.com/doc when you get a moment today please.\n\nSam",
                "Hi Jamie,\n\nThis is just a test email to warm up the inbox before our campaign starts.\n\nSam",
                "Hi Jamie,\n\nHere is the email you asked for, please review it and reply when ready.\n\nSam",
                "Hi!", "Hi Jamie \U0001F600,\n\nCould we find twenty minutes on Thursday to talk it through?\n\nSam"]:
        assert validate(bad), bad
    assert parse_opening("Here you go:\nSubject: Onboarding call\n\nHi Jamie,\nbody text") == \
        ("Onboarding call", "Hi Jamie,\nbody text")
    assert parse_opening("no subject line here") is None


def _llm_content(outputs, now=None, recent=()):
    cfg = Config(llm_enabled=False)
    state = {"t": 0.0}
    c = Content(cfg, random.Random(1), clock=lambda: state["t"])
    c.client, c.calls = True, 0
    c.recent = lambda: list(recent)

    def ask(prompt):
        c.calls += 1
        o = outputs[min(c.calls - 1, len(outputs) - 1)]
        if isinstance(o, Exception):
            raise o
        return o

    c._ask = ask
    return c, state


def test_llm_output_is_validated_and_regenerated():
    sender, rcpt = Account("a@example.com", "Sam Lee"), Account("p@gmail.com", "Jamie Rivera")
    good = ("Subject: Onboarding call\n\nHi Jamie,\n\nCould we find twenty minutes on Thursday to walk through "
            "calendar setup?\n\nSam")
    bad = "Subject: Onboarding call\n\nHi [Name],\n\nCould we find twenty minutes on Thursday to talk?\n\nSam"
    c, _ = _llm_content([bad, good])
    subj, body, src = c.opening(sender, rcpt)
    assert src == "llm" and c.calls == 2 and "[" not in body
    # a recent duplicate is rejected too, falling back to a template
    c, _ = _llm_content([good], recent=[("Onboarding call", "Hi Jamie,\n\nCould we find twenty minutes on Thursday "
                                                            "to walk through calendar setup?\n\nSam")])
    assert c.opening(sender, rcpt)[2] == "template"


def test_llm_circuit_breaker_pauses_then_resumes():
    sender, rcpt = Account("a@example.com", "Sam Lee"), Account("p@gmail.com", "Jamie Rivera")
    c, st = _llm_content([RuntimeError("Please run /login")])
    for _ in range(3):  # 3 failures (each call breaks after the first error)
        assert c.opening(sender, rcpt)[2] == "template"
    assert c.calls == 3
    c.opening(sender, rcpt)
    assert c.calls == 3, "paused: LLM must not be called while the breaker is open"
    st["t"] += 1801
    c.opening(sender, rcpt)
    assert c.calls == 4


def test_templates_avoid_recent_repeats():
    sender, rcpt = Account("a@example.com", "Sam Lee"), Account("p@gmail.com", "Jamie Rivera")
    c = Content(Config(), random.Random(5))
    first = c.reply(sender, rcpt, [], final=False)[0]
    c.recent = lambda: [("Re: x", first)]
    for _ in range(20):
        assert c.reply(sender, rcpt, [], final=False)[0] != first


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
