import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

from test_sim import make, run  # noqa: E402

from warmup.alerts import Alerter  # noqa: E402
from warmup.config import Config, load_config  # noqa: E402
from warmup.content import Content  # noqa: E402
from warmup.engine import Engine  # noqa: E402
from warmup.transport import AuthError, DryRunTransport  # noqa: E402
from warmup.validate import validate  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GOOD = yaml.safe_load((ROOT / "config.example.yaml").read_text())


def mutate(fn):
    raw = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    fn(raw)
    return validate(raw, Config())


def has(rep, text):
    return any(text in e for e in rep.errors), rep.errors


def test_example_config_is_valid():
    rep = validate(GOOD, Config())
    assert rep.errors == [], rep.errors


def test_typo_in_key_is_rejected_with_suggestion():
    rep = mutate(lambda r: r["ramp"].update({"max_per_dya": r["ramp"].pop("max_per_day")}))
    ok, errs = has(rep, "did you mean 'max_per_day'")
    assert ok, errs
    ok, errs = has(mutate(lambda r: r.update({"timezon": "America/New_York"})), "unknown setting 'timezon'")
    assert ok, errs


def test_value_errors():
    cases = [
        (lambda r: r.update(timezone="Asia/New_Yrok"), "not a valid IANA name"),
        (lambda r: r["send_window"].update(start="19:30"), "must be after send_window.start"),
        (lambda r: r["send_window"].update(start="9.30"), "24h"),
        (lambda r: r["ramp"].update(start_per_day=60, max_per_day=40), "above ramp.max_per_day"),
        (lambda r: r["ramp"].update(max_per_day="lots"), "must be a number"),
        (lambda r: r["ramp"].update(days_to_max=3.5), "whole number"),
        (lambda r: r["conversation"].update(reply_rate=1.5), "at most 1"),
        (lambda r: r["conversation"].update(min_turns=6, max_turns=3), "above max_turns"),
        (lambda r: r["conversation"].update(reply_delay_minutes=[240, 8]), "above max"),
        (lambda r: r["peers"].update(count=9), "only 5 accounts"),
        (lambda r: r["peers"]["accounts"][1].update(email=r["peers"]["accounts"][0]["email"]), "more than once"),
        (lambda r: r["peers"]["accounts"][1].update(password_env="PEER1_APP_PASSWORD"), "own app password"),
        (lambda r: r["primary"].update(email="not-an-email"), "not a valid email"),
        (lambda r: r["primary"].update(smtp_port=587), "port 465"),
        (lambda r: r["llm"].update(provider="gpt"), "'cli' or 'api'"),
        (lambda r: r["llm"].update(enabled="yes"), "true or false"),
        (lambda r: r["business"].update(topics="one topic"), "list of non-empty strings"),
        (lambda r: r.pop("primary"), "primary is required"),
        (lambda r: r["peers"].update(accounts=[]), "non-empty list"),
        (lambda r: r.update(weekend_factor=-1), "at least 0"),
    ]
    for fn, expect in cases:
        ok, errs = has(mutate(fn), expect)
        assert ok, (expect, errs)


def test_all_problems_reported_at_once_and_warnings_are_not_errors():
    def worse(r):
        r["timezone"] = "Nope/Nope"
        r["ramp"]["max_per_day"] = 0
        r["conversation"]["reply_rate"] = 2
    rep = mutate(worse)
    assert len(rep.errors) >= 3
    rep = mutate(lambda r: (r["ramp"].update(max_per_day=80), r["peers"].update(count=2)))
    assert not rep.errors and any("~50/day" in w for w in rep.warnings) and any("tiny closed pool" in w for w in rep.warnings)


def test_missing_secrets_are_listed_together(tmp=None):
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "config.yaml"
        cfg.write_text((ROOT / "config.example.yaml").read_text().replace("database: warmup.db", f"database: {d}/w.db"))
        for k in [k for k in os.environ if k.endswith("_APP_PASSWORD")]:
            del os.environ[k]
        cwd = os.getcwd()
        os.chdir(d)  # so a stray .env in the repo is not picked up
        try:
            try:
                load_config(str(cfg), require_secrets=True)
                raise AssertionError("should have failed")
            except SystemExit as e:
                msg = str(e)
            assert "PRIMARY_APP_PASSWORD" in msg and "PEER5_APP_PASSWORD" in msg, msg
            c = load_config(str(cfg), require_secrets=False)  # status / dry-run path
            assert c.primary.email == "you@example.com" and len(c.peers) == 5
        finally:
            os.chdir(cwd)


def test_bad_yaml_and_missing_file_give_clear_errors():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        for body, expect in [("a: [unclosed", "not valid YAML"), ("", "empty or is not a YAML mapping"),
                             ("- a\n- b\n", "empty or is not a YAML mapping")]:
            f = Path(d) / "c.yaml"
            f.write_text(body)
            try:
                load_config(str(f), require_secrets=False)
                raise AssertionError("should have failed")
            except SystemExit as e:
                assert expect in str(e), (body, str(e))
        try:
            load_config(str(Path(d) / "nope.yaml"))
            raise AssertionError("should have failed")
        except SystemExit as e:
            assert "not found" in str(e)


# ---------------------------------------------------------------- alerts
def _alerter(cfg, db, now, sent, fail=False, enabled=True):
    def post(url, payload):
        if fail:
            raise OSError("boom " + url)
        sent.append((url, payload))
    os.environ["ALERT_WEBHOOK_URL"] = "https://hooks.example/secret-token"
    return Alerter(cfg, db, clock=lambda: now["t"], enabled=enabled, post=post, get=lambda u: sent.append(("GET", u)))


def test_alert_throttling_and_retry_after_failed_delivery():
    cfg, eng, tr, st = make()
    cfg.primary = cfg.primary
    sent, now = [], {"t": 1.8e9}  # realistic epoch: the "never sent" default is 0
    al = _alerter(cfg, eng.db, now, sent)
    assert al.send("k", "Something broke", "details") is True
    assert al.send("k", "Something broke") is False              # throttled
    assert al.send("other", "Different problem") is True          # separate key
    now["t"] += 7 * 3600
    assert al.send("k", "Something broke") is True                # cooldown over
    assert al.send("k", "again", cooldown_hours=0) is True        # explicit no-throttle
    payload = sent[0][1]
    assert payload["text"] == payload["content"] and cfg.primary.email in payload["text"]
    # a failed delivery must not start the throttle (the next event retries) and must not leak the URL
    sent2 = []
    bad = _alerter(cfg, eng.db, now, sent2, fail=True)
    import logging
    records = []
    h = logging.Handler()
    h.emit = lambda r: records.append(r.getMessage())
    logging.getLogger("warmup.alerts").addHandler(h)
    assert bad.send("fresh", "x") is False
    assert not any("secret-token" in m for m in records)
    ok = _alerter(cfg, eng.db, now, sent2)
    assert ok.send("fresh", "x") is True
    # disabled (dry run) or no URL configured: silent no-op
    assert _alerter(cfg, eng.db, now, [], enabled=False).send("z", "x") is False
    del os.environ["ALERT_WEBHOOK_URL"]
    assert Alerter(cfg, eng.db).send("z", "x") is False


class AuthDown(DryRunTransport):
    def send(self, sender, *a, **k):
        raise AuthError("SMTP login rejected: 535")

    def check(self, account):
        return "SMTP: 535"


def test_engine_alerts_once_on_broken_account_and_heartbeats():
    cfg, eng, _, st = make()
    sent = []
    now = st
    al = _alerter(cfg, eng.db, now, sent)
    os.environ["HEARTBEAT_URL"] = "https://hb.example/ping"
    e = Engine(cfg, eng.db, AuthDown(), Content(cfg, random.Random(2)), random.Random(2),
               clock=lambda: st["t"], alerter=al)
    run(e, st, 3)
    broken = [p for u, p in sent if u != "GET" and "disabled" in p["text"]]
    texts = [p["text"].split("\n")[0] for p in broken]
    assert broken and len(texts) == len(set(texts)), "at most one alert per account, never one per tick or job"
    assert any(u == "GET" for u, _ in sent), "heartbeat pings after good ticks"
    del os.environ["HEARTBEAT_URL"], os.environ["ALERT_WEBHOOK_URL"]


def test_no_heartbeat_when_tick_crashes():
    cfg, eng, tr, st = make()
    sent = []
    al = _alerter(cfg, eng.db, st, sent)
    os.environ["HEARTBEAT_URL"] = "https://hb.example/ping"
    e = Engine(cfg, eng.db, tr, Content(cfg, random.Random(2)), random.Random(2), clock=lambda: st["t"], alerter=al)
    e._plan_day = lambda now: (_ for _ in ()).throw(RuntimeError("db exploded"))
    try:
        st["t"] += 3600  # inside the window
        e.tick()
        raise AssertionError("should raise")
    except RuntimeError:
        pass
    assert not any(u == "GET" for u, _ in sent), "a failed tick must not look healthy"
    del os.environ["HEARTBEAT_URL"], os.environ["ALERT_WEBHOOK_URL"]


def test_spam_hold_and_llm_pause_alert():
    cfg, eng, tr, st = make(spam_rate=0.9)
    sent = []
    al = _alerter(cfg, eng.db, st, sent)
    e = Engine(cfg, eng.db, tr, Content(cfg, random.Random(2)), random.Random(2), clock=lambda: st["t"], alerter=al)
    run(e, st, 14)
    holds = [p for u, p in sent if "Ramp on hold" in p["text"]]
    assert 1 <= len(holds) <= 10  # at most one per sending day (14 days = 10 weekdays), never per tick
    assert any("Ramp on hold" in p["text"] for u, p in sent)
    e.content.on_alert("LLM paused, sending repeating templates", "Last error: x")
    assert any("LLM paused" in p["text"] for u, p in sent)
    del os.environ["ALERT_WEBHOOK_URL"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
