# Selecting a review model

Use a native `provider:model` specification (resolved by `pydantic_ai.models.infer_model`) unless the deployment needs a custom client or credential store. For those cases, `factory:module:function` names an importable, synchronous, zero-argument Python function returning a `pydantic_ai.models.Model`. It does **not** name an agent, shell command, CLI plugin or login flow.

| Backend | Model specification | Worker dependency |
| --- | --- | --- |
| ChatGPT/Codex subscription | `openai-codex:gpt-6.2-sol` | Existing `pydantic-ai-slim[openai]==2.68.0` |
| GitHub Copilot | `github-copilot:<chat-completions-model-id>` | Same existing OpenAI extra |
| Anthropic API | e.g. `anthropic:claude-opus-5-5` | Optional `pydantic-ai-slim[anthropic]==2.68.0`; set `ANTHROPIC_API_KEY` |
| Claude Code subscription | `claude-code:claude-opus-5-5` | `claude-code` extra (Python 3.13–3.14); included in the receiver image. |

### Codex: provision credentials outside review jobs

The native `OpenAICodexModel` calls the Responses endpoint; it does not start `codex`. Provision a dedicated bot identity out of band and set `CODEX_HOME` to a private directory containing its CLI-format `auth.json`. Mount this only in the model worker, outside all review workspaces. Without `CODEX_HOME`, core looks in `~/.codex`; do not mount a developer's home. Core reads the file but does not update it or fall back to `OPENAI_API_KEY`.

A read-only snapshot is not durable token rotation: refreshed credentials live only in memory and a consumed refresh token can fail after restart. For a long-lived deployment, return `OpenAICodexModel(name, provider=OpenAICodexProvider(credential_source=store))` from a factory backed by an operator-managed secret store implementing `OpenAICodexCredentialSource`. Persist refreshed credentials atomically and avoid independent workers racing on one rotating grant. Do not share a provider instance across event loops.

### Burst queued reviews to Sol

Provision the dedicated Codex identity above, then set `PRISM_SOL_OVERFLOW_REPOS` in the receiver environment to a comma-separated list of exact repositories approved to send code to OpenAI (for example `your-org/public-project,your-org/another-project`). Set `PRISM_SOL_OVERFLOW_REPOS=*` to authorize Sol overflow for **all repositories this receiver reviews**, including private and future installations. Local review capacity is still used first. Restart the receiver to apply it. Blank or unset keeps overflow off; a GitHub App installation alone does not opt a repository in.

Prism prefers its normal review workers. When they are busy, all eligible queued reviews can concurrently use `openai-codex:gpt-6.2-sol` for its finders, including the simplifier. The usual independent checkers still verify findings; thread replies stay on the normal plan. Overflow reviews resume their own compatible sessions, never the primary reviewers’ transcripts. No active review is moved or duplicated. Previously queued eligible work can use the extra slot, but older local-only jobs ahead of it may delay pickup. Sol requests consume the service identity’s quota; there is no Prism concurrency cap on overflow finders. Receiver memory, disk, provider quotas, checker capacity and review deadlines still apply.

### Copilot: supply a separate inference credential

Set `GITHUB_COPILOT_API_KEY` in the model worker's secret environment. The native provider also accepts `GITHUB_COPILOT_API_TOKEN` and `COPILOT_GITHUB_TOKEN`; it does not need the PR-publishing GitHub App token. Choose an ID the account can actually use at `/chat/completions` (the account's `GET https://api.githubcopilot.com/models` catalog identifies supported endpoints). Entitlement to a model in a different Copilot interface is not sufficient.

Keep endpoint overrides under operator control: `GITHUB_COPILOT_BASE_URL`, `COPILOT_API_URL` and `GITHUB_COPILOT_API_BASE` can redirect authenticated requests. For example, a deployment that wants to pin the public endpoint can install this module **in the worker image**, not the evaluated repository:

```python
# Save as review_site/providers.py. Set finder or checker to factory:review_site.providers:copilot.
import os

from pydantic_ai.models import Model
from pydantic_ai.models.github_copilot import GitHubCopilotModel
from pydantic_ai.providers.github_copilot import GitHubCopilotProvider


def copilot() -> Model:
    provider = GitHubCopilotProvider(
        api_key=os.environ['GITHUB_COPILOT_API_KEY'],
        base_url='https://api.githubcopilot.com',
    )
    return GitHubCopilotModel(os.environ['REVIEW_COPILOT_MODEL'], provider=provider)
```

### Claude Code: require a model boundary, not CLI automation

By default Prism uses `ClaudeCodeModel` from the published `pydantic-claude-code` package. No proxy or coding CLI is needed. For a checkout run `uv run --extra claude-code python -m pydantic_ai_claude_code login` as the service identity, outside review jobs. Use `uv run --extra claude-code autoreviewer-web` when starting from the checkout. For a headless receiver, set `CLAUDE_CODE_CREDENTIALS=file` and `XDG_DATA_HOME` to a private persistent directory during login and at runtime; allow the service user to write refreshed credentials there. Use an account authorized for this use and model. Never put the token in `prism.json`.

**Already using a gateway?** Set `PRISM_CLAUDE_CODE_BASE_URL` (including the API prefix, e.g. `https://gateway.example.org/v1`) and `PRISM_CLAUDE_CODE_API_KEY` to your gateway's own credentials. The gateway must accept Chat Completions with structured tool calls and must not execute tools itself. Prism does not launch `claude`, read its credentials, or start a CLAI2 session. Without either gateway variable Prism uses the subscription adapter; `anthropic:` is the separate API-billed option.

### Local inference

Prism talks to the OpenAI-compatible **Chat Completions** API. It doesn't start or install a model server. With vLLM, llama.cpp, SGLang, TensorFold, or another compatible engine already running:

1. From the receiver's network, run `curl -fsS http://localhost:8000/v1/models` (adjust host and port). Use the returned `data[].id` strings exactly, including slashes or quantization tags. Enable the engine's tool-call parser/chat template if the model requires it: listing a model does not prove it can return tool calls or structured results.
2. For two distinct models behind one server, use `--local-url` as in the [README](../README.md#make-your-own-reviewer). If each server loads one model, use separate URLs instead:

   ```sh
   uv run prism-config \
     --finder local:FINDER_MODEL_ID --finder-url http://finder:8000/v1 \
     --checker local:CHECKER_MODEL_ID --checker-url http://checker:8080/v1
   ```

3. Set `PRISM_CONFIG` to the generated JSON file **inside the receiver** and restart it. Inside a container, `localhost` is the receiver itself. Use the inference container's service name on a shared private network, or an accessible host address (e.g. `host.containers.internal` on Podman). Don't publish unauthenticated inference ports to the internet.

Set `--local-context N` to the **served context window in tokens** (for example, `32768` if the server is configured for 32k). Prism starts compacting at half that window by default. For different windows, set `local_context` per full model ID in `prism.json`, e.g. `{"local:finder": 32768, "local:checker": 65536}`. Without a declared window or explicit `compact_at`, the trigger is 4096 tokens. Local aliases never inherit a hosted model’s context limit.

No cloud key or Python factory is needed. If the server requires authentication, set `PRISM_LOCAL_API_KEY` in the receiver's environment, and include the bearer header when listing models. This key goes to **all** configured `local:` endpoints; only use it with servers you trust with the same credential. Use separate trusted factories for endpoints needing different keys. You can also mix a `local:` finder with a hosted checker; the hosted checker then receives repository context.

### Other providers

For a provider outside the image's installed SDKs, install the matching Pydantic AI extra in the receiver environment and rebuild the image. A trusted model factory can supply custom transports or credential stores.

### Set model concurrency

Each model defaults to one concurrent request. If your providers support parallel requests, set a positive lane count for either or both models when creating the config. For example:

```sh
uv run prism-config --finder openai-codex:gpt-6.2-sol --finder-lanes 3 \
  --checker claude-code:claude-opus-5-5 --checker-lanes 2
```

`prism.json` also accepts `lanes` (concurrent requests) and `compact_at` (history tokens), both maps keyed by the full model specification. The portable config takes precedence over the historical Boodleton model settings. Omit `PRISM_CONFIG` only to keep that existing deployment mode.

### Keep the PR on the data side of the boundary

- Factories are arbitrary Python at import and call time. Select them only from trusted deployment configuration. Never import from a checkout, add it to `PYTHONPATH`, or start the worker in it. PR text, repository config and comments must not select imports, endpoints, environment or credentials.
- The review agent, not the model provider, dispatches the bot's registered tools. Do not start coding agents, load repository hooks/MCP config, or grant a provider shell, filesystem, network tools or sandbox-management access. A read-only checkout does not make a host shell safe.
- Keep provider secrets out of the checkout, job sandbox, subprocess environments, prompts and logs. Keep inference and GitHub publishing credentials separate. Enable an external provider only for repositories whose source may be sent there under your organization's policy.

## One capacity pool for finding and checking

Instead of dedicating a provider to overflow, list resources in preference order:

```sh
uv run prism-config --output prism.json --local-url https://inference.example.com/v1 \
  --pool-model local:qwen=5 --pool-model local:deepseek=10 \
  --pool-model openai-codex:gpt-6.2-sol=20
```

Use the actual model IDs served by your endpoints, configure provider credentials, and point
`PRISM_CONFIG` at the file. Each number is **concurrent model requests per Prism worker**, shared
between finding and verification—not whole PRs, or reserved threads on the inference server.
The worker prefers the first eligible model with capacity. If all are busy it chooses the least
loaded relative to capacity and queues behind its limiter. Each finding gets a placement decision
when verification begins; the checker cannot have the finder's resolved model name. Each agent
run stays on its selected model. This is load balancing, not health probing or retry-on-outage.

Pool mode replaces `PRISM_SOL_OVERFLOW_REPOS`: listing a hosted model authorizes review data
from **every repository this worker reviews**, including private repos, to go there. Omit hosted
models for a local-only worker. `PRISM_MAX_CONCURRENT_JOBS` still limits active PRs; raise it if
one PR alone cannot keep the pool busy. Conversation replies keep their configured model.
Each finder, simplifier and checker resumes its own compatible history on the same PR and merge base, on the same head or a descendant. Checkers only see their own history for the same candidate and must verify against the current checkout. These bounded transcripts are context, not cached approvals or crash checkpoints. Closing a PR removes them.
