# Local offline demo

This demo exercises the Agent entrypoint, requirement reader, orchestration, Runtime SDK events, traceability records, and a small deterministic verification step. It does not call a model and needs no API key.

From the repository root, install the project dependencies once, then run:

```powershell
python main.py demo/requirements --output-dir demo/output --type web --demo
```

The generated page is `demo/output/index.html`. Runtime event and traceability files are written under `demo/output/.arc/` by the official SDK. The SDK and generated output are not copied into the submitted source bundle as a task template.

To use the model-driven path, omit `--demo` and run through ARC-Bench so the Runner supplies `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `MODEL`. The model-driven path expects the Runner-prepared project under `--output-dir` and does not overwrite it with a bundled template.
