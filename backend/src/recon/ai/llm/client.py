"""The interface every language model client meets, and recorded answers.

A client takes a system prompt, a user message and a JSON schema, and returns JSON text that
matches the schema. The providers live in ``recon.ai.llm.providers``. ``RecordingJson`` wraps a
client and saves every answer to a file; ``ReplayJson`` serves those saved answers and nothing
else. That makes an evaluation repeatable for someone with no API key: they can replay what was
recorded, but cannot ask a new question.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class LlmError(RuntimeError):
    """The model could not give a usable answer."""


class LlmRefusal(LlmError):
    """The model or its safety classifiers declined the request."""


class QuotaExhausted(LlmError):
    """The provider's quota is used up for longer than is worth waiting, such as a free tier's
    requests for the day."""


class CassetteMiss(LlmError):
    """A replayed run asked something that was not recorded."""


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class JsonModel(Protocol):
    def complete(self, system: str, user: str, schema: dict[str, Any]) -> Completion: ...


def request_key(system: str, user: str, schema: dict[str, Any]) -> str:
    """Identifies a request. The model is not part of it: a cassette holds the answers of one
    model, in a file named for it."""
    payload = json.dumps(
        {"system": system, "user": user, "schema": schema}, sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _read(path: Path) -> dict[str, Completion]:
    answers: dict[str, Completion] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                answers[row["key"]] = Completion(
                    row["text"], row["model"], row["input_tokens"], row["output_tokens"]
                )
    return answers


class ReplayJson:
    """Serves answers recorded earlier, keyed by the exact request."""

    def __init__(self, path: Path) -> None:
        self._answers = _read(path)

    def __len__(self) -> int:
        return len(self._answers)

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> Completion:
        found = self._answers.get(request_key(system, user, schema))
        if found is None:
            raise CassetteMiss(
                "this request was not recorded; give the app an API key to run it live "
                "and record the answer"
            )
        return found


class RecordingJson:
    """Calls another client and appends each answer to a file, once per distinct request."""

    def __init__(self, inner: JsonModel, path: Path) -> None:
        self._inner = inner
        self._path = path
        self._answers = _read(path)

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> Completion:
        key = request_key(system, user, schema)
        if key in self._answers:
            return self._answers[key]
        completion = self._inner.complete(system, user, schema)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "key": key,
            "model": completion.model,
            "text": completion.text,
            "input_tokens": completion.input_tokens,
            "output_tokens": completion.output_tokens,
        }
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        self._answers[key] = completion
        return completion
