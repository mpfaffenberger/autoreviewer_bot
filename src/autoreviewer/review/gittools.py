"""Git tools for the review agents: see what changed, at any PR size, without a raw git command.

Typed on purpose. A raw `git` passthrough can't be made safe by blocking flags: e.g.
`git diff --no-index /run/secrets/github-app.pem /dev/null` would print the App's private key, which
is mounted in this container. These tools can only name the PR's own commits and paths inside the repo.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from pydantic_ai import ModelRetry
from pydantic_ai.toolsets import FunctionToolset

from autoreviewer.review.diff import annotate_git_diff, parse_changes
from autoreviewer.review.workspace import BASE, DIFF_ARGS, HEAD, PREVIOUS, GitError, Workspace, run_git

PAGE_LINES = 400
MAX_PAGE_LINES = 1000


def _safe_path(path: str) -> str:
    candidate = PurePosixPath(path)
    if not path or "\0" in path or candidate.is_absolute() or ".." in candidate.parts or path.startswith("-"):
        raise ModelRetry(f"{path!r} is not a path inside the repository; use a relative path like src/app.py")
    return str(candidate)


def _page(text: str, offset: int, limit: int, what: str) -> str:
    lines = text.splitlines()
    if not lines:
        return f"({what}: no output)"
    limit = max(1, min(limit, MAX_PAGE_LINES))
    offset = max(0, offset)
    chunk = lines[offset : offset + limit]
    end = offset + len(chunk)
    more = f"; call again with offset={end} for the rest" if end < len(lines) else ""
    return f"[{what}: lines {offset + 1}-{end} of {len(lines)}{more}]\n" + "\n".join(chunk)


def git_tools(workspace: Workspace) -> FunctionToolset[None]:
    toolset: FunctionToolset[None] = FunctionToolset()
    root = workspace.root

    @toolset.tool_plain
    async def changed_files() -> str:
        """List every file this PR changes: status (A added, M modified, D deleted, R renamed), lines added/removed."""
        changes = parse_changes(
            await run_git(root, "diff", "--name-status", "-z", *DIFF_ARGS, BASE, HEAD),
            await run_git(root, "diff", "--numstat", "-z", *DIFF_ARGS, BASE, HEAD),
        )
        rows = [
            f"{c.status} +{c.added if c.added is not None else '?'} -{c.removed if c.removed is not None else '?'} "
            f"{c.path}{'' if c.reviewable else '  (generated, vendored, binary, or deleted: not reviewed)'}"
            for c in changes
        ]
        return "\n".join(rows) or "(no changes)"

    @toolset.tool_plain
    async def diff(path: str | None = None, offset: int = 0, limit: int = PAGE_LINES) -> str:
        """Show the PR's diff (base -> head), for one file or all files, a page at a time.

        Lines that exist in the new file start with their new line number: cite those numbers in findings.
        """
        args = ["diff", "-U3", *DIFF_ARGS, BASE, HEAD]
        if path is not None:
            args += ["--", _safe_path(path)]
        return _page(annotate_git_diff(await run_git(root, *args)), offset, limit, f"diff {path or '(all files)'}")

    @toolset.tool_plain
    async def show_file(
        path: str, ref: Literal["base", "head"] = "base", offset: int = 0, limit: int = PAGE_LINES
    ) -> str:
        """Show a file as it was before the PR (ref="base") or after it (ref="head"), with line numbers."""
        try:
            text = await run_git(root, "show", f"{BASE if ref == 'base' else HEAD}:{_safe_path(path)}")
        except GitError:
            return f"{path} does not exist at {ref}"
        numbered = "\n".join(f"{n:>6}  {line}" for n, line in enumerate(text.splitlines(), start=1))
        return _page(numbered, offset, limit, f"{path} at {ref}")

    if workspace.previous is not None:

        @toolset.tool_plain
        async def diff_since_last_review(path: str | None = None, offset: int = 0, limit: int = PAGE_LINES) -> str:
            """Show what changed since your last review of this PR (previous head -> head), a page at a time.

            Lines that exist in the new file start with their new line number.
            """
            args = ["diff", "-U3", *DIFF_ARGS, PREVIOUS, HEAD]
            if path is not None:
                args += ["--", _safe_path(path)]
            what = f"changes since last review in {path or '(all files)'}"
            return _page(annotate_git_diff(await run_git(root, *args)), offset, limit, what)

    return toolset
