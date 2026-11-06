"""Config validation. Collects every problem at once instead of failing on the first, and
rejects unknown keys (a typo like `max_per_dya` would otherwise silently use the default)."""
from __future__ import annotations

import difflib
import math
import re
import shutil
from zoneinfo import ZoneInfo

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TIME = re.compile(r"^(\d{1,2}):(\d{2})$")

SCHEMA: dict[str, set[str] | None] = {
    "timezone": None, "database": None, "weekend_factor": None, "saturday_factor": None, "broken_retry_hours": None,
    "send_window": {"start", "end"},
    "business": {"description", "topics", "tone", "avoid"},
    "ramp": {"start_per_day", "max_per_day", "days_to_max", "hold_if_spam_rate_above"},
    "conversation": {"min_turns", "max_turns", "reply_rate", "reply_delay_minutes", "primary_open_share",
                     "peer_open_share", "peer_max_per_day", "star_rate"},
    "llm": {"enabled", "provider", "api_key_env", "command", "model"},
    "trails": {"enabled", "file", "fallback", "low_stock_alert"},
    "alerts": {"webhook_url_env", "heartbeat_url_env", "min_interval_hours"},
    "primary": {"email", "name", "persona", "password_env", "smtp_host", "smtp_port", "imap_host"},
    "peers": {"count", "accounts"},
}
ACCOUNT_KEYS = SCHEMA["primary"]


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def err(self, m: str) -> None:
        self.errors.append(m)

    def warn(self, m: str) -> None:
        self.warnings.append(m)


def _unknown_keys(rep: Report, d: dict, known: set[str], prefix: str) -> None:
    for k in d:
        if k not in known:
            m = difflib.get_close_matches(str(k), sorted(known), n=1)
            rep.err(f"unknown setting '{prefix}{k}'" + (f" (did you mean '{m[0]}'?)" if m else ""))


def _section(rep: Report, raw: dict, name: str) -> dict:
    v = raw.get(name)
    if v is None:
        return {}
    if not isinstance(v, dict):
        rep.err(f"'{name}' must be a mapping, got {type(v).__name__}")
        return {}
    _unknown_keys(rep, v, SCHEMA[name] or set(), f"{name}.")
    return v


def _num(rep: Report, d: dict, key: str, path: str, integer: bool = False,
         lo: float | None = None, hi: float | None = None):
    if key not in d:
        return None
    x = d[key]
    try:
        if isinstance(x, bool) or x is None:
            raise ValueError
        f = float(x)
        if not math.isfinite(f):
            raise ValueError
    except (TypeError, ValueError):
        rep.err(f"{path}{key} must be a number, got {x!r}")
        return None
    if integer and f != int(f):
        rep.err(f"{path}{key} must be a whole number, got {x!r}")
        return None
    if lo is not None and f < lo:
        rep.err(f"{path}{key} must be at least {lo:g}, got {x!r}")
        return None
    if hi is not None and f > hi:
        rep.err(f"{path}{key} must be at most {hi:g}, got {x!r}")
        return None
    return int(f) if integer else f


def _minutes(rep: Report, d: dict, key: str, path: str, default: str) -> int | None:
    s = str(d.get(key, default))
    m = TIME.match(s)
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        rep.err(f'{path}{key} must be a 24h "HH:MM" time, got {d.get(key)!r}')
        return None
    return int(m[1]) * 60 + int(m[2])


def _str(rep: Report, d: dict, key: str, path: str, required: bool = False) -> str | None:
    v = d.get(key)
    if v is None:
        if required:
            rep.err(f"{path}{key} is required")
        return None
    if not isinstance(v, str) or (required and not v.strip()):
        rep.err(f"{path}{key} must be a non-empty string")
        return None
    return v


def _account(rep: Report, a, path: str, env, require_secrets: bool) -> dict | None:
    if not isinstance(a, dict):
        rep.err(f"{path} must be a mapping")
        return None
    _unknown_keys(rep, a, ACCOUNT_KEYS or set(), f"{path}.")
    email = _str(rep, a, "email", f"{path}.", required=True)
    if email and not EMAIL.match(email):
        rep.err(f"{path}.email {email!r} is not a valid email address")
    name = _str(rep, a, "name", f"{path}.")
    _str(rep, a, "persona", f"{path}.")
    port = _num(rep, a, "smtp_port", f"{path}.", integer=True, lo=1, hi=65535)
    if port is not None and port != 465:
        rep.err(f"{path}.smtp_port is {port}, but only implicit-TLS SMTP (port 465) is supported")
    host = a.get("imap_host", "imap.gmail.com")
    if host != "imap.gmail.com":
        rep.warn(f"{path}.imap_host is {host!r}: placement tracking uses Gmail-only IMAP extensions "
                 "(X-GM-RAW, X-GM-LABELS) and will not work elsewhere")
    pw_env = _str(rep, a, "password_env", f"{path}.")
    if require_secrets:
        if not pw_env:
            rep.err(f"{path}.password_env is required (the env var holding the app password)")
        elif env is not None and not str(env.get(pw_env, "")).strip():
            rep.err(f"{path}: environment variable {pw_env} is empty or unset. Put the app password in .env")
    return {"email": (email or "").lower(), "name": name, "pw_env": pw_env}


def validate(raw, defaults, env=None, require_secrets: bool = False) -> Report:
    """`defaults` is a Config instance (for cross-checks on omitted values). `env` is the
    environment mapping (None skips all environment checks)."""
    rep = Report()
    if not isinstance(raw, dict):
        rep.err("config file is empty or is not a YAML mapping")
        return rep
    _unknown_keys(rep, raw, set(SCHEMA), "")
    D = defaults

    tz = raw.get("timezone", D.timezone)
    try:
        ZoneInfo(str(tz))
    except Exception:  # noqa: BLE001
        rep.err(f"timezone {tz!r} is not a valid IANA name (example: America/New_York)")
    wf = _num(rep, raw, "weekend_factor", "", lo=0, hi=1)
    _num(rep, raw, "saturday_factor", "", lo=0, hi=1)
    _num(rep, raw, "broken_retry_hours", "", lo=0.1)
    if "database" in raw and not (isinstance(raw["database"], str) and raw["database"].strip()):
        rep.err("database must be a file path")

    # send window
    w = _section(rep, raw, "send_window")
    s = _minutes(rep, w, "start", "send_window.", "09:30")
    e = _minutes(rep, w, "end", "send_window.", "18:00")
    if s is not None and e is not None:
        if e <= s:
            rep.err(f"send_window.end ({w.get('end')}) must be after send_window.start ({w.get('start')}); "
                    "overnight windows are not supported")
        elif e - s < 180:
            rep.warn("send_window is under 3 hours, conversations will bunch up")

    # ramp
    r = _section(rep, raw, "ramp")
    rs = _num(rep, r, "start_per_day", "ramp.", integer=True, lo=1)
    rm = _num(rep, r, "max_per_day", "ramp.", integer=True, lo=1)
    rd = _num(rep, r, "days_to_max", "ramp.", integer=True, lo=1)
    hold = _num(rep, r, "hold_if_spam_rate_above", "ramp.", lo=0, hi=1)
    rs_, rm_ = rs or D.ramp_start, rm or D.ramp_max
    if rs_ > rm_:
        rep.err(f"ramp.start_per_day ({rs_}) is above ramp.max_per_day ({rm_})")
    if rm_ > 50:
        rep.warn(f"ramp.max_per_day is {rm_}. Above ~50/day from one mailbox looks unnatural for a warmup")
    if (rd or D.ramp_days) < 14:
        rep.warn("ramp.days_to_max under 14 is aggressive for a new domain")
    if (hold or D.hold_spam_rate) > 0.5:
        rep.warn("ramp.hold_if_spam_rate_above over 0.5 means the ramp keeps climbing while most mail is in spam")

    # conversation
    c = _section(rep, raw, "conversation")
    mn = _num(rep, c, "min_turns", "conversation.", integer=True, lo=1)
    mx = _num(rep, c, "max_turns", "conversation.", integer=True, lo=1)
    if (mn or D.min_turns) > (mx or D.max_turns):
        rep.err(f"conversation.min_turns ({mn or D.min_turns}) is above max_turns ({mx or D.max_turns})")
    _num(rep, c, "reply_rate", "conversation.", lo=0, hi=1)
    _num(rep, c, "star_rate", "conversation.", lo=0, hi=1)
    pos = _num(rep, c, "primary_open_share", "conversation.", lo=0, hi=1)
    pes = _num(rep, c, "peer_open_share", "conversation.", lo=0, hi=1)
    pmax = _num(rep, c, "peer_max_per_day", "conversation.", integer=True, lo=1)
    if (pos if pos is not None else D.open_share) + (pes if pes is not None else D.peer_open_share) > 1:
        rep.warn("primary_open_share + peer_open_share is above 1, planned threads will exceed the daily cap")
    if "reply_delay_minutes" in c:
        d = c["reply_delay_minutes"]
        ok = (isinstance(d, (list, tuple)) and len(d) == 2 and
              all(isinstance(x, (int, float)) and not isinstance(x, bool) and x >= 0 for x in d))
        if not ok:
            rep.err(f"conversation.reply_delay_minutes must be [min, max] minutes, got {d!r}")
        elif d[0] > d[1]:
            rep.err(f"conversation.reply_delay_minutes min ({d[0]}) is above max ({d[1]})")

    # business
    b = _section(rep, raw, "business")
    desc = _str(rep, b, "description", "business.")
    if not desc:
        rep.warn("business.description is empty, the LLM has no idea what your company does")
    _str(rep, b, "tone", "business.")
    _str(rep, b, "avoid", "business.")
    topics = b.get("topics")
    if topics is not None and not (isinstance(topics, list) and all(isinstance(t, str) and t.strip() for t in topics)):
        rep.err("business.topics must be a list of non-empty strings")
    elif not topics:
        rep.warn("business.topics is empty, generic topics will be used")

    # llm
    m = _section(rep, raw, "llm")
    if "enabled" in m and not isinstance(m["enabled"], bool):
        rep.err(f"llm.enabled must be true or false, got {m['enabled']!r}")
    prov = str(m.get("provider", D.llm_provider)).lower()
    if prov not in ("cli", "api"):
        rep.err(f"llm.provider must be 'cli' or 'api', got {m.get('provider')!r}")
    llm_on = m.get("enabled", False) is True
    if not llm_on:
        rep.warn("llm.enabled is false: only repeating templates will be sent, fine for a test, not for weeks")
    elif env is not None and require_secrets:
        if prov == "api" and not str(env.get(m.get("api_key_env", D.llm_api_key_env), "")).strip():
            rep.warn(f"llm.provider is api but {m.get('api_key_env', D.llm_api_key_env)} is empty, "
                     "templates will be used")
        if prov == "cli" and not shutil.which(m.get("command", D.llm_command)):
            rep.warn(f"llm command {m.get('command', D.llm_command)!r} is not on PATH, templates will be used")

    # trails
    tr = _section(rep, raw, "trails")
    if "enabled" in tr and not isinstance(tr["enabled"], bool):
        rep.err(f"trails.enabled must be true or false, got {tr['enabled']!r}")
    if "file" in tr and not (isinstance(tr["file"], str) and tr["file"].strip()):
        rep.err("trails.file must be a file path")
    if str(tr.get("fallback", D.trails_fallback)).lower() not in ("live", "stop"):
        rep.err(f"trails.fallback must be 'live' or 'stop', got {tr.get('fallback')!r}")
    _num(rep, tr, "low_stock_alert", "trails.", integer=True, lo=0)

    # alerts
    al = _section(rep, raw, "alerts")
    _num(rep, al, "min_interval_hours", "alerts.", lo=0)
    if env is not None and require_secrets:
        wh = str(env.get(al.get("webhook_url_env", D.alert_webhook_env), "")).strip()
        if not wh:
            rep.warn(f"{al.get('webhook_url_env', D.alert_webhook_env)} is not set: failures will only be "
                     "written to the log, nobody gets paged")
        elif not wh.startswith("https://"):
            rep.warn("the alert webhook URL is not https")

    # accounts
    prim = _account(rep, raw.get("primary"), "primary", env, require_secrets) if "primary" in raw else None
    if "primary" not in raw:
        rep.err("primary is required")
    elif prim and prim["name"] == "Your Name":
        rep.warn("primary.name is still the placeholder 'Your Name'")

    peers = raw.get("peers")
    active: list[dict] = []
    if not isinstance(peers, dict):
        rep.err("peers is required and must be a mapping with 'accounts'")
    else:
        accts = peers.get("accounts")
        if not isinstance(accts, list) or not accts:
            rep.err("peers.accounts must be a non-empty list")
        else:
            cnt = _num(rep, peers, "count", "peers.", integer=True, lo=1)
            if cnt is not None and cnt > len(accts):
                rep.err(f"peers.count is {cnt} but only {len(accts)} accounts are listed")
            use = accts[: cnt] if cnt else accts
            for i, a in enumerate(use):
                info = _account(rep, a, f"peers.accounts[{i}]", env, require_secrets)
                if info:
                    active.append(info)
            if len(use) < 3:
                rep.warn(f"only {len(use)} peer account(s): a tiny closed pool is easy for Gmail to spot, use 5 to 10")

    everyone = ([prim] if prim else []) + active
    seen_mail: dict[str, int] = {}
    seen_env: dict[str, str] = {}
    for a in everyone:
        if a["email"]:
            seen_mail[a["email"]] = seen_mail.get(a["email"], 0) + 1
        if a["pw_env"]:
            if a["pw_env"] in seen_env:
                rep.err(f"{a['pw_env']} is used by both {seen_env[a['pw_env']]} and {a['email']}: "
                        "every account needs its own app password variable")
            seen_env[a["pw_env"]] = a["email"]
    for em, n in seen_mail.items():
        if n > 1:
            rep.err(f"{em} is listed more than once (the primary and peers must all be different accounts)")

    peer_cap = pmax or D.peer_max_per_day
    if active and rm_ > len(active) * peer_cap:
        rep.warn(f"ramp.max_per_day ({rm_}) is more than the peers can answer "
                 f"({len(active)} x peer_max_per_day {peer_cap})")
    if wf is not None and wf > 0.5:
        rep.warn("weekend_factor above 0.5: weekend volume this high is atypical for business mail")
    return rep
