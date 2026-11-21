---
name: Bug report
about: Report a reproducible defect in emberpost.
title: "[bug]: "
labels: bug
assignees: ''
---

> **Redact before posting.** Remove app passwords, API keys, webhook URLs and real email addresses from everything you paste. Use `peer1@gmail.com`-style placeholders.

## Description

**What happened**


**What you expected to happen**


## Reproduction steps

1.
2.
3.

## Environment

- OS (and version):
- Python version:
- How you run it (`run` / `tick` from cron / Docker / systemd):
- `llm.provider` (`cli` / `api` / off):
- Number of peers and your `ramp` settings:
- emberpost commit SHA:

## Output

Output of `python -m warmup validate` and `python -m warmup status` (redacted):

<details>
<summary>Click to expand</summary>

```
paste output here
```

</details>

## Logs / stack trace

Run with `-v` if you can.

<details>
<summary>Click to expand</summary>

```
paste logs / traceback here
```

</details>

## Checks

- [ ] I searched [existing issues](https://github.com/shubhamkatta/emberpost/issues?q=is%3Aissue) and this is not a duplicate.
- [ ] I am running the latest `main` (or noted the exact SHA above).
- [ ] I removed all credentials and real addresses from this report.
