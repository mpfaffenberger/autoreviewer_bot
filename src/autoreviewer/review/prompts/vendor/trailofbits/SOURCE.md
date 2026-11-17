# Vendored from Trail of Bits skills

https://github.com/trailofbits/skills/tree/149d7b21f4067765b82e67029730b42d228ef4b4, licensed CC-BY-SA-4.0 (see LICENSE).
Copied **unmodified** at commit `149d7b21f4067765b82e67029730b42d228ef4b4`:

- `sharp-edges/lang-*.md` and `sharp-edges/auth-patterns.md`: `plugins/sharp-edges/skills/sharp-edges/references/`
- `insecure-defaults/*.md`: `plugins/insecure-defaults/references/`

Only the reference files are vendored. The skills' `SKILL.md` workflows assume an interactive agent that runs
`git blame`, writes report files, and starts sub-agents, none of which a read-only reviewer can do.

The security lens (`review/agents.py`, `security_references`) passes these to the model as run instructions:
`insecure-defaults/*` and `auth-patterns.md` always, and `lang-<language>.md` for each language the PR changes.
They're never edited here; to update, re-fetch the same paths at a newer commit and re-run the security eval.
