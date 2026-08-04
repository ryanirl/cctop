"""cctop: a live monitor for multiple Claude Code sessions.

The package splits cleanly into a pure collector core (registry, transcript,
pricing, status, collect) with no UI dependency, and a thin presentation layer
(cli, and later a Textual app). All knowledge of Claude Code's undocumented,
version-internal on-disk formats is contained in the collector core so drift is
isolated to one place.
"""

__version__ = "0.3.0"
