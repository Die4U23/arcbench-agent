"""Explicit Flash routing policy shared by the packaged entrypoint and tests."""
from collections.abc import MutableMapping

TEXT_FLASH_MODELS = frozenset({"deepseek-flash", "deepseek-v4-flash"})
VISUAL_FLASH_MODELS = TEXT_FLASH_MODELS | {"deepseek-v4-flash-vision-exp"}


def configure_flash_models(environment: MutableMapping[str, str]) -> None:
    resolved = {}
    for field in ("MODEL", "VISUAL_MODEL", "VISUAL_REVIEW_MODEL"):
        model = environment.get(field, "").strip() or "deepseek-flash"
        allowed = TEXT_FLASH_MODELS if field == "MODEL" else VISUAL_FLASH_MODELS
        if model not in allowed:
            raise RuntimeError(f"Flash-only candidate rejects unsupported model in {field}: {model}")
        resolved[field] = model
    environment.update(resolved)
