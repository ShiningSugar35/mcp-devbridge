# Development

## Source environment

### Windows

```powershell
uv venv --python 3.12
uv pip install -p .venv -e ".[dev,package]"

cd third_party\codexpro
npm ci
npm run build
cd ..\..
```

Pyright is configured to use the repository `.venv`; release checks should still pass the explicit interpreter path so local and CI behavior stay aligned.

### Linux / SteamOS build host

```bash
uv venv --python 3.12
uv pip install -p .venv -e '.[dev,package]'
cd third_party/codexpro
npm ci
npm run build
cd ../..
```

Use a Linux build host for Linux PyInstaller artifacts. The release workflow uses Ubuntu 22.04 as the compatibility baseline.

## Verification

Run the Python gates from the repository root:

```powershell
.venv\Scripts\python.exe -m ruff check src tests
.venv\Scripts\python.exe -m pyright --pythonpath .venv\Scripts\python.exe src tests
.venv\Scripts\python.exe -m pyright --pythonpath .venv\Scripts\python.exe --pythonplatform Linux src tests
.venv\Scripts\python.exe -m pytest tests -q --disable-warnings
uv lock --check
```

Run the CodexPro gates from `third_party/codexpro`:

```powershell
npm run build
npm run smoke
npm audit --omit=dev
```

The smoke suite includes the release-critical root-scan, nested-Git, async-task, durable long-run, HTTP/widget and handoff paths. The long-run smoke covers persisted restart recovery, concurrent checkpoints, evidence requirements, stale-review rejection, rework, running/unknown task completion gates, secret rejection and path/symlink containment. Root-drive scans are expected to skip inaccessible subdirectories with warnings rather than fail the complete scan.

Before committing a release candidate also run:

```powershell
git diff --check
```

and parse-check the release scripts. On a system with Bash available:

```bash
bash -n scripts/build_linux.sh scripts/install_linux.sh scripts/live_upgrade.sh scripts/prepare_runtime_linux.sh
```

## Multi-root regression contract

Changes to Gateway/CodexPro routing must preserve these invariants:

- every READY project root is active at the same time;
- an absolute target chooses the most specific containing root;
- an ambiguous relative target is rejected rather than guessed;
- `..` and symlink/junction escapes are rejected;
- Gateway-local command/program `cwd` cannot escape the routed root;
- a `task_id` returns to the engine that created it;
- an opaque CodexPro workspace handle cannot override stronger path evidence;
- scoped Git tools can discover a nested repository below a drive-root project;
- stopping one active root does not stop the shared Hub while another root remains.

`tests/test_workspace_autoroute.py` and the CodexPro smoke scripts are the primary regression coverage for this contract.

## Durable long-run regression contract

Changes to long-running orchestration must preserve these invariants:

- multi-phase work can persist a plan independently of the current MCP transport/session;
- a step cannot become `done` without evidence;
- meaningful work after a PASS makes that review stale;
- FAIL review requires actionable rework and reopens affected steps;
- PASS/completion is rejected while an attached background task is running/cancelling;
- an unknown task after a process restart is fail-closed until explicit terminal evidence is persisted;
- durable state rejects secret-looking text, stays bounded, uses guarded paths and atomic replacement;
- ordinary MCP calls remain short/poll-based; the client is never required to keep one `tools/call` open for hours;
- local `loop-handoff` persists its current phase and terminal reason even when executor/reviewer/test raises or times out.

Run targeted checks from `third_party/codexpro` with:

```powershell
npm run build
node scripts/long-run-smoke.mjs
node scripts/execute-handoff-smoke.mjs
```

Then run the complete `npm run smoke`. Native MCP Tasks support, if added later, must be capability-negotiated and must not break the ordinary-tool fallback described in `LONG_RUNNING_TASKS.md`.

## Windows packaging

Build the complete v0.8.8 release candidate with:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1 -Version 0.8.8
```

The build uses a versioned staging directory and verifies the private runtime payload before compiling the Inno Setup installer. The output installer is:

```text
release/MCPDevBridge-Setup-0.8.8.exe
```

The Inno Setup definition uses per-user installation and `DisableDirPage=no`, so the destination-directory page remains available. Do not change this back to an automatically hidden directory page without an explicit product decision.

## Linux packaging

On the Linux build host:

```bash
bash scripts/build_linux.sh 0.8.8
```

The script performs runtime preparation, CodexPro build, Python tests/lint/typecheck, the complete CodexPro smoke suite, PyInstaller, a frozen headless smoke, and tarball creation.

The output is:

```text
release/MCPDevBridge-Linux-x86_64-0.8.8.tar.gz
```

The tarball includes `install.sh`. Installation defaults to `~/.local/opt/MCPDevBridge`, while `install.sh --target-dir <path>` supports a safe custom user-writable location.

## Live upgrade

### Windows

`scripts/live_upgrade.ps1` is bundled into the frozen application. Use the detached updater when replacing a running bridge so the process hosting the current MCP session does not have to overwrite itself. The updater records only non-secret resume metadata, terminates the intended old process tree, installs the new package, refreshes the desktop shortcut, launches the new application, and restores the previously-running project set where permitted.

Use the script’s dry-run path before changing release behavior.

### Linux

`scripts/live_upgrade.sh` performs the corresponding user-level replacement and restart without writing the system base. It preserves the current install directory, including a custom location, unless explicitly directed otherwise.

## Git and release discipline

The v0.8.8 maintenance branch is `release/v0.8.8`. The repository also contains newer historical v0.9.x tags/branches. Do not rewrite, delete, or force-push those histories as part of v0.8.8 maintenance.

A production release is complete only when:

1. source gates pass from a clean candidate tree;
2. the release commit is pushed;
3. tag `v0.8.8` points to that exact commit;
4. the GitHub Release workflow produces Windows and Linux assets from that tag;
5. the published Release assets and checksums are verified;
6. the Windows installed instance/shortcut is switched to the final release asset.

Actual test counts, artifact sizes, hashes, commit IDs and final Release state are recorded in the root `进度验收.md`; do not copy stale numbers from older releases into this document.


### Entry-model regression guard

The v0.8.8 tests intentionally reject reintroducing project ownership into ServiceCoordinator. StartOptions must remain transport-only; ProjectConfig must not regain a Gateway port; automatic workspace fallback must remain stateless unless the client explicitly requests the compatibility switch tool.
