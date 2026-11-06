from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .validate import validate


log = logging.getLogger("warmup.config")


@dataclass
class Account:
    email: str
    name: str
    password: str = ""
    persona: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    imap_host: str = "imap.gmail.com"

    @property
    def first_name(self) -> str:
        return self.name.split()[0]

    @property
    def domain(self) -> str:
        return self.email.split("@")[1]


@dataclass
class Config:
    timezone: str = "America/New_York"
    win_start: time = time(9, 30)
    win_end: time = time(18, 0)
    weekend_factor: float = 0.0  # 0 = no weekend sending
    saturday_factor: float | None = None  # Saturday only; None = same as weekend_factor
    database: str = "warmup.db"
    # ramp
    ramp_start: int = 3
    ramp_max: int = 40
    ramp_days: int = 35
    hold_spam_rate: float = 0.2  # hold the ramp if recent spam rate is above this
    # conversation
    min_turns: int = 2
    max_turns: int = 5
    reply_rate: float = 0.7
    reply_delay_min: tuple[int, int] = (8, 240)  # minutes
    open_share: float = 0.4  # share of daily cap the primary opens itself
    peer_open_share: float = 0.2  # peers opening threads to primary, as share of cap
    peer_max_per_day: int = 15
    star_rate: float = 0.25
    # content
    business_description: str = "a small B2B software company"
    topics: list[str] = field(default_factory=list)
    tone: str = ""
    avoid: str = ""
    llm_enabled: bool = False
    llm_model: str = "haiku"
    llm_command: str = "claude"
    llm_provider: str = "cli"  # "cli" = Claude Code CLI login, "api" = Anthropic API key
    llm_api_key_env: str = "ANTHROPIC_API_KEY"
    # accounts that failed login are re-checked after this long
    broken_cooldown_hours: float = 6.0
    # pre-generated conversation trails (see warmup/trails.py)
    trails_enabled: bool = False
    trails_file: str = "trails.jsonl"
    trails_fallback: str = "live"      # live = write on the fly when the file runs out, stop = send nothing
    trails_low_stock: int = 60         # alert when fewer unsent emails than this remain
    # alerts (URLs are secrets, so they live in env vars, not in the yaml)
    alert_webhook_env: str = "ALERT_WEBHOOK_URL"
    alert_heartbeat_env: str = "HEARTBEAT_URL"
    alert_min_interval_hours: float = 6.0
    # accounts
    primary: Account | None = None
    peers: list[Account] = field(default_factory=list)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def _account(d: dict, require_secrets: bool) -> Account:
    pw = ""
    env = d.get("password_env")
    if env:
        pw = os.environ.get(env, "").replace(" ", "")
        if not pw and require_secrets:
            raise SystemExit(f"Missing secret: env var {env} for {d['email']}")
    return Account(
        email=d["email"],
        name=d.get("name", d["email"].split("@")[0]),
        password=pw,
        persona=d.get("persona", ""),
        smtp_host=d.get("smtp_host", "smtp.gmail.com"),
        smtp_port=int(d.get("smtp_port", 465)),
        imap_host=d.get("imap_host", "imap.gmail.com"),
    )


def load_config(path: str, require_secrets: bool = True) -> Config:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"Config file not found: {p}. Copy config.example.yaml to config.yaml and edit it.")
    _load_env(p.parent / ".env")
    _load_env(Path(".env"))
    try:
        raw = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        raise SystemExit(f"{p} is not valid YAML:\n{e}") from e

    rep = validate(raw, Config(), env=os.environ, require_secrets=require_secrets)
    if isinstance(raw, dict) and isinstance(raw.get("database"), str):
        parent = Path(raw["database"]).expanduser().resolve().parent
        if not parent.is_dir():
            rep.err(f"database directory {parent} does not exist")
    for w in rep.warnings:
        log.warning("config: %s", w)
    if rep.errors:
        raise SystemExit(f"Invalid config ({p}), {len(rep.errors)} problem(s):\n" +
                         "\n".join(f"  - {e}" for e in rep.errors))

    c = Config()
    c.timezone = raw.get("timezone", c.timezone)
    w = raw.get("send_window") or {}
    c.win_start = _t(str(w.get("start", "09:30")))
    c.win_end = _t(str(w.get("end", "18:00")))
    c.weekend_factor = float(raw.get("weekend_factor", c.weekend_factor))
    if raw.get("saturday_factor") is not None:
        c.saturday_factor = float(raw["saturday_factor"])
    c.database = raw.get("database", c.database)

    r = raw.get("ramp") or {}
    c.ramp_start = int(r.get("start_per_day", c.ramp_start))
    c.ramp_max = int(r.get("max_per_day", c.ramp_max))
    c.ramp_days = int(r.get("days_to_max", c.ramp_days))
    c.hold_spam_rate = float(r.get("hold_if_spam_rate_above", c.hold_spam_rate))

    cv = raw.get("conversation") or {}
    c.min_turns = int(cv.get("min_turns", c.min_turns))
    c.max_turns = int(cv.get("max_turns", c.max_turns))
    c.reply_rate = float(cv.get("reply_rate", c.reply_rate))
    c.reply_delay_min = tuple(cv.get("reply_delay_minutes", c.reply_delay_min))  # type: ignore
    c.open_share = float(cv.get("primary_open_share", c.open_share))
    c.peer_open_share = float(cv.get("peer_open_share", c.peer_open_share))
    c.peer_max_per_day = int(cv.get("peer_max_per_day", c.peer_max_per_day))
    c.star_rate = float(cv.get("star_rate", c.star_rate))

    b = raw.get("business") or {}
    c.business_description = b.get("description") or c.business_description
    c.topics = b.get("topics") or []
    c.tone = b.get("tone") or ""
    c.avoid = b.get("avoid") or ""

    llm = raw.get("llm") or {}
    c.llm_enabled = bool(llm.get("enabled", False))
    c.llm_model = llm.get("model", c.llm_model)
    c.llm_command = llm.get("command", c.llm_command)
    c.llm_provider = str(llm.get("provider", c.llm_provider)).lower()
    c.llm_api_key_env = llm.get("api_key_env", c.llm_api_key_env)
    c.broken_cooldown_hours = float(raw.get("broken_retry_hours", c.broken_cooldown_hours))

    tr = raw.get("trails") or {}
    c.trails_enabled = bool(tr.get("enabled", c.trails_enabled))
    c.trails_file = str(tr.get("file", c.trails_file))
    c.trails_fallback = str(tr.get("fallback", c.trails_fallback)).lower()
    c.trails_low_stock = int(tr.get("low_stock_alert", c.trails_low_stock))
    if c.trails_enabled and not Path(c.trails_file).exists():
        log.warning("config: trails.enabled but %s does not exist yet. Run `python -m warmup generate` first; "
                    "until then emails are written live.", c.trails_file)

    al = raw.get("alerts") or {}
    c.alert_webhook_env = al.get("webhook_url_env", c.alert_webhook_env)
    c.alert_heartbeat_env = al.get("heartbeat_url_env", c.alert_heartbeat_env)
    c.alert_min_interval_hours = float(al.get("min_interval_hours", c.alert_min_interval_hours))

    c.primary = _account(raw["primary"], require_secrets)
    pe = raw["peers"]
    accts = pe["accounts"]
    if pe.get("count") is not None:
        accts = accts[: int(pe["count"])]
    c.peers = [_account(a, require_secrets) for a in accts]
    return c
