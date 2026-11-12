"""Email content. LLM first (validated, de-duplicated), template fallback."""
from __future__ import annotations

import difflib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

log = logging.getLogger("warmup.content")

LLM_MAX_TRIES = 2          # regenerations per email when output fails validation / is a duplicate
LLM_FAIL_LIMIT = 3         # consecutive hard failures before the LLM is paused
LLM_PAUSE_SECONDS = 1800
API_MODELS = {"": "claude-haiku-5-5", "haiku": "claude-haiku-5-5",
              "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}

DEFAULT_TOPICS = [
    "scheduling a demo or onboarding call",
    "following up on a quote or proposal",
    "asking for feedback on a draft document",
    "clarifying an invoice or payment detail",
    "coordinating a meeting agenda for next week",
    "sharing notes after a vendor demo",
    "checking availability for a workshop",
    "asking about onboarding steps for a new tool",
    "confirming deliverables for the end of the month",
    "requesting a reference or introduction",
]

OPENERS = [
    ("Quick question on {t}", "Hi {r},\n\nHope your week is going well. I wanted to check in about {t}. Do you have a few minutes this week to talk it through? Let me know what works on your side.\n\n{s}"),
    ("Re: {t}", "Hi {r},\n\nFollowing up on our earlier chat about {t}. I put together a few thoughts and would like your view before we lock anything in. Are you free for a short call on Thursday or Friday?\n\n{s}"),
    ("Next steps: {t}", "Hello {r},\n\nThanks for your time last week. On {t}, I think we're close. Could you confirm who else should be on the thread so nothing slips?\n\nThanks,\n{s}"),
    ("Checking in", "Hi {r},\n\nJust a note on {t}. I have most of what I need, but a couple of details are still open. Can you send them over when you get a chance?\n\nBest,\n{s}"),
]
REPLIES = [
    "Hi {r},\n\nThanks for the note. That works for me. I can do tomorrow afternoon or early next week, whichever is easier for you.\n\nBest,\n{s}",
    "Hi {r},\n\nGood to hear from you. I looked at this and have a couple of small questions, mainly around timing. Can we go over them on a quick call?\n\nThanks,\n{s}",
    "Hello {r},\n\nAppreciate the follow up. I'll check with my team and come back to you by end of day tomorrow.\n\nRegards,\n{s}",
    "Hi {r},\n\nMakes sense. I'd suggest we keep the scope as is for now and revisit once we have the numbers. Does that sound fine?\n\nBest,\n{s}",
]
CLOSERS = [
    "Hi {r},\n\nPerfect, that settles it. Thanks for the quick turnaround.\n\nBest,\n{s}",
    "Hi {r},\n\nGreat, thanks. I'll send a calendar invite shortly.\n\nCheers,\n{s}",
    "Hello {r},\n\nSounds good. Talk soon.\n\nRegards,\n{s}",
]


# A random angle per email keeps the same topic from producing the same subject and opening each time.
ANGLES = [
    "a concrete scheduling detail", "a worry about staff adoption", "a question from a client that prompted this",
    "a small setup problem you hit this week", "a request for a short written summary", "a colleague's suggestion",
    "a deadline coming up", "a change in your team's routine", "a follow-up to a call last week",
    "a doubt about privacy or consent wording", "a request to see how it looks on a phone", "a billing or invoice question",
    "a comparison with how you do it today", "a pilot with one practitioner first", "feedback after trying it for a few days",
]


def _strip(text: str) -> str:
    return text.strip().strip("`").strip()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


_BAD_PATTERNS = [
    (re.compile(r"\[[^\]\n]{0,60}\]"), "bracketed placeholder"),
    (re.compile(r"\{[^}\n]{0,60}\}"), "curly placeholder"),
    (re.compile(r"<[^>\n]{1,60}>"), "angle-bracket placeholder"),
    (re.compile(r"https?://|www\.", re.I), "link"),
    (re.compile(r"[\U0001F000-\U0001FFFF\u2600-\u27BF]"), "emoji"),
    (re.compile(r"as an ai|language model|warm-?ing? ?up|test email|lorem ipsum|"
                r"here(?:'s| is) (?:the|your|an?) (?:email|reply|draft)|^subject:", re.I | re.M), "meta text"),
]


def validate(body: str) -> str | None:
    """Return why an email body is unusable, or None if it is fine."""
    if len(body) < 40:
        return "too short"
    if len(body) > 1500:
        return "too long"
    for rx, why in _BAD_PATTERNS:
        if rx.search(body):
            return why
    return None


def parse_opening(out: str) -> tuple[str, str] | None:
    """Pull (subject, body) out of 'Subject: ...' output, tolerating a short preamble line."""
    lines = out.strip().splitlines()
    for i, ln in enumerate(lines[:3]):
        if ln.lower().startswith("subject:"):
            subject, body = ln[8:].strip(), "\n".join(lines[i + 1:]).strip()
            if subject and body:
                return subject, body
    return None


class ContentError(Exception):
    """strict mode: the LLM could not produce a usable email (templates are not an acceptable answer)."""


class Content:
    def __init__(self, cfg, rng: random.Random | None = None, clock=time.time, strict: bool = False):
        self.strict = strict              # strict = LLM output only, never templates (used to pre-generate trails)
        self.cfg = cfg
        self.rng = rng or random.Random()
        self.clock = clock
        self.recent = lambda: []          # engine plugs in: recent (subject, body) pairs for de-dup
        self.on_alert = lambda title, detail: None  # engine plugs in: page a human
        self.client = False
        self._fails = 0
        self._skip_until = 0.0
        self._pool_warned = 0.0
        self.last_error = ""
        if cfg.llm_enabled:
            if cfg.llm_provider == "api":
                if os.environ.get(cfg.llm_api_key_env):
                    self.client = True
                else:
                    log.warning("llm.provider=api but %s is not set, falling back to templates", cfg.llm_api_key_env)
            elif shutil.which(cfg.llm_command):
                self.client = True
            else:
                log.warning("'%s' not found on PATH, falling back to templates", cfg.llm_command)

    def _style(self) -> str:
        out = ""
        if self.cfg.tone:
            out += f"Tone: {self.cfg.tone}. "
        if self.cfg.avoid:
            out += f"Never include: {self.cfg.avoid}. "
        return out

    # ---- circuit breaker
    def _llm_ready(self) -> bool:
        return self.client and (self.strict or self.clock() >= self._skip_until)

    def _llm_failed(self, e: Exception) -> None:
        self._fails += 1
        self.last_error = str(e)
        log.warning("LLM call failed (%d in a row): %s", self._fails, e)
        if self.strict:
            return  # the caller waits and retries; there is nothing to fall back to
        if self._fails >= LLM_FAIL_LIMIT:
            self._skip_until = self.clock() + LLM_PAUSE_SECONDS
            self._fails = 0
            hint = ("run `claude` once interactively to log in again, or set llm.provider: api"
                    if self.cfg.llm_provider == "cli" else f"check {self.cfg.llm_api_key_env} and your API credit")
            log.error("LLM unavailable, using templates for %d min. %s", LLM_PAUSE_SECONDS // 60, hint)
            self.on_alert("LLM paused, sending repeating templates", f"Last error: {e}. Fix: {hint}.")

    # ---- de-duplication
    def _dup(self, subject: str | None, body: str) -> bool:
        nb = _norm(body)
        for s, b in self.recent():
            if subject and s and _norm(s) == _norm(subject):
                return True
            sm = difflib.SequenceMatcher(None, nb, _norm(b))
            if sm.quick_ratio() > 0.85 and sm.ratio() > 0.85:
                return True
        return False

    def _pool_exhausted(self) -> None:
        if self.clock() - self._pool_warned > 600:
            self._pool_warned = self.clock()
            log.warning("template pool exhausted, content is repeating. Enable the LLM for long runs.")

    def _ctx(self, sender) -> str:
        """The business sentence for a prompt. The founder speaks for the business; everyone else is a
        practitioner or customer in touch with it, not an employee."""
        desc = self.cfg.business_description
        if sender.email == getattr(self.cfg.primary, "email", None):
            return f"Your business: {desc}\n"
        return (f"The business you are in touch with: {desc}. "
                "You are a practitioner or customer contact of it, not an employee.\n")

    def _fail(self, what: str):
        raise ContentError(f"could not get a usable {what} from the LLM"
                           + (f" (last error: {self.last_error})" if self.last_error else ""))

    # ---- public
    def opening(self, sender, recipient, topic: str | None = None) -> tuple[str, str, str]:
        """Returns (subject, body, source) where source is 'llm' or 'template'. In strict mode a
        failure raises ContentError instead of returning a template."""
        pool = self.cfg.topics or DEFAULT_TOPICS
        if self._llm_ready():
            topic = topic or self.rng.choice(pool)
            used = []                      # subjects already taken: the LLM is told to steer clear of them
            for s, _b in self.recent():
                if s and s not in used:
                    used.append(s)
            avoid = ("Subjects already used, do not reuse or closely paraphrase any of them: "
                     + "; ".join(used[-40:]) + "\n") if used else ""
            angle = self.rng.choice(ANGLES)
            prompt = (
                f"You are {sender.name}{', ' + sender.persona if sender.persona else ''}. "
                f"Write a work email to {recipient.name}"
                f"{' (' + recipient.persona + ')' if recipient.persona else ''}.\n"
                f"{self._ctx(sender)}"
                f"Topic: {topic}\n"
                f"Angle for this email: {angle}\n"
                f"{avoid}"
                f"{self._style()}\n"
                "Rules: 3-6 sentences, plain text, natural and specific, one clear ask. "
                "No placeholders, brackets, links, attachments, emojis or sales language. "
                "Do not mention testing or warming up. Sign off with first name only.\n"
                "Format: first line 'Subject: <subject>', blank line, then the body."
            )
            for _ in range(LLM_MAX_TRIES):
                try:
                    out = self._ask(prompt)
                except Exception as e:  # noqa: BLE001
                    self._llm_failed(e)
                    break
                self._fails = 0
                parsed = parse_opening(_strip(out))
                if not parsed:
                    log.warning("LLM opening not in 'Subject:' format, retrying")
                    continue
                subject, body = parsed
                why = validate(body) or validate_subject(subject)
                if why:
                    self.last_error = f"output rejected: {why}"
                    log.warning("LLM opening rejected (%s), retrying", why)
                elif self._dup(subject, body):
                    self.last_error = "output duplicates a recent email"
                    log.warning("LLM opening duplicates a recent email, retrying")
                else:
                    return subject, body, "llm"
        if self.strict:
            self._fail("opening email")
        pick = None
        for _ in range(12):
            topic = topic or self.rng.choice(pool)
            subj, tpl = self.rng.choice(OPENERS)
            pick = (subj.format(t=topic[:60]),
                    tpl.format(r=recipient.first_name, t=topic, s=sender.first_name))
            if not self._dup(*pick):
                return (*pick, "template")
            topic = None
        self._pool_exhausted()
        return (*pick, "template")  # type: ignore[misc]

    def reply(self, sender, recipient, history: list[tuple[str, str]], final: bool) -> tuple[str, str]:
        """Returns (body, source)."""
        if self._llm_ready():
            convo = "\n\n---\n\n".join(f"{n} wrote:\n{b}" for n, b in history[-4:])
            prompt = (
                f"You are {sender.name}{', ' + sender.persona if sender.persona else ''}, "
                f"replying to {recipient.name} in an ongoing work email thread.\n"
                f"{self._ctx(sender)}\n"
                f"Thread so far:\n{convo}\n\n"
                f"{self._style()}\n"
                f"Write your reply: 2-5 sentences, plain text, responsive to what was said"
                f"{', and wrap the conversation up politely' if final else ', moving it forward with a concrete detail or question'}. "
                "No placeholders, brackets, links or attachments. Do not mention testing. "
                "Sign off with first name only. Output only the email body."
            )
            for _ in range(LLM_MAX_TRIES):
                try:
                    out = _strip(self._ask(prompt))
                except Exception as e:  # noqa: BLE001
                    self._llm_failed(e)
                    break
                self._fails = 0
                why = validate(out)
                if why:
                    self.last_error = f"output rejected: {why}"
                    log.warning("LLM reply rejected (%s), retrying", why)
                elif self._dup(None, out):
                    self.last_error = "output duplicates a recent email"
                    log.warning("LLM reply duplicates a recent email, retrying")
                else:
                    return out, "llm"
        if self.strict:
            self._fail("reply")
        tpls = list(CLOSERS if final else REPLIES)
        self.rng.shuffle(tpls)
        bodies = [t.format(r=recipient.first_name, s=sender.first_name) for t in tpls]
        for b in bodies:
            if not self._dup(None, b):
                return b, "template"
        self._pool_exhausted()
        return bodies[0], "template"

    def thread(self, opener, other, turns: int, topic: str | None = None) -> tuple[str, list[str]]:
        """A whole conversation in ONE LLM call: returns (subject, [body of message 1, 2, ... turns]).
        Messages alternate, starting with `opener`. Strict mode only (used to pre-generate trails): any
        failure raises ContentError, never a template."""
        pool = self.cfg.topics or DEFAULT_TOPICS
        if not self._llm_ready():
            self._fail("thread")
        topic = topic or self.rng.choice(pool)
        used: list[str] = []
        for s, _b in self.recent():
            if s and s not in used:
                used.append(s)
        avoid = ("Subjects already used, do not reuse or closely paraphrase any of them: "
                 + "; ".join(used[-40:]) + "\n") if used else ""
        prim = getattr(self.cfg.primary, "email", None)
        founder, peer = (opener, other) if opener.email == prim else (other, opener)

        def who(a) -> str:
            return f"{a.name}{' (' + a.persona + ')' if a.persona else ''}"
        order = " then ".join(f"message {i + 1} from {(opener if i % 2 == 0 else other).name}" for i in range(turns))
        prompt = (
            f"Write one complete work email conversation of exactly {turns} messages between two people.\n"
            f"{founder.name}{', ' + founder.persona if founder.persona else ''} speaks for this business: "
            f"{self.cfg.business_description}\n"
            f"{who(peer)} is a practitioner or customer contact of it, not an employee.\n"
            f"Order: {order}. {opener.name} writes first.\n"
            f"Topic: {topic}\n"
            f"Angle for this conversation: {self.rng.choice(ANGLES)}\n"
            f"{avoid}"
            f"{self._style()}\n"
            "Rules: the first message is 3-6 sentences with one clear ask; each reply is 2-5 sentences, responds to "
            "what was just said and moves it forward with a concrete detail; the last message wraps up politely. "
            "Plain text, natural and specific. No placeholders, brackets, links, attachments, emojis or sales "
            "language. Do not mention testing or warming up. Each message signs off with its own writer's first "
            "name only. The subject is short and specific.\n"
            'Output ONLY JSON, no code fences: {"subject": "<subject>", "messages": ["<message 1>", ...]} '
            f"with exactly {turns} strings in messages."
        )
        for _ in range(LLM_MAX_TRIES):
            try:
                out = self._ask(prompt)
            except Exception as e:  # noqa: BLE001
                self._llm_failed(e)
                break
            self._fails = 0
            try:
                raw = _strip(out)
                data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
                subject, bodies = str(data["subject"]).strip(), data["messages"]
                if not (isinstance(bodies, list) and len(bodies) == turns
                        and all(isinstance(b, str) for b in bodies)):
                    raise ValueError(f"expected {turns} messages")
            except (ValueError, KeyError, TypeError) as e:
                self.last_error = f"output rejected: bad JSON ({e})"
                log.warning("LLM thread was not the expected JSON (%s), retrying", e)
                continue
            bodies = [b.strip() for b in bodies]
            why = validate_subject(subject) or next((w for w in map(validate, bodies) if w), None)
            if why:
                self.last_error = f"output rejected: {why}"
                log.warning("LLM thread rejected (%s), retrying", why)
            elif self._dup(subject, bodies[0]) or any(self._dup(None, b) for b in bodies[1:]):
                self.last_error = "output duplicates a recent email"
                log.warning("LLM thread duplicates a recent email, retrying")
            else:
                return subject, bodies
        self._fail("thread")

    def expand_topics(self, n: int = 40) -> list[str]:
        """Ask the LLM for more conversation subjects so hundreds of emails do not circle the same dozen."""
        persona = getattr(self.cfg.primary, "persona", "") or "the founder"
        prompt = (
            f"Business: {self.cfg.business_description}\n"
            f"List {n} distinct, specific and realistic subjects for work emails between {persona} and "
            "small business owners or their staff who are prospects, new customers or partners. One per line, each "
            "under 100 characters, phrased like 'asking whether...', 'following up on...' or 'confirming...'. "
            "Cover scheduling, onboarding, setup, billing questions, privacy and security questions, "
            "feedback and team rollouts. No numbering, no quotes, no prices or discounts, nothing about "
            "testing or warming up. Output only the list.")
        seen = {t.lower() for t in self.cfg.topics}
        out: list[str] = []
        for ln in self._ask(prompt).splitlines():
            t = re.sub(r"^\s*(?:[-*\u2022]|\d+[.)])\s*", "", ln).strip().strip('"').strip()
            if (15 <= len(t) <= 110 and t.lower() not in seen
                    and not re.search(r"[\[\]{}<>]|https?://|\u20b9|\$|\brs\.?\s?\d", t, re.I)):
                seen.add(t.lower())
                out.append(t)
        return out

    # ---- internals
    def _ask(self, prompt: str) -> str:
        return self._ask_api(prompt) if self.cfg.llm_provider == "api" else self._ask_cli(prompt)

    def _ask_cli(self, prompt: str) -> str:
        """Headless Claude Code: `claude -p`. Uses the local Claude Code login, no API key."""
        cmd = [self.cfg.llm_command, "-p", prompt, "--output-format", "text"]
        if self.cfg.llm_model:
            cmd += ["--model", self.cfg.llm_model]
        # run in an empty temp dir so no project files or CLAUDE.md are picked up
        with tempfile.TemporaryDirectory() as d:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, cwd=d, stdin=subprocess.DEVNULL)
        msg = (r.stderr or r.stdout).strip()
        if r.returncode != 0:
            raise RuntimeError(msg[:200] or f"claude exited {r.returncode}")
        if "/login" in r.stdout or "Invalid API key" in r.stdout:  # expired login can exit 0
            raise RuntimeError("Claude Code login expired: " + r.stdout.strip()[:120])
        return r.stdout

    def _ask_api(self, prompt: str) -> str:
        key = os.environ.get(self.cfg.llm_api_key_env, "")
        model = API_MODELS.get(self.cfg.llm_model, self.cfg.llm_model)
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({"model": model, "max_tokens": 600,
                             "messages": [{"role": "user", "content": prompt}]}).encode(),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Anthropic API HTTP {e.code}: {e.read()[:200]!r}") from e
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def validate_subject(subject: str) -> str | None:
    if not 3 <= len(subject) <= 120:
        return "bad subject length"
    return validate(subject + " " * 40) if re.search(r"[\[\]{}<>]", subject) else None


def quote(prev_sender: str, prev_email: str, prev_date: str, prev_body: str) -> str:
    q = "\n".join("> " + ln for ln in prev_body.splitlines())
    return f"\n\nOn {prev_date}, {prev_sender} <{prev_email}> wrote:\n{q}"
