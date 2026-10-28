**English** | [简体中文](README_CN.md)

# ArcBench Agent

[![CI](https://github.com/Die4U23/arcbench-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Die4U23/arcbench-agent/actions/workflows/ci.yml)

A **requirement-driven Web development Agent** in Python. It turns requirement documents into applications through explicit planning, tool-driven implementation, verification, bounded repair, and candidate delivery.

Designed as a reusable workflow across business domains, with ARC-Bench providing the task and evaluation environment. Railway Ticket Booking is the first formally validated example; broader autonomous task completion remains an engineering goal under evaluation. Developed for **CS3604**.

## What it does

- **Requirement-driven orchestration**: normalize YAML or Markdown into a requirement tree, then track design, implementation, and local verification separately.
- **Tool-driven implementation**: inspect and edit project files, apply unique-match text replacements, and feed concrete build/test failures back into repair.
- **Layered verification**: build and test checks, source-evidence review, public-case regression, and browser/visual checks where applicable. Local success is not formal platform acceptance.
- **Bounded progress**: request, token, tool-call, and implementation-turn controls; repeated-failure and no-progress guards; candidate checkpoints for delivery.
- **Observable context reuse**: stable message prefixes, contiguous tool rounds, bounded history compression, and provider-reported cache hit/miss counters. Cache hits are not guaranteed.
- **Isolated generated execution**: generated scripts and browsers run in short-lived offline Linux containers, with a reviewed dependency cache and no silent host fallback.

Design background: [requirements](docs/PRD.md) · [technical choices](docs/TECH_SELECTION.md) · [performance methodology](docs/PERFORMANCE_REPORT.md).

## Architecture

```text
YAML / Markdown requirements
            |
            v
    Requirement tree ---> Orchestrator ---> Runtime events / delivery report
                               |
                  Plan -> Implement -> Verify -> Bounded repair
                               |          |
                 Model + file tools       v
                               |    Offline Docker sandbox
                               |    build / tests / browser
                               |          |
                               +---- evidence and failures
                                          |
                              Host-side model review
```

The host coordinates the run and calls the model service. Generated programs execute inside the sandbox; screenshots return to the host for model review. “Offline” describes generated-code execution, not model API traffic. [Execution boundary and residual risks](docs/dev/docker-execution.md).

## Generalization track and local progress

The project aims to reuse one requirements, planning, implementation, verification, and delivery workflow across notes, knowledge bases, Q&A, commerce, and travel applications. Business facts should come from task requirements, data, and tests. The formal generalization target is **one frozen Agent autonomously completing different tasks from clean workspaces, with complete acceptance results for each**.

The published entry still uses Flash and retains TicketBooking-specific compatibility paths. The following competition-track mechanisms are implemented in the local working tree; **their code is not shipped by this documentation update**:

| Mechanism under local development | Implementation |
| --- | --- |
| Task-input contracts | Pin requirements, public tests, helpers, and references by hash; retain ancestor constraints, prose obligations, dependencies, and data lifecycles |
| Dependency planning and context partitions | Schedule atomic business flows topologically, expose retrievable sources, and bind reviews to current source actually read |
| Stateful verification worlds | Explicit origins, storage, run IDs, seeds, and business clocks; initialize only SEED data and independently inspect actual SQL/HTTP state |
| Repair and candidate recovery | Compare gained, lost, and unexecuted sets by complete case identity; recreate worlds and rerun the full suite after restoring source |
| Durable budgets | Reserve and settle request, token, tool, cash, and time limits by campaign/task/stage; revalidate billing provenance and validity before each request |

As of **2026-10-28**, six **manually implemented local reference applications** have been exercised to check initialization, business state, and the original public-test workflow:

| Reference application | Domain | Local public cases | Available / published cases |
| --- | --- | --- | --- |
| Keep | Notes | 33/33 passed | 33/33 |
| BookStack | Knowledge base | 35/35 passed | 35/36; gap retained |
| Stack Overflow | Q&A | 66/66 passed | 66/68; gap retained |
| PrestaShop | Commerce | 86/86 passed | 86/86 |
| 12306 | Rail travel | 142/142 passed | 142/142 |
| Ctrip | Travel | 152/152 passed | 152/153; gap retained |

Across their separate frozen local runs, the six references passed all 514 available public cases, with zero skips and retries. BookStack, Stack Overflow, and Ctrip still retain unsuccessful whole-suite verifier results because of catalog mismatches. The six-task catalog has 514 available cases versus 518 published cases; the four-case gap remains unresolved. The [machine-readable progress summary](docs/releases/local-reference-public-cases-20261028.json) records local status, artifact hashes, and original report locations; the original run artifacts are not included in this documentation release.

These reference applications did not go through autonomous Agent generation and **do not establish cross-task autonomous completion or formal platform acceptance**. Real generation and formal acceptance for the six-task track remain pending, together with deployment, isolation/initialization, and applicable billing evidence for the online Runner. The reference-validation stage made no real model requests, paid evaluations, or platform submissions.

## Recorded results

### Latest frozen acceptance: 2026-10-20

The Railway Ticket Booking Demo passed **30/30** Stage 3 cases in [submission 3e5f0811cfe4](https://arc-bench.com/playground/task-bank/web/ticketbooking/submissions/3e5f0811cfe4). The preserved source is [`f75792c`](https://github.com/Die4U23/arcbench-agent/commit/f75792c4a7b4377b4628163abd2c3445550a21e8), tagged [`ticketbooking-flash-30of30-20261020`](https://github.com/Die4U23/arcbench-agent/tree/ticketbooking-flash-30of30-20261020).

| Metric | Frozen-run value |
| --- | ---: |
| Formal cases passed | 30 / 30 |
| Model | deepseek-flash |
| Complete responses / request attempts | 113 / 113 |
| Input / output tokens | 4,492,045 / 176,392 |
| Total tokens | 4,668,437 |
| Cache-hit / cache-miss input tokens | 4,097,280 / 394,765 |
| Whole-run input cache hit rate | **91.21%** |
| Platform-displayed duration | **28m21s** |
| Final frontend / backend self-tests | 18 / 18; 10 / 10 |
| Final local public-case regression / Chromium probes | 32 / 32; 4 / 4 |

[Release evidence](docs/releases/ticketbooking-formal30-flash-20261020.json) records the package identity, usage reconciliation, and provenance. All 113 attempts have complete responses; usage matches the final summary, but the provider bill was not verified. Local checks and formal acceptance are distinct evidence layers. The final delivery report, rather than an earlier unconfirmed 27/32 candidate log, supports the local 32/32 result.

The local Agent ZIP is 125,604 bytes, SHA-256 `8a2ec0f1c5d680426d5d6b514af852a745c66a369e144121dfd9dd6f3931f066`. Its contents match the frozen source allowing line-ending differences; the server-uploaded package hash is unavailable. The archive does not establish a reliable total build/repair count or monetary cost.

This is one task and one frozen run, **not a current-main rerun or a controlled A/B improvement claim**. In particular, this frozen source predates the current Docker sandbox, so its formal result does not validate the later isolation implementation. The 2026-10-14 record below remains historical evidence; differences between the two runs are not a controlled performance comparison.


### Earlier frozen acceptance: 2026-10-14

The Railway Ticket Booking Demo passed **30/30** Stage 3 cases in [submission 35128ac54f45](https://arc-bench.com/playground/task-bank/web/ticketbooking/submissions/35128ac54f45).

| Metric | Archived value |
| --- | ---: |
| Formal cases passed | 30 / 30 |
| Planning and generation model | deepseek-flash |
| Complete model responses | 209 |
| Input tokens | 8,286,426 |
| Output tokens | 219,017 |
| Total tokens | 8,505,443 |
| Whole-run input cache hit rate | 86.46% |

[Machine-readable release evidence](docs/releases/ticketbooking-formal30-flash-20261014.json) includes usage and local package hashes. The platform did not expose its uploaded ZIP hash, so server-package identity was not hash-verified.

This is one successful frozen submission, **not a rerun of current main or a general completion rate**. Local self-tests still had failures before delivery; a deliverable candidate was restored before formal acceptance. Requirement-node traceability was incomplete in that package. Later traceability, isolation, and other changes do not inherit this result. The 99.44% figure in the archive describes a stable request segment, not the whole run.

### Historical offline replay: stopping repeated failed tools

An archived 2026-09-28 deterministic replay repeatedly returned the same missing-file error. The older flow reached its 24-request budget; the failure guard stopped after 3 requests.

| Replay | Simulated requests | Cumulative serialized prompt characters |
| --- | ---: | ---: |
| Before guard | 24 | 185,070 |
| With repeated-failure guard | 3 | 14,758 |

[![Historical offline replay of repeated tool failures. The older flow reaches 24 simulated requests and 185070 cumulative prompt characters; the guarded flow stops at 3 requests and 14758 characters.](docs/performance/stalled-tool-curve.svg)](docs/performance/stalled-tool-data.json)

The chart uses **fake model responses and serialized characters, not provider tokens**. It demonstrates a stopping policy, not successful task completion, measured API savings, latency, or cache hit rate. Its baseline is labeled `pre-stall-70b3c36.zip`; the guarded archive is labeled `arc_agent`, without a commit pin. It is historical evidence, not a current-main benchmark. [Raw data](docs/performance/stalled-tool-data.json) · [method and other curves](docs/PERFORMANCE_REPORT.md).

## Quick start

### 1. Prepare the controller

Use 64-bit Python **3.12.6**, as pinned in [.python-version](.python-version). On Windows:

```powershell
.\scripts\setup.ps1
```

The script checks the Python version and prepares the virtual environment. Runtime dependencies are in [requirements.txt](requirements.txt); the ARC-Bench Runtime SDK is bundled in [arcbench-agent-runtime/](arcbench-agent-runtime/). [.nvmrc](.nvmrc) pins Node for local development; the sandbox image supplies its own reviewed toolchain.

### 2. Build the trusted execution image

A local Docker engine in **Linux-container mode** is required:

```sh
docker info --format '{{.OSType}}'
docker build -f sandbox/Dockerfile -t arcbench-sandbox:1 .
```

Build only this repository's reviewed Dockerfile, never one generated by the model. Building the image downloads dependencies; generated execution uses `--network none` and the pre-provisioned cache. Missing images, uncached package versions, and resource limits fail explicitly. The Agent does not install Docker, enable WSL, or change host security settings.

Read [Docker setup and limits](docs/dev/docker-execution.md) before running. Use a dedicated generated-project directory, not a home directory or a checkout containing secrets.

### 3. Inspect the command-line interface

```powershell
.\.venv\Scripts\python.exe main.py --help
```

The help command makes no model calls. To use deterministic demo mode, supply an existing requirement directory and a dedicated output directory, then add `--demo`. The legacy `demo/requirements` path printed by the setup script is not included in this checkout. Demo behavior is not evidence of model-driven task performance.

### 4. Run through ARC-Bench

The upload entry is `main.py`, with `requirements.txt`. The Runner provides `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `MODEL`; do not commit credentials. Target projects are read and edited in the explicit `--output-dir`, rather than replaced with a template.

Requirements may use `requirements.yaml`, `README.md`, or `requirements.md`; YAML takes precedence when both forms exist. Tasks with image references require image-input support from the configured Flash endpoint. The deployment must also support the local Linux Docker boundary; do not assume every hosted Runner exposes Docker.

## Runtime controls

| Control | Current upload entry behavior |
| --- | --- |
| Model | Only `deepseek-flash`; other `MODEL`, `VISUAL_MODEL`, or `VISUAL_REVIEW_MODEL` values are rejected before requests |
| Run allowance | `main.py` sets 10,000,000 total tokens and 250 model requests, overriding same-name environment values |
| Model request deadline | 300 seconds; SDK automatic retries disabled |
| Extra implementation limits | `--max-model-turns` and `--max-tool-calls` |
| Delivery reserve | Up to 400,000 tokens and 3 requests trigger early wrap-up |

Usage is checked around requests. A final response can cross the token threshold; **these controls are not a strict monetary spending cap**. Interrupted model responses retain unknown usage rather than being counted as successful zero-cost calls. Lower defaults described in older documents or library configuration do not override `main.py`.

A locally deliverable candidate can still have explicitly reported self-test failures. Build, dependency, or startup failures block that delivery path. `.arc/delivery-report.json` distinguishes local verification, delivery readiness, and pending formal acceptance. Neither a clean build nor a successful process exit means Stage 3 passed.

## How the checks are layered

- **Python regression** checks orchestration, transport, tool boundaries, review evidence, and reporting.
- **Real Docker integration** builds the reviewed image and exercises isolated scripts, offline dependencies, cleanup, filesystem boundaries, and browser/server execution.
- **Task-local checks** inspect the generated application. Ticket-booking public-case adapters and extra probes are local gates, not the complete official Playwright evaluator.
- **Platform Stage 3** is external acceptance, tied to a particular submitted package.

```sh
python -m unittest discover -s tests -v
```

CI installs Chromium for trusted fixtures. Its separate Docker job sets `ARCBENCH_RUN_DOCKER_TESTS=1` and runs `tests/test_docker_integration.py`. Mocked tests alone do not prove isolation, and Linux CI does not establish Windows Docker Desktop runtime validation. See [CI configuration](.github/workflows/ci.yml) and [verification scope](docs/dev/docker-execution.md#verification).

## Current limits

- The archived formal results cover one task and specific frozen packages; there is no established multi-task completion rate or controlled Claude Code comparison here.
- Offline character curves do not establish real token, cache, time, or cost improvements.
- The offline npm cache supports reviewed exact versions, not arbitrary online dependency installation.
- Docker is a practical boundary, not perfect isolation. The daemon and image are trusted; arbitrary host/controller crashes can leave containers requiring operator cleanup.
- Generated code can damage its permitted project files. Local tests and reports are not tamper-proof attestation.
- Visual acceptance depends on endpoint image capability and valid browser evidence. It cannot be replaced by a build-only result.

## Layout

```text
main.py                    Flash-only upload entry and fixed run controls
agent/requirements.py      YAML / Markdown requirement normalization
agent/orchestrator.py      Planning, implementation, verification, and repair
agent/llm.py                Model calls and tool conversation
agent/tools.py              Project file tools and execution routing
agent/verify.py             Local verification and result collection
agent/visual_acceptance.py  Browser evidence and visual review
sandbox/                   Trusted offline image, worker, dependency cache
arcbench-agent-runtime/    Bundled ARC-Bench Runtime SDK
tests/                     Python regression and Docker integration
docs/releases/             Frozen submission evidence
docs/performance/          Historical replay data and plots
```

## Next steps

1. Keep formal submission evidence separate from current implementation and local regression.
2. Measure cache efficiency, tokens, runtime, and costs on matched real workloads before claiming improvement.
3. Broaden task coverage while retaining explicit budget and execution boundaries.

## Project background

Developed for **CS3604**. ARC-Bench supplies the task and evaluation setting; its starter supplies the bundled Runtime SDK. This repository implements the Agent's orchestration and verification workflow.

Further reading: [PRD](docs/PRD.md) · [technical selection](docs/TECH_SELECTION.md) · [performance report](docs/PERFORMANCE_REPORT.md) · [execution security](docs/dev/docker-execution.md).
