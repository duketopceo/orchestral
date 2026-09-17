"""Dataclass models for tasks, model configs, and run settings."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ModelConfig:
    slug: str
    name: str
    role: str  # orchestrator | worker | reference
    input_price_per_mtok: float
    output_price_per_mtok: float
    context: int = 128_000
    max_tokens: int = 8_192
    retry_limit: int = 2
    price_per_image: float = 0.0
    price_per_video_second: float = 0.0
    # metadata keys: provider ("openrouter"|"openai-compatible"), base_url,
    # api_key_env, modalities, vision — see docs/model-config.md
    metadata: dict[str, Any] = field(default_factory=dict)

    def supports(self, modality: str) -> bool:
        """True when the model declares the modality in metadata.modalities."""
        return modality in (self.metadata.get("modalities") or [])

    @property
    def input_price(self) -> float:
        """Price per token (not per million)."""
        return self.input_price_per_mtok / 1_000_000

    @property
    def output_price(self) -> float:
        return self.output_price_per_mtok / 1_000_000

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "role": self.role,
            "input_price_per_mtok": self.input_price_per_mtok,
            "output_price_per_mtok": self.output_price_per_mtok,
            "context": self.context,
            "max_tokens": self.max_tokens,
            "retry_limit": self.retry_limit,
            "price_per_image": self.price_per_image,
            "price_per_video_second": self.price_per_video_second,
            "metadata": self.metadata,
        }


@dataclass
class TaskSpec:
    id: str
    type: str  # one of TASK_TYPES — enforced by load_task
    prompt: str
    validation: list[str] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


# Implemented task types — dispatch lives in runner.py; load_task fails fast
# on anything else so a typo can't produce a silently-passing run.
TASK_TYPES = frozenset({
    "html", "image", "video", "multi-file", "code",
    "constraint", "needle", "sql", "extract", "api",
    "bugfix", "terminal",
})


class ConfigError(ValueError):
    """User-facing config problem — the message is meant to print bare."""


def load_yaml(path: Path | str) -> Any:
    import yaml

    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def load_models(path: Path | str = "models") -> list[ModelConfig]:
    root = Path(path)
    configs: list[ModelConfig] = []
    if not root.exists():
        return configs
    for f in sorted(root.glob("*.yaml")):
        try:
            data = load_yaml(f)
        except Exception as exc:
            raise ConfigError(f"cannot parse model config {f}: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"model config {f} must be a YAML mapping")
        for item in data.get("models", []):
            if not isinstance(item, dict):
                raise ConfigError(f"model config {f}: each entry must be a mapping")
            if str(item.get("slug", "")).startswith("~"):
                continue  # ~ prefix marks a disabled entry
            try:
                configs.append(ModelConfig(**item))
            except TypeError as exc:
                raise ConfigError(f"model config {f}: {exc}") from exc
    return configs


def load_task(path: Path | str) -> TaskSpec:
    try:
        data = load_yaml(path)
    except Exception as exc:
        raise ConfigError(f"cannot parse task spec {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"task spec {path} must be a YAML mapping")
    try:
        task = TaskSpec(**data)
    except TypeError as exc:
        raise ConfigError(f"task spec {path}: {exc}") from exc
    if task.type not in TASK_TYPES:
        raise ConfigError(
            f"task spec {path}: unknown type '{task.type}' "
            f"— expected one of {', '.join(sorted(TASK_TYPES))}"
        )
    return task


def find_task(task_id: str, root: Path | str = "tasks") -> Path | None:
    root = Path(root)
    for f in sorted(root.rglob("*.yaml")):
        try:
            data = load_yaml(f)
        except Exception as exc:
            raise ConfigError(f"cannot parse task spec {f}: {exc}") from exc
        if isinstance(data, dict) and data.get("id") == task_id:
            return f
    return None
