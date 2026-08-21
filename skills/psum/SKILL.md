---
name: psum
description: Use when the user asks what they were working on, what they forgot about, what is half-finished, what is stalled or dormant, what has uncommitted or unpushed work, or what is left to do in a project — anything answered from the psum workspace portfolio index, whether through the psum CLI on the machine that holds the workspace or through the exported PORTFOLIO notes in a connected notes vault.
---

# Querying the workspace portfolio

`psum` indexes every project in a workspace and derives, per project: a
status, an attention score with reasons, when it was last worked, open
items, plan checkbox progress, and uncommitted/unpushed git state. This
skill answers questions from that index.

## Pick a backend — probe, do not assume

1. If you can run shell commands **and** `command -v psum` succeeds, use
   **the CLI**.
2. Otherwise use **the vault notes** through the connected notes MCP
   (`PORTFOLIO/PORTFOLIO-DATA.md` and `PORTFOLIO/PORTFOLIO-INDEX.md`).

Both are two-tier: get the compact list of every project first, then pull
detail only for the few that matter. Never read `state/facts.json` directly
— it is ~300 KB and `psum query` exists to avoid that.

## Freshness — state it, every time

Both backends carry the scan time.

- **CLI:** the envelope's `scan_age`. If it exceeds ~12h, say so and offer
  `psum scan` before answering.
- **Vault:** the `**Scanned:**` line. You cannot refresh it from here. Say
  how old it is.

A stale answer that sounds current is this skill's characteristic failure.
The user acts on these answers by opening a project; being wrong about
which one costs them a context switch.

## CLI recipes

```bash
psum query                          # compact JSON for every project
psum query --status mid-flight      # narrow before reading, if the question is narrow
psum query --category NAME
psum query --sort recent --limit 20
psum query --detail PROJECT         # full record: every plan and spec file with counts
```

`--detail` takes a slug, an exact name, or a unique case-insensitive
substring. Filter the compact result yourself for anything the flags do not
cover — that is what the extra fields are for.

**Compact fields:** `slug`, `name`, `category`, `status`, `attention`,
`last_worked`, `age`, `open`, `roadmap`, `plans` (`"checked/total"`),
`dirty`, `unpushed`, `ahead`, `stashes`, `handoff`, `half_plan`,
`redacted`, `path`, `reasons`.

## Vault recipes

- Compact list → read `PORTFOLIO/PORTFOLIO-DATA.md`. Rows are pipe-delimited
  inside a fenced block; the `Columns:` line above it names the fields in
  order. Booleans are `Y`/`-`; multiple reasons join with `; `.
- Detail for one project → read its `#### <name>` section in
  `PORTFOLIO/PORTFOLIO-INDEX.md`, which carries the description.

If either note is missing, the export has not run on the machine that holds
the workspace. Say that. Do not answer from memory of an earlier
conversation — you have no way to tell how far the workspace has moved since.

The vault carries **aggregate** plan progress (`3/21`), not the per-plan
breakdown. Answering "which specific plan steps are unchecked" needs the CLI
— say so rather than guessing.

## What the fields mean

| Status | Meaning |
|---|---|
| `mid-flight` | something was left running: uncommitted work, a HANDOFF, or a partially executed plan |
| `active` | worked recently |
| `stalled` | open items and no recent work |
| `dormant` | quiet for a long time |
| `done` | no open items, clean tree, nothing unpushed, and at least one plan fully ticked |
| `archived` | set by config; never derived |

`attention` combines mid-flight state, a stall term, and open-item pressure.
`reasons` is the human-readable why — quote it rather than re-deriving it.

## Answer discipline

- Name the field behind each claim. "Left mid-flight 9 days ago — 5
  uncommitted files and a HANDOFF.md" beats "looks like you should pick this
  up".
- Lead with `status`, `age` and `reasons` whenever recommending what to pick
  up next.
- `plans: "0/21"` means a plan exists and nothing in it is ticked. That is a
  different situation from `"0/0"`, which means no plan at all. Do not
  conflate them.
- **Redacted projects are counts-only.** `redacted: true` means the name,
  path and contents were deliberately withheld — client work. Refer to such
  a project by its digest identifier, report its counts, and never speculate
  about what it is.
- This skill is **read-only**. It never marks anything done and never edits
  a project. If the user wants an item tracked elsewhere, say so and let
  them ask.

## Question → move

| Question | CLI | Vault |
|---|---|---|
| what did I forget about | `psum query` → high `attention`, old `age` | DATA rows, sort by attention |
| what's half-done | `psum query` → `half_plan`, or `plans` with a nonzero denominator and a low numerator | DATA `half_plan` / `plans` columns |
| what did I leave running | `psum query --status mid-flight` | DATA rows where `status` is `mid-flight` |
| what's dirty or unpushed | `psum query` → `dirty > 0` or `unpushed > 0` | DATA `dirty` / `unpushed` columns |
| what's left in X | `psum query --detail X` → `docs.plans[].unchecked` | INDEX section for X (aggregate only) |
| what is X | `psum query --detail X` → `description` | INDEX section for X |
| what have I touched lately | `psum query --sort recent --limit 20` | INDEX "Recent" table |
