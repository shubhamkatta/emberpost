from __future__ import annotations

import argparse
import logging
import random
import sys
import time

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore

from .alerts import Alerter
from .config import load_config
from .db import DB
from .engine import Engine
from .transport import DryRunTransport, SMTPIMAPTransport


def acquire_lock(db_path: str):
    """One writer per database. Returns the open lock file (keep it referenced), None when
    locking is unavailable, or False if another warmup process already holds it."""
    if fcntl is None or db_path == ":memory:":
        return None
    f = open(db_path + ".lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    return f


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="warmup")
    ap.add_argument("command", choices=["validate", "check", "tick", "run", "status", "health", "backup", "alert-test",
                                            "generate", "trails"])
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="no network, log only")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--count", type=int, default=500,
                    help="generate: keep at least this many UNSENT emails in the trails file (default 500)")
    ap.add_argument("--out", default=None, help="generate/trails: trails file (default: trails.file from config)")
    ap.add_argument("--show", type=int, default=0, help="trails: print the next N unsent conversations")
    ap.add_argument("--to", default="backups", help="backup: destination directory")
    ap.add_argument("--keep", type=int, default=14, help="backup: how many backups to keep")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = load_config(a.config, require_secrets=not a.dry_run and a.command not in ("status", "alert-test", "health", "backup",
                                                                         "generate", "trails"))
    if a.command == "validate":
        print("Config OK" + (" (dry run: secrets not checked)" if a.dry_run else ""))
        return 0
    lock = None
    if a.command in ("tick", "run"):
        lock = acquire_lock(cfg.database)
        if lock is False:
            msg = f"another warmup process is already using {cfg.database}"
            if a.command == "tick":  # cron overlap is normal, not an error
                print(msg + ", skipping this tick")
                return 0
            print(msg, file=sys.stderr)
            return 1
    db = DB(cfg.database)
    transport = DryRunTransport() if a.dry_run else SMTPIMAPTransport()
    alerter = Alerter(cfg, db, enabled=not a.dry_run)
    eng = Engine(cfg, db, transport, alerter=alerter)
    eng.sleep = time.sleep if not a.dry_run else (lambda s: None)  # type: ignore

    if a.command == "check":
        bad = 0
        for acct in [cfg.primary, *cfg.peers]:
            err = transport.check(acct)
            print(f"{'OK  ' if not err else 'FAIL'} {acct.email} {err}")
            bad += bool(err)
        return 1 if bad else 0
    if a.command in ("generate", "trails"):
        from .content import Content
        from .trails import GenerationStopped, TrailStore, generate_trails, report
        store = TrailStore(a.out or cfg.trails_file)
        if a.command == "trails":
            print(report(store, db, cfg, a.show))
            return 0
        if not cfg.trails_enabled and not a.out:
            logging.warning("trails.enabled is false in the config: the file will be written but not used "
                            "until you enable it")
        content = Content(cfg, random.Random(), strict=True)
        if not content.client:
            print("The LLM is not available (check llm.enabled, llm.provider and that `claude` is on PATH "
                  "or the API key is set). Trails are never written from templates.", file=sys.stderr)
            return 2
        gen_lock = acquire_lock(str(store.path))
        if gen_lock is False:
            print(f"another `generate` is already writing {store.path}", file=sys.stderr)
            return 1
        try:
            generate_trails(cfg, db, content, store, a.count, progress=lambda m: print(m, flush=True))
        except GenerationStopped as e:
            print(f"STOPPED: {e}. Nothing is lost; run `generate` again to continue.", file=sys.stderr)
            return 3
        return 0
    if a.command == "health":
        why = eng.health()
        print("healthy" if not why else f"UNHEALTHY: {why}")
        return 1 if why else 0
    if a.command == "backup":
        print(f"Backup written: {db.backup(a.to, a.keep)}")
        return 0
    if a.command == "alert-test":
        ok = alerter.send("test", "Test alert: if you can read this, alerting works",
                          cooldown_hours=0)
        print("Alert delivered" if ok else
              f"Alert NOT delivered: set {cfg.alert_webhook_env} in .env (or check the URL, see the log)")
        alerter.heartbeat()
        return 0 if ok else 1
    if a.command == "status":
        print(eng.status())
        return 0
    if a.command == "tick":
        try:
            eng.tick()
        except Exception as e:  # noqa: BLE001
            alerter.send("tick_failing", "warmup tick crashed", f"{type(e).__name__}: {e}", cooldown_hours=1)
            raise
        print(eng.status())
        return 0
    # run: loop forever, ticking every 4-7 minutes
    logging.info("Running. Primary=%s, peers=%d", cfg.primary.email, len(cfg.peers))
    fails = 0
    while True:
        try:
            eng.tick()
            fails = 0
        except Exception as e:  # noqa: BLE001
            fails += 1
            logging.exception("tick failed (%d in a row)", fails)
            if fails >= 3:
                alerter.send("tick_failing", f"warmup has failed {fails} ticks in a row",
                             f"{type(e).__name__}: {e}", cooldown_hours=3)
        time.sleep(random.uniform(240, 420))


if __name__ == "__main__":
    sys.exit(main())
