"""Seeded vulnerabilities in this repo's own security-sensitive code, isolated and buried in real commits.

Each mutation is a plausible "tidy-up" that opens a real hole: a forgeable webhook, code execution in the
bot, shell injection from a file name, a token leaked into URLs or logs. Clean controls change security
code without changing its behaviour, to measure false alarms. Titles never hint at the hole.
"""

from __future__ import annotations

from evals.cases import CLEAN_CONTROLS, Case

SECURITY_CORPUS = "d5e9888"
"""A snapshot that has every file below."""
SUBTLE_CORPUS = "4156bde"
"""Has posting.py, the read-only workspace, and sanitized progress (#21/#22)."""

_SIGNATURE = (
    "src/autoreviewer/web/app.py",
    'if not signature.startswith("sha256=") or not verify(',
    'if not signature.startswith("sha256=") and not verify(',
)
_EXEC = (
    "src/autoreviewer/review/simplify.py",
    'compile(source, "<suggestion>", "exec", flags=ast.PyCF_ONLY_AST, dont_inherit=True)',
    'exec(compile(source, "<suggestion>", "exec"), {})  # catches errors a parse alone misses',
)
_SHELL = (
    "src/autoreviewer/review/lint.py",
    """        process = await asyncio.create_subprocess_exec(
            find_ruff_bin(), "check", "--isolated", "--no-cache", "--exit-zero", "--output-format", "json",
            "--select", TYPE_RULES, *overrides, "--", *paths,
            cwd=root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )  # fmt: skip""",
    """        command = (
            f"{find_ruff_bin()} check --isolated --no-cache --exit-zero --output-format json --select {TYPE_RULES} "
            f"--config 'lint.flake8-annotations.mypy-init-return = true' -- {' '.join(paths)}"
        )
        process = await asyncio.create_subprocess_shell(
            command, cwd=root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )""",
)
_TOKEN_URL = (
    "src/autoreviewer/review/workspace.py",
    '    await run_git(dest, "fetch", "-q", "--depth=1", "--no-tags", url, *commits, env=auth)',
    '    remote = url.replace("https://", f"https://x-access-token:{token}@") if token else url\n'
    '    await run_git(dest, "fetch", "-q", "--depth=1", "--no-tags", remote, *commits, env=auth)',
)
_LOG_REDACTION = (
    "src/autoreviewer/logs.py",
    '_SECRET_QUERY = re.compile(r"([?&](?:token|access_token|code|client_secret|jwt)=)',
    '_SECRET_QUERY = re.compile(r"([?&](?:access_token|code|client_secret|jwt)=)',
)


def _case(
    name: str, seed: tuple[str, str, str], title: str, *, host: str | None = None, anchor: str | None = None
) -> Case:
    path, old, new = seed
    return Case(name, path, old, new, title, anchor=anchor, host=host, corpus=SECURITY_CORPUS)


SHELL_ANCHOR = "-- {' '.join(paths)}"

CASES: list[Case] = [
    _case("sig_bypass", _SIGNATURE, "Tidy the webhook signature check"),
    _case("exec_suggestion", _EXEC, "Make the suggestion parse check stricter"),
    _case("ruff_shell", _SHELL, "Simplify the ruff invocation", anchor=SHELL_ANCHOR),
    _case("token_in_url", _TOKEN_URL, "Authenticate fetches without an extra header", anchor="remote = url.replace"),
    _case("log_redaction", _LOG_REDACTION, "Narrow the log redaction pattern"),
    # --- buried in the real PR that added (or touched) the code ---------------------------------------
    _case("buried_sig_bypass", _SIGNATURE, "Review on request: @chaos-emerald-reviewer review", host="759db7e"),
    _case("buried_exec", _EXEC, "Optional one-click simplifications, guided by pstack principles", host="d5e9888"),
    _case(
        "buried_ruff_shell",
        _SHELL,
        "Enforce type annotations with ruff, not a model",
        host="d3f782d",
        anchor=SHELL_ANCHOR,
    ),
    _case(
        "buried_token_url",
        _TOKEN_URL,
        "Review through a git workspace instead of a diff pasted into the prompt",
        host="62250a4",
        anchor="remote = url.replace",
    ),
    _case(
        "buried_log_redaction",
        _LOG_REDACTION,
        "Trace with Logfire: pydantic-ai agent runs, webhook requests, and logs",
        host="2ac3ec1",
    ),
    # --- subtle: plausible tidy-ups that open a hole a correctness read can miss -----------------------
    Case(
        "subtle_marker_unanchored",
        "src/autoreviewer/review/posting.py",
        '_MARKER = re.compile(r"<!-- autoreviewer:([0-9a-f]{16}) -->\\Z")',
        '_MARKER = re.compile(r"<!-- autoreviewer:([0-9a-f]{16}) -->")',
        "Simplify the fingerprint pattern",
        corpus=SUBTLE_CORPUS,
    ),
    Case(
        "subtle_writable_checkout",
        "src/autoreviewer/review/agents.py",
        "    return [LocalWorkspace(root, read_only=True), FileSystem(read_only=True, tools=sorted(READ_ONLY_TOOL_NAMES))]",  # noqa: E501
        "    return [LocalWorkspace(root), FileSystem()]",
        "Use the harness defaults for repository access",
        corpus=SUBTLE_CORPUS,
    ),
    Case(
        "subtle_unsanitized_comment",
        "src/autoreviewer/review/render.py",
        '    body = sanitize("\\n\\n".join(parts))',
        '    body = "\\n\\n".join(parts)',
        "Keep the model's formatting intact in comments",
        corpus=SUBTLE_CORPUS,
    ),
    Case(
        "subtle_unsanitized_progress",
        "src/autoreviewer/review/progress.py",
        '    return "```text\\n" + sanitize(text, limit=MAX_DETAILS).replace("```", "``\\u200b`") + "\\n```"',
        '    return "```text\\n" + text[:MAX_DETAILS].replace("```", "``\\u200b`") + "\\n```"',
        "Show progress text verbatim",
        corpus=SUBTLE_CORPUS,
    ),
    # --- clean: security code changed, behaviour identical ----------------------------------------------
    Case(
        "clean_signature_demorgan",
        _SIGNATURE[0],
        'if not signature.startswith("sha256=") or not verify(settings.webhook_secret, body, signature):',
        'if not (signature.startswith("sha256=") and verify(settings.webhook_secret, body, signature)):',
        "Read the signature check as one condition",
        corpus=SECURITY_CORPUS,
        clean=True,
    ),
    *CLEAN_CONTROLS,
]
