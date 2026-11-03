"""Letting a model call tools, the same way for every provider, with recorded steps.

A conversation is a list of messages: the user's text, the model's turns, and the results of
the tools it called. Each call to ``ToolModel.step`` sends the conversation so far and returns
the model's next turn, which always calls at least one tool. The caller runs the tools and
asks again, until the model calls the tool that ends the task.

A model's turn is kept as the provider returned it (``Step.raw``) and sent back unchanged,
because some providers attach signed reasoning to a turn and refuse a conversation in which
it was altered.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from recon.llm.client import CassetteMiss


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    # A JSON Schema object for the arguments.
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = ""


@dataclass(frozen=True)
class Step:
    """One turn of the model: the tools it called, and the turn as the provider sent it."""

    calls: tuple[ToolCall, ...]
    raw: Any
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class ToolResults:
    """What the tools of one turn returned, in the order they were called."""

    results: tuple[tuple[ToolCall, dict[str, Any]], ...]


# The user's text, a turn of the model, or the results of its tools.
Message = str | Step | ToolResults


class ToolModel(Protocol):
    def step(self, system: str, messages: Sequence[Message], tools: Sequence[Tool]) -> Step: ...


def _plain(message: Message) -> dict[str, Any]:
    if isinstance(message, str):
        return {"user": message}
    if isinstance(message, Step):
        return {"model": message.raw}
    return {"results": [[asdict(call), content] for call, content in message.results]}


def step_key(system: str, messages: Sequence[Message], tools: Sequence[Tool]) -> str:
    """Identifies a request: the whole conversation so far and the tools on offer."""
    payload = json.dumps(
        {
            "system": system,
            "messages": [_plain(m) for m in messages],
            "tools": [asdict(t) for t in tools],
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _row(key: str, step: Step) -> dict[str, Any]:
    return {
        "key": key,
        "kind": "step",
        "model": step.model,
        "raw": step.raw,
        "calls": [asdict(c) for c in step.calls],
        "input_tokens": step.input_tokens,
        "output_tokens": step.output_tokens,
    }


def _read_steps(path: Path) -> dict[str, Step]:
    steps: dict[str, Step] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            steps[row["key"]] = Step(
                calls=tuple(ToolCall(**c) for c in row["calls"]),
                raw=row["raw"],
                model=row["model"],
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
            )
    return steps


class ReplaySteps:
    """Serves turns recorded earlier, keyed by the exact conversation."""

    def __init__(self, path: Path) -> None:
        self._steps = _read_steps(path)

    def __len__(self) -> int:
        return len(self._steps)

    def step(self, system: str, messages: Sequence[Message], tools: Sequence[Tool]) -> Step:
        found = self._steps.get(step_key(system, messages, tools))
        if found is None:
            raise CassetteMiss(
                "this conversation was not recorded; give the app an API key to run it live "
                "and record it"
            )
        return found


class RecordingSteps:
    """Asks another model and appends each turn to a file, once per distinct conversation."""

    def __init__(self, inner: ToolModel, path: Path) -> None:
        self._inner = inner
        self._path = path
        self._steps = _read_steps(path)

    def step(self, system: str, messages: Sequence[Message], tools: Sequence[Tool]) -> Step:
        key = step_key(system, messages, tools)
        if key in self._steps:
            return self._steps[key]
        step = self._inner.step(system, messages, tools)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_row(key, step), ensure_ascii=False, sort_keys=True) + "\n")
        self._steps[key] = step
        return step
