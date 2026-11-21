# Security Policy

## Supported versions

emberpost is at v0.x. There is a single supported line, **`main`**. For production, pin to a specific commit SHA.

| Version | Supported |
| ------- | --------- |
| `main`  | ✅ |
| < v1.0  | Best-effort |

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Use **GitHub private vulnerability reporting**:
<https://github.com/shubhamkatta/emberpost/security/advisories/new>

It is encrypted in transit, auditable, and lets us coordinate a fix and a CVE privately.

Include, if you can:

- A description of the issue and the impact you observed.
- Steps to reproduce, ideally a minimal config or script.
- Suspected severity (low / medium / high / critical).
- Whether you have disclosed it elsewhere.

**Never include real credentials** (app passwords, API keys, webhook URLs) in a report. Redact them.

## What to expect

- **Acknowledgement within 72 hours.**
- Triage and initial assessment within 7 days.
- Coordinated disclosure: a fix lands in `main`, an advisory is published, and reporters are credited, or kept anonymous if preferred.
- Target patch window: 30 days for high and critical, 90 days for medium and low.

## Scope

**In scope:**

- The application (`warmup/`): credential handling, config parsing, SQLite state, locking, the trails file parser, webhook and heartbeat delivery.
- Leaks of secrets in logs, error messages, alerts or the database.
- Header and content injection into outgoing mail (for example CRLF in subjects).
- The `Dockerfile`, `docker-compose.yml` and `deploy/` systemd units.

**Out of scope (report upstream):**

- Vulnerabilities in Gmail, Google Workspace or Google's account security.
- Third-party services you configure: Slack, Discord, healthchecks.io, the Anthropic API.
- Issues in dependencies that an upstream release already fixes. Open a normal issue or PR and we will bump.
- Mail being filed as Spam or accounts being restricted by a provider. That is a deliverability matter, not a vulnerability.

## Hardening guidance for operators

emberpost handles mailbox credentials, so treat the host like a secrets store:

- **Use app passwords, never account passwords,** and revoke them when you stop. Each account needs 2-Step Verification.
- **Keep secrets in `.env` only.** The alert webhook URL, heartbeat URL, API key and app passwords are read from environment variables and never from `config.yaml`. Never commit `.env`. It is already in `.gitignore`.
- **Restrict file permissions.** `chmod 600 .env`. In the systemd route, `/etc/emberpost/*` is `root:emberpost` with mode `640`, and the state directory is `0700`.
- **Run the container or unit as shipped.** The image runs unprivileged on a read-only filesystem with all capabilities dropped. The systemd unit uses `ProtectSystem=strict`, `NoNewPrivileges` and a restricted address-family set.
- **Treat the SQLite database and `trails.jsonl` as sensitive.** They hold full email bodies and addresses. Back them up off the machine, and encrypt backups.
- **Protect the webhook URL.** Anyone who has it can post into your channel. Rotate it if it leaks.
- **Use `llm.provider: api` on servers,** with a key scoped to this use, and set a spend limit in the Anthropic console.
- **Keep dependencies current.** Dependabot proposes updates weekly. Merge them.
- **Use only accounts you own.** See the README's Responsible use section.
