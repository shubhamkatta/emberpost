"""SMTPIMAPTransport against a fake Gmail.

The fake models the parts of Gmail's SMTP/IMAP behaviour the transport relies on: per-account
folders with a \\Junk-flagged spam folder, X-GM-RAW searches (rfc822msgid: and category:), X-GM-LABELS,
read-only vs read-write SELECT (STORE/EXPUNGE are refused read-only, like a real EXAMINE), COPY and
EXPUNGE. It proves our logic against that model, NOT against Google: see the README's note on
checking real accounts before trusting placement numbers."""
import email
import imaplib
import random
import smtplib
import sys
from contextlib import contextmanager
from email import policy
from email.utils import parseaddr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_sim import make, run  # noqa: E402

from warmup.config import Account  # noqa: E402
from warmup.content import Content  # noqa: E402
from warmup.db import DB  # noqa: E402
from warmup.engine import Engine  # noqa: E402
from warmup.transport import AuthError, SMTPIMAPTransport, build_message  # noqa: E402

SPAM = "[Gmail]/Spam"


# ============================================================ the fake
class World:
    def __init__(self, spam_folder=SPAM, tuple_labels=False):
        self.boxes: dict[str, dict] = {}
        self.spam_folder = spam_folder
        self.tuple_labels = tuple_labels      # FETCH replies as (b"...{n}", b"literal") like imaplib does for literals
        self.log: list[tuple] = []
        self.sent: list[email.message.EmailMessage] = []
        self.smtp_error: Exception | None = None
        self.copy_fails = False
        self.store_aborts = False
        self.to_spam = lambda sender, rcpt: False
        self.to_promo = lambda sender, rcpt: False
        self._uid = 100

    def add(self, address, password="pw"):
        self.boxes[address] = {"password": password, "folders": {"INBOX": [], self.spam_folder: [], "[Gmail]/All Mail": []}}

    def put(self, address, folder, mid, flags=(), labels=(), category=None):
        self._uid += 1
        m = {"uid": self._uid, "mid": mid, "flags": set(flags), "labels": set(labels), "category": category, "headers": {}}
        self.boxes[address]["folders"][folder].append(m)
        return m

    def folder(self, address, name):
        return self.boxes[address]["folders"][name]

    def list_lines(self):
        return [b'(\\HasNoChildren) "/" "INBOX"',
                b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
                b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"',
                f'(\\HasNoChildren \\Junk) "/" "{self.spam_folder}"'.encode()]

    def deliver(self, msg):
        sender, rcpt = parseaddr(msg["From"])[1], parseaddr(msg["To"])[1]
        folder = self.spam_folder if self.to_spam(sender, rcpt) else "INBOX"
        cat = "promotions" if folder == "INBOX" and self.to_promo(sender, rcpt) else None
        m = self.put(rcpt, folder, msg["Message-ID"], labels={"\\Inbox"} if folder == "INBOX" else set(), category=cat)
        m["headers"] = dict(msg.items())
        self.sent.append(msg)


class FakeIMAP:
    world: World

    def __init__(self, host, port=993, timeout=None):
        self.user = self.sel = self.readonly = None
        self.world.log.append(("connect", host))

    def login(self, user, pw):
        box = self.world.boxes.get(user)
        if box is None or box["password"] != pw:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials (Failure)")
        self.user = user
        return "OK", [b"logged in"]

    def list(self):
        return "OK", self.world.list_lines()

    def select(self, mailbox, readonly=False):
        name = mailbox.strip('"')
        folders = self.world.boxes[self.user]["folders"]
        if name not in folders:
            return "NO", [b"[NONEXISTENT] Unknown Mailbox"]
        self.sel, self.readonly = name, readonly
        self.world.log.append(("select", self.user, name, readonly))
        return "OK", [str(len(folders[name])).encode()]

    def _msgs(self):
        return self.world.boxes[self.user]["folders"][self.sel]

    def uid(self, cmd, *args):
        cmd = cmd.upper()
        self.world.log.append(("uid", cmd, args, self.sel))
        if cmd == "SEARCH":
            assert args[0] == "X-GM-RAW"
            terms = args[1].strip('"').split()
            mid = next(t[len("rfc822msgid:"):] for t in terms if t.startswith("rfc822msgid:"))
            cat = next((t[len("category:"):] for t in terms if t.startswith("category:")), None)
            hits = [m for m in self._msgs() if m["mid"].strip("<>") == mid and (cat is None or m["category"] == cat)]
            return "OK", [b" ".join(str(m["uid"]).encode() for m in hits)]
        target = next((m for m in self._msgs() if str(m["uid"]) == args[0]), None)
        if cmd == "FETCH":
            labels = " ".join(f'"{x}"' for x in sorted(target["labels"])) if target else ""
            if self.world.tuple_labels:
                lit = f"({labels})".encode()
                return "OK", [(f"1 (X-GM-LABELS {{{len(lit)}}}".encode(), lit), f" UID {args[0]})".encode()]
            return "OK", [f"1 (X-GM-LABELS ({labels}) UID {args[0]})".encode()]
        if self.world.store_aborts and cmd == "STORE":
            raise imaplib.IMAP4.abort("socket error: EOF")
        if cmd in ("STORE", "COPY") and (self.readonly and cmd == "STORE"):
            return "NO", [b"[READ-ONLY] Mailbox is read-only"]
        if target is None:
            return "NO", [b"no such uid"]
        if cmd == "STORE":
            items = set(args[2].strip("()").split())
            (target["flags"] if args[1].upper() == "+FLAGS" else target["labels"]).update(items)
            return "OK", [b"done"]
        if cmd == "COPY":
            if self.world.copy_fails:
                return "NO", [b"[OVERQUOTA] try later"]
            dest = args[1].strip('"')
            self.world._uid += 1
            self.world.boxes[self.user]["folders"][dest].append(
                {**target, "uid": self.world._uid, "flags": set(target["flags"]), "labels": {"\\Inbox"}})
            return "OK", [b"copied"]
        raise AssertionError(f"fake IMAP: unhandled {cmd}")

    def expunge(self):
        if self.readonly:
            return "NO", [b"[READ-ONLY]"]
        keep = [m for m in self._msgs() if "\\Deleted" not in m["flags"]]
        self.world.boxes[self.user]["folders"][self.sel][:] = keep
        return "OK", [b"expunged"]

    def logout(self):
        self.world.log.append(("logout", self.user))
        return "BYE", [b"bye"]


class FakeSMTP:
    world: World

    def __init__(self, host, port, timeout=None):
        self.world.log.append(("smtp_connect", host, port))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, pw):
        box = self.world.boxes.get(user)
        if box is None or box["password"] != pw:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
        self.world.log.append(("smtp_login", user))

    def send_message(self, msg):
        if self.world.smtp_error:
            raise self.world.smtp_error
        self.world.deliver(msg)


@contextmanager
def patched(obj, name, value):
    """Temporarily replace an attribute, restoring the ORIGINAL afterwards."""
    orig = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, orig)


def _raises(exc):
    def f(*a, **k):
        raise exc
    return f


@contextmanager
def gmail(world):
    FakeIMAP.world = FakeSMTP.world = world
    old = imaplib.IMAP4_SSL, smtplib.SMTP_SSL
    imaplib.IMAP4_SSL, smtplib.SMTP_SSL = FakeIMAP, FakeSMTP
    try:
        yield world
    finally:
        imaplib.IMAP4_SSL, smtplib.SMTP_SSL = old


def acct(address="me@example.com", name="Sam Lee", pw="pw"):
    return Account(address, name, password=pw)


def world_with(*addresses, **kw):
    w = World(**kw)
    for a in addresses:
        w.add(a)
    return w


MID = "<1700000000.1.abc@example.com>"


# ============================================================ build_message
def test_threading_headers_and_addresses():
    sender, to = acct(), acct("peer@gmail.com", "Walker, Chris")
    m = build_message(sender, to, "Re: Demo", "Hi", MID, "<prev@x>", ["<a@x>", "<prev@x>"])
    assert m["Message-ID"] == MID and m["In-Reply-To"] == "<prev@x>" and m["References"] == "<a@x> <prev@x>"
    first = build_message(sender, to, "Demo", "Hi", MID, None, [])
    assert first["In-Reply-To"] is None and first["References"] is None
    parsed = email.message_from_bytes(m.as_bytes(), policy=policy.default)
    assert parsed["To"].addresses[0].display_name == "Walker, Chris"      # comma survives quoting
    assert parsed["To"].addresses[0].addr_spec == "peer@gmail.com"
    assert parsed["From"].addresses[0].addr_spec == "me@example.com"


def test_non_ascii_body_and_subject_round_trip():
    body = "Hi Jamie,\n\nThe Practice plan is $29/mo, and it’s billed yearly with two months free.\n\nSam"
    m = build_message(acct(), acct("p@gmail.com", "Jamie Rivera"), "Plans – $ pricing", body, MID, None, [])
    parsed = email.message_from_bytes(m.as_bytes(), policy=policy.default)
    assert parsed["Subject"] == "Plans – $ pricing"
    assert parsed.get_body().get_content().rstrip("\n") == body
    assert parsed.get_content_charset() == "utf-8"


def test_header_injection_is_refused():
    try:
        build_message(acct(), acct("p@gmail.com"), "Hi\r\nBcc: evil@x.com", "body", MID, None, [])
    except ValueError:
        return
    raise AssertionError("a newline in the subject must not reach the headers")


# ============================================================ SMTP
def test_send_logs_in_as_sender_and_delivers():
    w = world_with("me@example.com", "p@gmail.com")
    with gmail(w):
        SMTPIMAPTransport().send(acct(), acct("p@gmail.com", "Jamie"), "Demo", "Hello there", MID, None, [])
    assert ("smtp_connect", "smtp.gmail.com", 465) in w.log and ("smtp_login", "me@example.com") in w.log
    assert len(w.folder("p@gmail.com", "INBOX")) == 1


def test_smtp_rejected_login_is_auth_error_without_leaking_password():
    w = world_with("me@example.com", "p@gmail.com")
    with gmail(w):
        try:
            SMTPIMAPTransport().send(acct(pw="WRONG-secret-pw"), acct("p@gmail.com"), "s", "b", MID, None, [])
        except AuthError as e:
            assert "me@example.com" in str(e) and "WRONG-secret-pw" not in str(e)
            return
    raise AssertionError("expected AuthError")


def test_disconnect_during_smtp_login_is_treated_as_auth_error():
    w = world_with("me@example.com", "p@gmail.com")
    with gmail(w), patched(FakeSMTP, "login", _raises(smtplib.SMTPServerDisconnected("Connection unexpectedly closed"))):
        try:
            SMTPIMAPTransport().send(acct(), acct("p@gmail.com"), "s", "b", MID, None, [])
        except AuthError as e:
            assert "me@example.com" in str(e) and "closed during login" in str(e)
            return
    raise AssertionError("expected AuthError")


def test_check_reports_both_smtp_and_imap_when_both_fail():
    w = world_with("p@gmail.com")
    with gmail(w):
        out = SMTPIMAPTransport().check(acct("p@gmail.com", pw="bad"))
    assert out.startswith("SMTP:") and "; IMAP:" in out and "AUTHENTICATIONFAILED" in out, out
    with gmail(w), patched(FakeSMTP, "login", _raises(smtplib.SMTPServerDisconnected("Connection unexpectedly closed"))):
        out = SMTPIMAPTransport().check(acct("p@gmail.com"))        # IMAP fine, SMTP dropped
    assert out.startswith("SMTP:") and "IMAP" not in out


def test_other_smtp_failures_are_not_auth_errors():
    w = world_with("me@example.com", "p@gmail.com")
    for err in (smtplib.SMTPServerDisconnected("gone"), smtplib.SMTPRecipientsRefused({}),
                smtplib.SMTPDataError(421, b"4.7.0 try later"), TimeoutError("slow")):
        w.smtp_error = err
        with gmail(w):
            try:
                SMTPIMAPTransport().send(acct(), acct("p@gmail.com"), "s", "b", MID, None, [])
            except AuthError:
                raise AssertionError(f"{type(err).__name__} must not disable the account") from None
            except Exception as e:  # noqa: BLE001
                assert e is err  # propagates, so the engine's normal retry path handles it


# ============================================================ IMAP helpers
def test_imap_rejected_login_is_auth_error():
    w = world_with("p@gmail.com")
    with gmail(w):
        try:
            SMTPIMAPTransport().locate(acct("p@gmail.com", pw="WRONG-secret-pw"), MID)
        except AuthError as e:
            assert "p@gmail.com" in str(e) and "WRONG-secret-pw" not in str(e)
            return
    raise AssertionError("expected AuthError")


def _spam_name(lines):
    class C:
        def list(self):
            return "OK", lines
    return SMTPIMAPTransport._spam_folder(C())


def test_spam_folder_found_by_junk_flag_not_by_name():
    assert _spam_name([b'(\\HasNoChildren \\Junk) "/" "[Google Mail]/Spam"']) == "[Google Mail]/Spam"
    assert _spam_name([b'(\\HasChildren \\Noselect) "/" "[Gmail]"',
                       b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"']) == "[Gmail]/Spam"
    assert _spam_name([b'(\\Junk) "/" Spam']) == "Spam"                       # unquoted name
    assert _spam_name([b'(\\HasNoChildren) "/" "INBOX"']) == "[Gmail]/Spam"    # nothing flagged: documented fallback
    assert _spam_name([]) == "[Gmail]/Spam"


def test_spam_folder_tolerates_odd_list_replies():
    class C:
        def list(self):
            return "OK", None
    assert SMTPIMAPTransport._spam_folder(C()) == "[Gmail]/Spam"
    class D:
        def list(self):
            return "OK", [None, b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"']
    try:
        assert SMTPIMAPTransport._spam_folder(D()) == "[Gmail]/Spam"
    except AttributeError:
        raise AssertionError("a None entry in LIST output must not crash") from None


def test_find_query_selection_and_duplicates():
    w = world_with("p@gmail.com")
    w.put("p@gmail.com", "INBOX", MID)
    dup = w.put("p@gmail.com", "INBOX", MID)           # same Message-ID twice
    w.put("p@gmail.com", "INBOX", "<other@x>")
    with gmail(w):
        c = FakeIMAP("imap.gmail.com")
        c.login("p@gmail.com", "pw")
        assert SMTPIMAPTransport._find(c, "INBOX", MID) == str(dup["uid"])      # last UID wins
        assert SMTPIMAPTransport._find(c, "INBOX", "<nope@x>") is None
        assert SMTPIMAPTransport._find(c, "No Such Folder", MID) is None        # select NO: no crash
    searches = [e for e in w.log if e[:2] == ("uid", "SEARCH")]
    assert searches[0][2] == ("X-GM-RAW", '"rfc822msgid:1700000000.1.abc@example.com"')  # brackets stripped, quoted
    selects = [e for e in w.log if e[0] == "select"]
    assert all(e[3] is True for e in selects), "lookups must select read-only"


# ============================================================ locate
def test_locate_inbox_spam_missing():
    w = world_with("p@gmail.com")
    w.put("p@gmail.com", "INBOX", "<in@x>")
    w.put("p@gmail.com", SPAM, "<sp@x>")
    t = SMTPIMAPTransport()
    with gmail(w):
        assert t.locate(acct("p@gmail.com"), "<in@x>") == "inbox"
        assert t.locate(acct("p@gmail.com"), "<sp@x>") == "spam"
        assert t.locate(acct("p@gmail.com"), "<gone@x>") is None


def test_locate_promotions_by_label_or_by_category_search():
    w = world_with("p@gmail.com")
    w.put("p@gmail.com", "INBOX", "<lab@x>", labels={"\\Inbox", "Promotions"})
    w.put("p@gmail.com", "INBOX", "<cat@x>", labels={"\\Inbox"}, category="promotions")  # tab not in labels
    w.put("p@gmail.com", "INBOX", "<plain@x>", labels={"\\Inbox"})
    t = SMTPIMAPTransport()
    with gmail(w):
        assert t.locate(acct("p@gmail.com"), "<lab@x>") == "promotions"
        assert t.locate(acct("p@gmail.com"), "<cat@x>") == "promotions"
        assert t.locate(acct("p@gmail.com"), "<plain@x>") == "inbox"


def test_locate_handles_literal_label_replies():
    w = world_with("p@gmail.com", tuple_labels=True)
    w.put("p@gmail.com", "INBOX", "<lab@x>", labels={"\\Inbox", "Promotions"})
    w.put("p@gmail.com", "INBOX", "<plain@x>", labels={"\\Inbox"})
    t = SMTPIMAPTransport()
    with gmail(w):
        assert t.locate(acct("p@gmail.com"), "<lab@x>") == "promotions"
        assert t.locate(acct("p@gmail.com"), "<plain@x>") == "inbox"


def test_locate_is_read_only_and_always_logs_out():
    w = world_with("p@gmail.com")
    m = w.put("p@gmail.com", "INBOX", "<in@x>", labels={"\\Inbox"})
    t = SMTPIMAPTransport()
    with gmail(w):
        t.locate(acct("p@gmail.com"), "<in@x>")
        assert m["flags"] == set() and m["labels"] == {"\\Inbox"}, "locating must not mark mail read"
        before = sum(1 for e in w.log if e[0] == "logout")
        with patched(FakeIMAP, "uid", _raises(imaplib.IMAP4.abort("EOF"))):
            try:
                t.locate(acct("p@gmail.com"), "<in@x>")
            except imaplib.IMAP4.abort:
                pass
        assert sum(1 for e in w.log if e[0] == "logout") == before + 1, "connection leaked on error"


# ============================================================ engage
def test_engage_marks_read_important_and_optionally_starred():
    w = world_with("p@gmail.com")
    a = w.put("p@gmail.com", "INBOX", "<a@x>", labels={"\\Inbox"})
    b = w.put("p@gmail.com", "INBOX", "<b@x>", labels={"\\Inbox"})
    other = w.put("p@gmail.com", "INBOX", "<other@x>", labels={"\\Inbox"})
    t = SMTPIMAPTransport()
    with gmail(w):
        t.engage(acct("p@gmail.com"), "<a@x>", "inbox", star=False)
        t.engage(acct("p@gmail.com"), "<b@x>", "inbox", star=True)
    assert a["flags"] == {"\\Seen"} and "\\Important" in a["labels"]
    assert b["flags"] == {"\\Seen", "\\Flagged"}
    assert other["flags"] == set() and "\\Important" not in other["labels"], "untouched mail must stay untouched"


def test_engage_rescues_spam_to_inbox_and_empties_spam():
    w = world_with("p@gmail.com", spam_folder="[Google Mail]/Spam")      # non-default folder name
    w.put("p@gmail.com", "[Google Mail]/Spam", "<s@x>")
    t = SMTPIMAPTransport()
    with gmail(w):
        t.engage(acct("p@gmail.com"), "<s@x>", "spam", star=True)
    assert w.folder("p@gmail.com", "[Google Mail]/Spam") == []
    inbox = w.folder("p@gmail.com", "INBOX")
    assert len(inbox) == 1 and inbox[0]["mid"] == "<s@x>"
    assert {"\\Seen", "\\Flagged"} <= inbox[0]["flags"] and "\\Important" in inbox[0]["labels"]
    assert "\\Deleted" not in inbox[0]["flags"]


def test_engage_never_deletes_from_spam_if_the_copy_failed():
    w = world_with("p@gmail.com")
    w.put("p@gmail.com", SPAM, "<s@x>")
    w.copy_fails = True
    t = SMTPIMAPTransport()
    with gmail(w):
        try:
            t.engage(acct("p@gmail.com"), "<s@x>", "spam", star=False)
            raise AssertionError("a failed rescue must be reported, not swallowed")
        except RuntimeError as e:
            assert "could not copy" in str(e)
    assert len(w.folder("p@gmail.com", SPAM)) == 1, "COPY failed: the only copy of the mail was deleted"
    assert w.folder("p@gmail.com", "INBOX") == []


def test_engage_when_message_already_moved_is_a_noop():
    w = world_with("p@gmail.com")
    t = SMTPIMAPTransport()
    with gmail(w):
        t.engage(acct("p@gmail.com"), "<gone@x>", "spam", star=True)
        t.engage(acct("p@gmail.com"), "<gone@x>", "inbox", star=True)
    assert all(not w.folder("p@gmail.com", f) for f in ("INBOX", SPAM))


def test_engage_logs_out_and_raises_when_the_connection_drops():
    w = world_with("p@gmail.com")
    w.put("p@gmail.com", "INBOX", "<a@x>")
    w.store_aborts = True
    t = SMTPIMAPTransport()
    with gmail(w):
        try:
            t.engage(acct("p@gmail.com"), "<a@x>", "inbox", star=False)
            raise AssertionError("should raise")
        except imaplib.IMAP4.abort:
            pass
    assert ("logout", "p@gmail.com") in w.log


# ============================================================ check
def test_check_reports_smtp_then_imap_problems():
    t = SMTPIMAPTransport()
    w = world_with("p@gmail.com")
    with gmail(w):
        assert t.check(acct("p@gmail.com")) == ""
        assert t.check(acct("p@gmail.com", pw="bad")).startswith("SMTP:")
        assert not [m for m in w.sent], "check must not send mail"
        with patched(FakeIMAP, "login", _raises(imaplib.IMAP4.error("[AUTHENTICATIONFAILED]"))):
            assert t.check(acct("p@gmail.com")).startswith("IMAP:")
        with patched(smtplib, "SMTP_SSL", _raises(OSError("network unreachable"))):
            assert "network unreachable" in t.check(acct("p@gmail.com"))      # returned, not raised


# ============================================================ engine + transport together
def test_full_warmup_over_the_fake_gmail():
    cfg, _, _, st = make(peers=4)
    everyone = [cfg.primary, *cfg.peers]
    for a in everyone:
        a.password = "pw"
    w = world_with(*[a.email for a in everyone])
    w.to_spam = lambda s, r: r == "p1@gmail.com"          # one peer's filter hates us
    w.to_promo = lambda s, r: r == "p2@gmail.com"         # another files us under Promotions
    rng = random.Random(7)
    with gmail(w):
        eng = Engine(cfg, DB(":memory:"), SMTPIMAPTransport(), Content(cfg, rng), rng, clock=lambda: st["t"])
        run(eng, st, 8)

    rows = eng.db.q("select * from messages")
    assert len(rows) >= 20 and len(w.sent) == len(rows)
    placements = {r["placement"] for r in rows if r["arrived_at"]}
    assert {"inbox", "spam", "promotions"} <= placements, placements
    assert "missing" not in placements

    processed = {r["message_id"] for r in rows if r["arrived_at"]}
    pending = {r["message_id"] for r in rows if not r["arrived_at"]}
    for a in everyone:
        # nothing that was handled is left in spam; whatever remains there is not yet polled
        assert {m["mid"] for m in w.folder(a.email, SPAM)} <= pending, a.email
        for m in w.folder(a.email, "INBOX"):
            if m["mid"] in processed:
                assert "\\Seen" in m["flags"] and "\\Important" in m["labels"], (a.email, m["mid"])
    # the spam-filtered peer really got its mail back in the inbox
    assert any(m["mid"] in processed for m in w.folder("p1@gmail.com", "INBOX"))
    assert any(r["placement"] == "spam" for r in rows)

    # replies are real replies: In-Reply-To points at a message the sender actually received
    by_mid = {m["Message-ID"]: m for m in w.sent}
    replies = [m for m in w.sent if m["In-Reply-To"]]
    assert replies
    for m in replies:
        parent = by_mid[m["In-Reply-To"]]
        assert parseaddr(parent["To"])[1] == parseaddr(m["From"])[1], "reply parent must have been addressed to the replier"
        assert m["References"].split()[-1] == m["In-Reply-To"]
        assert m["Subject"].lower().startswith("re:")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
