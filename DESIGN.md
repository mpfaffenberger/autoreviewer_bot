# autoreviewer: design

A Macroscope-style GitHub PR reviewer, built on `pydantic-ai-harness`, served on
the homelab beside Boodleton and running on chaos-emerald's own models.

Status: **draft for Mike to review**. The open questions are in §12.

---

## 1. What it does (the Macroscope-ish contract)

| Behaviour | Rule |
|---|---|
| Trigger | PR `opened`, `reopened`, `ready_for_review`, `synchronize` (push), or `@autoreviewer review` |
| Lenses | **Correctness**, **DRY / don't-reinvent-the-wheel**, **Security** |
| Output | One GitHub review: a summary plus inline comments anchored to diff lines |
| Verdict | **Any open P0/P1 finding → `REQUEST_CHANGES`.** Otherwise `APPROVE` (P2/P3 findings are posted as non-blocking comments). `AUTOREVIEWER_BLOCK_AT_PRIORITY=3` makes every finding block |
| Noise policy | The lens is thorough and scores its own confidence; the verifier and code filter. Still **no nits**: style, naming, and taste are never findings |
| Conversation | Anyone (humans *and* bots) can reply in a finding's thread. The reviewer answers, holds its position, or withdraws the finding |
| Follow-up pushes | Re-checks open findings. Fixed ones get a "fixed in `abc123`" reply and the thread is resolved. Then it reviews only the new commits |
| Merge gate | Also publishes a **check run** (`autoreviewer`) that repos can mark as required |

> Why a check run as well as the review? Whether a GitHub App's
> `REQUEST_CHANGES` actually blocks a merge depends on each repo's
> branch-protection settings. A failing required check blocks it every time,
> so treat the check run as the gate and the review as the conversation.

### Not using the harness's `Macroscope` capability

`pydantic_ai_harness.Macroscope` shells out to the **hosted Macroscope CLI**:
their service and their models, on a Macroscope account. We want to *behave
like* Macroscope on *our* DeepSeek/Qwen, so that capability doesn't fit.

---

## 2. Architecture

```
GitHub ──webhook──▶ Cloudflare ──▶ Caddy :8443 ──▶ autoreviewer:8000  (FastAPI)
                                                     │  verify HMAC, route event
                                                     ▼
                                              DBOS durable queue (SQLite)
                                                     │  idempotent workflow ids
                     ┌───────────────────────────────┼─────────────────────────┐
                     ▼                               ▼                         ▼
              review_pr workflow           handle_reply workflow     installation workflow
                     │                               │
        fetch diff + tarball (installation token, held by CODE only)
                     │                               │
        ┌──── 3 lens agents (parallel) ────┐   conversation agent
        │ correctness │ DRY │ security     │   (thread history via StepPersistence)
        └──────────────┬───────────────────┘          │
                verifier agent (different model)      │
                       │                              │
                       ▼                              ▼
              Poster: pure code. Anchors comments, sets the verdict,
              writes the check run, resolves threads, updates the store
                                     │
                                     ▼
                 http://boodleton:8000/v1  ──▶ qwen3.8-flash-next / deepseek-v4.1-flash
```

**Main rule: the LLM never touches GitHub.** The agents get read-only file
tools on an extracted tarball and return **typed Pydantic output**.
Deterministic code turns that output into API calls. So:

- a prompt-injected PR can't make the bot merge, push, comment elsewhere, or read secrets;
- the verdict (`REQUEST_CHANGES` / `APPROVE`) is computed, not generated;
- the GitHub layer is unit-testable without a model (SRP).

That's why we deliberately **don't** use the harness `GitHub` (MCP)
capability. It would hand the model write tools carrying an installation token.

---

## 3. Harness building blocks (reuse > reinvent)

| Need | Use | Notes |
|---|---|---|
| Model access | `OpenAIChatModel` + `OpenAIProvider(base_url='http://boodleton:8000/v1')` | Internal container DNS on `homelab-public`, so traffic never goes back out through Cloudflare |
| Repo access for agents | `LocalWorkspace(clone_dir, read_only=True)` + `FileSystem(read_only=True, tools=[..., 'list_files', 'grep'])` | DRY lens *needs* grep to find existing helpers |
| Honour repo conventions | `RepoContext()` | Loads the target repo's `AGENTS.md`/`CLAUDE.md` so we review against *their* rules |
| Thread memory | `StepPersistence(store=SqliteStepStore(database=...))` with `conversation_id=<thread>` | Resume with `list_runs(conversation_id=…)` → `continue_run(...)` |
| Crash-safe jobs + queue | `DBOSDurability()` + DBOS queue (SQLite system DB) | Gives the async job queue, retries, and webhook-redelivery idempotency (`SetWorkflowID(delivery_id)`) without hand-rolling any of it |
| Per-tenant budgets | `SpendLimits(budgets=[Budget(tokens=…, window='day', scope=installation_id)])` | Our models cost $0/token, so budget **tokens**, not USD |
| Injection telemetry | `PromptInjectionDefender()` (report-only) | Flags suspicious PRs in logs. The real defence is the structure in §2 |
| GitHub API | **`githubkit`** (async, typed, does App JWT → installation tokens) | Don't hand-roll JWT signing or token caching |
| Evals | `pydantic_evals` | Precision/recall on a seeded-bug PR corpus, see §10 |

Not used, on purpose (YAGNI):
- `Shell`, sandboxes: the *review* path never executes PR code. Running code (repros, base-vs-head) is an opt-in stage with its own threat model: see [EXECUTION.md](EXECUTION.md).
- `Coder`: we never edit their code.
- `Subagents` / `DynamicWorkflow`: the pipeline is fixed, so orchestrate it in Python with `asyncio.gather`. Letting a model orchestrate a fixed pipeline just burns tokens and adds randomness.
- `Macroscope`, `GitHub` MCP: see above.

---

## 4. The review pipeline

1. **Gate.** Is the installation active and the repo enabled? Is the PR not a draft? Is the head not already reviewed?
2. **Fetch** (code, installation token):
   - the diff: `pulls/{n}/files`, or `compare/{last_reviewed_sha}...{head}` for incremental runs (fall back to the full diff after a force-push);
   - the repo snapshot: `GET /repos/{o}/{r}/tarball/{head_sha}` into a size-capped tmpfs dir. No git binary, and no token in a remote URL.
3. **Filter.** Drop lockfiles, vendored, generated, and binary files. If the total changed lines exceed `MAX_DIFF_LINES` (≈3000), post one "split this PR" comment and set the check to `neutral`.
4. **Lenses** (in parallel). One factory, three instruction files. Each returns a report: every qualifying finding (not just the first), an overall verdict, and the files it inspected. A finding below confidence 4 is dropped without a verifier call.
5. **Verify.** A *different* model re-reads the cited code for each candidate and judges it against a fixed rubric (introduced by this PR, reachable trigger, no unstated assumptions, not intentional, actionable). It returns `confirmed | rejected` with a reason, and may correct the priority. Unconfirmed findings are dropped. Using a second model means the two models' mistakes are less likely to line up, and this step does most of the work on precision.
6. **Prove** (optional, see [EXECUTION.md](EXECUTION.md)). For confirmed high-severity findings, a repro test runs on head and on base in a throwaway sandbox. Findings that don't reproduce are dropped; bugs already present on base become non-blocking FYIs.
7. **Dedupe.** Each finding gets a fingerprint, `sha256(lens, path, normalised_snippet, title)`, so the same issue is never posted twice, across lenses or across pushes.
8. **Anchor.** GitHub rejects review comments on lines outside the diff hunks (HTTP 422). Findings that can be anchored become inline comments. Findings on unchanged code (common for DRY: "this already exists in `utils/x.py`") are anchored to the *new* code that duplicates it. Anything that still can't be anchored goes into the review body.
9. **Stale check.** If the PR head moved while we were thinking, drop the result. The newer push has its own workflow.
10. **Post** (code): one review (`REQUEST_CHANGES` if any blocking finding is open, else `APPROVE`), the check run, and the store update.

### The lenses

| Lens | Looks for | Explicitly *not* |
|---|---|---|
| Correctness | logic errors, wrong edge cases, broken contracts with callers (grep the call sites!), race conditions, resource leaks, error handling that swallows failures | style, naming, "consider adding tests" |
| DRY | new code duplicating (a) code elsewhere in the PR, (b) an existing helper in the repo, (c) stdlib or an **already-declared** dependency | suggesting *new* dependencies; abstracting things that merely look alike (rule of three) |
| Security | injection (SQL, shell, path, template), authn/z gaps, SSRF, unsafe deserialisation, secrets in code, weak crypto, missing input validation at trust boundaries | theoretical issues with no reachable input |

Every lens must cite `path:line` evidence, and the verifier checks those
citations. Each finding carries a **priority** (P0-P3, from OpenAI Codex's review rubric) and a
calibrated **confidence** (0-10, from Qodo PR-Agent). Lessons taken from published reviewer prompts
(Codex, PR-Agent, Anthropic's security review, Gemini CLI):

- **Be thorough during review; filter afterwards.** The lens reports every finding with an honest
  confidence rather than suppressing doubtful ones, because a separate verifier and code-side
  thresholds do the filtering. Telling one model to both search and self-censor produced "approves
  everything".
- **A checklist and a method**, not just "find bugs": understand the change, read callers and the
  base version, then walk a list of bug classes for every changed line.
- **Explicit acceptance criteria** in the verifier, so precision is a rubric, not a mood.
- **Comments state the trigger first**, then the consequence. No praise, no "please verify".

### Measuring it

`evals/` runs the real reviewer over seeded bugs in this repo's own code, with no GitHub calls:

    set -a; . ~/.config/homelab/autoreviewer.env; set +a
    BOODLETON_BASE_URL=http://127.0.0.1:8484/v1 uv run python -m evals.run --label NAME

Isolated cases put one bug in a tiny PR. Buried cases plant one bug inside a real feature commit of
several hundred lines, which is what reviews actually face. Clean cases (refactors, and real commits
left untouched) measure noise. Any prompt or pipeline change should be measured before and after.

**Secret-handling rule:** if the security lens finds a committed secret, the
comment names the file and line but **never quotes the value**. The poster
also redacts anything that looks like a credential before posting.

---

## 5. Conversations (bots replying to the bot)

Events: `pull_request_review_comment` (thread replies) and `issue_comment`
(PR-level `@autoreviewer …`).

```
reply arrives in a thread whose root comment is ours
  ├─ author is us (login == "<app-slug>[bot]")         → ignore (no self-loops!)
  ├─ thread bot_turns >= MAX_BOT_TURNS (4)             → one "escalating to a human" reply, then silence
  └─ else → handle_reply workflow:
        history  = StepPersistence history for conversation_id = f"{repo_id}/{pr}/{thread_node_id}"
        context  = the Finding row + code at the current head (read-only FS)
        output   = ThreadReply(body: str, decision: Literal['keep', 'withdraw', 'fixed'])
        poster   → reply in thread; on withdraw/fixed: resolve thread (GraphQL resolveReviewThread),
                   update finding status, recompute verdict, maybe APPROVE
```

Policy (enforced in code, not trusted to the prompt):
- **DRY** findings can be withdrawn by argument ("that helper has different semantics because …").
- **Correctness/security** findings can be withdrawn only if the agent shows the code proves it was a false positive. "Won't fix, out of scope" from a bot is *not* enough.
- **Human override:** `@autoreviewer dismiss <finding-id>` from a user with write access. The code checks their permission via the API, not by trusting the comment.
- Bot↔bot ping-pong is capped per thread (`MAX_BOT_TURNS`) *and* per PR per hour, so two chatty bots can't burn the GPU all night.

**Outbound sanitising:** model text has `@mentions` neutralised (wrapped in
backticks) before posting. Otherwise an injected PR could make us spam-ping
people.

---

## 6. State

Two stores, each with one job:

| Store | Holds | Why separate |
|---|---|---|
| `SqliteStepStore` (harness) | message history per thread conversation | Off the shelf, and resumable |
| `app.db` (ours, SQLite WAL) | domain truth: installs, PRs, findings | StepPersistence snapshots hold *messages only*, not domain state |

```sql
installations (id PK, account_login, account_type, status TEXT CHECK(status IN ('pending','active','suspended')),
               plan TEXT, created_at)
pull_requests (repo_id, number, last_reviewed_sha, check_run_id, verdict, updated_at,
               PRIMARY KEY (repo_id, number))
findings      (id PK, repo_id, pr_number, lens, severity, path, line, title, body, fingerprint UNIQUE,
               status TEXT CHECK(status IN ('open','fixed','withdrawn','dismissed')),
               comment_id, thread_node_id, created_sha, closed_sha, bot_turns INT DEFAULT 0)
```

Concurrency: a push and a reply on the same PR can race. Serialise per PR with
a short lease row, the same pattern as Boodleton's `work_timeline_store`
lease. Don't build a distributed lock for a single box.

---

## 7. Models & capacity

| Role | Default model | Why |
|---|---|---|
| Lenses ×3 | `qwen3.8-flash-next` | 262k context, local on the RTX PRO 6000, MTP drafting makes it fast |
| Verifier | `deepseek-v4.1-flash` | A *different* model, so its mistakes are less likely to match the lenses' |
| Conversation | `deepseek-v4.1-flash` | Keeps the Qwen slots free for reviews |

All model choices live in config, not in code.

**Capacity reality check:** Qwen serves about 3 concurrent 262k slots, and
**you share them** with your own Boodleton/Code Puppy use. One review is about
4+ agent runs with tool loops. So:
- set DBOS queue `worker_concurrency=2`;
- bound each run with a per-run `UsageLimits` (request + token caps);
- set per-installation daily token budgets (`SpendLimits`);
- expect a queue during busy hours, and say so on the landing page.

**Structured output on local models:** tool-call output parsing on
SGLang/TensorFold is the most likely flaky part. Use `output_retries=3`, keep
the output schemas flat, and record parse failures in the evals.

**Credential scoping gap:** Boodleton API keys have **no model allowlist**, so
the reviewer's key could reach the ChatGPT/Claude OAuth-backed models.
Reselling those subscriptions as a paid service would break their ToS. Either
add a `models` allowlist to Boodleton keys (small patch) or at minimum hard-pin
the model names in reviewer config. Prefer the allowlist.

---

## 8. Subscriptions ("as a service")

A **GitHub App** install *is* the subscription unit. The user installs it on an
account or org and picks repos, and GitHub handles the consent UI.

- `installation` / `installation_repositories` webhooks → upsert `installations`.
- New installs start as `pending`, which stops randoms eating your GPU. You approve them, either from a tiny admin page or by plugging into **Boodleton's existing GitHub-OAuth access-request flow** (`access_control.py`). Reuse that rather than building a second user system.
- Uninstall → `deleted` event → purge that tenant's findings and histories.
- **Payments: decide later (YAGNI).** Options in §12. GitHub Marketplace paid plans have eligibility hurdles (verified publisher org, minimum installs), so it probably isn't day one.

App permissions (least privilege): **Pull requests: R/W, Checks: R/W,
Contents: R, Metadata: R.** Events: `pull_request`,
`pull_request_review_comment`, `pull_request_review`, `issue_comment`,
`check_run` (re-run button), `installation`, `installation_repositories`.

---

## 9. Hosting on chaos-emerald (Boodleton-style)

- **Hostname:** `review.chaos-emerald.com`, Cloudflare-proxied, routed to **Caddy** (8443 is already forwarded, so no new router hole):
  ```caddy
  review.chaos-emerald.com {
  	encode zstd gzip
  	reverse_proxy autoreviewer:8000
  }
  ```
- **Cloudflare gotcha:** the edge is already 403-ing `minecraft.` with some rule. Make sure WAF / Bot Fight Mode doesn't challenge GitHub's webhook POSTs; add a skip rule for `/webhook`. HMAC (`X-Hub-Signature-256`, constant-time compare) is the real authentication.
- **Webhook handler:** verify HMAC, enqueue, return `202` well inside GitHub's 10-second timeout. Never do model work inline.
- **Quadlet** `homelab-deploy/quadlet/autoreviewer.container`, copying Boodleton's hardening: `ReadOnly=true`, `DropCapability=all`, `NoNewPrivileges=true`, `Network=homelab-public.network`, tmpfs `/tmp` with a size cap (repo tarballs land here), `--memory=2g --pids-limit=256`, data volume `~/.local/share/autoreviewer:/var/lib/autoreviewer:Z`.
- **Secrets** in `~/.config/homelab/` (0600), following the homelab README convention: `autoreviewer.env` (webhook secret, App ID, Boodleton key) and `autoreviewer.pem` (App private key, mounted read-only).
- Add the service to the README's routing table and update commands.

---

## 10. Repo layout

```
autoreviewer_bot/
  pyproject.toml                 # pydantic-ai-slim[openai,dbos], pydantic-ai-harness, githubkit, fastapi, uvicorn
  Containerfile
  src/autoreviewer/
    config.py                    # pydantic-settings: models, limits, secrets paths
    app.py                       # FastAPI: POST /webhook, GET /healthz, GET / (install landing)
    webhooks.py                  # HMAC verify + event → workflow routing (pure, unit-tested)
    github.py                    # githubkit wrapper: tokens, diff, tarball, review, check run, resolve thread
    workflows.py                 # DBOS workflows: review_pr, handle_reply, handle_installation
    pipeline.py                  # filter → lenses → verify → dedupe → anchor
    poster.py                    # Findings → GitHub calls; verdict; sanitising (no model imports!)
    diffmap.py                   # unified-diff parsing, line anchoring
    store.py                     # app.db access
    agents/
      schemas.py                 # Finding, Verification, ThreadReply
      factory.py                 # build_lens_agent(lens), build_verifier(), build_conversation_agent()
      prompts/{correctness,dry,security,verifier,conversation}.md
  tests/
  evals/                         # pydantic_evals datasets of seeded-bug PRs
```

The lenses are **data, not classes**. Adding a fourth lens means one prompt
file plus one enum value, with no new code paths (open/closed principle):

```python
class Lens(StrEnum):
    CORRECTNESS = 'correctness'
    DRY = 'dry'
    SECURITY = 'security'

def build_lens_agent(lens: Lens) -> Agent[ReviewDeps, list[Finding]]:
    return Agent(
        settings.lens_model,                      # OpenAIChatModel → boodleton
        deps_type=ReviewDeps,
        output_type=list[Finding],
        output_retries=3,
        instructions=(PROMPTS / f'{lens}.md').read_text(),
        capabilities=[
            FileSystem(read_only=True, tools=[*READ_TOOLS, 'list_files', 'grep']),
            RepoContext(),
            SpendLimits(budgets=settings.tenant_budgets),
            PromptInjectionDefender(),
        ],
    )
```

---

## 11. Milestones

| # | Ships | Done when |
|---|---|---|
| M0 | **Done 2026-11-16.** GitHub App `chaos-emerald-reviewer` (id 5786748), receiver at `review.chaos-emerald.com/webhook` (signature check, repo allowlist, dedupe), quadlet, Caddy route | PR #18 in `pydantic-ai-claude-code` got the neutral "autoreviewer is listening" check run. Jobs run in an in-process `Dispatcher`; DBOS replaces it behind the same `submit()` in M1, when a review must survive a restart |
| M1 | **Done 2026-11-16.** Correctness lens (`qwen3.8-flash-next`) + verifier (`deepseek-v4.1-flash`) with read-only file tools over a safe checkout, computed verdict, inline comments, check run, stale-head check; **DBOS** workflow per PR head (`review:owner/repo#N@sha`, SQLite, queue concurrency 2, max 3 recoveries) | PR #19: seeded bug flagged, fix approved. PR #20: container SIGKILLed mid-review, DBOS recovered it with one check run and one review |
| **Eval gate** | `pydantic_evals` corpus of ~30 PRs (seeded bugs, duplicated helpers, injected vulns, plus clean PRs) | Precision ≥ 0.8 on the clean set, *before* adding lenses |
| M2 | DRY + security lenses, fingerprints, incremental re-review, auto-resolve fixed threads | The eval corpus passes per lens |
| M3 | Thread conversations, bot-turn caps, human dismiss | A bot author can argue a DRY finding away; ping-pong is capped |
| M4 | Installation approval flow, tenant budgets, landing page | A stranger can install it and land in `pending` |
| M5 | Running code: repros, base-vs-head, test-verified `fixed` | See [EXECUTION.md](EXECUTION.md) section 9 |

The eval gate matters. A reviewer that blocks merges with false positives
gets uninstalled in a week, and that's exactly the pitfall Macroscope
competes on.

---

## 12. Open questions for Mike

1. **Who can subscribe?** Allowlist-approved (pending → active), or open to anyone?
2. **Money?** Free for approved users / Stripe later / GitHub Marketplace later?
3. **Clean PR verdict:** `APPROVE`, or just a passing check plus a `COMMENT` summary?
4. **Model split:** is Qwen for the lenses and DeepSeek for verifying/chatting OK, given the GPU sharing?
5. **Name / App slug:** `autoreviewer`? Something sillier?
6. **Boodleton key allowlist patch:** OK to add a per-key `models` allowlist to Boodleton (§7)?
