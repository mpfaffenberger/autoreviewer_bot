# Prism: running code (repros and regression tests)

Extends [DESIGN.md](DESIGN.md). Status: **draft**.

Running PR code changes the threat model. Before, we read untrusted *text*;
now we execute untrusted *code* written by strangers (and their bots) on
the review host. Everything below follows from that.

---

## 1. What it buys us

| Without execution | With execution |
|---|---|
| "Model A says it's a bug, model B agrees" | "Here's a failing test. It passes on `main` and fails on this PR" |
| `fixed` = the model believes the author | `fixed` = **the stored repro passes on the new head** |
| A bug that was already on `main` can block the PR | Base-vs-head runs tell **regressions** apart from **pre-existing** bugs |
| Precision depends on prompt tuning | A finding that didn't reproduce is dropped, so precision is driven by evidence |

The second row matters most for the bot-to-bot chat. When an author bot says
"fixed it", we re-run the test instead of arguing. Disagreements get settled
by evidence, not by whoever argues longest.

## 2. What we deliberately do NOT do

- **Re-run the repo's existing test suite.** That's their CI's job. Read the
  PR's existing check runs (we already hold `Checks: R/W`) and cite failures
  if useful. Running every suite twice on our GPU box is waste (DRY, YAGNI).
- **Push commits.** No repro tests committed to their branch. That would mean
  `Contents: write`, a much bigger blast radius. We *show* the test; they add it.
- **Repro every finding.** Only confirmed `high` correctness/security findings
  in a supported ecosystem, capped per PR. Race conditions, config issues and
  design-level DRY findings stay on the verifier-only path.

---

## 3. Measured constraints on this box

Measured 2026-11-16, not assumed:

| Check | Result | Consequence |
|---|---|---|
| `unshare -Ur` inside a `--cap-drop all --security-opt no-new-privileges` rootless podman container | **fails** (`uid_map: Operation not permitted`) | `BubblewrapSandbox` **cannot** run inside the reviewer container |
| `bwrap --unshare-all` on the host | works | bwrap is fine *on the host*, but as `bazzite` it can read `~/.config/prism`, the SSH keys, and Boodleton's OAuth tokens |
| `/dev/kvm` | present | Real VMs are possible |
| qemu / libvirt / firecracker / gVisor | none installed (Bazzite is immutable) | VM tooling must be layered, or shipped in a container that gets only `/dev/kvm` |
| RAM visible to Linux | 31 GiB (≈19 available), 32 cores | Sandboxes compete with inference and Boodleton, so cap them hard |

The harness's own bwrap docs say it is "not a boundary against an agent trying
to harm the host user", and recommend a separate user or a cloud sandbox for
untrusted agents. Tenant PR code is the most untrusted input we have.

**So: no execution on the host kernel, ever.** The boundary is a VM.

---

## 4. Sandbox options

| Option | Boundary | Ops | Tenant code leaves the review host? | Verdict |
|---|---|---|---|---|
| `BubblewrapSandbox(LocalWorkspace)` on the host | shared kernel, same user as all our secrets | none | no | **Rejected** |
| Podman per job via the podman socket | socket = host-level control | low | no | **Rejected.** The homelab README forbids exactly this |
| **smolvm microVM per job**, run by a dedicated `smolrunner` account (section 5) | own guest kernel (KVM via libkrun); the VMM runs as a user that can't read our secrets | low-medium | no | **Recommended self-hosted** |
| Hand-rolled qemu + golden qcow2 + egress proxy | own kernel; qemu is the most battle-tested VMM | high (qemu isn't even installed on immutable Bazzite) | no | Superseded by smolvm. Fallback if the smolvm spike fails |
| `E2BSandbox` (Firecracker microVM per sandbox) | strong, managed | ~zero | **yes, so you must disclose it** | Fallback if self-hosting stalls |

smolvm hands us almost everything the hand-rolled VM plan needed to build:

| The qemu plan needed | smolvm gives |
|---|---|
| qemu/libvirt layered onto immutable Bazzite | One binary plus a bundled libkrun; `/dev/kvm` is already `0666` here |
| A golden qcow2 image + rebuild script | **Any OCI image** (`ghcr.io/astral-sh/uv:python3.12-bookworm-slim`) |
| A 5-10 s boot | Sub-second boot after the first pull of an image |
| An allowlisting egress proxy | Built in: `allow_hosts` / `allow_cidrs` (SDK), `--block-private` (CLI) |
| Per-attempt clean state | **`branch`**: copy-on-write children of a live machine |
| A cache of installed deps | **`checkpoint`** after setup, keyed by `(repo_id, lock_hash)` |

---

## 5. `sandboxd` on smolvm (built and deployed 2026-11-16)

smolvm's own security model says the VMM runs "with the permissions of the invoking host user", and
"for hostile local co-tenants, add host-level account separation and OS confinement around the VMM
process". libkrun adds that socket networking (TSI) "uses the VMM's host network access". So the VM
is one wall, and **the account the VMM runs as is the second**.

```
reviewer (holds LLM key + GitHub tokens)                    sandboxd  (User=smolrunner, prism-sandboxd.socket)
  agent.run(..., workspace=SandboxdBackend(...))              SandboxRegistry: limits, capacity, reaper
  └─ SandboxdClient ── /run/prism/sandboxd.sock ──▶   SmolProvider: smol VM with allow_hosts
       (0660 smolrunner:sandboxd, systemd-owned)              └─ harness SmolSandboxBackend(sandbox=vm)
                                                                   └─ "libkrun VM" child process
```

**Reuse, not reinvention.** The harness already has `SmolSandbox` (pydantic/pydantic-ai#10148,
in `pydantic-ai-harness` 0.68.0). We use its `SmolSandboxBackend` for every command: literal argv,
exit 127, timeouts that kill the process tree, bounded output, gone-VM detection. We add only:

| Piece | Why upstream doesn't cover it |
|---|---|
| `SmolProvider` (`src/autoreviewer/sandbox/providers.py`) | `SmolSandbox` networking is all-or-nothing; host allowlists are #11601. We create the VM with smol's `allow_hosts` and hand it to `SmolSandboxBackend(sandbox=...)` |
| `sandboxd` + `SandboxdBackend` (`daemon.py`, `client.py`, `protocol.py`) | Upstream runs the VMM inside the agent's process, as the agent's user. Ours runs it as `smolrunner`, away from the secrets |

`SandboxdBackend` is a normal `WorkspaceBackend` + `SupportsCommands`, so `Shell`, `FileSystem`, `Coder`
etc. work unchanged. It passes core's own `pydantic_ai.workspaces.conformance` suite through the
whole chain (client, real unix socket, daemon, provider): 39 passed with a local provider, 40 passed
on real microVMs (skips: no native realpath; guest root bypasses file permissions).

**Daemon policy** (`Limits`): `max_sandboxes` (2), `max_age_s` (20 min, then reaped), CPU/memory
ceilings, `permitted_hosts` (pypi only) and `permitted_images` (digest-pinned). A client can never
ask for more than these. VMs are non-persistent and non-detached, so a daemon crash leaves no orphans.

**Layers, all verified by `deploy/sandboxd/verify.sh`:**

1. **smolvm allowlist.** Allowlisted hosts open; everything else, including raw IPs, the LAN, the
   host's LAN IP, the VM gateway and cloud metadata, is refused by smolvm itself (no firewall hits).
2. **The `smolrunner` account.** `/var/home` is `0700` + `InaccessiblePaths`; the app under
   `/opt/prism-sandboxd` is root-owned and read-only to it.
3. **systemd confinement.** `ProtectSystem=strict`, no capabilities, `/dev/kvm` only,
   `NoNewPrivileges`, `MemoryMax=8G` (VMs live in-process, so this caps all of them).
4. **uid firewall** (`deploy/sandboxd/egress.nft`). With smolvm's allowlist deliberately OFF, a VM
   still can't reach the DeepSeek box, router, Boodleton, host loopback, metadata, or non-443 ports.

### What the deploy taught us (keep these)

| Finding | Consequence |
|---|---|
| `RestrictSUIDSGID=yes` makes smolvm's rootfs extraction fail (`extract rootfs tar ... sbin/init`) | Dropped, with reasoning in the unit. Bisected with `systemd-run` |
| smolvm's guest DNS proxy ignores loopback nameservers and uses 1.1.1.1 / 8.8.8.8 | Those four resolvers on :53 allowed; the router stays blocked; fails closed if it changes |
| The VMM talks to itself over loopback UDP on an ephemeral port; blocking it hangs VM creation | Loopback UDP allowed into 32768-60999 only; loopback TCP stays blocked |
| Rejected TSI connects hang in the guest instead of failing | Only matters with layer 1 off; the backstop probe gives each target its own exec timeout |
| `Type=simple` reports active before the socket is bound | systemd socket activation: no readiness race, connections queue across restarts, lazy start |
| **Bug, fixed:** `meta skuid != smolrunner accept` doesn't match packets with no socket (kernel RSTs/ICMP, TIME_WAIT), so they fell into the rejects and hit the host's own LAN/loopback traffic for ~1h | Base chain now only `jump`s on a positive `skuid` match; idle reject counter verified flat |

### Incident: systemd fell off D-Bus (2026-11-16, 10:15-10:28)

`sudo systemd-run --pipe ... > file-in-/var/home` handed PID 1 an fd SELinux won't let `init_t`
write; PID 1 logged `Got disconnect on API bus`. For ~12 min logind couldn't create sessions and the
openpup SMS inbox returned 503 (Boodleton, Caddy, puppy stayed up). `daemon-reexec` alone didn't fix
it; clearing the leftover `run-p*.service` transient units did. **Never redirect `systemd-run --pipe`
output; run transient units without `--pipe` and read `journalctl -u`.** `verify.sh` follows this.
Check with `busctl --system call org.freedesktop.systemd1 /org/freedesktop/systemd1 org.freedesktop.DBus.Peer Ping`.

### Operating it

```sh
sudo deploy/sandboxd/install.sh     # install or update (idempotent); adds you to group sandboxd
deploy/sandboxd/verify.sh           # both layers; run after every install and smolvm upgrade
sudo deploy/sandboxd/uninstall.sh   # remove everything it created
journalctl -u prism-sandboxd # create/destroy/reap log
```

Still open: the reviewer container reaching the socket (SELinux `container_t` connecting to a socket
owned by a system service; the SMS bridge needed `SecurityLabelDisable` for the same reason), and
whether `restore_checkpoint(network=False)` can cut egress after dependency install (section 6.1).

---

## 6. Pipeline changes

Old: `lenses → verify → dedupe → anchor → post`
New: `lenses → verify → **prove** → dedupe → anchor → post`

### 6.1 Setup (deterministic code, not the LLM)

1. Get a VM, then upload the head tarball to `/work/head` and the merge-base tarball to `/work/base` (the reviewer fetches them via the GitHub API; the VM never sees a token).
2. Detect the ecosystem. **v1: Python only** (`uv.lock`, `poetry.lock`, `requirements*.txt` + `pyproject.toml`). Anything else skips the prove stage.
3. Install with the network still on, minimising code execution during install:
   `uv sync --frozen --no-build` (wheels only, no `setup.py`). If that fails, retry with builds allowed **once**. It's in a VM anyway.
4. Cut the network.

### 6.2 Prove (reproducer agent)

```python
class ReproResult(BaseModel):
    status: Literal['reproduced', 'not_reproduced', 'untestable']
    test_path: str            # e.g. repro/test_finding_42.py, never inside their tree
    test_code: str
    command: str              # e.g. uv run pytest repro/test_finding_42.py -x
    head_output: str          # trimmed
    base_output: str | None   # trimmed; None if the code under test doesn't exist on base

reproducer = Agent(
    settings.repro_model,
    deps_type=ReviewDeps,               # holds job.sandbox: our thin sandboxd client
    output_type=ReproResult,
    output_retries=3,
    instructions=(PROMPTS / 'reproducer.md').read_text(),
    toolsets=[repro_tools],             # 2 tools, below
    capabilities=[
        LocalWorkspace(clone_dir, read_only=True),   # READ their code locally, as in DESIGN.md
        FileSystem(read_only=True, tools=[*READ_TOOLS, 'grep']),
        PromptInjectionDefender(),                    # program output is attacker-controlled
    ],
)

repro_tools = FunctionToolset[ReviewDeps]()

# The run gets `workspace=SandboxdBackend(client, spec=...)`, so these use ctx.workspace like any harness tool.
@repro_tools.tool
async def write_repro(ctx: RunContext[ReviewDeps], name: str, code: str) -> str:
    """Write repro/<name>.py inside the sandbox (the only place the agent can write)."""
    await ctx.workspace.write_text(f'head/repro/{safe_name(name)}.py', code)
    return 'written'

@repro_tools.tool
async def run_repro(ctx: RunContext[ReviewDeps], name: str) -> str:
    """Run `uv run pytest repro/<name>.py -x` on head. Returns trimmed output."""
    result = await ctx.workspace.run(['uv', 'run', 'pytest', f'repro/{safe_name(name)}.py', '-x'], timeout=60)
    return trim(result.stdout + result.stderr)
```

The model never gets a general shell. It can write one test file and run
pytest on it, which is all a repro needs (YAGNI). The VM is still the security
boundary. The narrow tools are there to keep the model on task.

`SandboxdBackend` is a full workspace backend (section 5), so swapping the two
tools for `Shell`/`FileSystem` later is a capability change, not new plumbing.

Then **code, not the model,** re-runs `command` on head and base itself.
**We never trust the model's claimed output**, because the model could invent
it. Then we classify:

| Head | Base | Classification | Effect |
|---|---|---|---|
| fails | passes | **regression** | blocking, with an `executed` evidence badge |
| fails | fails | **pre-existing** | moved to the review summary as an FYI. **Does not block this PR** |
| passes | n/a | **not reproduced** | dropped (precision over recall, since every comment blocks) |
| n/a | n/a | untestable | stays on the verifier-only path |

Budgets: at most 3 findings proved per PR, 2 attempts per finding, a 60 s
timeout per test run, and 15 min wall clock per VM. Proving is best effort,
and if the VM pool is busy the review still posts with verifier-only evidence.

### 6.3 Showing the repro

Each inline comment gets a collapsed block:

````markdown
**Regression: `parse_window()` drops the last bucket** (correctness, high, reproduced)

<details><summary>Repro: fails on this PR, passes on main</summary>

```python
# repro/test_finding_42.py
def test_last_bucket_kept(): ...
```

`uv run pytest repro/test_finding_42.py -x`

```text
E   AssertionError: assert 3 == 4
```
</details>
````

Outbound rules (the poster's job, enforced in code):
- Make each fence longer than any run of backticks in the content, so untrusted output can't break out of the code block.
- Trim output to about 40 lines, run the secret redactor, and neutralise `@mentions`.
- Stack traces print local paths and env. The VM has no secrets, which is exactly why that's fine.

### 6.4 Conversations

`handle_reply` gains one tool-free path. If the reply claims a fix, or the PR
got a new push, **re-run the stored repro on the new head:**

- passes → status `fixed`, reply "Repro passes on `abc123`, resolving.", resolve the thread;
- still fails → reply with the fresh failing output.

No model is involved in that decision, so an author bot can't talk its way
past a red test. The conversation agent still handles everything else
(arguments, questions, false-positive claims).

---

## 7. State additions

```sql
repros (finding_id PK REFERENCES findings, test_path, test_code, command, ecosystem,
        lock_hash,                       -- key for the post-setup smolvm checkpoint (with repo_id)
        created_sha)
repro_runs (id PK, finding_id, sha, side TEXT CHECK(side IN ('head','base')),
            exit_code, output_trimmed, duration_ms, ran_at)
```

---

## 8. New risks and mitigations

| Risk | Mitigation |
|---|---|
| VM escape (guest kernel / libkrun bug) | The VMM runs as `smolrunner` (no access to our files, `ProtectSystem=strict`); uid-level nftables blocks the LAN; pin and update smolvm promptly. libkrun is younger than qemu or Firecracker, so this layering matters |
| Crypto-miner / fork bomb in tests | vCPU/RAM/disk caps, 60 s per run, 15 min per VM, one VM at a time |
| Exfiltrating tenant code | Per-job VM: nothing persists. The egress allowlist only allows package registries. (The shared-VM variant is weaker, hence trusted subscribers only) |
| Install-time code (`setup.py`, build backends) | `--no-build` first; it's all inside the VM regardless |
| Unsigned smolvm releases / fast-moving project | Pin the exact version, verify the SHA-256 ourselves, review upgrades, pin the SDK |
| Cross-tenant cache poisoning via checkpoints | Checkpoint keys always include `repo_id`; never share a post-setup checkpoint across repos or installations |
| Prompt injection via test output | The model still has no GitHub write; results are re-run by code; the defender reports suspicious output |
| Gaming the repro (tests detect the sandbox) | Accept it. Worst case we drop a real finding (lower recall), never post a false one |
| Private-repo code sent to E2B (if chosen) | Disclose on the landing page; make it per-installation opt-in |

---

## 9. Milestones (after DESIGN.md M3)

| # | Ships | Done when |
|---|---|---|
| M5a | **Done 2026-11-16.** smolvm spike, then `sandboxd`, `smolrunner`, uid firewall, `verify.sh` | Conformance suite passes on real VMs; both egress layers verified |
| M5b | Prove stage (Python only) on top of `SandboxdBackend`, behind a flag | Seeded-regression evals show `reproduced` with the base/head classification |
| M5c | Re-run stored repros on push and on "fixed" replies; post-setup checkpoint cache | An author bot's fix flips the thread to resolved with no LLM call; a second push skips `uv sync` |

The spike goes first because it's cheap and decides everything after it. With
smolvm, `sandboxd` is a thin shim rather than a VM platform, so there's no
longer a reason to ship on E2B first and throw that adapter away.

## 10. Open questions

1. ~~`smolrunner` account and uid firewall~~: done.
2. Is there a spare box (or the `192.168.1.46` machine) that could run `sandboxd` instead of the inference host? smolvm makes moving it trivial, since only the unix socket becomes TCP+auth.
3. Python-only v1 OK? What's the next ecosystem?
4. Should pre-existing bugs found by a repro become FYI notes in the summary (current design), GitHub issues, or nothing?
