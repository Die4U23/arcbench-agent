# Offline container execution

Generated programs, dependency lifecycle scripts, static JavaScript checks, Vitest, preview servers and Chromium run in short-lived Linux containers. The host keeps orchestration, model API calls/credentials and runtime Git operations. Missing Docker, a missing image, unsupported dependencies, timeouts and unverified cleanup fail explicitly; there is no automatic host-execution fallback.

## Setup

Use a local Docker engine in Linux-container mode. On Windows this requires a configured Docker Desktop Linux engine; the application does not install Docker, enable WSL or change system/security settings. Remote SSH/TCP Docker contexts are rejected. From this repository's trusted checkout:

```sh
docker info --format '{{.OSType}}'
docker build -f sandbox/Dockerfile -t arcbench-sandbox:1 .
```

Only build the reviewed repository Dockerfile. Never build a Dockerfile produced by the model. The runtime uses `--pull=never`; it will not replace the image behind the operator's back. Rebuild the trusted image when controller/worker code or the cache manifest changes.

The image contains Node 24.18.0, Python 3.12.6, the repository's pinned Python dependencies, Playwright Chromium and the exact npm versions in `sandbox/toolchain/package.json` / `package-lock.json`. Image building can download those reviewed dependencies. Executing generated code cannot access the network beyond its own loopback interface. New npm packages or uncached versions fail; pin available versions or deliberately review/update and rebuild the cache. Failure messages list the available exact versions. This is an intentional compatibility restriction, not a silent substitute for arbitrary online npm installation.

## Boundary and limits

Each invocation uses a non-root UID, a private container process namespace, `--network none`, a read-only root filesystem, dropped capabilities, no-new-privileges, an init reaper and CPU/memory/PID/tmpfs limits. No host home, Docker socket, device, project directory or model credential is mounted into generated execution. The only host mount is a read-only, controller-produced request file. Workspace data lives in bounded container tmpfs.

The controller imports/exports bounded regular-file snapshots, not a container-writable host worktree. Git/controller metadata, environment/credential files and dependency directories are excluded. Traversal, alternate Windows path forms, symlinks, junctions, hardlinks, special files, case aliases and size/count violations are rejected. Exports require an unchanged host input snapshot and verified container cleanup. Source-file content updates are not a multi-file atomic transaction; use one controller writer. Exported new/modified source files are normalized to ordinary 0644 files, not privileged or executable files. Invoke source scripts through their interpreter, or set permissions inside a package script when necessary.

Limits include 512-byte / 16-component relative paths, 10,000 scanned directories, 5,000 exported files, 8 MiB per file and 32 MiB total source/artifact content, bounded stdout/stderr, 2 GiB container memory, 256 PIDs and bounded workspace/temp storage. Oversized tasks fail rather than silently truncating their source. A command deadline also bounds ordinary execution. The worker quiesces other live processes in its owned PID namespace before export; the host then verifies removal of the invocation's exact owned container. A timeout or killed Docker CLI is not itself proof of cleanup.

Visual acceptance captures browser evidence in the container, then sends bounded PNG captures to the host's model reviewer. The worker cannot approve a visual reference on the model's behalf. The host validates the original manifest/case identities and keeps model interruptions as failures. Public/native browser guards and their backend share container-local `127.0.0.1:3000`; no host port is published.

Runtime Git does not inherit model/cloud tokens, executable global/system Git configuration, hooks or fsmonitor commands. Generated file tools cannot write Git metadata or credential configuration. Runtime repositories must have an owned project-local `.git` directory, not a worktree pointer or linked metadata. These protections target newly created/controlled workspaces; they do not cleanse an already compromised old repository or machine.

## Verification

The normal unit job explicitly installs Chromium for trusted browser fixtures. Existing execution tests exercise worker internals with an explicit test-only patch; separate default-controller tests verify Docker routing and refusal to fall back. A dedicated Docker CI job builds the real image and runs `tests/test_docker_integration.py` with `ARCBENCH_RUN_DOCKER_TESTS=1`. These tests cover credential sentinels, host filesystem exclusion, loopback-only networking, protected Git metadata, malicious link exports, cache misses, deadlines, unrelated-container survival, detached children and a real isolated browser/server flow with a host-side fake model reviewer. They do not invoke a paid model.

Local mocked tests alone do not establish Docker isolation. The Docker job must pass before merging. Windows Docker Desktop execution is a separate verification target; Linux CI must not be represented as a completed Windows runtime test.

## Residual risks

Docker shares kernel/runtime attack surfaces and is not a proof of perfect isolation. The local Docker daemon and the built image are trusted. Generated code can still corrupt or delete its own permitted project files. Same-container tests and reports are not tamper-proof attestation against deliberately adversarial programs. Full formal acceptance remains external.

The worker has a deadline and normal/error/timeout cleanup is verified. A host controller/daemon crash, or an adversary stopping a same-UID worker after the controller dies, can still leave a container until an operator or independent supervisor removes it. There is no claim of guaranteed cleanup after arbitrary host failure. Inspect containers bearing `arcbench.sandbox.owner` when recovering a failed controller; do not kill unrelated containers. No new persistent host supervisor or security setting is installed by this patch.

Filename exclusions are not a general secret scanner: secrets embedded in ordinary source files are not detected. Use a dedicated generated-project directory, not a personal home directory or a credential-bearing checkout.

Docker client proxy defaults are explicitly overridden with empty upper/lowercase HTTP_PROXY, HTTPS_PROXY, FTP_PROXY, ALL_PROXY and NO_PROXY variables, because the client can inject proxy credentials from config.json even when its own environment is filtered. Portable paths reject tildes (including NTFS 8.3 aliases such as git~1), as well as Windows wildcard/metacharacter names, before any host lookup or write. This intentionally excludes otherwise valid POSIX names containing those characters.

Exports reject Unicode control/format characters and non-NFC names to avoid ignored-character and normalization aliases. Case-only file or directory renames across input/output snapshots are rejected before writes, preventing an add-then-delete operation from deleting its own replacement on case-insensitive hosts.

The build seeds full registry metadata for the locked dependency graph without lifecycle scripts, then checks a fresh no-lock offline install of the reviewed toolchain. Direct pacote calls use `/opt/npm-cache/_cacache`, matching the content-cache directory that npm CLI derives from `--cache=/opt/npm-cache`; seeding the parent directory produces an unrelated cache that offline npm install cannot read. The metadata format also matches npm's full-metadata resolution request. This catches metadata/tarball drift at image-build time; it does not make arbitrary versions available. The container memory limit is 2 GiB and its temporary tmpfs limit is 1 GiB to accommodate the offline cache and browser.
