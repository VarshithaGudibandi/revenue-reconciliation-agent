"""The Gemini and OpenAI-compatible clients, against a fake server, and how a provider is chosen."""

import json
from collections import Counter
from typing import Any

import httpx
import pytest

from recon.evaluation.explanation_eval import explain_inputs
from recon.evaluation.llm_cli import StopOnQuota
from recon.llm.client import Completion, LlmError, LlmRefusal, QuotaExhausted
from recon.llm.providers import (
    AnthropicJson,
    GeminiJson,
    OpenAICompatibleJson,
    find_key,
    live_client,
    model_for,
)

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}


class Server:
    """Answers each request with the next of a list of responses, and keeps the requests."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def body(self, index: int = 0) -> Any:
        return json.loads(self.requests[index].content)


def gemini_answer(text: str = '{"a": 1}', reason: str = "STOP", **extra: Any) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {"parts": [{"text": "thinking…", "thought": True}, {"text": text}]},
                    "finishReason": reason,
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 100,
                "candidatesTokenCount": 20,
                "thoughtsTokenCount": 30,
            },
            "modelVersion": "gemini-3.1-flash-lite",
            **extra,
        },
    )


def gemini(server: Server, waits: list[float] | None = None) -> GeminiJson:
    sleep = waits.append if waits is not None else (lambda _: None)
    return GeminiJson(model="gemini-3.1-flash-lite", key="k", http=server.client(), sleep=sleep)


def test_gemini_is_asked_for_json_constrained_by_the_schema() -> None:
    server = Server(gemini_answer())

    completion = gemini(server).complete("system text", "user text", SCHEMA)

    request = server.requests[0]
    assert request.url.path.endswith("/models/gemini-3.1-flash-lite:generateContent")
    assert request.headers["x-goog-api-key"] == "k"
    assert server.body() == {
        "systemInstruction": {"parts": [{"text": "system text"}]},
        "contents": [{"role": "user", "parts": [{"text": "user text"}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": SCHEMA},
    }
    # The thought is not part of the answer, but its tokens are billed as output.
    assert completion == Completion('{"a": 1}', "gemini-3.1-flash-lite", 100, 50)


def test_gemini_waits_as_long_as_a_rate_limit_asks_then_tries_again() -> None:
    limited = httpx.Response(
        429,
        json={
            "error": {
                "details": [
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}
                ]
            }
        },
    )
    waits: list[float] = []
    server = Server(limited, httpx.Response(503), gemini_answer())

    completion = gemini(server, waits).complete("s", "u", SCHEMA)

    assert completion.text == '{"a": 1}'
    assert waits == [7.0, 16.0]


def test_gemini_gives_up_after_a_few_refusals_and_at_once_on_a_bad_request() -> None:
    server = Server(*[httpx.Response(429)] * 4)
    with pytest.raises(QuotaExhausted, match="4 tries"):
        gemini(server).complete("s", "u", SCHEMA)
    assert len(server.requests) == 4

    server = Server(*[httpx.Response(503)] * 4)
    with pytest.raises(LlmError, match="503"):
        gemini(server).complete("s", "u", SCHEMA)
    assert len(server.requests) == 4

    server = Server(httpx.Response(400, json={"error": {"message": "bad schema"}}))
    with pytest.raises(LlmError, match="400"):
        gemini(server).complete("s", "u", SCHEMA)
    assert len(server.requests) == 1


def test_a_daily_quota_fails_at_once_instead_of_waiting_hours() -> None:
    used_up = httpx.Response(429, json={"error": {"details": [{"retryDelay": "11386s"}]}})
    waits: list[float] = []
    server = Server(used_up)

    with pytest.raises(QuotaExhausted, match="190 minutes"):
        gemini(server, waits).complete("s", "u", SCHEMA)
    assert waits == []
    assert len(server.requests) == 1


def test_gemini_blocked_cut_off_or_empty_answers_raise() -> None:
    blocked = httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
    with pytest.raises(LlmRefusal):
        gemini(Server(blocked)).complete("s", "u", SCHEMA)
    with pytest.raises(LlmRefusal):
        gemini(Server(gemini_answer(reason="SAFETY"))).complete("s", "u", SCHEMA)
    with pytest.raises(LlmError, match="token limit"):
        gemini(Server(gemini_answer(reason="MAX_TOKENS"))).complete("s", "u", SCHEMA)
    with pytest.raises(LlmError, match="no text"):
        gemini(Server(gemini_answer(text=""))).complete("s", "u", SCHEMA)
    with pytest.raises(LlmError, match="no answer"):
        gemini(Server(httpx.Response(200, json={}))).complete("s", "u", SCHEMA)


def openai_answer(content: str | None = '{"a": 1}', reason: str = "stop", **message: Any) -> Any:
    return httpx.Response(
        200,
        json={
            "model": "some-model",
            "choices": [{"message": {"content": content, **message}, "finish_reason": reason}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 12},
        },
    )


def test_an_openai_compatible_server_is_asked_for_a_strict_schema() -> None:
    server = Server(openai_answer())
    client = OpenAICompatibleJson(
        model="some-model", key="k", base_url="http://local:11434/v1/", http=server.client()
    )

    completion = client.complete("system text", "user text", SCHEMA)

    request = server.requests[0]
    assert str(request.url) == "http://local:11434/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer k"
    assert server.body()["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "answer", "schema": SCHEMA, "strict": True},
    }
    assert server.body()["messages"][0] == {"role": "system", "content": "system text"}
    assert completion == Completion('{"a": 1}', "some-model", 80, 12)


def test_an_openai_compatible_refusal_or_cut_off_raises() -> None:
    def ask(response: httpx.Response) -> Completion:
        client = OpenAICompatibleJson(model="m", key="k", http=Server(response).client())
        return client.complete("s", "u", SCHEMA)

    with pytest.raises(LlmRefusal):
        ask(openai_answer(content=None, refusal="no"))
    with pytest.raises(LlmRefusal):
        ask(openai_answer(reason="content_filter"))
    with pytest.raises(LlmError, match="token limit"):
        ask(openai_answer(reason="length"))
    with pytest.raises(LlmError, match="no text"):
        ask(openai_answer(content=""))


def test_a_key_is_found_in_the_provider_s_usual_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert find_key("gemini") is None
    monkeypatch.setenv("GOOGLE_API_KEY", "from-google")
    assert find_key("gemini") == "from-google"
    monkeypatch.setenv("GEMINI_API_KEY", "from-gemini")
    assert find_key("gemini") == "from-gemini"
    assert find_key("gemini", "configured") == "configured"
    assert find_key("openai") is None


def test_each_provider_gets_its_client_and_default_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert isinstance(live_client("gemini"), GeminiJson)
    assert live_client("gemini").model == "gemini-3.1-flash-lite"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert isinstance(live_client("anthropic"), AnthropicJson)

    client = live_client("openai", model="local-model", key="k", base_url="http://x/v1")
    assert isinstance(client, OpenAICompatibleJson)


def test_a_missing_key_model_or_provider_is_a_clear_error() -> None:
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        live_client("gemini")
    with pytest.raises(ValueError, match="RECON_LLM_MODEL"):
        model_for("openai")
    with pytest.raises(ValueError, match="unknown provider"):
        live_client("mistery")


def test_an_evaluation_stops_when_the_quota_is_used_up_rather_than_scoring_a_partial_run() -> None:
    class UsedUp:
        def complete(self, system: str, user: str, schema: dict[str, Any]) -> Completion:
            raise QuotaExhausted("the quota is used up")

    with pytest.raises(SystemExit, match="partial run"):
        StopOnQuota(UsedUp()).complete("s", "u", SCHEMA)


def test_the_explanation_evaluation_shows_the_model_each_kind_of_case_under_aliases() -> None:
    inputs = explain_inputs(seed=0, size=200, per_kind=2)

    kinds = Counter(inp.kind for inp in inputs)
    assert len(kinds) >= 6
    assert max(kinds.values()) == 2
    for inp in inputs:
        assert [r.ref for r in inp.records] == [f"R{i}" for i in range(1, len(inp.records) + 1)]
        assert all(r.amount is not None for r in inp.records)
        if inp.proposed_ref is not None:
            assert inp.proposed_ref in inp.ids()
