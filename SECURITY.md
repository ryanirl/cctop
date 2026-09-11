# Security & trust

cctop reads local files that Claude Code and Codex already write, plus a small
number of free, read-only API calls. It touches OAuth tokens only to the extent
required to make those reads. This document states exactly what it does and does
not do, because a tool that sits next to your credentials should be auditable.

## What cctop reads

| Source | Purpose |
|---|---|
| `~/.claude*/sessions/<pid>.json`, transcripts, `stats-cache.json` | live sessions, tokens, cost, activity |
| `~/.codex/sessions/**` rollout files, `ps` | live Codex sessions and stats |
| `GET https://api.anthropic.com/api/oauth/usage` | real Claude usage limits (the endpoint `/usage` uses) |
| `GET https://chatgpt.com/backend-api/codex/usage` | real Codex usage limits |
| macOS Keychain item `Claude Code-credentials-<hash>`, or `~/.claude*/.credentials.json` | the OAuth token used to authenticate the two GETs above |
| `~/.codex/auth.json` | the token used to authenticate the Codex usage GET |
| `~/.local/state/cctop/limits/*.json` | usage windows recorded by the optional statusline hook (percentages and reset times only) |

All of it is read-only. The two usage GETs are plain reads that consume no
message quota.

## What cctop does with your token

- It reads the token for an account **only from that account's own store** (its
  per-config-dir Keychain service or its own credentials file), never from a
  shared default, so one account can never be authenticated with another's
  token. The usage response's organization is additionally verified against the
  account.
- The token is placed in the `Authorization` header of the usage GET and used
  nowhere else.
- The token is **never** logged, printed, written to disk by cctop, included in
  `--json` output, or transmitted anywhere except the provider's own API
  (`api.anthropic.com` / `chatgpt.com`) that issued it.

## The only things cctop ever writes

cctop is read-only except for explicit actions you invoke:

1. **Account provisioning** (`add-account`, the `a` key): creates a new
   `~/.claude-N` directory; with `--alias` appends one shell-rc line (after
   backing the file up); with `--from` copies an allowlist of *user config*
   (`CLAUDE.md`, `settings.json`, `commands/agents/skills/hooks/output-styles`)
   — never credentials, identity (`.claude.json`), or session state. It never
   deletes, overwrites, or edits existing lines.
2. **Token refresh** (automatic near expiry and on a 401, or the `R` key):
   runs `claude mcp list` for an account so the **Claude Code binary itself**
   renews its own token at startup (a quota-free, non-interactive command).
   cctop does not write the credential; it delegates to the tool that owns it.
   Automatic attempts are bounded (a failed refresh backs off and goes quiet
   until a real `/login` changes the stored expiry) and can be disabled with
   `auto_refresh_tokens = false`.
3. **Session resume** (`Enter` in history search): hands the terminal to
   `claude --resume` / `codex resume` for the selected session, with
   `CLAUDE_CONFIG_DIR` pinned to the session's own account. The search itself
   is a read-only ripgrep scan of the transcript files; any new conversation
   content is written by the resumed tool, never by cctop.

4. **Statusline hook** (`cctop statusline install`): sets `statusLine` in an
   account's `settings.json` to the `cctop-statusline` command (wrapping any
   existing statusline so it keeps working), after a one-time backup of the
   file. The hook itself only ever writes percentages and reset times under
   `~/.local/state/cctop/`; it sees the same document Claude Code shows in
   the status bar and never a credential. `cctop statusline uninstall`
   restores the previous setting.

5. **Quota probe** (long-lived `setup-token` logins only, `quota_probe = true`
   by default): runs `claude -p quota --max-turns 1 --tools "" --system-prompt
   ... --no-session-persistence` under the account's config dir every
   `quota_probe_seconds` and reads the `rate_limit_event` from its output. The
   Claude Code binary makes the request as itself; cctop never reads or sends
   the token. This is the one thing cctop does that spends message quota: a
   few hundred tokens per probe, only for logins the usage endpoint refuses.
   Disable with `quota_probe = false` (the statusline hook then stands alone).

There is deliberately **no** delete, logout, or credential-writing path in
cctop's own code.

## Reporting

This is a personal project; open an issue for anything that looks wrong. If you
find a genuine credential-handling flaw, please report it privately first.
