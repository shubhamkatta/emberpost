# emberpost

> Warm up a new sending mailbox with realistic, threaded conversations.

Warms up one Google Workspace mailbox by holding threaded business conversations with a configurable pool of Gmail accounts you own.

## What it does

- Primary account opens threads to peers, peers open threads to primary. Threads run 2 to 5 messages with delays of 8 minutes to 4 hours, only inside your send window, weekdays only by default.
- Replies are real replies: `In-Reply-To`, `References`, quoted text, `Re:` subjects.
- Every message is located in the recipient mailbox (Inbox, Promotions or Spam). If it landed in Spam it is moved back to Inbox. Received mail is marked read, sometimes starred, marked Important.
- Volume ramps from `start_per_day` to `max_per_day` over `days_to_max` sending days (weekdays count, weekends don't). The ramp pauses while the 3-day spam rate is above `hold_if_spam_rate_above`.
- Content comes from Claude (`llm.provider: cli` uses headless Claude Code and your login; `api` uses an Anthropic API key, the better choice on a server) with a template fallback. Every LLM email is validated (no placeholders, links, emojis or meta text) and compared with recent mail; bad or repeated output is regenerated, then falls back to a template. After 3 hard LLM failures in a row (expired login, no credit) the LLM is paused for 30 minutes and an ERROR is logged. `status` shows the LLM/template split. Templates repeat, so use the LLM for a month-long run.
- State lives in SQLite (`warmup.db`, WAL mode). Restarting is safe.

## Reliability

- **No double sends.** Each email is composed once and saved with its Message-ID before SMTP is touched, and the sent message and job are closed in one transaction. If the process dies mid-send, the next tick asks the recipient mailbox whether it arrived: if yes it is just recorded, if no it is resent under the same Message-ID, so a rare double delivery is collapsed by Gmail.
- **One process at a time.** `tick` and `run` take a lock on `warmup.db.lock`. A second `tick` (cron overlap) skips quietly, a second `run` exits with an error.
- **Login failures are handled, not guessed from error text.** A rejected login pauses that account (persisted in the DB, survives restarts), keeps its jobs and attempt counts, and re-checks the login every `broken_retry_hours`. Paused accounts show as `BROKEN` in `status`.

## Pre-generated conversations (trails)

Instead of asking the LLM at send time, you can write the conversations ahead of time and let the engine send them.

```
python -m warmup generate --count 500    # keep at least 500 UNSENT emails in trails.jsonl
python -m warmup trails                  # stock, per-peer split, what has been used
python -m warmup trails --show 3         # read the next 3 conversations
```

- **The file** (`trails.file`, default `trails.jsonl`) is plain text, one whole conversation per line: opener, recipient, subject, and every message in order. It is append-only. You can open it, read it, delete a line you dislike, or add your own. Each line is validated on load (alternating senders, no placeholders, links or emojis); bad lines are skipped with a warning, never sent.
- **`generate --count N` tops up to N unsent emails**, so it is safe to re-run, and it resumes after an interruption: conversations are written one at a time, so a crash leaves only whole, valid ones. It uses the LLM only (it refuses to write templates), spreads conversations evenly across your peers, mixes who opens, asks the LLM for extra topics so 500 emails do not circle the same dozen, and checks every email against the earlier ones. If the LLM stays unavailable for 30 minutes it stops cleanly (exit code 3).
- **State lives in the database** (table `trail_state`), not in the file: each conversation is `reserved` when a day is planned, `active` after its first email, `done` after its last, or `failed` if its opener could not be sent. The last turn sent is recorded in the same transaction as the send, so a crash cannot desynchronise them. `status` shows the stock.
- **The engine** picks the next unused conversation for each opening it plans (in file order, per direction) and sends each turn's pre-written text when that turn is due. Replies still depend on `reply_rate`, so a conversation can stop early. Conversations whose accounts are no longer configured are ignored.
- **When the file runs out**, `trails.fallback: live` writes emails on the fly as before; `stop` sends nothing new (conversations already started still finish). You get an alert when stock drops below `trails.low_stock_alert`.
- Sign-offs use the first names configured at generation time. If you rename an account afterwards, regenerate or edit its lines.

## Alerting and monitoring

Set these in `.env` (the URLs are secrets, so they are never read from `config.yaml`) and test with `python -m warmup alert-test`:

- `ALERT_WEBHOOK_URL`: a Slack or Discord incoming webhook, or any URL that accepts JSON. You are alerted when an account is disabled (and when it recovers), the ramp goes on hold for spam, a job is dropped after 4 failures, the LLM is paused, or 3 ticks in a row crash. Each kind of alert is throttled (`alerts.min_interval_hours`, default 6) and the throttle survives restarts. A failed delivery is retried on the next event, and the URL is never logged.
- `HEARTBEAT_URL`: a dead-man's-switch (healthchecks.io, Better Stack, Uptime Kuma push). It is pinged only after a tick succeeds, so when the process dies, the machine sleeps or every tick fails, the pings stop and the monitor alerts you. Nothing running inside the process can report its own death.
- `python -m warmup health` exits non-zero if the loop stopped ticking or nothing has succeeded for hours (used by the Docker `HEALTHCHECK`).

## Config validation

Every command validates `config.yaml` and reports all problems at once. Unknown keys are errors with a suggestion (`max_per_dya` gives "did you mean 'max_per_day'?") because a typo would otherwise silently use the default. It also checks the timezone, send window, ranges and types, `peers.count` against the accounts listed, duplicate emails or password variables, missing secrets, and port 465. Warnings (not errors) cover things like fewer than 3 peers, over 50 sends a day, a ramp under 14 days, the LLM being off, or no alert webhook set.

## Commands

| Command | What it does |
|---|---|
| `validate` | Check the config only. |
| `check` | Log in to SMTP and IMAP for every account. |
| `tick` | Run one cycle (for cron). Skips quietly if another process holds the lock. |
| `run` | Loop forever. |
| `status` | Ramp day, cap, placement rates, content source, broken accounts. |
| `health` | Exit non-zero if the loop is stalled or failing. |
| `generate --count N` | Write conversations ahead of time until N unsent emails are in the trails file. |
| `trails [--show N]` | Trails stock, usage and a preview of the next conversations. |
| `backup --to DIR --keep N` | Verified online copy of the database. |
| `alert-test` | Send a test alert and heartbeat. |

Common flags: `-c config.yaml`, `--dry-run` (no network, no secrets needed), `-v`.

## Setup

1. Prerequisites on the primary domain, before sending anything: SPF, DKIM (enable in Workspace admin, then publish the key), DMARC (`p=none` to start). Warmup does not fix missing authentication.
2. On every Gmail account and the primary: enable 2-Step Verification, create an App Password, enable IMAP. For Workspace, an admin may need to allow app passwords/IMAP.
3. Copy `config.example.yaml` to `config.yaml` and `.env.example` to `.env`. Fill them in. `peers.count` picks how many of the listed accounts are active.
4. `pip install -r requirements.txt`
5. `python -m warmup check` verifies SMTP and IMAP login for every account.
6. `python -m warmup run` loops forever. Or run `python -m warmup tick` from cron every 5 minutes.
7. `python -m warmup status` shows ramp day, cap, placement rates. `validate` checks the config without sending anything.

`--dry-run` runs the whole engine with no network, but it still writes to the configured database (messages, jobs, trail progress), so point `database:` at a throwaway file when you try it. Tests: `pytest` runs everything. `test_sim` simulates 40 days, `test_robust` covers crash recovery, locking, broken accounts and content validation, `test_config` covers config validation and alerting, `test_ops` covers health and backup, and `test_imap` runs the Gmail transport against a fake Gmail, and `test_trails` covers generation, the file format and how the engine consumes it.

## Deploying

The `claude` CLI login does not exist on a server or in a container, so set `llm.provider: api` and `ANTHROPIC_API_KEY` for either route. Before going live: `python -m warmup validate`, `python -m warmup check`, and `python -m warmup alert-test`.

### Docker

```
cp config.example.yaml config.yaml    # set database: /data/warmup.db and llm.provider: api
cp .env.example .env                  # app passwords, ANTHROPIC_API_KEY, ALERT_WEBHOOK_URL, HEARTBEAT_URL
docker compose up -d --build
docker compose logs -f
docker compose exec emberpost python -m warmup status -c /config/config.yaml
```

Runs as an unprivileged user with a read-only filesystem and all capabilities dropped. The database is on the `emberpost-data` volume. The image's `HEALTHCHECK` runs `warmup health`, which fails if the loop stopped ticking (30 min) or no tick has succeeded for 3 hours.

### systemd (VPS)

```
sudo useradd --system --home /opt/emberpost --shell /usr/sbin/nologin emberpost
sudo mkdir -p /opt/emberpost /etc/emberpost
sudo cp -r warmup requirements.txt /opt/emberpost/
sudo python3 -m venv /opt/emberpost/venv && sudo /opt/emberpost/venv/bin/pip install -r /opt/emberpost/requirements.txt
sudo cp config.yaml /etc/emberpost/config.yaml      # set database: /var/lib/emberpost/warmup.db
sudo cp .env /etc/emberpost/env                     # KEY=value lines, used as the unit's EnvironmentFile
sudo chown root:emberpost /etc/emberpost/* && sudo chmod 640 /etc/emberpost/*
sudo cp deploy/emberpost*.service deploy/emberpost-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now emberpost emberpost-backup.timer
journalctl -u emberpost -f
```

The unit restarts on failure and is sandboxed (`ProtectSystem=strict`, only `/var/lib/emberpost` writable). Logs go to journald, which handles rotation.

### Operations

- `python -m warmup backup --to DIR --keep 14` takes a verified online copy of the database (safe while `run` is going). The systemd timer does this daily into `/var/lib/emberpost/backups`. Those sit on the same disk, so copy them off the machine too.
- Restoring: stop the service, replace `warmup.db` with a backup, start it. In-flight sends are reconciled on the next tick.
- Stopping with SIGTERM or `docker stop` is safe even mid-send (see Reliability).
- CI (`.github/workflows/ci.yml`) runs lint and the tests on Python 3.10 to 3.13, then builds the image and validates the example config inside it. Dependencies are pinned in `requirements.txt`; Dependabot proposes updates weekly.
- Development: `pip install -r requirements.txt -r requirements-dev.txt`, then `pytest` and `ruff check .`.

## Limits you should know

- Secrets are app passwords over SMTP/IMAP. OAuth would be cleaner and is the next step if Google restricts app passwords for your org.
- A tight pool of Gmail accounts talking only to your domain is a pattern Google can detect. More peers, different personas, and moderate volume make it look less uniform. 5 to 10 peers is a good range. Keep `max_per_day` modest (30 to 50).
- Promotions-tab placement is recorded but not auto-fixed. It is detected from `X-GM-LABELS` and from Gmail's `category:promotions` search.
- The IMAP code is unit-tested against a fake Gmail (`tests/test_imap.py`), which proves the logic against a model of Gmail, not Gmail itself. Before trusting placement numbers, send a few messages between real accounts and confirm that `status` agrees with what you see in the inboxes, especially Spam and Promotions.
- Warmup helps reputation only if your real sending is also clean: verified lists, low bounce rate, easy unsubscribe, spam complaints under 0.1%. Keep a trickle of warmup running after you start real campaigns.
- Gmail may flag the Gmail peers if they only talk to one address. Add real use of those inboxes if you can.
