"""Command-line options and the client they select, shared by the evaluations that use a model."""

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from recon.llm.client import Completion, JsonModel, QuotaExhausted, RecordingJson, ReplayJson
from recon.llm.providers import KEY_VARIABLES, PROVIDERS, find_key, live_client, model_for
from recon.llm.tools import Message, RecordingSteps, ReplaySteps, Step, Tool, ToolModel

# List prices in US dollars per million input and output tokens, for an estimate. Check current
# pricing before relying on it. A model on a provider's free tier costs nothing within its quota.
LIST_PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


def add_llm_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--provider", default="gemini", choices=PROVIDERS)
    parser.add_argument("--model", help="defaults to the provider's default model")
    parser.add_argument("--base-url", help="for --provider openai: any compatible server")
    parser.add_argument("--effort", default="low", choices=["low", "medium", "high"])
    parser.add_argument("--replay", help="serve answers recorded in this file; no network")
    parser.add_argument("--record", help="append the live answers to this file")


class StopOnQuota:
    """Ends the evaluation when the quota is used up.

    The mappers and adjudicators treat a failed call as one unanswered question, which is right
    in the application but would turn an evaluation cut short into a result. Answers recorded
    before the stop are kept, and a later run with the same --record file carries on from them.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> Completion:
        try:
            completion: Completion = self._inner.complete(system, user, schema)
        except QuotaExhausted as error:
            raise SystemExit(f"Stopped, so as not to score a partial run: {error}") from error
        return completion

    def step(self, system: str, messages: Sequence[Message], tools: Sequence[Tool]) -> Step:
        try:
            step: Step = self._inner.step(system, messages, tools)
        except QuotaExhausted as error:
            raise SystemExit(f"Stopped, so as not to score a partial run: {error}") from error
        return step


def _live(args: argparse.Namespace) -> Any:
    if find_key(args.provider) is None:
        names = " or ".join(KEY_VARIABLES[args.provider])
        raise SystemExit(
            f"No key for {args.provider} ({names}) and no --replay file was given, so there "
            "is nothing to ask. Run with the rules, or replay recorded answers."
        )
    args.model = model_for(args.provider, args.model)
    return live_client(args.provider, model=args.model, base_url=args.base_url, effort=args.effort)


def tool_client(args: argparse.Namespace) -> ToolModel:
    """As ``json_client``, for a model that calls tools."""
    if args.replay:
        replay = ReplaySteps(Path(args.replay))
        if not len(replay):
            raise SystemExit(f"{args.replay} holds no recorded turns")
        return replay
    live = _live(args)
    return StopOnQuota(RecordingSteps(live, Path(args.record)) if args.record else live)


def json_client(args: argparse.Namespace) -> JsonModel:
    """The client the options select: replayed answers, or the API, optionally recorded."""
    if args.replay:
        replay = ReplayJson(Path(args.replay))
        if not len(replay):
            raise SystemExit(f"{args.replay} holds no recorded answers")
        return replay
    live = _live(args)
    return StopOnQuota(RecordingJson(live, Path(args.record)) if args.record else live)


def cost_line(model: str | None, input_tokens: int, output_tokens: int) -> str | None:
    tokens = f"tokens: {input_tokens} in, {output_tokens} out"
    price = LIST_PRICES.get(model or "")
    if price is None:
        return tokens
    cost = input_tokens / 1e6 * price[0] + output_tokens / 1e6 * price[1]
    return f"{tokens}; about ${cost:.2f} at list price for {model}"
