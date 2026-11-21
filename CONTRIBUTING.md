# Contributing to emberpost

Thanks for stopping by. **emberpost** warms a new Google Workspace mailbox with realistic, threaded conversations between accounts you own. It ramps volume, rescues mail from Spam, and never double-sends. It is deliberately small: one process, one SQLite file, two runtime dependencies.

First-time contributors are welcome. Useful contributions include:

- **Bug fixes**, including the boring ones.
- **Tests**, especially crash-recovery and transport edge cases.
- **Docs**, such as clearer setup steps and deploy recipes (Fly, Hetzner, Render, Kubernetes).
- **Transports**, such as OAuth for Gmail or Microsoft 365 / generic IMAP support.
- **Content providers**, such as other LLM backends alongside the Claude CLI and API.
- **Deliverability signals** that can be measured, not guessed.

If you are unsure whether something fits, open an issue first, before sinking time into a large PR.

## Ground rules

- **Never commit secrets or personal data.** No real email addresses, app passwords, webhook URLs, `config.yaml`, `.env`, `*.db` or `trails.jsonl`. Use `example.com` and `peerN@gmail.com` in examples and tests. These files are already in `.gitignore`.
- **Correctness over cleverness.** The core promise is that a crash cannot cause a double send or a lost job. Changes to `engine.py`, `db.py` or `transport.py` must keep it.
- **Keep dependencies at two.** New runtime dependencies need a strong reason. Prefer the standard library.
- **Warmup only.** PRs that add features whose main use is bulk unsolicited email will be declined.

## Getting started

```bash
git clone git@github.com:<your-username>/emberpost.git
cd emberpost
git remote add upstream git@github.com:shubhamkatta/emberpost.git

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

### Run the tests and lint

```bash
pytest -q          # 66 tests, no network, no real accounts
ruff check .       # lint
```

Both must be green before you open a PR. CI runs them on Python 3.10 to 3.13, then builds the Docker image and validates the example config inside it.

### Try changes without risk

`--dry-run` runs the whole engine with no network and no secrets. It still writes to the configured database, so point `database:` at a throwaway file:

```bash
cp config.example.yaml /tmp/ep.yaml     # set database: /tmp/ep.db
python -m warmup validate -c /tmp/ep.yaml
python -m warmup tick -c /tmp/ep.yaml --dry-run -v
```

## Project layout

| Path | Role |
|---|---|
| `warmup/engine.py` | Planning, ramp, spam-rate gate, send loop, crash recovery. |
| `warmup/transport.py` | SMTP send and IMAP locate, rescue and engage. |
| `warmup/content.py` | LLM and template email composition, validation, de-duplication. |
| `warmup/trails.py` | The pre-generated conversation file: format, validation, generation. |
| `warmup/db.py` | SQLite schema, WAL mode, locking. |
| `warmup/config.py`, `validate.py` | Config loading and error reporting. |
| `warmup/alerts.py` | Webhook alerts and the heartbeat. |
| `warmup/cli.py` | Command-line entry point. |
| `tests/` | `test_sim` (40-day simulation), `test_robust` (crash recovery), `test_imap` (fake Gmail), `test_trails`, `test_config`, `test_ops`. |
| `deploy/` | systemd units and backup timer. |

The transport is an interface. Tests run the full engine against a fake one, so most logic is testable without Gmail.

## Making changes

- **Add a config key?** Add it to `config.py`, validate it in `validate.py`, show it in `config.example.yaml`, and document it in the README. Unknown keys are errors by design, so a missing validation entry breaks users.
- **Change what gets sent?** Every email must still pass content validation: no placeholders, links, emojis or meta text.
- **Change the send path?** Add a test in `test_robust.py` that kills the process at the new step and proves the next tick recovers without a double send.
- **Change IMAP behaviour?** Extend the fake Gmail in `test_imap.py` first. Rescue must copy before it deletes, so the only copy is never lost.
- **Touch secrets handling?** Passwords and webhook URLs must never appear in logs or error messages. Existing tests assert this. Keep them passing.

## Branching and commits

Branch from `main`:

- `feat/<slug>` for features, `fix/<slug>` for bug fixes, `docs/<slug>` for docs, `chore/<slug>` for tooling, deps and CI.
- Security issues: do **not** open a public PR. See [SECURITY.md](SECURITY.md).

Commit messages follow Conventional Commits:

```
feat(trails): resume generation after an LLM outage
fix(engine): do not advance the ramp on a day with no sends
docs(readme): add Hetzner deploy recipe
chore(ci): add Python 3.14 to the matrix
```

Keep the subject short, lowercase, with no trailing period. Use the body to explain **why**. The diff already shows what. Reference issues with `Fixes #123`. Keep commits atomic: a lint fix, a rename and a feature belong in three commits.

## Pull request checklist

- [ ] `pytest -q` and `ruff check .` pass locally.
- [ ] New behaviour has a test. Bug fixes have a test that fails without the fix.
- [ ] No secrets, real addresses or personal data in code, tests, fixtures or logs.
- [ ] Changes to sending, locking or the database keep crash recovery intact, with a test.
- [ ] New config keys are validated and documented (`config.example.yaml` and README).
- [ ] No new runtime dependency, or the PR explains why one is needed.
- [ ] The PR description says what changed, why, and links the related issue.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
