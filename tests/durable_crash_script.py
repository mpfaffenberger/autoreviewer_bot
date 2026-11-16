"""Helper for test_durable: one process that crashes mid-review, then one that recovers it.

    python durable_crash_script.py <dir> enqueue-and-crash   # exits 9 from inside the lens model
    python durable_crash_script.py <dir> recover              # DBOS recovers the review; exits 0

GitHub is faked to <dir>/github.json so its state survives the crash, like the real one would.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from autoreviewer.review import durable
from autoreviewer.review.agents import correctness_agent, verifier_agent
from autoreviewer.web.routing import PullRequestUpdated
from tests.review_fakes import FakeGitHub, finding, job_deps, verifier_model

DIR = Path(sys.argv[1])
MODE = sys.argv[2]
PULL = PullRequestUpdated(installation_id=1, owner="mpfaffenberger", repo="demo", number=7, head_sha="a" * 40)


class PersistentGitHub(FakeGitHub):
    """FakeGitHub whose writes are saved to disk after every call."""

    def _save(self) -> None:
        reviews = [{**r, "comments": [asdict(c) for c in r["comments"]]} for r in self.reviews]
        (DIR / "github.json").write_text(json.dumps({"checks": self.checks, "reviews": reviews}))

    @classmethod
    def load(cls) -> PersistentGitHub:
        github = cls()
        if (state := DIR / "github.json").exists():
            saved = json.loads(state.read_text())
            github.checks, github.reviews = saved["checks"], saved["reviews"]
        return github

    async def start_check(self, *args: Any, **kwargs: Any) -> int:
        check_id = await super().start_check(*args, **kwargs)
        self._save()
        return check_id

    async def finish_check(self, *args: Any, **kwargs: Any) -> None:
        await super().finish_check(*args, **kwargs)
        self._save()

    async def create_review(self, *args: Any, **kwargs: Any) -> None:
        await super().create_review(*args, **kwargs)
        self._save()


def crashing_lens(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    if MODE == "enqueue-and-crash":
        os._exit(9)  # a hard crash in the middle of the review: no cleanup, no exception handling
    return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args={"findings": [finding(5)]})])


async def main() -> None:
    github = PersistentGitHub.load()
    deps = job_deps(github, DIR)
    deps = type(deps)(**{**deps.__dict__, "agents": lambda root: (
        correctness_agent(FunctionModel(crashing_lens), root), verifier_agent(verifier_model(lambda f: True), root)
    )})  # fmt: skip
    durable.configure(DIR / "dbos.sqlite")
    await durable.launch(deps, concurrency=2)
    try:
        if MODE == "enqueue-and-crash":
            await (await durable.enqueue(PULL)).get_result()  # never returns: the lens kills the process
        else:
            # launch() recovered the interrupted workflow; wait for it by its deterministic id.
            from dbos import DBOS

            handle = await DBOS.retrieve_workflow_async(durable.workflow_id(PULL))
            await asyncio.wait_for(handle.get_result(), timeout=60)
    finally:
        durable.shutdown()


asyncio.run(main())
