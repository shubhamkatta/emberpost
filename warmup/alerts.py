"""Alerts: a webhook (Slack, Discord and most generic receivers) plus a dead-man's-switch heartbeat.

Everything here is best effort and must never break the engine. Alerts are throttled per key
(persisted in the DB) so a problem that lasts all day pages you once, not every tick."""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request

log = logging.getLogger("warmup.alerts")


def _post_json(url: str, payload: dict) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json", "user-agent": "emberpost"})
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


def _get(url: str) -> None:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"user-agent": "emberpost"}), timeout=10) as r:
        r.read()


class Alerter:
    def __init__(self, cfg, db, clock=time.time, enabled: bool = True, post=_post_json, get=_get):
        self.cfg, self.db, self.clock, self.enabled = cfg, db, clock, enabled
        self._post, self._get = post, get

    def _url(self, env_name: str) -> str:
        return os.environ.get(env_name, "").strip() if self.enabled else ""

    def send(self, key: str, title: str, detail: str = "", cooldown_hours: float | None = None) -> bool:
        """Returns True if a notification was delivered. Throttled per `key`."""
        url = self._url(self.cfg.alert_webhook_env)
        if not url:
            return False
        hours = self.cfg.alert_min_interval_hours if cooldown_hours is None else cooldown_hours
        now = self.clock()
        last = float(self.db.get("alert:" + key, "0") or 0)
        if hours > 0 and now - last < hours * 3600:
            return False
        text = f"[emberpost {self.cfg.primary.email}] {title}" + (f"\n{detail[:500]}" if detail else "")
        try:
            # "text" is what Slack reads, "content" is what Discord reads
            self._post(url, {"text": text, "content": text})
        except Exception as e:  # noqa: BLE001  (the URL is a secret: do not log it)
            log.warning("alert delivery failed (%s), will retry on the next event", type(e).__name__)
            return False
        self.db.set("alert:" + key, str(now))  # only throttle after a successful delivery
        return True

    def heartbeat(self) -> None:
        """Ping a dead-man's-switch URL (healthchecks.io, Better Stack, Uptime Kuma push...). Call it
        only after a tick succeeded: if the process dies or ticks keep failing, the pings stop and the
        monitor alerts you, which no in-process alert can do."""
        url = self._url(self.cfg.alert_heartbeat_env)
        if not url:
            return
        try:
            self._get(url)
        except Exception as e:  # noqa: BLE001
            log.warning("heartbeat ping failed (%s)", type(e).__name__)
