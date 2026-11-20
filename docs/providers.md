# Selecting a review model

Use a native `provider:model` specification (resolved by `pydantic_ai.models.infer_model`) unless the deployment needs a custom client or credential store. For those cases, `factory:module:function` names an importable, synchronous, zero-argument Python function returning a `pydantic_ai.models.Model`. It does **not** name an agent, shell command, CLI plugin or login flow.

| Backend | Model specification | Worker dependency |
| --- | --- | --- |
| ChatGPT/Codex subscription | `openai-codex:<entitled-model-id>` | Existing `pydantic-ai-slim[openai]==2.68.0` |
| GitHub Copilot | `github-copilot:<chat-completions-model-id>` | Same existing OpenAI extra |
| Anthropic API | e.g. `anthropic:claude-sonnet-4-6` | Optional `pydantic-ai-slim[anthropic]==2.68.0`; set `ANTHROPIC_API_KEY` |
| Claude Code via your inference gateway | `claude-code:<gateway-model-id>` | Existing OpenAI extra. Requires an authorized, model-only Chat Completions endpoint; not a local Claude Code login. |

### Codex: provision credentials outside review jobs

The native `OpenAICodexModel` calls the Responses endpoint; it does not start `codex`. Provision a dedicated bot identity out of band and set `CODEX_HOME` to a private directory containing its CLI-format `auth.json`. Mount this only in the model worker, outside all review workspaces. Without `CODEX_HOME`, core looks in `~/.codex`; do not mount a developer's home. Core reads the file but does not update it or fall back to `OPENAI_API_KEY`.

A read-only snapshot is not durable token rotation: refreshed credentials live only in memory and a consumed refresh token can fail after restart. For a long-lived deployment, return `OpenAICodexModel(name, provider=OpenAICodexProvider(credential_source=store))` from a factory backed by an operator-managed secret store implementing `OpenAICodexCredentialSource`. Persist refreshed credentials atomically and avoid independent workers racing on one rotating grant. Do not share a provider instance across event loops.

### Copilot: supply a separate inference credential

Set `GITHUB_COPILOT_API_KEY` in the model worker's secret environment. The native provider also accepts `GITHUB_COPILOT_API_TOKEN` and `COPILOT_GITHUB_TOKEN`; it does not need the PR-publishing GitHub App token. Choose an ID the account can actually use at `/chat/completions` (the account's `GET https://api.githubcopilot.com/models` catalog identifies supported endpoints). Entitlement to a model in a different Copilot interface is not sufficient.

Keep endpoint overrides under operator control: `GITHUB_COPILOT_BASE_URL`, `COPILOT_API_URL` and `GITHUB_COPILOT_API_BASE` can redirect authenticated requests. For example, a deployment that wants to pin the public endpoint can install this module **in the worker image**, not the evaluated repository:

```python
# review_site/providers.py — configure factory:review_site.providers:copilot
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

Prism's `claude-code:` prefix is a gateway adapter, not a native subscription login. Set `PRISM_CLAUDE_CODE_BASE_URL` (including the API prefix, e.g. `https://gateway.example.org/v1`) and `PRISM_CLAUDE_CODE_API_KEY` to your gateway's own credentials. The gateway must accept Chat Completions with structured tool calls and must not execute tools itself. Prism does not launch `claude`, read its credentials, or start a CLAI2 session. Without such a gateway use `anthropic:` for direct API access or supply an installed model factory.

### Other providers and local servers

For a provider outside the image's installed SDKs, install the matching Pydantic AI extra in the receiver environment (e.g. `uv add 'pydantic-ai-slim[anthropic]==2.68.0'` in your deployment checkout and rebuild the image). For an OpenAI-compatible server, use `openai-chat:<model-id>` with `OPENAI_BASE_URL` and `OPENAI_API_KEY`. For two different compatible endpoints, use separate factories with explicit URLs and credentials. Providers must support the tool calls and structured results the review agents use; resolving a model name alone cannot verify that.

`prism.json` also accepts `lanes` (concurrent requests) and `compact_at` (history tokens), both maps keyed by the full model specification. The portable config takes precedence over the historical Boodleton model settings. Omit `PRISM_CONFIG` only to keep that existing deployment mode.

### Keep the PR on the data side of the boundary

- Factories are arbitrary Python at import and call time. Select them only from trusted deployment configuration. Never import from a checkout, add it to `PYTHONPATH`, or start the worker in it. PR text, repository config and comments must not select imports, endpoints, environment or credentials.
- The review agent, not the model provider, dispatches the bot's registered tools. Do not start coding agents, load repository hooks/MCP config, or grant a provider shell, filesystem, network tools or sandbox-management access. A read-only checkout does not make a host shell safe.
- Keep provider secrets out of the checkout, job sandbox, subprocess environments, prompts and logs. Keep inference and GitHub publishing credentials separate. Enable an external provider only for repositories whose source may be sent there under your organization's policy.
