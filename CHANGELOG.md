# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-07-25

### Added
- Optional hot-switch mode: saved Claude config directories become credential
  profiles for one main session directory, with lock-safe Keychain/config swaps,
  current-token sync-back, `cctop switch [NAME]`, and the TUI `x` key.
- A headless `cctop autoswitch` supervisor keeps automatic rotation running
  independently of the TUI and terminal job control.
- Automatic rotation to the healthy Claude profile with the most headroom at
  1% remaining (configurable), including delegated refresh of expired saved
  access tokens without a login/logout cycle. Candidates must have strictly
  more headroom than the active profile, preventing ping-pong while still using
  every account that can make progress.

### Changed
- `get_token` no longer falls back to the default account's Keychain service, so
  a logged-out config dir reads as having no token instead of borrowing the
  default account's. The default-profile Keychain and identity-path rules now
  live in one place (`authctl`) instead of being restated per call site.
- A rejected live credential now triggers delegated refresh and then immediate
  profile recovery, even while usage reads are rate-limited. The dead main
  credential is never synced over its saved profile, and stale usage data can
  no longer select a locally expired target.
- Automatic rotation now accepts an above-threshold profile when it has
  strictly more headroom than the active one, so a fully exhausted login can
  hand work to a still-usable account without introducing switch ping-pong.
- The TUI and autoswitch supervisor now share last-good usage metadata. During
  HTTP 429 backoff, new cctop processes keep showing cached percentages, reset
  times, and reading age instead of replacing useful data with an error.

### Preserved
- Original per-config-directory session monitoring and account provisioning
  remain the default behavior when `hot_switch` is disabled; the login profiles
  cctop snapshots for itself are hidden from account discovery while it is off.

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

[0.3.0]: https://github.com/ryanirl/cctop/releases/tag/v0.3.0
[0.2.0]: https://github.com/ryanirl/cctop/releases/tag/v0.2.0
[0.1.0]: https://github.com/ryanirl/cctop/releases/tag/v0.1.0
