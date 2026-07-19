"""Model pricing and cost computation from transcript usage.

Prices are per million tokens and are editable here (later, overridable via
config). When a model id is not in the table we return None for cost rather than
guessing a wrong number: the caller shows the token counts and a "-" for cost,
which is the honest fail-soft behavior.
"""

from __future__ import annotations

from dataclasses import dataclass

# Cache multipliers relative to the base input rate (Anthropic standard).
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_5M_MULTIPLIER = 1.25
CACHE_WRITE_1H_MULTIPLIER = 2.0

DEFAULT_CONTEXT_WINDOW = 200_000


@dataclass(frozen=True)
class ModelPricing:
    """Per-million-token input and output rates, plus the context window size."""

    input_per_mtok: float
    output_per_mtok: float
    context_window: int


# Keyed by the model id as it appears in transcript `message.model`. Values are
# current as of 2026-07; treat as a starting point and correct as prices change.
_PRICING: dict[str, ModelPricing] = {
    "claude-opus-4-8": ModelPricing(5.0, 25.0, 1_000_000),
    "claude-opus-4-7": ModelPricing(5.0, 25.0, 1_000_000),
    "claude-opus-4-6": ModelPricing(5.0, 25.0, 1_000_000),
    "claude-sonnet-5": ModelPricing(3.0, 15.0, 1_000_000),
    "claude-sonnet-4-6": ModelPricing(3.0, 15.0, 1_000_000),
    "claude-haiku-4-5": ModelPricing(1.0, 5.0, 200_000),
    "claude-fable-5": ModelPricing(10.0, 50.0, 1_000_000),
    "claude-mythos-5": ModelPricing(10.0, 50.0, 1_000_000),
}


def _base_model_id(model: str) -> str:
    """Strip a context-tier suffix like "[1m]" so it matches the price table."""
    return model.split("[", 1)[0]


def pricing_for(model: str | None) -> ModelPricing | None:
    """The ModelPricing for a transcript model id, or None if unrecognized."""
    if not model:
        return None
    return _PRICING.get(_base_model_id(model))


def context_window_for(model: str | None) -> int:
    """The context window in tokens for a model, defaulting when unknown."""
    pricing = pricing_for(model)
    return pricing.context_window if pricing else DEFAULT_CONTEXT_WINDOW


def cost_for_usage(model: str | None, usage: dict) -> float | None:
    """Cost in USD for one assistant message's `usage` block.

    Returns None when the model is unpriced, so the caller can distinguish
    "unknown cost" from "zero cost" and avoid reporting a fabricated number.
    """
    pricing = pricing_for(model)
    if pricing is None:
        return None

    input_rate = pricing.input_per_mtok / 1_000_000
    output_rate = pricing.output_per_mtok / 1_000_000

    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cache_read_tokens = usage.get("cache_read_input_tokens", 0)

    creation = usage.get("cache_creation") or {}
    write_5m = creation.get("ephemeral_5m_input_tokens")
    write_1h = creation.get("ephemeral_1h_input_tokens")
    if write_5m is None and write_1h is None:
        # No TTL breakdown available; price all cache creation at the 5m rate.
        write_5m = usage.get("cache_creation_input_tokens", 0)
        write_1h = 0

    cost = (
        input_tokens * input_rate
        + output_tokens * output_rate
        + cache_read_tokens * input_rate * CACHE_READ_MULTIPLIER
        + (write_5m or 0) * input_rate * CACHE_WRITE_5M_MULTIPLIER
        + (write_1h or 0) * input_rate * CACHE_WRITE_1H_MULTIPLIER
    )
    return cost
