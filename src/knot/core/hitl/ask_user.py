"""The framework-floor ``ask_user`` tool.

Execute-less by design (``execute_fn=None``): a call to it is never run —
the existing execute-less path in ``knot.core.loop`` parks the run with a
``question``-kind ``PendingInputRequest`` instead, exactly as it would for
any other tool with no executor. ``knot.core.hitl.resume.resolve_inputs``
answers it with an ``AnswerResponse``.

Unlike ``load_skill`` (present only when an agent has skills), ``ask_user``
is unconditional: every compiled agent gets it (see the framework-floor
insertion point in ``knot.authoring.compile``).
"""

from __future__ import annotations

from knot.core.tools import AgentTool

ASK_USER_TOOL_NAME = "ask_user"

_PARAMETERS = {
    "type": "object",
    "properties": {
        "question": {
            "type": "string",
            "description": "The question to put to the human user.",
        },
        "options": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional suggested answers to offer the human user.",
        },
    },
    "required": ["question"],
    "additionalProperties": False,
}

_DESCRIPTION = (
    "Ask the human user a question when you need information that only they "
    "can provide (a decision, a missing fact, a preference). Calling this "
    "tool pauses the run until the human answers; the answer is returned as "
    "this call's result on the next turn."
)


def build_ask_user_tool() -> AgentTool:
    """Build the framework-floor ``ask_user`` tool."""
    return AgentTool(
        name=ASK_USER_TOOL_NAME,
        description=_DESCRIPTION,
        parameters=_PARAMETERS,
        execute_fn=None,
    )


__all__ = ["ASK_USER_TOOL_NAME", "build_ask_user_tool"]
