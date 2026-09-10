"""Pydantic models for tasks, model configs, and run settings."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class ModelConfig:
    slug: str
    name: str
    role: str  # orchestrator | worker | reference
    input_price_per_mtok: float
    output_price_per_mtok: float
    context: int = 128_000
    max_tokens: int = 8_192
    metadata: dict[str, Any] = field(default_factory=dict)

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
            "metadata": self.metadata,
        }


@dataclass
class TaskSpec:
    id: str
    type: str  # html | image | video | api | multi-file
    prompt: str
    validation: list[str] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def load_yaml(path: Path | str) -> Any:
    import yaml

    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def load_models(path: Path | str = "models") -> list[ModelConfig]:
    root = Path(path)
    configs: list[ModelConfig] = []
    if not root.exists():
        return configs
    for f in sorted(root.glob("*.yaml")):
        data = load_yaml(f)
        for item in data.get("models", []):
            configs.append(ModelConfig(**item))
    return configs


def load_task(path: Path | str) -> TaskSpec:
    data = load_yaml(path)
    return TaskSpec(**data)


def find_task(task_id: str, root: Path | str = "tasks") -> Optional[Path]:
    root = Path(root)
    for f in root.glob("*.yaml"):
        data = load_yaml(f)
        if data.get("id") == task_id:
            return f
    return None
