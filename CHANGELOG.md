# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- History search across every account and both providers: `/` in the TUI opens
  a live, debounced search over all transcript files (Claude projects/ JSONL
  and Codex rollouts), grouped by session, tagged with the owning account,
  titled from session metadata, with the matching messages previewed and the
  query highlighted. `Enter` resumes the selected session under its own
  account (`CLAUDE_CONFIG_DIR` pinned, cwd restored) via the owner binary;
  `Ctrl+R` toggles regex mode.
- `cctop search QUERY [--regex] [--account NAME] [--limit N] [--json]`: the
  same search from the CLI; `--json` emits match rows plus a summary row for
  scripts and skills.
- The scan backend prefers a real `rg`, falls back to the ripgrep embedded in
  the Claude Code binary (probed with `--version` before trust), and finally a
  pure-Python scan, so search works with zero extra dependencies. Matches are
  post-filtered against decoded message text so metadata (session ids, paths)
  can never produce a false hit, and literal queries are JSON-escaped for the
  prefilter so quoted text still matches. Sidechain/subagent transcripts are
  excluded.
- A transcript viewer between finding and resuming: `Enter` on a result opens
  the conversation read-only (tool noise hidden, matches highlighted, starting
  at the first match) to confirm it is the right session; from there `Enter`/`o`
  resumes in the current terminal and `t` resumes in a new Terminal window,
  leaving cctop running.
- Directory scoping: `--dir PATH` (CLI) or `Ctrl+D` (TUI, toggles the launch
  directory) restricts results to sessions whose working directory is under it.
- A dedicated PATH bar under the search bar (`Tab` to reach it; `--path TEXT`
  on the CLI): a case-insensitive substring filter on the session's cwd or
  transcript path, live like the query and combinable with `Ctrl+D`.
- Browse mode: with an empty search bar the screen lists recent sessions
  across every account, so it opens as a session browser and the PATH bar
  alone answers "what ran in this repo". Browse and search are one rendering
  path with identical semantics: sessions always sort by last activity (file
  mtime), the preview always shows the session card (plus highlighted
  snippets when a query matched), and browse is simply "no hits". On the CLI,
  a query-less `cctop search` with `--json`/piped output lists sessions as
  `{"type": "session", ...}` rows.
- One cursor: while typing in the SEARCH or PATH bar no result row is
  selected, so `Enter` in a bar never opens a session by accident. `Down` (or
  a click) selects; `Up` past the first row returns to the bar; new results
  always start unselected.
- `cctop search` with no query opens the search TUI; with a query it opens
  pre-filled. `--json` or piped stdout prints instead, as before.
- Richer search results: an explicit provider column, a live marker (a teal
  dot on sessions whose process is running right now, read from the registry
  cctop already watches, so a session is never resumed twice by accident),
  the model, the session start time, and a turns column (assistant-message
  count via one ripgrep --count pass over just the displayed sessions).
  All of it is in the `--json` rows too.
- Python 3.9 support: `tomllib` falls back to `tomli` before 3.11 (the only
  real incompatibility; the 3.9-and-up APIs in use are `str.removeprefix` and
  `Path.is_relative_to`), and CI now runs 3.9 through 3.13.

## [0.2.0] - 2026-07-19

### Added
- `cctop setup`: a small provider chooser (Claude Code / OpenAI Codex) that only
  lets you pick a provider cctop can actually detect; an undetected one is shown
  dimmed and unselectable, and a note appears if neither is found. For the common
  auto-detected case it tells you you're already set; otherwise, after an explicit
  confirm, it hands off to that provider's agent to configure cctop interactively
  (ideal for custom account layouts). cctop itself writes nothing.
- A settings screen (`,`) that live-edits the config with no restart: usage
  refresh interval, heatmap range, and per-account show/hide + rename, written
  to `config.toml` and applied immediately.
- `r` is now a true force-refresh: it clears per-account rate-limit backoff,
  refetches immediately, and resets the next-refresh timer + countdown.
- The footer shows a live countdown to the next usage refresh (e.g. "next in
  2m14s"), ticking every second alongside how long ago it last updated.
- Optional `~/.config/cctop/config.toml` (created by `cctop config init`, never
  written silently) to rename, hide, reorder, or add accounts and tune settings;
  it layers over auto-detection, which now also picks up non-numeric
  `~/.claude-*` config dirs (e.g. `~/.claude-work`), not just numeric ones.

## [0.1.0] - 2026-07-19

### Added
- The USAGE panel wraps account blocks onto multiple rows and sizes bars to
  the terminal width, reflowing live on resize instead of overflowing when many
  accounts or a narrow window are in play.
- Live TUI monitoring Claude Code and OpenAI Codex sessions side by side, with
  real usage-limit gauges from the free `/api/oauth/usage` and Codex usage GETs.
- GitHub-style activity heatmaps and lifetime stats, per provider.
- Delegated token refresh (`R`): renews an account's OAuth token via the owning
  binary, without cctop ever writing a credential.
- One-step account provisioning (`add-account --login` / the `a` key): create,
  optionally clone user config from another account (`--from`, allowlisted), set
  an explicit alias (`--alias`, never automatic), and sign in.
- `cctop doctor`: a read-only self-check (platform, claude/codex binaries, and
  each account's credential, token expiry, tier, and live-session count).
- A never-started usage window (0% with no reset) now renders "no usage yet"
  instead of a misleading empty 0% bar, and a `percent` value carrying a reset
  epoch (a known upstream bug) is guarded instead of shown as a full bar.
- Rate-limit resilience: per-account backoff (Retry-After aware) that keeps the
  last good usage numbers instead of blanking on a transient 429/5xx/network
  failure, and a refresh that will not "refresh into another 429".
- Test suite, `ruff`/`mypy` gates, and CI across Python 3.11-3.13 on macOS
  (macOS-only for the first release; Linux support is future work).
- `LICENSE` (MIT), `SECURITY.md`, and packaging metadata (distribution
  `cctop-tui`; the command stays `cctop`).

[0.2.0]: https://github.com/ryanirl/cctop/releases/tag/v0.2.0
[0.1.0]: https://github.com/ryanirl/cctop/releases/tag/v0.1.0
