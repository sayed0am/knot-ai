# Skills

A skill is a chunk of instructions an agent can pull into context on demand,
instead of paying for it in every system prompt. An agent (or bundle) lists
its skills briefly in its system prompt — id, name, one-line description —
and the model decides when it actually needs the full text, at which point
it calls the framework-provided `load_skill` tool to fetch it. See
[`examples/fleet/agents/support/skills/refund-policy`](../examples/fleet/agents/support/skills/refund-policy)
for a complete, working example.

## Directory and file layout

```
skills/<skill_id>/
  SKILL.md
  ...any other supporting files the skill's body wants to reference...
```

`<skill_id>` is the directory name and must match the same
`^[a-z][a-z0-9_-]*$` pattern as every other id in a fleet. A
`skills/<id>/` directory without a `SKILL.md` inside it is a compile
*warning* (skipped, not fatal) — everything else next to `SKILL.md` (images,
reference docs, additional markdown files) is yours to organize; the
framework only ever reads `SKILL.md` itself, but the directory is preserved
on disk under the skill's recorded `path`, so a skill's body is free to
describe or point at sibling files by relative path if your own tooling or
the agent's own instructions know to look for them there.

## The `SKILL.md` format

```markdown
---
name: Refund Policy
description: How to evaluate refund eligibility and process a refund request.
---

# Refund Policy

... the full body, in markdown, handed back verbatim by load_skill ...
```

- **Frontmatter** is a YAML mapping between two `---` delimiter lines at the
  very top of the file.
- **`name`** and **`description`** are the only required frontmatter keys,
  and both must be non-empty. Missing either (or a missing/malformed
  frontmatter block entirely) fails that one skill at compile time — it
  never brings down the whole agent or bundle by itself.
- **Extra frontmatter keys are tolerated and ignored.** This is a deliberate
  compatibility note: a `SKILL.md` file authored for another agent-skills
  tool — with its own extra frontmatter fields (tags, version pins,
  tool-specific metadata, etc.) — loads here unmodified, as long as it has
  `name` and `description`. You do not need to strip anything out to reuse
  an existing skill library.
- **The body** is everything after the closing `---`, with only leading and
  trailing blank lines stripped — no other transformation. Whatever markdown
  you write is exactly what the model receives when it loads the skill.

## Where a skill can live

A skill belongs either directly to an agent (`agents/<id>/skills/<skill>/`)
or to a bundle (`shared/<bundle>/skills/<skill>/`), reaching every agent
that `use:`s that bundle. Skill ids collide (a hard compile error) exactly
like tool names do — an agent's own skill and a bundle's skill of the same
id, or two bundles' skills of the same id pulled in by the same agent, must
be renamed to coexist.

## Runtime behavior

An agent only gets the `load_skill` tool at all if it ends up with at least
one skill (its own, plus whatever its `use:`d bundles contribute) — an
agent with no skills has no `load_skill` tool and no skill-listing block in
its system prompt.

When present, `load_skill`'s parameter schema is a strict enum of that
agent's exact skill ids (`{"skill": {"type": "string", "enum": [...]}}`) —
the model can only ever ask for a real skill by id, never an arbitrary
string. Its system-prompt listing looks like:

```
Available skills (call load_skill with the id to load full instructions):
- refund-policy: Refund Policy - How to evaluate refund eligibility and process a refund request.
```

Calling `load_skill` with an id that isn't in that set doesn't crash the
run — it comes back as an ordinary tool-error result naming every available
id, so the model can self-correct on its next turn. A successful call
returns the skill's body text verbatim, as one text content block — nothing
is summarized or reformatted, though like any other tool result it is still
subject to the agent's `limits.max_result_bytes`, if one is configured (see
[`docs/authoring-agents.md`](authoring-agents.md)).

`load_skill` is marked idempotent — loading the same skill twice in a
session is always safe and always returns the same text.
