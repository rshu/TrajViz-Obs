# `/obs` Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the merged bridge and mod work end to end on `main`: align the mod's bridge calls with the bridge as built, regenerate the mod's fixture from the real bridge, write the docs, and prove the plugin headless and in a browser.

**Architecture:** No new subsystems. The mod's `hooks/lib/bridge.ts` callers and the bridge's subcommands already exist; this plan reconciles their contracts where the two parallel builds diverged, then runs the checks the mod plan's Task 15 lists.

**Tech Stack:** as the two earlier plans.

**Spec:** `docs/superpowers/specs/2026-10-22-obs-plugin-design.md`; plans `docs/superpowers/plans/2026-10-22-obs-bridge.md` and `2026-10-22-obs-mod.md`.

## Global Constraints

Those of both earlier plans, plus the bridge contract as built (binding on the mod):
- `view` and `json` take `--window N`, `--artifacts DIR` and `--kind current|other`; an empty current session answers `{"empty": true, "reason"}` with exit 0.
- `llm-prompt` always writes `<artifacts>/prompts-<feature>.json` and omits `prompts` from stdout when that file is over 256 KB; the mod reads the file when `prompts` is absent. `llm-apply` and the direct subcommands take `--model`; the label result carries `errors`.
- `compare` writes `compare/<other-stem>.json` (with an `orientation` key) and `scorecard` writes `compare/scorecard.json`; `converge-cli` requires `--artifacts DIR`.
- `bundle` returns `{"path", "bytes", "members"}`; `dashboard`/`converge` print `{"url"}` first and block until stdin closes; a busy explicit port surfaces as the child's first stderr line.
- Handlers are `export async function name($, ctx)`; `CommandContext` carries no `$`; dispatch is a static switch (mod Task 1 ruling).

## Review Focus

- `/obs analyze` on a session whose prompts file exceeds 256 KB: the mod must load prompts from the file, not fail on a missing key (Task 1).
- `/obs` on a fresh session: the empty-state row, no card (Task 2).
- `/obs view steps` on the fixture: a 25-row page and the file path in the hidden context (Task 2).
- The dashboard opened with the session loaded and the sub-agent tree present, with no click (Task 3).
- Headless `claude -p "/obs --session <fixture>" --plugin-dir .` prints the provenance line and the overview (Task 3).

---

### Task 1: Reconcile the mod's bridge calls with the bridge as built

**Files:**
- Modify: `hooks/lib/bridge.ts`, `hooks/lib/llm.ts`, `hooks/commands/llm.ts`, `hooks/commands/inline.ts`, `hooks/commands/exports.ts`, `hooks/commands/compare.ts`, `hooks/commands/misc.ts`, `hooks/lib/snapshot.ts` (whatever each call needs), `types/index.d.ts` (label `errors`)
- Test: the existing `hooks/*.test.tsx` files touched by each change

**Interfaces:**
- Consumes: the bridge subcommands exactly as `bridge/trajviz_obs/*.py` register them (`python -I bridge/trajviz_obs_cli.py --help` and each `add_parser`).
- Produces: every `runBridge` argv in the mod matches a real subcommand and its flags; `PromptPlan` loads prompts from `path` when `prompts` is absent; `label` results show `errors`; `converge-cli` passes `--artifacts`.

- [ ] **Step 1: Diff the contracts** — Run every `add_parser` in `bridge/trajviz_obs/*.py` against every `runBridge([...])` argv in `hooks/`; list each mismatch in `.superpowers/sdd/2026-10-23-obs-integration/contract-diff.md`.
- [ ] **Step 2: Write failing tests** for each mismatch found (e.g. `test('plan with prompts omitted loads them from the file', …)` with `fs.read` answered; `test('view passes --kind current, --window and --artifacts', …)`).
- [ ] **Step 3: Run** `claude plugin test .` — Expected: the new tests fail.
- [ ] **Step 4: Fix the mod side** (never the bridge, unless a bridge flag is impossible for the mod to supply; then fix the bridge and its tests).
- [ ] **Step 5: Run** `claude plugin validate --strict . && claude plugin test . && npx -y -p typescript@5.9.3 tsc -p . && (cd bridge && .venv/bin/python -m pytest tests -q)` — Expected: all pass.
- [ ] **Step 6: Commit** — `git commit -m "fix(mod): align bridge calls with the bridge CLI"`

### Task 2: Real fixture, docs and packaging checks

**Files:**
- Modify: `hooks/testdata/snapshot.ts` (regenerated from the real bridge on the vendored fixture with `--window 200000`)
- Create: `README.md`, `CHANGELOG.md`, `LICENSE` (MIT, Rui Shu), `docs/upstream-notes.md`

- [ ] **Step 1: Regenerate the fixture** — Run `bridge/.venv/bin/python -I bridge/trajviz_obs_cli.py snapshot --transcript bridge/tests/fixtures/native_claude_code/django__django-11477.jsonl --window 200000 --artifacts /tmp/obs-fixture-artifacts` and write the document as the `SNAPSHOT` constant; adjust `types/index.d.ts` nullability where the real document differs; run `claude plugin test .` — Expected: pass.
- [ ] **Step 2: Write the docs** per the mod plan's Task 15 Step 1 (README: what `/obs` shows, install lines for terminal and desktop, first run and requirements, the command table, `/config` options, LLM modes and cost, artifacts folder, known limitations from the spec, development commands; CHANGELOG 0.1.0; LICENSE; upstream-notes from the spec's list).
- [ ] **Step 3: Validate** — Run `claude plugin validate --strict .` — Expected: passed.
- [ ] **Step 4: Commit** — `git commit -m "docs: README, changelog, license, upstream notes; real fixture snapshot"`

### Task 3: End to end

- [ ] **Step 1: Headless smoke** — Run `claude -p "/obs doctor" --plugin-dir .` then `claude -p "/obs --session bridge/tests/fixtures/native_claude_code/django__django-11477.jsonl" --plugin-dir .` then `claude -p "/obs view tool_usage --session bridge/tests/fixtures/native_claude_code/django__django-11477.jsonl" --plugin-dir .` — Expected: the doctor table; the provenance line and overview text; a table. The first run creates the managed environment (watch for the setup lines).
- [ ] **Step 2: Interactive type lay** — Run `claude --plugin-dir . -p "/obs help"` once so `.claude-plugin/types/` is laid for `tsc`; then `npx -y -p typescript@5.9.3 tsc -p .` — Expected: no errors.
- [ ] **Step 3: Browser check** — Start `<venv python> -I bridge/trajviz_obs_cli.py dashboard --transcript bridge/tests/fixtures/native_claude_code/django__django-11477.jsonl --port 7899 --artifacts /tmp/obs-e2e` in the background, open the URL with Playwright, expect the KPI cards without a click, then end the process.
- [ ] **Step 4: Record** the outcomes in the ledger and commit any fix.
