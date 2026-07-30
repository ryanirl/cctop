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

The two usage GETs are plain reads that consume no message quota. Credential
writes happen only when hot-switch mode is enabled, as described below.

## What cctop does with your token

- It reads the token for an account **only from that account's own store** (its
  per-config-dir Keychain service or its own credentials file), never from a
  shared default, so one account can never be authenticated with another's
  token. The usage response's organization is additionally verified against the
  account.
- The token is placed in the `Authorization` header of the usage GET and used
  nowhere else.
- The token is **never** logged, printed, included in `--json` output, or
  transmitted anywhere except the provider's own API (`api.anthropic.com` /
  `chatgpt.com`) that issued it. In hot-switch mode, complete credential
  records move only between their existing local Keychain/file stores.

## The only things cctop ever writes

cctop is read-only except for these account-management actions:

1. **Account provisioning** (`add-account`, the `a` key): creates a new
   `~/.claude-N` directory; with `--alias` appends one shell-rc line (after
   backing the file up); with `--from` copies an allowlist of *user config*
   (`CLAUDE.md`, `settings.json`, `commands/agents/skills/hooks/output-styles`)
   — never credentials, identity (`.claude.json`), or session state. It never
   deletes, overwrites, or edits existing lines.
2. **Token refresh** (the `R` key): runs `claude mcp list` for an account so the
   **Claude Code binary itself** renews its own token at startup (a quota-free,
   non-interactive command). cctop does not write the credential; it delegates
   to the tool that owns it.
3. **Hot switching** (`hot_switch = true`, the `x` key, `cctop switch`, and the
   configured automatic threshold): copies a complete saved credential record
   into the main Claude Code Keychain store and replaces only the
   `oauthAccount` field in the main config. Before switching away, the current
   main credential is synced back to its matching saved profile so a rotated
   refresh token is preserved. If the active login has no stable saved profile,
   cctop creates one automatically under `~/.config/cctop/profiles/<org-id>`;
   its credential remains in a per-profile Keychain item (or the existing
   file-backed format), while the identity file contains only Claude's account
   metadata. Writes are performed under Claude Code's own advisory
   credential/config locks; file-backed credentials use atomic replace, and
   Keychain payloads are sent through `security -i` stdin rather than exposed
   as plaintext process arguments.

There is deliberately **no** delete or logout path.

## Reporting

This is a personal project; open an issue for anything that looks wrong. If you
find a genuine credential-handling flaw, please report it privately first.
