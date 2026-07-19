# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `cctop setup`: a small provider chooser (Claude Code / OpenAI Codex) that only
  lets you pick a provider cctop can actually detect; an undetected one is shown
  dimmed and unselectable, and a note appears if neither is found. After you
  confirm (the gate before anything launches `claude`/`codex`), it writes the
  config and, for Claude, verifies each account's token.
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
- The USAGE panel now wraps account blocks onto multiple rows and sizes bars to
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

[Unreleased]: https://github.com/ryanirl/cctop/commits/main
