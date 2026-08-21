# workspace-portfolio

Reads a directory full of git repositories and tells you what you forgot you were working on.

If you have accumulated dozens of projects across a workspace folder, the expensive question is
not "what is in here" — `ls` answers that. It is *which of these did I leave half-finished, and
which of them still has something waiting for me.* This answers that question in 65 milliseconds,
without calling a model.

```
PROJECT                       CATEGORY      STATUS      LAST  OPEN  ATTN
----------------------------  ------------  ----------  ----  ----  ----
web-console                   Acme          mid-flight    9h          55
    Cloudflare Worker and SPA backend for the customer portal, rendering
    call-flow visualisations from the shared client library.
report-validator                            mid-flight    7d          45
    CLI tool that cross-checks exported billing data against contract rules
    and flags inconsistencies before invoicing.
mstodo-mcp                                  mid-flight   1mo          41
```

`ATTN` is the ranking that matters: a project scores high when it is *interrupted* — uncommitted
work, an unpushed branch, a `HANDOFF.md`, a plan half-executed — and higher still when it has been
interrupted for a while after a period of real activity. A project you finished cleanly scores
zero no matter how long ago it was.

## Install

Python 3.12+, standard library only. No dependencies, no virtualenv, no build step.

```bash
git clone https://github.com/dszp/workspace-portfolio.git
cd workspace-portfolio
ln -s "$PWD/psum" ~/.local/bin/psum     # or anywhere on your PATH
psum scan && psum
```

Edit `config/projects.toml` and set `root` to whatever folder holds your projects. That is the
only setting you must change.

## Running it on a schedule

`scan` and `index` make no model calls, and `describe` is gated on the prompt's inputs, so a
workspace you did not touch costs nothing. `tools/psum-cron.sh` is a ready wrapper:

```bash
cp tools/psum-cron.sh ~/.local/bin/psum-cron && chmod +x ~/.local/bin/psum-cron
crontab -e     # 30 5,17 * * * $HOME/.local/bin/psum-cron
```

Twice a day, twelve hours apart, is a better default than nightly: the question this answers —
what did I leave half-finished — changes over hours, so a morning run reflects yesterday and an
evening run reflects today. It costs no more than one run, because a second pass over an
unchanged workspace makes no model calls at all.

Order matters and the wrapper gets it right: `scan` produces the facts `describe` reads, and
`describe` writes the descriptions `index` renders. Running `index` first publishes a report
missing the descriptions generated seconds later.

Note that `index` commits on **every** run, not only when something substantive changed — the
report carries a scan timestamp and relative ages ("9h", "1d"), so its bytes differ each time.
That is one commit a day in whatever repo holds your data, which is a reasonable history to
have; it is not the tool failing to notice that nothing happened.

## Where your data lives

By default the tool keeps its data beside its code: `config/projects.toml`, `state/`,
`descriptions.toml` and `INDEX.md` all sit at the repo root. That is fine for one person with
one checkout, and it is what you get if you change nothing.

It stops being fine the moment the code is shared. Running `psum index` in a plain clone
auto-commits your private workspace map — every project name and path — into your checkout of
somebody else's source, and there is nowhere to put your data that is not *inside the program*.

`PSUM_HOME` separates them:

```bash
export PSUM_HOME=~/my-portfolio      # your data: config, state, descriptions, INDEX.md
psum scan && psum index              # code stays read-only wherever you cloned it
```

The directory can be empty to start with, and can be its own private git repo — which is the
point: the tool is public and shared, your workspace map is private and yours. If it holds no
`config/projects.toml`, the defaults shipped with the code are used, so a fresh `PSUM_HOME`
works immediately and you only write a config when you want to change a setting.

Unset, `PSUM_HOME` resolves to the code root, so existing single-checkout installs are unaffected.

## The five commands

| Command | Cost | What it does |
|---|---|---|
| `psum` | free, instant | Prints the table. Reads cached facts; makes no system calls. |
| `psum scan` | ~30s, no network | Walks the workspace and rewrites `state/facts.json`. |
| `psum index` | instant | Renders `INDEX.md` and a vault-safe copy, and commits them. |
| `psum describe` | **costs model tokens** | Writes one-to-three-sentence descriptions of each project. |
| `psum query` | free, instant | Emits the same cached facts as JSON, for a model to reason over. |

Everything except `describe` is deterministic and free. Run `psum scan && psum` when you sit down;
run `describe` rarely.

```
psum --recent              sort by last worked instead of attention
psum --category NAME       filter to one category
psum --status stalled      filter to one status
psum --all                 include dormant and archived
psum --desc                show the description for every row, not just the top ones

psum query                 machine-readable JSON for every project
  --detail PROJECT ...       full record(s) by slug, name or substring
```

> `psum query` reads the committed `state/facts.json` and never rescans — it is
> the read path the `psum` skill uses, so it must stay fast and side-effect free.

## How status is decided

Six rules, evaluated in order. The first that matches wins.

1. **config override** — you pinned the status in `config/projects.toml`. This is also the only way
   to reach `archived`; nothing derives it.
2. **done** — no open items, no `HANDOFF.md`, a clean working tree, nothing unpushed, *and* at
   least one plan document with every checkbox ticked. Finishing the plan is what marks it done.
3. **mid-flight** — uncommitted files, unpushed commits, a branch ahead, a `HANDOFF.md`, or a
   partially-executed plan.
4. **active** — worked on within `active_days` (default 21).
5. **stalled** — quiet, but with open items still listed.
6. **dormant** — quiet, and nothing outstanding.

`done` is deliberately evaluated *before* the recency rules. Placed after them it would be
unreachable for anything finished this week, and a finished project would decay back to `dormant`
once it went quiet — describing a time window rather than a state.

"Last worked" is the newest of four independent signals: the last git commit, the newest file
mtime, the last Claude Code session in that directory, and the last entry in a
[Remember](https://github.com/anthropics/claude-code) log. The last two are optional; absent, the
first two carry it.

## Attention scoring

```
attention = mid_flight (cap 60) + stall (40 × intensity × ramp) + pressure (cap 20)
```

capped at 100. `intensity` is *anchored at the project's own last commit*, not at now — it asks
"how hot was this before it went quiet", which stays meaningful however long ago that was. Anchored
at now, a 90-day window is zero by construction for exactly the projects you have most thoroughly
forgotten, which is the population this tool exists to surface.

All weights live in the `[weights]` block of the config.

## Open items

Three tiers, kept separate on purpose:

- **`open_items`** — `BACKLOG.md`, `TODO.md`, `HANDOFF.md`, `NEXT.md`. These feed both status and
  attention, because they are things somebody wrote down as waiting.
- **`roadmap_items`** — `ROADMAP.md`. Reported, but never feeds attention. A roadmap accumulates
  "someday" entries that never resolve, and letting it score would permanently pin aspirational
  projects to the top of the list.
- **Plan and spec checkboxes** — counted per-file only, to detect a half-executed plan. They are
  not summed into `open_items`; an agreed plan's unchecked boxes are a description of the plan, not
  a backlog.

## Redaction — for client work

If you keep client projects in the same workspace, point `redact_prefixes` at them:

```toml
redact_prefixes = ["clients/"]
```

Every free-text field of a matching project is nulled **in the record itself**, inside the scanner,
before anything downstream — any report, any committed file, any model — can see it. The project's
path, name, commit subjects, branch names, remote URLs, document titles and error messages are all
replaced. The slug becomes a stable digest so reports stay consistent between runs without carrying
the path.

What survives is the shape: that a redacted project exists, its status, how long since you touched
it, and its attention score. You still see *"something under clients/ has been mid-flight for two
weeks"* without the index naming who.

Redacted projects never get a description generated, and no subprocess is ever launched for one.

## Descriptions (optional)

`psum describe` writes a short description per project and stores it in `descriptions.toml`, which
is tracked in git so the diff of that file is the record of a description actually changing.

It shells out to the [Claude Code CLI](https://claude.com/claude-code) (`claude -p`) once per
project. Each entry is gated on a hash of **the prompt's own inputs** — the README and
`CLAUDE.md` first paragraphs, the `package.json` description, the name, the category, and the
top-level file names. Nothing else. A description says what a project *is*, and that does not
change because you made a commit.

That distinction is the difference between a command you can schedule and one you cannot. Gating
on the project's general content hash instead means every commit rewrites the prose — and so does
a project decaying from `active` to `stalled` with no content change at all. A run where the
inputs are untouched makes **zero** model calls, takes under a tenth of a second, and leaves the
file byte-identical, which is what makes a nightly run cost nothing.

```toml
["some/project"]
text = "n8n community node that wraps a PBX platform's REST API for workflow use."
source = "ai"        # "manual" = never regenerate; "redacted" = fixed placeholder
prompt_hash = "sha256:..."
```

Edit any description and set `source = "manual"` and it is never touched again.

The prompt is deliberately narrow: the project's facts record, the first paragraph of `README.md`
and `CLAUDE.md`, the `package.json` description, and the top-level file names. No repository crawl.

**Caveat worth knowing:** the subprocesses are not sandboxed. `--allowed-tools ""` does *not*
restrict a `claude` subprocess — it is an allowlist addition, and an empty one adds nothing — so
this passes `--disallowed-tools` with an explicit list instead. That genuinely blocks tool use, but
it is a flag, not a jail. A redacted project's contents are never *sent*, because no prompt is ever
built for one; that guarantee rests on no prompt naming it, not on a sandbox.

If you would rather not use a model at all, never run `describe`. Every other surface works without
it and simply shows no description.

## Obsidian and other places to put the output

`psum index` writes two identical files: `INDEX.md` at the repo root, and
`state/vault/PORTFOLIO-INDEX.md`.

The second exists because a *generated* file must not be bidirectionally synced. Point a sync tool
at a file your generator also owns and the two will eventually fight — in the case that motivated
this split, an Obsidian normaliser failed to parse a table row containing a link with raw spaces,
folded the entire remaining document into that one cell as escaped text, and wrote it back. Sync
the copy under `state/vault/` instead; nothing of value lives there and the next render overwrites
it.

If you use Obsidian, one way to get this onto your phone is the
[Realtime](https://github.com/nealol/realtime) plugin with
[`@realtime-md/cli`](https://www.npmjs.com/package/@realtime-md/cli), which can bind a folder on a
server into a vault that syncs in seconds. A working installer for that bridge — including the
folder-mount management, attachment filtering, and the sync-lock handling — is published at
[`dszp/remote-vs-code-hosting`](https://github.com/dszp/remote-vs-code-hosting) as
`deploy/72-plans-vault.sh`.

None of that is required. `PORTFOLIO-INDEX.md` is a plain markdown file — put it in any vault, any
static site, any wiki, or read it where it sits.

## Configuration reference

| Setting | Default | Meaning |
|---|---|---|
| `PSUM_HOME` (env) | the code root | Where config, state, descriptions and `INDEX.md` live. |
| `root` | `~/workspace` | The folder to scan. |
| `max_depth` | `4` | How deep to look for projects. |
| `active_days` | `21` | Quiet longer than this and a project stops being `active`. |
| `parallelism` | `1` | Workers for the scan. See below. |
| `describe_parallelism` | `6` | Concurrent `claude` subprocesses. |
| `describe_model` | `sonnet` | Model for descriptions. |
| `desc_rows` | `10` | How many top rows show a description by default. |
| `exclude_globs` | `node_modules`, `.venv`, `vendor` | Never walked, never counted. |
| `hide_status` | `dormant`, `archived` | Hidden from the default table. |
| `redact_prefixes` | `clients/` | See Redaction. |

`parallelism` defaults to 1 because threading measured *slower* on the machine this was built on:
the scan is several hundred git subprocess spawns, and fork/exec contention outweighed the
overlapped I/O (1 worker → 30.3s, 3 → 47.3s, 6 → 53-57s on 5 cores). Raise it only after measuring
on your own hardware.

## Design notes

- **`state/facts.json` is disposable.** It is gitignored and can be deleted at any time; `psum scan`
  rebuilds it. Everything durable — `descriptions.toml`, `INDEX.md` — is tracked.
- **Discovery claims deepest-first.** A repo claims its whole tree, so a nested repo becomes its own
  record and the outer one excludes it. A non-repo directory becomes a record only if it still holds
  markdown nobody claimed — which is what stops a category folder appearing beside its own children.
- **Dot-directories are never projects.** `.claude`, `.github`, `.vscode` hold tooling state. Inside
  a repo that is moot, but a category folder is not a repo and does not claim its tree, so without
  this rule any `.claude/skills/*` holding a `SKILL.md` gets promoted to a project.
- **Concurrency is handled.** `state/.lock` is an `flock`; every write is temp-file-plus-rename. Two
  runs at once will not corrupt anything — the second reports the lock and exits.

## Keeping private things out of a public repo

This tool reads a whole workspace, so the repo that runs it accumulates names you may not want
published — clients, private projects, internal hosts. The dangerous case is not the obvious
one: it is a default value, an example in a comment, or a fixture that quietly carries a real
name into a public commit. Review does not catch that reliably. A check that runs before the
commit does.

```bash
bash tools/install-hooks.sh
```

That installs `tools/leakguard.sh` as this clone's `pre-commit` hook. It scans the **staged
blob** — not the worktree, which differs the moment you stage something and keep editing — and
refuses the commit on:

- **Built-in, no configuration:** absolute `/home/<user>` and `/Users/<user>` paths, email
  addresses (GitHub noreply and `example.com` excepted), and private-key headers.
- **Your own denylist:** client names, private repo names, internal hosts.

The denylist deliberately lives at `.git/leakguard-patterns`, **not** in the repository. A list
of secret names committed to a public repo publishes exactly what it was written to protect —
`.leakguard.example` is the committed template and contains no real names.

Hooks are per-clone and never travel with a push, so run the installer in each checkout that
matters — and re-run it after editing `tools/leakguard.sh`, because the hook is a *copy*, not a
symlink. (A symlink into the worktree would let a branch that edits the guard change what guards
the commit editing it.)

Placeholder home paths — `/home/user/`, `/home/$DEV_USER/`, `/Users/<user>/`, `/home/.config/` —
are allowlisted. A guard that fires on every deploy guide is a guard that gets uninstalled. `git commit --no-verify` bypasses it, documented on purpose: a guard nobody can
override is a guard people delete.

## Development

```bash
uv run --with pytest pytest -q      # 198 tests
```

No dependencies to install. The tests build real git repositories in temp directories rather than
mocking git, so they are slower than unit tests and considerably more honest.

## License

MIT — see [LICENSE](LICENSE).
