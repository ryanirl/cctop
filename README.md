# cctop

[![CI](https://github.com/ryanirl/cctop/actions/workflows/ci.yml/badge.svg)](https://github.com/ryanirl/cctop/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-teal.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)
![macOS](https://img.shields.io/badge/macOS-only-lightgrey.svg)

A live terminal monitor for multiple **Claude Code** and **OpenAI Codex**
accounts and their subscription usage, in the spirit of `nvtop`/`htop`. It
tracks every running session across one or more accounts, shows real
usage-limit gauges, and gives a GitHub-style view of your activity over time.

Everything is read from files the tools already write plus a couple of free,
read-only usage reads; nothing here ever spends message quota. cctop touches
your credentials only to make those reads, never logs or transmits a token, and
is read-only except for two explicit, additive account actions. See
[SECURITY.md](SECURITY.md) for the full trust statement.

![cctop](https://raw.githubusercontent.com/ryanirl/cctop/main/docs/hero.png)

<sub>A sanitized demo: real usage limits, models, and activity, with account
aliases and project names anonymized.</sub>

## What it shows

- **Usage limits** (top panel), per account, side by side: the 5-hour, weekly
  (all models), and weekly (Fable) windows with utilization, reset countdown,
  and the currently-binding window marked. These are the real numbers from
  Anthropic's `GET /api/oauth/usage` endpoint (the same one `/usage` uses) — a
  free read, no tokens consumed.
- **Sessions** (table): every live Claude Code process, tagged by account, with
  status, model, cwd, context %, cumulative tokens, cost, and age. Selecting a
  row opens a detail panel (full cwd, pid, version, token/cost/context
  breakdown).
- **Statistics** (press `s`): a GitHub-style daily-activity heatmap, lifetime
  totals (messages, sessions, tool calls, active days, longest streak, busiest
  day), a top-models-by-tokens chart, and a per-account breakdown, all merged
  from each account's `stats-cache.json`.

## Install

Requires **macOS** and **Python 3.11+** (Linux support is planned).

Once published to PyPI (distribution name `cctop-tui`), the one-liners are:

```bash
uv tool install cctop-tui     # or: pipx install cctop-tui
uvx cctop-tui                 # run without installing (starts the `cctop` TUI)
```

From a clone (current default while pre-release):

```bash
cd cctop
uv venv --python 3.12
uv pip install -e .           # add ".[dev]" for the test/lint tooling
cctop
```

Or, easiest: clone the repo, open it in Claude Code, and say **"set up cctop for
me."** The bundled `cctop-setup` skill (`.claude/skills/cctop-setup/`) installs
it and can add accounts for you. The skill is strictly additive — it never
deletes, removes, or modifies existing config, credentials, or sessions.

## Usage

```bash
cctop                 # launch the live TUI (default)
cctop --once          # print a one-shot snapshot and exit
cctop --json          # emit the snapshot as JSON (for scripting)
cctop --no-limits     # skip the usage fetch (no network, session table only)

cctop accounts        # list discovered accounts (read-only)
cctop doctor          # read-only self-check (platform, binaries, token/expiry)
cctop add-account     # provision a new account (dry-run; see Accounts below)
```

TUI keys: `s` stats · `r` refresh limits · `R` refresh token · `a` add account ·
`q` quit.

### Token refresh (`R`)

A Claude Code OAuth access token lives only ~12-15h, and the CLI refreshes it
lazily when you *use* an account, so an account you are merely monitoring drifts
past expiry and the usage read starts failing (`token expired`). Press `R` and
cctop asks the tool that owns the credential to renew it: it runs
`claude mcp list` under each account's config dir (a quota-free command whose
startup renews and rewrites the Keychain record). **cctop never writes a
credential itself** — it only triggers the owner binary and reads the result —
and it is a no-op on tokens that are still valid. An account whose refresh token
is itself dead reports "needs re-login" (only a fresh `/login` can fix that).

### Accounts

cctop discovers accounts by globbing `~/.claude` (cc-0) and `~/.claude-<N>`
(cc-<N>). A newly added account is picked up automatically; no shell alias is
required for cctop to see it.

Add one in a single step, from the shell or the TUI:

```bash
cctop add-account --login                      # create the dir + sign in
cctop add-account --from cc-0 --login          # ...cloning cc-0's user config
cctop add-account --from cc-0 --alias cc-2 --login   # ...and set a cc-2 alias
```

Or press `a` in the TUI: it suspends the dashboard, optionally clones config and
sets an alias (both prompted, both default to none), runs the login, and shows
the new account on return.

It is **strictly additive and explicit**:

- **Never auto-aliases** — a shell alias is written only when you pass `--alias
  NAME` (or type one at the prompt); dir creation alone never touches your rc
  file. When it does add one, it backs up the rc file first and is idempotent.
- **`--from` clones an allowlist only** — `CLAUDE.md`, `settings.json`, and
  `commands/ agents/ skills/ hooks/ output-styles/` when present. Identity
  (`.claude.json`), credentials, and all session state (projects, sessions,
  history) are **never** copied, existing files are never overwritten, and the
  source account is never modified.
- **Login is isolated** to the new config dir (via `CLAUDE_CONFIG_DIR` and its
  own Keychain service), so it can never read or overwrite another account.
- There is deliberately **no delete/remove operation** anywhere.

If you set an alias, run `source ~/.zshrc` (or open a new shell) to use it.

## How it works

| Data | Claude source | Codex source | Cost |
|---|---|---|---|
| Live sessions | `~/.claude*/sessions/<pid>.json` | `ps` + `~/.codex/sessions/` rollouts | free (file/ps) |
| Tokens / context | transcript JSONL, tailed | rollout `token_count` events | free (file) |
| Usage limits | `GET /api/oauth/usage` (Keychain token) | `GET chatgpt.com/backend-api/codex/usage` (`~/.codex/auth.json` token) | free (GET) |
| Statistics | `~/.claude*/stats-cache.json` | aggregated from rollout files | free (file) |

## Providers

cctop tracks both **Claude Code** and **OpenAI Codex** side by side. Claude
accounts are `cc-0`/`cc-1`/…; a Codex account (`cx-0`, from `~/.codex`) is added
automatically when present. Usage windows and stats are read per provider (they
differ in shape: Codex exposes primary/secondary windows plus per-model limits,
and its stats come from rollout files rather than a stats cache). Codex sessions
show token/context but no dollar cost, since Codex is subscription-based.

The credential for the usage read is resolved per account from the macOS
Keychain service `Claude Code-credentials-<sha256(config_dir)[:8]>`, used only to
authenticate to Anthropic's own API, never logged or persisted. The usage
response's org is verified against the account so one account can never show
another's numbers.

Undocumented, version-internal formats (the sessions registry, transcript
schema, usage JSON) are all read defensively and isolated to the collector core
(`registry`, `transcript`, `usage`, `stats`, `monitor`, `pricing`, `status`,
`codex*` for the Codex provider, `authctl` for delegated token refresh, and
`manage` for additive account provisioning), with a thin Textual presentation
layer on top.

## Notes

- Pricing (`pricing.py`) is a small editable table; update it as rates change.
- The palette is the house teal (`#20B2AA`) on monochrome terminal. Usage bars
  stay teal even when maxed (fullness is the signal); red is reserved for a
  blocked or dead session.

## Author

Ryan 'RyanIRL' Peters
