"""Tool calling for each provider against stand-ins for their APIs, and recorded turns."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from recon.ai.llm.client import CassetteMiss, LlmError
from recon.ai.llm.providers import AnthropicJson, GeminiJson, OpenAICompatibleJson
from recon.ai.llm.tools import (
    Message,
    RecordingSteps,
    ReplaySteps,
    Step,
    Tool,
    ToolCall,
    ToolResults,
    step_key,
)

ADD = Tool("add", "Add two numbers.", {"type": "object", "properties": {"a": {"type": "integer"}}})


class Server:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.bodies: list[Any] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return self.responses.pop(0)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def test_gemini_must_call_a_tool_and_gets_its_own_turn_back_unchanged() -> None:
    signed = {"functionCall": {"name": "add", "args": {"a": 1}}, "thoughtSignature": "sig"}
    server = Server(
        httpx.Response(200, json={"candidates": [{"content": {"parts": [signed]}}]}),
        httpx.Response(200, json={"candidates": [{"content": {"parts": [signed]}}]}),
    )
    model = GeminiJson(model="m", key="k", http=server.client())

    first = model.step("sys", ["question"], [ADD])
    model.step("sys", ["question", first, ToolResults(((first.calls[0], {"sum": 1}),))], [ADD])

    assert first.calls == (ToolCall("add", {"a": 1}),)
    body = server.bodies[0]
    assert body["toolConfig"] == {"functionCallingConfig": {"mode": "ANY"}}
    assert body["tools"][0]["functionDeclarations"][0]["parametersJsonSchema"] == ADD.parameters
    second = server.bodies[1]["contents"]
    assert second[1] == {"role": "model", "parts": [signed]}
    assert second[2] == {
        "role": "user",
        "parts": [{"functionResponse": {"name": "add", "response": {"sum": 1}}}],
    }


def test_a_turn_without_a_tool_call_is_an_error() -> None:
    text_only = {"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}
    model = GeminiJson(
        model="m", key="k", http=Server(httpx.Response(200, json=text_only)).client()
    )
    with pytest.raises(LlmError, match="without calling a tool"):
        model.step("sys", ["q"], [ADD])


def test_an_openai_compatible_server_is_required_to_call_a_tool() -> None:
    call = {"id": "c1", "type": "function", "function": {"name": "add", "arguments": '{"a": 2}'}}
    answer = {"choices": [{"message": {"content": None, "tool_calls": [call]}}], "model": "m"}
    server = Server(httpx.Response(200, json=answer), httpx.Response(200, json=answer))
    model = OpenAICompatibleJson(model="m", key="k", http=server.client())

    first = model.step("sys", ["q"], [ADD])
    model.step("sys", ["q", first, ToolResults(((first.calls[0], {"sum": 2}),))], [ADD])

    assert first.calls == (ToolCall("add", {"a": 2}, "c1"),)
    assert server.bodies[0]["tool_choice"] == "required"
    sent = server.bodies[1]["messages"]
    assert sent[2]["tool_calls"] == [call]
    assert sent[3] == {"role": "tool", "tool_call_id": "c1", "content": '{"sum": 2}'}


def test_anthropic_tool_use_blocks_become_calls_and_go_back_as_they_came() -> None:
    block = SimpleNamespace(
        type="tool_use",
        id="t1",
        name="add",
        input={"a": 3},
        model_dump=lambda **_: {"type": "tool_use", "id": "t1", "name": "add", "input": {"a": 3}},
    )
    response = SimpleNamespace(
        stop_reason="tool_use",
        content=[block],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        model="claude",
    )
    calls: list[dict[str, Any]] = []

    def create(**arguments: Any) -> SimpleNamespace:
        calls.append(arguments)
        return response

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    model = AnthropicJson(client=client)

    first = model.step("sys", ["q"], [ADD])
    model.step("sys", ["q", first, ToolResults(((first.calls[0], {"sum": 3}),))], [ADD])

    assert first.calls == (ToolCall("add", {"a": 3}, "t1"),)
    assert calls[0]["tools"][0]["input_schema"] == ADD.parameters
    assert calls[1]["messages"][1] == {"role": "assistant", "content": first.raw}
    assert calls[1]["messages"][2]["content"][0]["tool_use_id"] == "t1"


class Scripted:
    def __init__(self) -> None:
        self.asked = 0

    def step(self, system: str, messages: Any, tools: Any) -> Step:
        self.asked += 1
        return Step((ToolCall("add", {"a": len(messages)}),), [{"turn": len(messages)}], "m")


def test_turns_are_recorded_once_and_replayed_by_the_whole_conversation(tmp_path: Path) -> None:
    cassette = tmp_path / "steps.jsonl"
    live = Scripted()
    recording = RecordingSteps(live, cassette)

    first = recording.step("sys", ["q"], [ADD])
    assert recording.step("sys", ["q"], [ADD]) == first
    later: list[Message] = ["q", first, ToolResults(((first.calls[0], {"sum": 1}),))]
    second = recording.step("sys", later, [ADD])
    assert live.asked == 2

    replay = ReplaySteps(cassette)
    assert replay.step("sys", ["q"], [ADD]) == first
    assert replay.step("sys", later, [ADD]) == second
    with pytest.raises(CassetteMiss):
        replay.step("sys", ["another question"], [ADD])
    # A different result for the same call is a different conversation.
    changed: list[Message] = ["q", first, ToolResults(((first.calls[0], {"sum": 99}),))]
    assert step_key("sys", changed, [ADD]) != step_key("sys", later, [ADD])
