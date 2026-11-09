"""Mail transports. SMTPIMAPTransport talks to real Gmail/Workspace. DryRunTransport simulates."""
from __future__ import annotations

import imaplib
import logging
import random
import re
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, formatdate

from .config import Account

log = logging.getLogger("warmup.transport")


class AuthError(Exception):
    """Login was rejected (bad/revoked app password, IMAP disabled, account locked)."""


def _smtp_login(s: smtplib.SMTP, a: Account) -> None:
    try:
        s.login(a.email, a.password)
    except smtplib.SMTPAuthenticationError as e:
        raise AuthError(f"SMTP login rejected for {a.email}: {e.smtp_code} {e.smtp_error!r}") from e
    except smtplib.SMTPServerDisconnected as e:
        # Google sometimes drops the connection during AUTH instead of answering 535 (seen with a
        # rejected app password). The TLS session and EHLO already worked, so treat it as a login problem.
        raise AuthError(f"SMTP connection closed during login for {a.email} "
                        f"(usually a rejected app password): {e}") from e


def build_message(sender: Account, to: Account, subject: str, body: str, message_id: str,
                  in_reply_to: str | None, references: list[str]) -> EmailMessage:
    m = EmailMessage()
    m["From"] = formataddr((sender.name, sender.email))
    m["To"] = formataddr((to.name, to.email))
    m["Subject"] = subject
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = message_id
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    if references:
        m["References"] = " ".join(references)
    m.set_content(body)
    return m


def _flatten(data) -> str:
    """imaplib returns bytes for simple replies but (header, literal) tuples when the server sends a
    literal. Join either form into one string."""
    parts: list[bytes] = []
    for item in data or []:
        if isinstance(item, tuple):
            parts.extend(p for p in item if isinstance(p, bytes))
        elif isinstance(item, bytes):
            parts.append(item)
    return b" ".join(parts).decode(errors="ignore")


class Transport:
    def send(self, sender, to, subject, body, message_id, in_reply_to, references): ...
    def locate(self, recipient: Account, message_id: str) -> str | None:
        """Return 'inbox' | 'promotions' | 'spam' or None if not found yet."""
    def engage(self, recipient: Account, message_id: str, placement: str, star: bool) -> None: ...
    def check(self, account: Account) -> str:
        """Return '' if OK else an error string."""
        return ""


class SMTPIMAPTransport(Transport):
    # -- sending
    def send(self, sender, to, subject, body, message_id, in_reply_to, references):
        msg = build_message(sender, to, subject, body, message_id, in_reply_to, references)
        with smtplib.SMTP_SSL(sender.smtp_host, sender.smtp_port, timeout=30) as s:
            _smtp_login(s, sender)
            s.send_message(msg)

    # -- imap helpers
    def _imap(self, a: Account) -> imaplib.IMAP4_SSL:
        c = imaplib.IMAP4_SSL(a.imap_host, timeout=30)
        try:
            c.login(a.email, a.password)
        except imaplib.IMAP4.error as e:
            raise AuthError(f"IMAP login rejected for {a.email}: {e}") from e
        return c

    @staticmethod
    def _spam_folder(c: imaplib.IMAP4_SSL) -> str:
        typ, data = c.list()
        for line in data or []:
            if not isinstance(line, bytes):
                continue
            s = line.decode(errors="ignore")
            if "\\Junk" in s:
                m = re.search(r'"/" (.+)$', s)
                if m:
                    return m.group(1).strip().strip('"')
        return "[Gmail]/Spam"

    @staticmethod
    def _find(c: imaplib.IMAP4_SSL, folder: str, mid: str, readonly=True) -> str | None:
        typ, _ = c.select(f'"{folder}"', readonly=readonly)
        if typ != "OK":
            return None
        raw = mid.strip("<>")
        typ, data = c.uid("SEARCH", "X-GM-RAW", f'"rfc822msgid:{raw}"')
        if typ != "OK" or not data or not data[0]:
            return None
        return data[0].split()[-1].decode()

    def locate(self, recipient, message_id):
        c = self._imap(recipient)
        try:
            uid = self._find(c, "INBOX", message_id)
            if uid:
                return "promotions" if self._is_promotions(c, uid, message_id) else "inbox"
            if self._find(c, self._spam_folder(c), message_id):
                return "spam"
            return None
        finally:
            try:
                c.logout()
            except Exception:
                pass

    @staticmethod
    def _is_promotions(c: imaplib.IMAP4_SSL, uid: str, mid: str) -> bool:
        """INBOX is selected. Gmail's tabs are not reliably reported in X-GM-LABELS, so also ask Gmail's own
        search: X-GM-RAW accepts the `category:` operator."""
        typ, data = c.uid("FETCH", uid, "(X-GM-LABELS)")
        if typ == "OK" and "promotions" in _flatten(data).lower():
            return True
        typ, data = c.uid("SEARCH", "X-GM-RAW", f'"category:promotions rfc822msgid:{mid.strip("<>")}"')
        return typ == "OK" and bool(data and data[0])

    def engage(self, recipient, message_id, placement, star):
        c = self._imap(recipient)
        try:
            if placement == "spam":
                spam = self._spam_folder(c)
                uid = self._find(c, spam, message_id, readonly=False)
                if uid:
                    typ, resp = c.uid("COPY", uid, "INBOX")  # copy back to inbox = "not spam"
                    if typ != "OK":
                        # deleting now would destroy the only copy
                        raise RuntimeError(f"could not copy {message_id} back to INBOX: {typ} {resp}")
                    c.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
                    c.expunge()
            uid = self._find(c, "INBOX", message_id, readonly=False)
            if uid:
                flags = r"(\Seen \Flagged)" if star else r"(\Seen)"
                c.uid("STORE", uid, "+FLAGS", flags)
                c.uid("STORE", uid, "+X-GM-LABELS", r"(\Important)")
        finally:
            try:
                c.logout()
            except Exception:
                pass

    def check(self, account):
        """'' if both SMTP and IMAP log in, else every problem found (SMTP and IMAP are tried
        independently: IMAP often gives the clearer error)."""
        errs = []
        try:
            with smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, timeout=20) as s:
                _smtp_login(s, account)
        except Exception as e:  # noqa: BLE001
            errs.append(f"SMTP: {e}")
        try:
            c = self._imap(account)
            c.logout()
        except Exception as e:  # noqa: BLE001
            errs.append(f"IMAP: {e}")
        return "; ".join(errs)


class DryRunTransport(Transport):
    """No network. Delivers everything instantly; spam_rate simulates filtering."""

    def __init__(self, spam_rate: float = 0.0, seed: int = 1):
        self.rng = random.Random(seed)
        self.spam_rate = spam_rate
        self.sent: list[dict] = []
        self.placement: dict[str, str] = {}
        self.engaged: list[tuple[str, str, bool]] = []

    def send(self, sender, to, subject, body, message_id, in_reply_to, references):
        self.sent.append(dict(frm=sender.email, to=to.email, subject=subject, body=body,
                              mid=message_id, irt=in_reply_to))
        self.placement[message_id] = "spam" if self.rng.random() < self.spam_rate else "inbox"
        log.info("[dry] %s -> %s | %s", sender.email, to.email, subject)

    def locate(self, recipient, message_id):
        return self.placement.get(message_id)

    @staticmethod
    def _is_promotions(c: imaplib.IMAP4_SSL, uid: str, mid: str) -> bool:
        """INBOX is selected. Gmail's tabs are not reliably reported in X-GM-LABELS, so also ask Gmail's own
        search: X-GM-RAW accepts the `category:` operator."""
        typ, data = c.uid("FETCH", uid, "(X-GM-LABELS)")
        if typ == "OK" and "promotions" in _flatten(data).lower():
            return True
        typ, data = c.uid("SEARCH", "X-GM-RAW", f'"category:promotions rfc822msgid:{mid.strip("<>")}"')
        return typ == "OK" and bool(data and data[0])

    def engage(self, recipient, message_id, placement, star):
        self.engaged.append((message_id, placement, star))
