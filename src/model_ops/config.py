from __future__ import annotations

from dataclasses import dataclass

from .utils import env_bool, env_int


@dataclass(frozen=True)
class ModelOpsConfig:
    enabled: bool
    dry_run_default: bool
    benchmark_repetitions: int


def load_model_ops_config() -> ModelOpsConfig:
    return ModelOpsConfig(
        enabled=env_bool("JARVIS_MODEL_OPS_ENABLED", True),
        dry_run_default=env_bool("JARVIS_MODEL_OPS_DRY_RUN_DEFAULT", True),
        benchmark_repetitions=max(1, env_int("JARVIS_MODEL_OPS_BENCH_REPS", 1)),
    )
