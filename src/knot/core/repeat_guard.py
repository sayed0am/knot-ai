"""The repeat-tool-call guard: chain tracking and advisory copy.

Pure pieces only — no loop wiring, mirroring the same "pure core, policy in
the loop" split ``knot.core.compaction`` draws relative to
``knot.authoring.runtime``. This module answers three questions: "is this
call the same as the last one?" (``canonical_key``), "has a chain of
identical calls crossed a threshold?" (``RepeatChain``), and "what does the
advisory say?" (``build_advisory``). Everything about *when* a chain resets
or *how* an advisory reaches the model lives in ``knot.core.loop``.
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from knot.providers.messages import UserMessage
from knot.providers.types import JSONValue

#: Wraps every guard-authored advisory's text, the same way
#: ``<compacted-summary>`` marks a compaction summary: providers accept it
#: unchanged as ordinary user text, but it is grep-able and visually
#: distinguishable from words the human user actually wrote (spec:
#: "distinguishable from human words").
ADVISORY_TAG_OPEN = "<repeat-tool-reminder>"
ADVISORY_TAG_CLOSE = "</repeat-tool-reminder>"

#: The first threshold's advisory: short and generic, deliberately close to
#: dsh's wording (see design.md D4) so the nudge reads as familiar framework
#: copy rather than a novel warning the model has to parse from scratch.
FIRST_REMINDER = (
    "You are repeating the exact same tool call with identical arguments. "
    "Carefully analyze the previous result before calling again: if the "
    "task is not complete, try a different approach or different "
    "arguments instead of repeating the call."
)

#: Later thresholds name the tool, the run length, and a bounded preview of
#: the repeated arguments so the model has enough to actually change course.
_DETAILED_TEMPLATE = (
    "You have now called `{tool_name}` with identical arguments {count} "
    "times in a row. Carefully analyze the previous result before calling "
    "again: if the task is not complete, try a different approach or "
    "different arguments, or conclude instead of repeating the call. "
    "Repeated arguments: {preview}"
)


def canonical_key(name: str, arguments: Mapping[str, JSONValue]) -> str:
    """The chain key: tool name plus a deep, property-order-insensitive
    serialization of ``arguments``.

    ``sort_keys=True`` makes two argument objects differing only in
    property order serialize identically; ``separators=(",", ":")`` keeps
    the key compact; ``default=str`` never raises on an odd argument value
    (call arguments are untrusted input, same as
    ``knot.core.hitl.policies`` treats them) — worst case, two
    incomparable values collapse to the same string and the chain merely
    undercounts, it never crashes the loop.
    """
    args_json = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return f"{name}\x00{args_json}"


@dataclass(frozen=True, slots=True)
class RepeatGuardSettings:
    """One run's resolved repeat-guard configuration.

    Mirrors ``knot.authoring.config.RepeatGuardConfig`` field for field —
    that's the author-facing wire schema (validated, ``agent.yaml``-shaped);
    this is the plain dataclass the pure guard and the loop actually pass
    around, so this module never needs to import ``knot.authoring`` (which
    would invert the package layering). See ``knot.core.compaction.
    CompactionSettings`` for the same pattern.
    """

    enabled: bool = True
    thresholds: tuple[int, ...] = (3, 5, 8)
    exclude: tuple[str, ...] = ("ask_user", "load_skill")
    preview_cap: int = 500


def _matches_exclude(pattern: str, qualified_name: str) -> bool:
    """One exclude pattern against one qualified tool name.

    Two independent ways to match, both applied: a ``*``-wildcard glob over
    the full qualified name (``fnmatch``, matching the approval-policy
    naming convention noted in ``knot.core.hitl.policies``), and — for a
    plain pattern with no wildcard — the same ``__``-qualified-suffix rule
    ``policies._resolve_policy`` uses, so ``exclude: [load_skill]`` also
    covers a connection-qualified ``crm__load_skill`` without the author
    needing to spell out ``*__load_skill``.
    """
    if fnmatch.fnmatchcase(qualified_name, pattern):
        return True
    if "*" not in pattern and qualified_name.endswith(f"__{pattern}"):
        return True
    return False


def is_excluded(name: str, exclude: Sequence[str]) -> bool:
    return any(_matches_exclude(pattern, name) for pattern in exclude)


@dataclass(slots=True)
class RepeatChain:
    """One run's consecutive-identical-call tracker.

    ``fired`` remembers every threshold that has already produced an
    advisory *this run* (design D4: "once-per-threshold") — it survives
    ``reset()`` deliberately, so a chain that resets, regrows, and crosses
    the same threshold again does not re-nag; only a brand-new
    ``RepeatChain`` (a brand-new ``run_agent_loop`` call) starts fresh.
    """

    key: str | None = None
    count: int = 0
    fired: set[int] = field(default_factory=set)

    def observe(
        self, name: str, arguments: Mapping[str, JSONValue], settings: RepeatGuardSettings
    ) -> int | None:
        """Record one tracked tool call; return the threshold that just
        fired, or ``None``.

        Excluded tools are transparent: they neither increment nor reset
        (return ``None`` without touching ``key``/``count``). Detection
        always compares the full canonical key; ``settings.preview_cap``
        only bounds advisory text, never the comparison itself.
        """
        if not settings.enabled or is_excluded(name, settings.exclude):
            return None

        key = canonical_key(name, arguments)
        if key == self.key:
            self.count += 1
        else:
            self.key = key
            self.count = 1

        if self.count in settings.thresholds and self.count not in self.fired:
            self.fired.add(self.count)
            return self.count
        return None

    def reset(self) -> None:
        """New non-guard human input: restart the consecutive count.

        Deliberately leaves ``fired`` untouched — see the class docstring.
        """
        self.key = None
        self.count = 0

    def last_call(self) -> tuple[str, str]:
        """``(tool_name, canonical_args_json)`` for the call currently
        tracked, i.e. the one whose observation may have just fired a
        threshold. Only meaningful right after ``observe`` returns
        non-``None``."""
        assert self.key is not None
        name, _, args_json = self.key.partition("\x00")
        return name, args_json


def build_advisory(
    *,
    is_first_threshold: bool,
    tool_name: str,
    count: int,
    canonical_args: str,
    preview_cap: int,
) -> UserMessage:
    """Build the ``<repeat-tool-reminder>``-tagged advisory ``UserMessage``
    for one fired threshold.

    ``is_first_threshold`` selects the short generic nudge (the run's
    lowest configured threshold) versus the detailed, tool-naming template
    every later threshold uses. The args preview is head-truncated at
    ``preview_cap`` characters with an omitted-count marker; detection
    itself (``RepeatChain.observe``) already ran against the untruncated
    ``canonical_args``, so truncation here only ever affects what the model
    reads, never what was compared.
    """
    if is_first_threshold:
        body = FIRST_REMINDER
    else:
        preview = canonical_args
        if len(preview) > preview_cap:
            omitted = len(preview) - preview_cap
            preview = f"{preview[:preview_cap]}... ({omitted} more chars)"
        body = _DETAILED_TEMPLATE.format(tool_name=tool_name, count=count, preview=preview)
    text = f"{ADVISORY_TAG_OPEN}\n{body}\n{ADVISORY_TAG_CLOSE}"
    return UserMessage(content=text)


__all__ = [
    "ADVISORY_TAG_CLOSE",
    "ADVISORY_TAG_OPEN",
    "FIRST_REMINDER",
    "RepeatChain",
    "RepeatGuardSettings",
    "build_advisory",
    "canonical_key",
    "is_excluded",
]
