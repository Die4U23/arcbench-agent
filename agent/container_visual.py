"""Browser execution is isolated; model review and its credentials stay on the host."""

from __future__ import annotations

import base64
import json

from .container_checks import check_from_document
from .docker_sandbox import DockerSandbox
from .model_transport import ModelRequestInterrupted
from .sandbox_files import _safe_target
from .sandbox_process import SandboxInfrastructureError
from .verify import CheckResult, VerificationResult


def verify_isolated_visual(project, task, references, model):
    from .visual_acceptance import _read_manifest, REPORT_RELATIVE_PATH

    report = {"version": 1, "cases": [], "flows": [], "passed": False}
    checks = []
    interruption = None
    try:
        _safe_target(project.resolve(), "arcbench-visual-acceptance.json")
        manifest = _read_manifest(project, references)
        expected = {case["name"]: case for case in manifest["cases"]}
        result = DockerSandbox().invoke(
            "visual_capture",
            project,
            task=task,
            options={"references": references},
            timeout=900,
        )
        checks = [check_from_document(document) for document in result["checks"]]
        candidate = result["report"]
        if not isinstance(candidate, dict) or not isinstance(
            candidate.get("cases"), list
        ):
            raise ValueError("invalid isolated visual report")
        report = candidate
        if any(not isinstance(case, dict) for case in report["cases"]):
            raise ValueError("invalid isolated visual case record")
        if sorted(case.get("name", "") for case in report["cases"]) != sorted(expected):
            raise ValueError(
                "isolated visual report does not cover the original manifest"
            )
        if any(
            case.get("verdict") not in ("capture_pending", "repair", "interrupted")
            for case in report["cases"]
        ):
            raise ValueError(
                "worker cannot approve visual references without host model review"
            )
        seen = set()
        for capture in result["captures"]:
            name = capture["name"]
            if (
                name not in expected
                or name in seen
                or capture["reference"] != expected[name]["reference"]
            ):
                raise ValueError("unexpected isolated visual case")
            seen.add(name)
            records = [case for case in report["cases"] if case.get("name") == name]
            indices = [
                index
                for index, check in enumerate(checks)
                if check.name == "visual reference: " + name
            ]
            if len(records) != 1 or len(indices) != 1:
                raise ValueError("ambiguous isolated visual evidence")
            record = records[0]
            if record.get("verdict") != "capture_pending":
                continue  # Preserve a browser/capture failure rather than reviewing it away.
            screenshot = base64.b64decode(capture["png"], validate=True)
            if len(screenshot) > 8 * 1024 * 1024 or not screenshot.startswith(
                b"\x89PNG\r\n\x1a\n"
            ):
                raise ValueError("invalid isolated visual PNG")
            review = model.review_visual_case(
                reference=expected[name]["reference"],
                reference_dir=task,
                case=expected[name],
                screenshot=screenshot,
            )
            record.update(
                verdict=review["verdict"],
                issues=review["issues"],
                matched=review["matched"],
            )
            passed = review["verdict"] == "pass"
            checks[indices[0]] = CheckResult(
                "visual reference: " + name,
                passed,
                0 if passed else 1,
                json.dumps(record, ensure_ascii=False),
            )
    except SandboxInfrastructureError:
        raise
    except ModelRequestInterrupted as exc:
        interruption = exc
        report["model_request_interruption"] = exc.report()
        checks.append(CheckResult("isolated visual review", False, 1, str(exc)))
    except Exception as exc:
        checks.append(
            CheckResult(
                "isolated visual acceptance", False, 1, f"{type(exc).__name__}: {exc}"
            )
        )
    if not checks:
        checks.append(
            CheckResult("isolated visual acceptance", False, 1, "No checks executed")
        )
    report["passed"] = all(check.passed for check in checks)
    try:
        path = _safe_target(project.resolve(), REPORT_RELATIVE_PATH.as_posix())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except (OSError, ValueError) as exc:
        checks.append(CheckResult("visual report export", False, 1, str(exc)))
        report["passed"] = False
    if interruption is not None:
        raise interruption
    return VerificationResult(report["passed"], tuple(checks))
