"""Human-in-the-loop: approval policies, the resume protocol, and ``ask_user``.

One pause/resume protocol serves both tool approvals and questions. A
parked session holds nothing in memory: everything needed to resume lives
in the durable entry log (``knot.core.session``), and this package is the
sole write path while a session is parked. See ``knot.core.hitl.resume``
for the resolve/repair protocol and ``knot.core.hitl.policies`` for how a
tool name maps to an approval decision.
"""

# ruff: noqa: F401 - this module intentionally defines the public facade

from .ask_user import ASK_USER_TOOL_NAME, build_ask_user_tool
from .policies import ApprovalPolicy, CustomPolicy, NamedPolicy, approved_tools, build_decision_hook
from .resume import (
    AnswerResponse,
    ApproveResponse,
    CrashWindow,
    CrashWindowReport,
    DenyResponse,
    RepairReport,
    ResolveOutcome,
    current_state,
    detect_crash_windows,
    expire_pending,
    is_ready_to_continue,
    operator_skip,
    repair_crash_windows,
    resolve_inputs,
    write_child_completion,
)

__all__ = [name for name in globals() if not name.startswith("_")]
