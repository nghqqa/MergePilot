# Contributing to MergePilot

Thanks for helping improve MergePilot. This repository is an Apache-2.0 prototype with an isolated-stack workflow; it is not a production service or a claim of external-customer validation.

## Before you start

- Read the root [README](README.md) for the supported entry points and current truth boundaries.
- Read [docs/README.md](docs/README.md) before relying on a historical benchmark, evidence record, or milestone document.
- Coding agents must also follow [AGENTS.md](AGENTS.md).
- Do not commit credentials, local paths, generated caches, temporary screenshots, or environment files.

## Development expectations

- Keep a change focused. Do not combine product work, historical-archive cleanup, and documentation reshaping in one pull request.
- Preserve fail-closed behavior, least-privilege boundaries, fixture isolation, and the distinction between isolated verification and production claims.
- Do not edit historical material in `evidence/` or `verification/` unless the task explicitly concerns its retention or format.
- State precisely what you ran. Do not claim a test, push, merge, external action, or verification result that did not occur.

## Local checks

For the ordinary Python suites, keep `EPHEMERAL_PG_VERIFY` unset and run:

```bash
python -m pytest -q tests/demo_console tests/isolated_live tests/verification --import-mode=importlib
```

Use the isolated-stack instructions in the [README](README.md) only when the task explicitly requires Docker/WSL validation. Never place passwords, tokens, or DSNs in command arguments, committed files, screenshots, or logs.

## Pull requests

Describe the user-visible purpose, changed paths, tests run, and remaining limitations. Keep current truth boundaries intact unless the task explicitly authorizes a separately reviewed change.
