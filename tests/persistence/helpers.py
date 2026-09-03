from __future__ import annotations

from pathlib import Path

from rag_app.composition.p06_runtime import P06Runtime, build_p06_runtime
from rag_app.core.identifiers import deterministic_id

PROFILE_ROOT = Path(__file__).resolve().parents[2] / "configs" / "profiles"


def runtime_with_kb(
    data_dir: Path,
    *,
    profile_name: str = "dev-p06-memory.json",
) -> tuple[P06Runtime, str, str]:
    runtime = build_p06_runtime(PROFILE_ROOT / profile_name, data_dir=data_dir)
    project_id = deterministic_id("prj", profile_name, "test-project")
    knowledge_base_id = deterministic_id("kb", project_id, "test-kb")
    runtime.control.put_project(project_id, "Test Project")
    runtime.control.put_knowledge_base(
        knowledge_base_id,
        project_id,
        "Test KB",
        profile_id=runtime.components.profile.profile_id,
    )
    return runtime, project_id, knowledge_base_id
