"""The language model providers, each behind the same ``JsonModel`` interface.

Gemini is the default because its API has a free tier. Anthropic is called through its SDK, and
anything that speaks the OpenAI chat-completions protocol (OpenAI itself, or a gateway or local
server that copies it) through ``OpenAICompatibleJson``. Every client asks for a structured
output: JSON constrained by the schema the caller gives, never free text to be parsed.
"""

import json
import os
import re
import time
from collections.abc import Callable, Sequence
from typing import Any, Literal, get_args

import anthropic
import httpx

from recon.ai.llm.client import Completion, LlmError, LlmRefusal, QuotaExhausted
from recon.ai.llm.tools import Message, Step, Tool, ToolCall

Provider = Literal["gemini", "anthropic", "openai"]
PROVIDERS: tuple[str, ...] = get_args(Provider)

# Where each provider's key is looked for when RECON_LLM_API_KEY is not set: the names its own
# tools use, so a key already in the environment is picked up.
KEY_VARIABLES: dict[str, tuple[str, ...]] = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "openai": ("OPENAI_API_KEY",),
}
# A versioned name rather than a "latest" alias. Even so, Google may answer with a newer model
# (asked for gemini-3.5-flash, it answered as gemini-3.6-flash), so a completion records the
# model the answer reports, not the one asked for. An OpenAI-compatible server has no default:
# its models are whatever it serves.
DEFAULT_MODELS: dict[str, str] = {
    # Flash-Lite rather than Flash: on the free tier Flash allowed 20 requests a day, which one
    # evaluation uses up; Flash-Lite answered the mapping task as well in a third of the time.
    "gemini": "gemini-3.1-flash-lite",
    "anthropic": "claude-opus-5-5",
}

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"
OPENAI_URL = "https://api.openai.com/v1"
# Statuses worth another try: rate limited, or the service briefly unavailable.
# Ends of a Gemini answer that mean it was withheld, as against cut off or malformed.
GEMINI_REFUSALS = frozenset(
    {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"}
)
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
ATTEMPTS = 4
MAX_WAIT_SECONDS = 60.0


def find_key(provider: str, configured: str | None = None) -> str | None:
    """The key to use: the configured one, else the first of the provider's usual variables."""
    if configured:
        return configured
    return next(
        (os.environ[name] for name in KEY_VARIABLES[provider] if os.environ.get(name)), None
    )


def model_for(provider: str, configured: str | None = None) -> str:
    if configured:
        return configured
    if provider not in DEFAULT_MODELS:
        raise ValueError(f"RECON_LLM_MODEL must name a model for the {provider} provider")
    return DEFAULT_MODELS[provider]


def live_client(
    provider: str,
    *,
    model: str | None = None,
    key: str | None = None,
    base_url: str | None = None,
    effort: str = "low",
    fallbacks: bool = True,
) -> "GeminiJson | AnthropicJson | OpenAICompatibleJson":
    """A client for the provider, with its key looked up if none is given."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}; choose one of {', '.join(PROVIDERS)}")
    name = model_for(provider, model)
    found = find_key(provider, key)
    if provider == "anthropic":
        # The SDK reads ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN itself when given nothing.
        client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        return AnthropicJson(model=name, effort=effort, fallbacks=fallbacks, client=client)
    if found is None:
        names = " or ".join(("RECON_LLM_API_KEY", *KEY_VARIABLES[provider]))
        raise ValueError(f"the {provider} provider needs a key in {names}")
    if provider == "gemini":
        return GeminiJson(model=name, key=found, base_url=base_url or GEMINI_URL)
    return OpenAICompatibleJson(model=name, key=found, base_url=base_url or OPENAI_URL)


def _retry_after(response: httpx.Response) -> float | None:
    """The wait a refusal asks for: a Retry-After header, or Google's RetryInfo detail."""
    header = response.headers.get("retry-after")
    if header and header.replace(".", "", 1).isdigit():
        return float(header)
    try:
        details = response.json().get("error", {}).get("details", [])
    except ValueError:
        return None
    for detail in details if isinstance(details, list) else []:
        delay = detail.get("retryDelay") if isinstance(detail, dict) else None
        if isinstance(delay, str) and (match := re.fullmatch(r"([\d.]+)s", delay)):
            return float(match.group(1))
    return None


def post_json(
    http: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """POST and return the JSON answer, waiting and retrying while rate limited.

    Free tiers refuse with 429 as soon as a per-minute quota is spent; the answer usually says
    how long to wait. A wait of more than a minute means a daily quota, and fails at once with
    ``QuotaExhausted``, as does anything else that is not a success.
    """
    for attempt in range(ATTEMPTS):
        try:
            response = http.post(url, headers=headers, json=body)
        except httpx.TransportError as error:
            if attempt == ATTEMPTS - 1:
                raise LlmError(f"the API could not be reached: {error}") from error
            sleep(min(2.0**attempt, MAX_WAIT_SECONDS))
            continue
        wait = _retry_after(response) if response.status_code == 429 else None
        if wait is not None and wait > MAX_WAIT_SECONDS:
            raise QuotaExhausted(
                f"the quota is used up and resets in about {wait / 60:.0f} minutes: "
                f"{response.text[:300]}"
            )
        if response.status_code in RETRY_STATUSES and attempt < ATTEMPTS - 1:
            # An overloaded service ("high demand") needs longer than a rate limit does.
            backoff = 2.0 ** (attempt + 1) * (1 if response.status_code == 429 else 4)
            sleep(min(wait or backoff, MAX_WAIT_SECONDS))
            continue
        if response.status_code == 429:
            raise QuotaExhausted(f"still rate limited after {ATTEMPTS} tries")
        if response.status_code != 200:
            raise LlmError(f"the API answered {response.status_code}: {response.text[:300]}")
        answer: dict[str, Any] = response.json()
        return answer
    raise LlmError("the API kept refusing the request")  # pragma: no cover - loop always returns


class GeminiJson:
    """Asks a Gemini model for JSON that matches a schema, over the REST API.

    The schema goes in ``responseJsonSchema``, which takes JSON Schema as it is, rather than
    ``responseSchema``, which takes Google's narrower OpenAPI subset.
    """

    def __init__(
        self,
        *,
        model: str,
        key: str,
        base_url: str = GEMINI_URL,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self._key = key
        self._url = f"{base_url.rstrip('/')}/models/{model}:generateContent"
        self._http = http or httpx.Client(timeout=180.0)
        self._sleep = sleep

    def _generate(self, body: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """The parts of the answer, and the whole answer, after checking how it ended."""
        answer = post_json(
            self._http,
            self._url,
            headers={"x-goog-api-key": self._key},
            body=body,
            sleep=self._sleep,
        )
        blocked = answer.get("promptFeedback", {}).get("blockReason")
        if blocked:
            raise LlmRefusal(f"the request was blocked: {blocked}")
        candidates = answer.get("candidates") or []
        if not candidates:
            raise LlmError("the model returned no answer")
        candidate = candidates[0]
        reason = candidate.get("finishReason")
        if reason == "MAX_TOKENS":
            raise LlmError("the answer was cut off at the token limit")
        if reason in GEMINI_REFUSALS:
            raise LlmRefusal(f"the model stopped without answering: {reason}")
        if reason not in (None, "STOP"):
            raise LlmError(f"the model stopped without answering: {reason}")
        parts: list[dict[str, Any]] = candidate.get("content", {}).get("parts", [])
        return parts, answer

    def _usage(self, answer: dict[str, Any]) -> tuple[str, int, int]:
        usage = answer.get("usageMetadata", {})
        return (
            answer.get("modelVersion", self.model),
            usage.get("promptTokenCount", 0),
            # Thinking is billed as output, so it is counted with the answer.
            usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0),
        )

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> Completion:
        parts, answer = self._generate(
            {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "responseJsonSchema": schema,
                },
            }
        )
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text:
            raise LlmError("the model returned no text")
        model, input_tokens, output_tokens = self._usage(answer)
        return Completion(text, model, input_tokens, output_tokens)

    def step(self, system: str, messages: Sequence[Message], tools: Sequence[Tool]) -> Step:
        """The model's next turn. It must call a tool: free text is not an answer here."""
        contents: list[dict[str, Any]] = []
        for message in messages:
            if isinstance(message, str):
                contents.append({"role": "user", "parts": [{"text": message}]})
            elif isinstance(message, Step):
                contents.append({"role": "model", "parts": message.raw})
            else:
                contents.append(
                    {"role": "user", "parts": [_gemini_result(r) for r in message.results]}
                )
        parts, answer = self._generate(
            {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": contents,
                "tools": [
                    {
                        "functionDeclarations": [
                            {
                                "name": t.name,
                                "description": t.description,
                                "parametersJsonSchema": t.parameters,
                            }
                            for t in tools
                        ]
                    }
                ],
                "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
            }
        )
        calls = tuple(
            ToolCall(
                name=p["functionCall"]["name"],
                arguments=p["functionCall"].get("args") or {},
                id=p["functionCall"].get("id", ""),
            )
            for p in parts
            if "functionCall" in p
        )
        if not calls:
            raise LlmError("the model answered without calling a tool")
        model, input_tokens, output_tokens = self._usage(answer)
        return Step(calls, parts, model, input_tokens, output_tokens)


def _gemini_result(result: tuple[ToolCall, dict[str, Any]]) -> dict[str, Any]:
    call, content = result
    response: dict[str, Any] = {"name": call.name, "response": content}
    if call.id:
        response["id"] = call.id
    return {"functionResponse": response}


class OpenAICompatibleJson:
    """Asks any server that speaks the OpenAI chat-completions protocol for JSON.

    Strict schema mode is requested; a server that does not support it will say so.
    """

    def __init__(
        self,
        *,
        model: str,
        key: str,
        base_url: str = OPENAI_URL,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self._key = key
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._http = http or httpx.Client(timeout=180.0)
        self._sleep = sleep

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> Completion:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": schema, "strict": True},
            },
        }
        answer = post_json(
            self._http,
            self._url,
            headers={"Authorization": f"Bearer {self._key}"},
            body=body,
            sleep=self._sleep,
        )
        choices = answer.get("choices") or []
        if not choices:
            raise LlmError("the model returned no answer")
        choice = choices[0]
        message = choice.get("message", {})
        if message.get("refusal") or choice.get("finish_reason") == "content_filter":
            raise LlmRefusal("the model declined the request")
        if choice.get("finish_reason") == "length":
            raise LlmError("the answer was cut off at the token limit")
        text = message.get("content")
        if not text:
            raise LlmError("the model returned no text")
        usage = answer.get("usage", {})
        return Completion(
            text=text,
            model=answer.get("model", self.model),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )

    def step(self, system: str, messages: Sequence[Message], tools: Sequence[Tool]) -> Step:
        chat: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for message in messages:
            if isinstance(message, str):
                chat.append({"role": "user", "content": message})
            elif isinstance(message, Step):
                chat.append(message.raw)
            else:
                chat.extend(
                    {"role": "tool", "tool_call_id": call.id, "content": json.dumps(content)}
                    for call, content in message.results
                )
        answer = post_json(
            self._http,
            self._url,
            headers={"Authorization": f"Bearer {self._key}"},
            body={
                "model": self.model,
                "messages": chat,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.parameters,
                        },
                    }
                    for t in tools
                ],
                "tool_choice": "required",
            },
            sleep=self._sleep,
        )
        choices = answer.get("choices") or []
        if not choices:
            raise LlmError("the model returned no answer")
        choice = choices[0]
        message_out = choice.get("message", {})
        if message_out.get("refusal") or choice.get("finish_reason") == "content_filter":
            raise LlmRefusal("the model declined the request")
        if choice.get("finish_reason") == "length":
            raise LlmError("the answer was cut off at the token limit")
        raw_calls = message_out.get("tool_calls") or []
        try:
            calls = tuple(
                ToolCall(
                    name=c["function"]["name"],
                    arguments=json.loads(c["function"].get("arguments") or "{}"),
                    id=c.get("id", ""),
                )
                for c in raw_calls
            )
        except (KeyError, ValueError) as error:
            raise LlmError(f"the model's tool call could not be read: {error}") from error
        if not calls:
            raise LlmError("the model answered without calling a tool")
        raw = {
            "role": "assistant",
            "content": message_out.get("content"),
            "tool_calls": raw_calls,
        }
        usage = answer.get("usage", {})
        return Step(
            calls,
            raw,
            answer.get("model", self.model),
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
        )


class AnthropicJson:
    """Asks Claude for JSON that matches a schema.

    Thinking cannot be switched off on the default model, so the cost of a call is set with
    ``effort``. Forced tool use is not available on it either, which is why the answer shape
    is a structured output rather than a tool call.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODELS["anthropic"],
        effort: str = "low",
        fallbacks: bool = True,
        max_tokens: int = 16000,
        client: Any = None,
    ) -> None:
        if client is None:
            client = anthropic.Anthropic()
        self._client = client
        self.model = model
        self._effort = effort
        self._fallbacks = fallbacks
        self._max_tokens = max_tokens

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> Completion:
        arguments: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {
                "effort": self._effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        }
        try:
            if self._fallbacks:
                response = self._client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **arguments
                )
            else:
                response = self._client.messages.create(**arguments)
        except anthropic.APIError as error:
            raise LlmError(f"the API request failed: {error}") from error

        if response.stop_reason == "refusal":
            raise LlmRefusal("the model declined the request")
        if response.stop_reason == "max_tokens":
            raise LlmError("the answer was cut off at the token limit")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LlmError("the model returned no text")
        return Completion(
            text=text,
            model=getattr(response, "model", self.model),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def step(self, system: str, messages: Sequence[Message], tools: Sequence[Tool]) -> Step:
        """The model's next turn. Forced tool use is not available on the default model, so the
        system prompt has to ask for a tool call, and a turn without one is an error."""
        chat: list[dict[str, Any]] = []
        for message in messages:
            if isinstance(message, str):
                chat.append({"role": "user", "content": message})
            elif isinstance(message, Step):
                chat.append({"role": "assistant", "content": message.raw})
            else:
                chat.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": call.id,
                                "content": json.dumps(content),
                            }
                            for call, content in message.results
                        ],
                    }
                )
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self._max_tokens,
                system=system,
                messages=chat,
                tools=[
                    {"name": t.name, "description": t.description, "input_schema": t.parameters}
                    for t in tools
                ],
                output_config={"effort": self._effort},
            )
        except anthropic.APIError as error:
            raise LlmError(f"the API request failed: {error}") from error
        if response.stop_reason == "refusal":
            raise LlmRefusal("the model declined the request")
        if response.stop_reason == "max_tokens":
            raise LlmError("the answer was cut off at the token limit")
        calls = tuple(
            ToolCall(name=b.name, arguments=dict(b.input), id=b.id)
            for b in response.content
            if b.type == "tool_use"
        )
        if not calls:
            raise LlmError("the model answered without calling a tool")
        return Step(
            calls,
            [b.model_dump(exclude_none=True) for b in response.content],
            getattr(response, "model", self.model),
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
