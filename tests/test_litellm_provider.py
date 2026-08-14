"""Tests for the LiteLLM adapter's import guard and streaming behavior.

The subprocess tests run in an isolated ``uv run python -c`` process so a
prior import elsewhere in the test session (or litellm being genuinely
installed as an optional extra) can't mask a regression.
"""

from __future__ import annotations

import subprocess
import sys
import types
from collections.abc import AsyncIterator
from json import dumps
from pathlib import Path

import pytest

from knot.providers import litellm as litellm_adapter
from knot.providers.events import AssistantDoneEvent
from knot.providers.messages import UserMessage

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_importing_knot_providers_never_imports_litellm() -> None:
    code = "import sys, knot.providers; print('litellm' in sys.modules)"
    result = subprocess.run(
        ["uv", "run", "python", "-c", code],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        check=True,
    )
    assert result.stdout.strip() == "False", result.stderr


def test_importing_litellm_adapter_module_itself_does_not_import_litellm() -> None:
    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "-c",
            "import sys, knot.providers.litellm; print('litellm' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        check=True,
    )
    assert result.stdout.strip() == "False", result.stderr


def test_missing_litellm_raises_clear_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(litellm_adapter, "_litellm_module", None)
    monkeypatch.setitem(sys.modules, "litellm", None)  # forces `import litellm` to raise
    with pytest.raises(ImportError, match=r"pip install 'knot-ai\[litellm\]'"):
        litellm_adapter.LiteLLMProvider()


class _FakeStream:
    """A minimal async-iterable mimicking litellm's streaming response object."""

    def __init__(self, chunks: list[dict]) -> None:
        self._chunks = chunks

    def __aiter__(self) -> AsyncIterator[dict]:
        return self._iterator()

    async def _iterator(self) -> AsyncIterator[dict]:
        for chunk in self._chunks:
            yield chunk


def _install_fake_litellm(monkeypatch: pytest.MonkeyPatch, acompletion) -> None:
    fake_module = types.ModuleType("litellm")
    fake_module.acompletion = acompletion  # type: ignore[attr-defined]
    monkeypatch.setattr(litellm_adapter, "_litellm_module", None)
    monkeypatch.setitem(sys.modules, "litellm", fake_module)


async def _collect(stream: AsyncIterator[object]) -> list[object]:
    return [event async for event in stream]


async def test_provider_constructs_and_streams_when_litellm_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_kwargs: dict = {}

    async def fake_acompletion(**kwargs):
        captured_kwargs.update(kwargs)
        return _FakeStream(
            [
                {"choices": [{"index": 0, "delta": {"content": "Hel"}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": None}]},
                {
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                },
            ]
        )

    _install_fake_litellm(monkeypatch, fake_acompletion)

    provider = litellm_adapter.LiteLLMProvider()
    events = await _collect(
        provider.stream_response(
            model="gpt-4o-mini",
            system="You are helpful.",
            messages=[UserMessage(content="hi")],
            tools=[],
        )
    )

    assert [event.type for event in events] == [
        "start",
        "text_start",
        "text_delta",
        "text_delta",
        "text_end",
        "done",
    ]
    done = events[-1]
    assert isinstance(done, AssistantDoneEvent)
    assert done.message.text == "Hello"
    assert done.reason == "stop"
    assert done.message.usage.total_tokens == 5

    assert captured_kwargs["model"] == "gpt-4o-mini"
    assert captured_kwargs["messages"][0] == {"role": "system", "content": "You are helpful."}
    assert captured_kwargs["stream"] is True


async def test_provider_streams_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_acompletion(**kwargs):
        return _FakeStream(
            [
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_1",
                                        "function": {"name": "get_weather", "arguments": ""},
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"arguments": dumps({"city": "nyc"})}}
                                ]
                            },
                            "finish_reason": None,
                        }
                    ]
                },
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            ]
        )

    _install_fake_litellm(monkeypatch, fake_acompletion)
    provider = litellm_adapter.LiteLLMProvider()
    events = await _collect(
        provider.stream_response(
            model="gpt-4o-mini", system="s", messages=[UserMessage(content="hi")], tools=[]
        )
    )

    done = events[-1]
    assert isinstance(done, AssistantDoneEvent)
    assert done.reason == "toolUse"
    assert done.message.tool_calls[0].name == "get_weather"
    assert done.message.tool_calls[0].arguments == {"city": "nyc"}
