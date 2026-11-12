from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timedelta
from email.utils import make_msgid

from .alerts import Alerter
from .config import Account, Config
from .content import Content, quote
from .db import DB
from .trails import Trail, TrailStore, eligible, used_ids
from .transport import AuthError, Transport

log = logging.getLogger("warmup.engine")


class Engine:
    def __init__(self, cfg: Config, db: DB, transport: Transport, content: Content | None = None,
                 rng: random.Random | None = None, clock=time.time, alerter: Alerter | None = None):
        self.cfg, self.db, self.t = cfg, db, transport
        self.rng = rng or random.Random()
        self.clock = clock
        self.content = content or Content(cfg, self.rng, clock)
        self.alerter = alerter or Alerter(cfg, db, clock, enabled=False)  # CLI enables it
        self.content.recent = self._recent_messages
        self.content.on_alert = lambda title, detail: self.alerter.send("llm_paused", title, detail)
        self.accounts: dict[str, Account] = {a.email: a for a in [cfg.primary, *cfg.peers]}  # type: ignore
        self.trails: TrailStore | None = TrailStore(cfg.trails_file) if cfg.trails_enabled else None

    def _recent_messages(self, n: int = 100) -> list[tuple[str, str]]:
        return [(r["subject"], r["body"]) for r in
                self.db.q("select subject, body from messages order by id desc limit ?", (n,))]

    # ------------------------------------------------------------ broken accounts (persisted)
    def broken_accounts(self) -> dict[str, dict]:
        out = {}
        for r in self.db.q("select k, v from meta where k like 'broken:%'"):
            out[r["k"][len("broken:"):]] = json.loads(r["v"])
        return out

    def is_broken(self, email: str) -> bool:
        return self.db.get("broken:" + email) is not None

    def mark_broken(self, email: str, err, now: float) -> None:
        if not self.is_broken(email):
            log.error("Account %s disabled until login works again: %s", email, err)
            self.alerter.send(f"broken:{email}", f"Account {email} disabled: login rejected",
                              f"{err}\nRegenerate its app password / re-enable IMAP. It is re-checked every "
                              f"{self.cfg.broken_cooldown_hours:g}h.", cooldown_hours=1)
        since = json.loads(self.db.get("broken:" + email) or "{}").get("since", int(now))
        self.db.set("broken:" + email, json.dumps({"since": since, "checked": int(now), "error": str(err)[:200]}))

    def _recheck_broken(self, now: float) -> None:
        for email, info in self.broken_accounts().items():
            acct = self.accounts.get(email)
            if acct is None or now - info["checked"] < self.cfg.broken_cooldown_hours * 3600:
                continue
            err = self.t.check(acct)
            if err:
                self.mark_broken(email, err, now)
            else:
                self.db.unset("broken:" + email)
                log.warning("Account %s logs in again, re-enabled", email)
                self.alerter.send(f"recovered:{email}", f"Account {email} recovered, sending resumed",
                                  cooldown_hours=0)

    # ------------------------------------------------------------ time helpers
    def local(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, self.cfg.tz)

    def _day_factor(self, d) -> float:
        """Share of the weekday volume sent on this day: 1 Mon-Fri, saturday_factor on Saturday
        (falls back to weekend_factor), weekend_factor on Sunday."""
        wd = d.weekday()
        if wd < 5:
            return 1.0
        if wd == 5 and self.cfg.saturday_factor is not None:
            return self.cfg.saturday_factor
        return self.cfg.weekend_factor

    def _day_ok(self, d) -> bool:
        return self._day_factor(d) > 0

    def in_window(self, ts: float) -> bool:
        d = self.local(ts)
        return self._day_ok(d) and self.cfg.win_start <= d.time() < self.cfg.win_end

    def next_window_open(self, ts: float) -> float:
        d0 = self.local(ts)
        for i in range(8):
            day = (d0 + timedelta(days=i)).date()
            start = datetime.combine(day, self.cfg.win_start, self.cfg.tz)
            if self._day_ok(start) and start.timestamp() > ts:
                return start.timestamp() + self.rng.uniform(0, 45 * 60)
        return ts + 86400

    def clamp(self, ts: float) -> float:
        return ts if self.in_window(ts) else self.next_window_open(ts)

    def midnight(self, ts: float) -> float:
        d = self.local(ts)
        return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    # ------------------------------------------------------------ ramp
    def ramp_cap(self, day_idx: int) -> int:
        c = self.cfg
        frac = min(day_idx / max(c.ramp_days, 1), 1.0)
        return max(1, round(c.ramp_start + (c.ramp_max - c.ramp_start) * frac))

    def spam_rate(self, now: float, days: int = 3) -> tuple[float, int]:
        rows = self.db.q("select placement from messages where arrived_at>=? and placement is not null",
                         (int(now - days * 86400),))
        n = len(rows)
        s = sum(1 for r in rows if r["placement"] == "spam")
        return (s / n if n else 0.0), n

    def today_cap(self, now: float) -> int:
        day = int(self.db.get("day_idx", "0"))
        cap = self.ramp_cap(day)
        return round(cap * self._day_factor(self.local(now)))

    def _advance_day(self, now: float) -> None:
        today = self.local(now).date().isoformat()
        last = self.db.get("day_date")
        if last == today:
            return
        if last is not None:
            rate, n = self.spam_rate(now)
            if n >= 5 and rate > self.cfg.hold_spam_rate:
                log.warning("Spam rate %.0f%% over last 3 days, holding ramp", rate * 100)
                self.alerter.send("spam_hold", f"Ramp on hold: {rate:.0%} of the last {n} mails hit spam",
                                  f"The daily cap stops growing until the 3-day spam rate is under "
                                  f"{self.cfg.hold_spam_rate:.0%}. Check SPF/DKIM/DMARC and Postmaster Tools.",
                                  cooldown_hours=24)
            else:
                self.db.set("day_idx", str(int(self.db.get("day_idx", "0")) + 1))
        self.db.set("day_date", today)

    # ------------------------------------------------------------ tick
    def tick(self) -> None:
        self.db.set("last_tick", str(int(self.clock())))  # loop is alive (even if this tick then fails)
        self._tick()
        self.db.set("last_ok", str(int(self.clock())))    # ...and this one completed
        self.alerter.heartbeat()  # only reached when the tick succeeded

    def health(self, max_tick_age_min: float = 30, max_ok_age_hours: float = 3) -> str:
        """'' if healthy, else why not. For Docker HEALTHCHECK / monitoring."""
        now = self.clock()
        last, ok = self.db.get("last_tick"), self.db.get("last_ok")
        if last is None:
            return "no tick has run yet"
        if now - int(last) > max_tick_age_min * 60:
            return f"last tick was {(now - int(last)) / 60:.0f} min ago (limit {max_tick_age_min:g})"
        if ok is None or now - int(ok) > max_ok_age_hours * 3600:
            return "ticks are running but none has succeeded in the last %gh (see the log)" % max_ok_age_hours
        return ""

    def _tick(self) -> None:
        now = self.clock()
        self._recover_sending(now)  # bookkeeping for sends interrupted by a crash; safe at any hour
        if not self.in_window(now):
            return
        self._recheck_broken(now)
        self._advance_day(now)
        self._plan_day(now)
        self._poll_arrivals(now)
        self._send_due(now)

    # ------------------------------------------------------------ planning
    def _plan_day(self, now: float) -> None:
        key = "planned:" + self.local(now).date().isoformat()
        if self.db.get(key):
            return
        cap = self.today_cap(now)
        if cap <= 0:
            self.db.set(key, "0")
            return
        d = self.local(now)
        end = datetime.combine(d.date(), self.cfg.win_end, self.cfg.tz).timestamp() - 30 * 60
        if end - now < 20 * 60:
            return  # too late today, try tomorrow
        n_primary = max(1, round(cap * self.cfg.open_share))
        n_peer = round(cap * self.cfg.peer_open_share)
        planned = {"primary": 0, "peer": 0, "trail": 0}
        for who, n in (("primary", n_primary), ("peer", n_peer)):
            for _ in range(n):
                pick = self._pick_open(who == "primary")
                if pick is None:
                    continue  # trails exhausted and fallback is "stop"
                sender, recipient, trail_id = pick
                self._add_job(self.rng.uniform(now, end), "open", sender, recipient, trail_id=trail_id)
                planned[who] += 1
                planned["trail"] += bool(trail_id)
        self.db.set(key, f"{planned['primary']}+{planned['peer']}")
        log.info("Planned day: cap=%d, primary opens=%d, peer opens=%d (%d from trails)",
                 cap, planned["primary"], planned["peer"], planned["trail"])
        self._check_trail_stock()

    # ------------------------------------------------------------ pre-generated trails
    def _reserve_trail(self, primary_opens: bool) -> Trail | None:
        """Take the next unused trail for this direction and mark it reserved (state lives in the DB)."""
        self.trails.refresh()  # type: ignore[union-attr]
        used = used_ids(self.db)
        prim = self.cfg.primary.email  # type: ignore[union-attr]
        for tid in self.trails.order:  # type: ignore[union-attr]
            if tid in used:
                continue
            t = self.trails.trails[tid]  # type: ignore[union-attr]
            if not eligible(t, self.accounts):
                continue
            if primary_opens != (t.opener == prim) or (not primary_opens and t.recipient != prim):
                continue
            self.db.x("insert or ignore into trail_state(trail_id,status,updated_at) values(?, 'reserved', ?)",
                      (tid, int(self.clock())))
            return t
        return None

    def _pick_open(self, primary_opens: bool):
        """(sender, recipient, trail_id) for one planned opening, or None to skip the slot."""
        if self.trails is not None:
            t = self._reserve_trail(primary_opens)
            if t:
                return t.opener, t.recipient, t.id
            if self.cfg.trails_fallback == "stop":
                return None
        p = self.cfg.primary.email  # type: ignore[union-attr]
        peer = self.rng.choice(self.cfg.peers).email
        return (p, peer, None) if primary_opens else (peer, p, None)

    def trail_stock(self) -> int:
        """Unsent, usable emails left in the trails file."""
        if self.trails is None:
            return 0
        self.trails.refresh()
        used = used_ids(self.db)
        return sum(t.total for t in self.trails.trails.values() if t.id not in used and eligible(t, self.accounts))

    def _check_trail_stock(self) -> None:
        if self.trails is None:
            return
        stock = self.trail_stock()
        if stock >= self.cfg.trails_low_stock:
            return
        how = ("nothing more will be sent" if self.cfg.trails_fallback == "stop"
               else "emails are being written live instead")
        log.warning("Trails running low: %d unsent emails left, %s", stock, how)
        self.alerter.send("trails_low", f"Pre-generated emails running low: {stock} left",
                          f"Run `python -m warmup generate --count N` to top up; {how}.", cooldown_hours=24)

    def _add_job(self, due, kind, sender, recipient, thread_id=None, turn=1, trail_id=None) -> None:
        self.db.x("insert into jobs(due_at,kind,thread_id,sender,recipient,turn,trail_id) values(?,?,?,?,?,?,?)",
                  (int(due), kind, thread_id, sender, recipient, turn, trail_id))

    # ------------------------------------------------------------ arrivals
    def _poll_arrivals(self, now: float) -> None:
        rows = self.db.q(
            "select m.*, t.total_turns from messages m join threads t on t.id=m.thread_id "
            "where m.arrived_at is null and m.sent_at<? and m.sent_at>?", (int(now - 20), int(now - 3 * 86400)))
        for m in rows:
            rcpt = self.accounts[m["recipient"]]
            if self.is_broken(rcpt.email):
                continue
            try:
                placement = self.t.locate(rcpt, m["message_id"])
            except AuthError as e:
                self.mark_broken(rcpt.email, e, now)
                continue
            except Exception as e:  # noqa: BLE001
                log.warning("locate failed for %s: %s", rcpt.email, e)
                continue
            if placement is None:
                if now - m["sent_at"] > 6 * 3600:
                    self.db.x("update messages set arrived_at=?, placement='missing' where id=?", (int(now), m["id"]))
                continue
            star = self.rng.random() < self.cfg.star_rate
            try:
                self.t.engage(rcpt, m["message_id"], placement, star)
            except AuthError as e:
                self.mark_broken(rcpt.email, e, now)
            except Exception as e:  # noqa: BLE001
                log.warning("engage failed for %s: %s", rcpt.email, e)
            self.db.x("update messages set arrived_at=?, placement=? where id=?", (int(now), placement, m["id"]))
            if m["turn"] < m["total_turns"] and self.rng.random() < self.cfg.reply_rate:
                lo, hi = self.cfg.reply_delay_min
                due = self.clamp(now + self.rng.uniform(lo, hi) * 60)
                self._add_job(due, "reply", m["recipient"], m["sender"], m["thread_id"], m["turn"] + 1)

    # ------------------------------------------------------------ sending
    def _sent_today(self, email: str, now: float) -> int:
        r = self.db.q("select count(*) c from messages where sender=? and sent_at>=?",
                      (email, int(self.midnight(now))))
        return r[0]["c"]

    def _defer(self, j, due: float, status: str = "pending", **extra) -> None:
        cols = "".join(f", {k}=?" for k in extra)
        self.db.x(f"update jobs set status=?, due_at=?{cols} where id=?",
                  (status, int(due), *extra.values(), j["id"]))

    def _send_due(self, now: float) -> None:
        jobs = self.db.q("select * from jobs where status='pending' and due_at<=? order by due_at", (int(now),))
        for j in jobs:
            sender, rcpt = self.accounts[j["sender"]], self.accounts[j["recipient"]]
            is_primary = sender.email == self.cfg.primary.email  # type: ignore
            cap = self.today_cap(now) if is_primary else self.cfg.peer_max_per_day
            if self._sent_today(sender.email, now) >= cap or self.is_broken(sender.email):
                self._defer(j, self.next_window_open(now))
                continue
            try:
                # Compose once and persist, so every retry or crash recovery reuses the same
                # content and Message-ID. Mark 'sending' BEFORE touching SMTP.
                if j["payload"]:
                    p = json.loads(j["payload"])
                else:
                    p = self._compose(j, now)
                    self.db.x("update jobs set payload=? where id=?", (json.dumps(p), j["id"]))
                self.db.x("update jobs set status='sending', started_at=? where id=?",
                          (int(self.clock()), j["id"]))
                self.t.send(sender, rcpt, p["subject"], p["full"], p["mid"], p["irt"], p["refs"])
            except AuthError as e:
                # not the job's fault: park the account, keep the job and its attempt count
                self.mark_broken(sender.email, e, now)
                self._defer(j, self.next_window_open(now))
                continue
            except Exception as e:  # noqa: BLE001
                att = j["attempts"] + 1
                log.error("job %s failed (%d): %s", j["id"], att, e)
                self._defer(j, now + 1800 * att, attempts=att, error=str(e)[:300],
                            status="failed" if att >= 4 else "pending")
                if att >= 4 and j["trail_id"]:
                    self.db.x("update trail_state set status='failed', updated_at=? where trail_id=?",
                              (int(now), j["trail_id"]))
                if att >= 4:
                    self.alerter.send("job_failed", f"A {j['kind']} email from {sender.email} failed 4 times, dropped",
                                      str(e))
                continue
            # The mail is out. Record it atomically; if this fails the job stays 'sending'
            # and _recover_sending() finishes the bookkeeping on the next tick.
            try:
                self._finish(j, p, self.clock())
            except Exception:  # noqa: BLE001
                log.exception("job %s sent but not recorded; recovery will reconcile it", j["id"])
            # small human-like gap between consecutive sends
            if hasattr(self, "sleep"):
                self.sleep(self.rng.uniform(5, 40))

    def _compose(self, j, now: float) -> dict:
        sender, rcpt = self.accounts[j["sender"]], self.accounts[j["recipient"]]
        trail_id = None
        if j["kind"] == "open":
            tr = self.trails.get(j["trail_id"]) if self.trails is not None else None
            if tr is not None:
                trail_id, subject, body, src, total = tr.id, tr.subject, tr.messages[0]["body"], "trail", tr.total
            else:
                subject, body, src = self.content.opening(sender, rcpt)
                total = self.rng.randint(self.cfg.min_turns, self.cfg.max_turns)
            tid = self.db.x("insert into threads(subject,a,b,total_turns,created_at,trail_id) values(?,?,?,?,?,?)",
                            (subject, sender.email, rcpt.email, total, int(now), trail_id))
            turn, irt, refs, full = 1, None, [], body
        else:
            tid, turn = j["thread_id"], j["turn"]
            th = self.db.q("select * from threads where id=?", (tid,))[0]
            msgs = self.db.q("select * from messages where thread_id=? order by turn", (tid,))
            total = th["total_turns"]
            tr = self.trails.get(th["trail_id"]) if self.trails is not None else None
            if tr is not None and turn <= tr.total and tr.messages[turn - 1]["from"] == sender.email:
                trail_id, body, src = tr.id, tr.messages[turn - 1]["body"], "trail"
            else:
                hist = [(self.accounts[m["sender"]].name, m["body"]) for m in msgs]
                body, src = self.content.reply(sender, rcpt, hist, final=(turn >= total))
            last = msgs[-1]
            when = self.local(last["sent_at"]).strftime("%a, %b %d, %Y at %I:%M %p")
            full = body + quote(self.accounts[last["sender"]].name, last["sender"], when, last["body"])
            subject = th["subject"] if th["subject"].lower().startswith("re:") else "Re: " + th["subject"]
            irt, refs = last["message_id"], [m["message_id"] for m in msgs]
        mid = make_msgid(domain=sender.domain)
        return dict(tid=tid, turn=turn, subject=subject, body=body, full=full, mid=mid,
                    irt=irt, refs=refs, source=src, trail_id=trail_id, total=total)

    def _finish(self, j, p: dict, sent_at: float) -> None:
        """Record a sent message, the trail's progress and the closed job in one transaction."""
        stmts = [
            ("insert or ignore into messages(thread_id,turn,sender,recipient,message_id,subject,body,"
             "sent_at,source) values(?,?,?,?,?,?,?,?,?)",
             (p["tid"], p["turn"], j["sender"], j["recipient"], p["mid"], p["subject"], p["body"],
              int(sent_at), p["source"])),
            ("update jobs set status='done' where id=?", (j["id"],)),
        ]
        if p.get("trail_id"):
            status = "done" if p["turn"] >= p.get("total", 0) else "active"
            stmts.append((
                "insert into trail_state(trail_id,status,thread_id,last_turn,updated_at) values(?,?,?,?,?) "
                "on conflict(trail_id) do update set status=excluded.status, thread_id=excluded.thread_id, "
                "last_turn=excluded.last_turn, updated_at=excluded.updated_at",
                (p["trail_id"], status, p["tid"], p["turn"], int(sent_at))))
        self.db.tx(stmts)

    def _recover_sending(self, now: float) -> None:
        """Jobs left in 'sending' mean the process died mid-send (the lock guarantees we are the
        only process, so nothing is genuinely in flight). Ask the recipient mailbox whether the
        mail arrived: if so just record it, otherwise queue it again with the SAME Message-ID so
        a rare double delivery is collapsed by the mail server."""
        for j in self.db.q("select * from jobs where status='sending'"):
            p = json.loads(j["payload"])
            rcpt = self.accounts[j["recipient"]]
            found = None
            if not self.is_broken(rcpt.email):
                try:
                    found = self.t.locate(rcpt, p["mid"])
                except Exception as e:  # noqa: BLE001
                    log.warning("recovery: cannot check %s for job %s: %s", rcpt.email, j["id"], e)
            if found is not None:
                log.warning("job %s: send had completed before the interruption, recording it", j["id"])
                self._finish(j, p, j["started_at"] or now)
            else:
                log.warning("job %s: interrupted before delivery was confirmed, will resend", j["id"])
                self._defer(j, now)

    # ------------------------------------------------------------ reporting
    def status(self) -> str:
        now = self.clock()
        day = int(self.db.get("day_idx", "0"))
        rate, n = self.spam_rate(now, 7)
        rows = self.db.q("select placement, count(*) c from messages where placement is not null "
                         "and arrived_at>=? group by placement", (int(now - 7 * 86400),))
        pl = {r["placement"]: r["c"] for r in rows}
        pend = self.db.q("select count(*) c from jobs where status='pending'")[0]["c"]
        failed = self.db.q("select count(*) c from jobs where status='failed'")[0]["c"]
        lines = [
            f"Ramp day {day}, today's primary cap {self.today_cap(now)} (max {self.cfg.ramp_max})",
            f"Primary sent today: {self._sent_today(self.cfg.primary.email, now)}",  # type: ignore
            f"Last 7 days placement: {pl or 'no data'}  spam rate {rate:.0%} (n={n})",
            f"Jobs pending {pend}, failed {failed}",
        ]
        src = self.db.q("select coalesce(source,'unknown') s, count(*) c from messages "
                        "where sent_at>=? group by s", (int(now - 7 * 86400),))
        if src:
            lines.append("Content source (7d): " + ", ".join(f"{r['s']} {r['c']}" for r in src))
        if self.trails is not None:
            st = {r["status"]: r["c"] for r in self.db.q("select status, count(*) c from trail_state group by status")}
            lines.append(f"Trails: {self.trail_stock()} unsent emails in stock, used {st or 'none'}")
        for email, info in self.broken_accounts().items():
            lines.append(f"BROKEN {email}: {info['error']}")
        return "\n".join(lines)
