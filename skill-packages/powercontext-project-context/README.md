# PowerContext Project Context Skill

Restore, maintain, and hand off **durable project context** with
[PowerContext](https://github.com/oceanbase/powercontext), the OceanBase project-memory runtime.

This Skill tells an agent **when** to reach for PowerContext and **which** operation to call, so that a
long-running project keeps its decisions, constraints, handoffs, and review queue without re-deriving them
from scratch in every session.

## What it covers

| Intent | Operation |
| --- | --- |
| Find prior decisions | `pc_search` |
| Inventory or audit Memory | `pc_memory_list` |
| Save, correct, or retire Memory | `pc_remember`, `pc_memory_revise`, `pc_memory_retire` |
| Inspect Memory revisions or statistics | `pc_memory_changes`, `pc_stats` |
| Transfer or resume work | `pc_handoff_current`, `pc_handoff_continue` |
| Inspect and decide review candidates | `pc_review_list`, `pc_review_get`, `pc_review_approve`, `pc_review_reject`, `pc_review_revise` |
| Generate an Experience or Skill candidate | `pc_experience_generate`, `pc_skill_generate` |
| Discover or import external Skills | `pc_external_scan`, `pc_external_list`, `pc_external_resolve`, `pc_external_import` |

Detailed workflows live in `references/`:

- [`references/scope-memory.md`](references/scope-memory.md) — read context, write only on request
- [`references/work-handoff.md`](references/work-handoff.md) — structured transfer and Work Contracts
- [`references/review-publication.md`](references/review-publication.md) — candidate review and external Skills

## Requirements

The Skill drives the PowerContext tools (`pc_*`). They are exposed by the PowerContext server and reach an
agent through its MCP integration.

```bash
pip install powercontext
powercontext server run
```

Then install the Skill into the agent host. For the Pi host:

```bash
powercontext setup pi --source /path/to/powercontext
```

For any other MCP-capable host, point the host at the running PowerContext server and register the
`pc_*` tools.

## Install

With the ModelScope CLI:

```bash
pip install modelscope
modelscope skills add @<owner>/powercontext-project-context
```

## Design notes

- **The Skill is a router, not an executor.** It selects the right PowerContext operation; it never
  invents one, and it never treats a read as an authorization to write.
- **Writes need explicit intent.** Memory saves, handoff commits, and candidate decisions stay human
  actions; the Skill states the exact operation and its authorization boundary.
- **Evidence stays exact.** Citations, Scope, Artifact references, and versions are passed through
  unchanged so a restored decision can still be traced to the evidence that produced it.

## License

Apache-2.0. See the [PowerContext repository](https://github.com/oceanbase/powercontext) for the full
project and its contribution guide.
