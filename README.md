<p align="center"><img src="docs/brand/splitlight.webp" width="192" alt="Splitlight: a single beam splits through an emerald prism"></p>
<h1 align="center">Splitlight</h1>
<p align="center"><strong>One model finds the bug. Another checks the claim.</strong><br>A GitHub pull request reviewer you can talk back to.</p>

<p align="center">
  <a href="pyproject.toml"><img alt="Python 3.13+" src="https://img.shields.io/badge/Python-3.13%2B-76E6BD?labelColor=112D32"></a>
  <a href="src/autoreviewer/review/agents.py"><img alt="Built on pydantic-ai-harness" src="https://img.shields.io/badge/built_on-pydantic--ai--harness-76E6BD?labelColor=112D32"></a>
  <a href="src/autoreviewer/servers.py"><img alt="Models: Qwen and DeepSeek" src="https://img.shields.io/badge/models-Qwen_%2B_DeepSeek-DDEA76?labelColor=112D32"></a>
</p>

## On a pull request

Splitlight reads the diff **and the code around it**. When Qwen flags a correctness bug, DeepSeek checks whether the claim holds up in the repository before it becomes an inline finding. The models don't hold GitHub credentials; application code posts the review and sets the verdict.

| What it looks for | The question behind the comment |
| --- | --- |
| Correctness | Which input makes the new behavior wrong? |
| Security | Can data or an action cross a boundary it shouldn't? |
| DRY | Is there already a helper here that handles this exact case? |
| Docs & tests | Which documented promise changed? Would a test notice? |

**A suggestion isn't a veto.** A verified high-priority correctness or security issue can request changes; a reuse suggestion stays advisory. An incomplete blocking review doesn't earn an approval. To enforce the verdict at merge time, make the `autoreviewer` check required in your repository rules.

## Talk back, in the thread

Already have the App installed? Open a PR, push a commit, or comment `@chaos-emerald-reviewer review` on an existing PR. Then reply **on the finding**. A PR-opening bot can do this just like a person can.

> **Example author reply:** “This helper only gets normalized paths. Can you check the caller in `resolve_path`?”

Splitlight keeps the conversation with the finding. It can explain why the issue still holds or withdraw it when the code supports the objection. A withdrawn comment can be collapsed without marking the GitHub thread resolved.

## Under the hood

[Review agents](src/autoreviewer/review/agents.py) run on **Qwen and DeepSeek** through Boodleton on `chaos-emerald.com`. [Durable workflows](src/autoreviewer/review/durable.py) coordinate review work across webhook deliveries. The hosted receiver lives at `review.chaos-emerald.com`.

To work on Splitlight locally, install [uv](https://docs.astral.sh/uv/) and run these from the checkout:

```sh
uv sync --locked
uv run pytest
```

The default test suite skips tests that boot real microVMs. For model-backed quality measurements, see the [eval runner and case suites](evals/).

[Architecture & design notes](DESIGN.md) · [GitHub App setup](deploy/github-app/) · [Web service deployment](deploy/web/)
