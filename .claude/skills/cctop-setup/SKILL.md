---
name: cctop-setup
description: Install and configure cctop, the multi-account Claude Code usage monitor, for the user. Use when the user wants to set up / install cctop, get it running, or add a Claude account (subscription) for it to track. STRICTLY ADDITIVE: this skill only installs and adds; it must never delete, remove, rewrite, or reconfigure any existing files, credentials, sessions, aliases, or shell config.
---

# cctop setup

Set up cctop (a live TUI that monitors multiple Claude Code sessions and
subscription usage) for the user. Everything here is additive and safe.

## Absolute safety rules (do not violate)

1. **Never delete or remove anything** — no `rm`, no removing files, dirs,
   sessions, accounts, aliases, or credentials. cctop itself has no delete
   operation; do not add one or work around its absence.
2. **Never modify existing configuration in place** — do not edit, rewrite, or
   reorder existing lines in `~/.zshrc`, `~/.bashrc`, `settings.json`,
   `.claude.json`, `config.toml`, or any other config the user already has. The
   only permitted change to a shell rc file is the **append** performed by
   `cctop add-account --alias NAME` (which backs the file up first and is
   idempotent); without `--alias`, add-account never touches a shell rc file.
3. **Never log out, revoke, or touch credentials** — do not run `claude logout`,
   `codex logout`, delete `.credentials.json`, or read/print any token.
4. **Confirm before any change outside the repo** — installing into the repo's
   own `.venv` is fine without asking. Adding an account (which appends a shell
   alias and creates a new `~/.claude-N` dir) requires explicit user
   confirmation first; show the dry-run plan and wait for a yes.
5. If the user asks to **remove/delete/reset** an account or setup, decline and
   explain that cctop is intentionally additive-only to avoid ever breaking a
   working setup. Offer to point them at the manual steps instead, but do not do
   it for them.

## Step 1 — Install

Work in the cloned repo directory. Prefer `uv`; fall back to `python -m venv`.

```bash
# from the repo root
uv venv --python 3.12 && uv pip install -e .
# fallback if uv is unavailable:
# python3 -m venv .venv && .venv/bin/pip install -e .
```

Verify it imports and runs (read-only):

```bash
.venv/bin/cctop --once        # one-shot snapshot; should print the table
.venv/bin/cctop accounts      # lists discovered accounts (read-only)
```

If `cctop --once` works, installation is done. Tell the user how to launch the
live TUI: `.venv/bin/cctop` (keys: `s` stats, `r` refresh limits, `R` refresh
token, `a` add account, `q` quit), and suggest they add a shell alias like
`alias cctop='<repo>/.venv/bin/cctop'` — but only append it, and only if they
want it.

## Step 2 — Accounts (only if the user wants to add one)

cctop auto-discovers every `~/.claude` (cc-0) and `~/.claude-<N>` (cc-<N>)
account, so existing accounts need no setup — just confirm they show up in
`cctop accounts`. No shell alias is required for cctop to see an account.

To add a **new** subscription/account, use the built-in additive command. Always
show the dry-run first, get confirmation, then apply:

```bash
.venv/bin/cctop add-account                      # dry-run: prints the plan, changes nothing
.venv/bin/cctop add-account --login              # ONLY after the user confirms: create dir + sign in
.venv/bin/cctop add-account --from cc-0 --login  # ...also clone cc-0's user config into the new account
.venv/bin/cctop add-account --alias cc-2 --login # ...and set an explicit cc-2 shell alias
```

What each flag does, all additive:

- **No flag** creates only the next `~/.claude-N` dir (or reuses an existing
  logged-out one). It does **not** write a shell alias.
- **`--alias NAME`** (opt-in only) backs up the rc file and appends one alias
  line. Never automatic.
- **`--from ACCOUNT`** copies an allowlist of user config only (`CLAUDE.md`,
  `settings.json`, `commands/ agents/ skills/ hooks/ output-styles/`); it never
  copies `.claude.json`, credentials, or session state, never overwrites an
  existing file, and never modifies the source.
- **`--login`** runs `claude auth login` scoped to the new dir so the user signs
  in themselves (browser flow); it is isolated to the new account and cannot
  touch another. This is a sign-**in**, never a logout.

After applying, if an alias was set tell the user to `source ~/.zshrc` (or open a
new shell) to use it. cctop picks the account up automatically either way.

## Step 3 — Confirm

Run `cctop accounts` once more and show the user the result: each account with
its tier, whether a token is present, and its live session count. Point out that
usage limits and stats are read for free (no tokens consumed), and that nothing
was deleted or modified.

## What NOT to do (recap)

- No `rm`, no `logout`, no editing existing config lines, no deleting sessions
  or accounts, no reading/printing tokens.
- The only writes you may perform: create the repo `.venv`, and (with
  confirmation) run `cctop add-account --apply`, which is additive and backed up.
