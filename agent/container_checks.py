"""Convert container results at the trusted controller boundary."""

from .sandbox_process import SandboxInfrastructureError

from .docker_sandbox import DockerSandbox


def check_from_document(document):
    from .verify import CheckResult

    if not isinstance(document, dict) or type(document.get("passed")) is not bool:
        raise ValueError("invalid sandbox check result")
    if not isinstance(document.get("name"), str) or not isinstance(
        document.get("output"), str
    ):
        raise ValueError("invalid sandbox check text")
    code = document.get("exit_code")
    if code is not None and type(code) is not int:
        raise ValueError("invalid sandbox exit code")
    requirements = document.get("requirement_ids", [])
    statuses = document.get("case_statuses", [])
    if not isinstance(requirements, list) or any(
        not isinstance(item, str) for item in requirements
    ):
        raise ValueError("invalid sandbox requirement identities")
    if not isinstance(statuses, list) or any(
        not isinstance(pair, list)
        or len(pair) != 2
        or any(not isinstance(value, str) for value in pair)
        for pair in statuses
    ):
        raise ValueError("invalid sandbox case identities")
    return CheckResult(
        document["name"],
        document["passed"],
        code,
        document["output"],
        tuple(requirements),
        tuple(tuple(pair) for pair in statuses),
    )


def run_checks(operation, project, *, options=None, task=None, timeout=600):
    from .verify import CheckResult

    try:
        result = DockerSandbox().invoke(
            operation, project, options=options, task=task, timeout=timeout
        )
        checks = result.get("checks")
        if not isinstance(checks, list):
            raise ValueError("sandbox did not return check results")
        return tuple(check_from_document(document) for document in checks)
    except SandboxInfrastructureError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        return (CheckResult("isolated " + operation, False, None, str(exc)),)
