"""Command-line options and the client they select, shared by the evaluations that use a model."""

import argparse
import os
from pathlib import Path

from recon.mapping.llm_client import AnthropicJson, JsonModel, RecordingJson, ReplayJson

# List prices in US dollars per million input and output tokens, from the Claude API reference
# cached on 2026-10-06. An estimate for planning: check current pricing before relying on it.
LIST_PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


def add_llm_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--effort", default="low", choices=["low", "medium", "high"])
    parser.add_argument("--replay", help="serve answers recorded in this file; no network")
    parser.add_argument("--record", help="append the live answers to this file")


def json_client(args: argparse.Namespace) -> JsonModel:
    """The client the options select: replayed answers, or the API, optionally recorded."""
    if args.replay:
        replay = ReplayJson(Path(args.replay))
        if not len(replay):
            raise SystemExit(f"{args.replay} holds no recorded answers")
        return replay
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "No ANTHROPIC_API_KEY is set and no --replay file was given, so there is "
            "nothing to ask. Run with the rules, or replay recorded answers."
        )
    live: JsonModel = AnthropicJson(model=args.model, effort=args.effort)
    return RecordingJson(live, Path(args.record)) if args.record else live


def cost_line(model: str, input_tokens: int, output_tokens: int) -> str | None:
    price = LIST_PRICES.get(model)
    if price is None:
        return None
    cost = input_tokens / 1e6 * price[0] + output_tokens / 1e6 * price[1]
    return (
        f"tokens: {input_tokens} in, {output_tokens} out; "
        f"about ${cost:.2f} at list price for {model}"
    )
