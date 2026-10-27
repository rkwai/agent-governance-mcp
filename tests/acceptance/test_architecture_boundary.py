"""Phase 1 acceptance checks for the hard runtime/substrate boundary."""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = ROOT / "src" / "ai_nature_company"

FORBIDDEN_IMPORT_PREFIXES = frozenset(
    {
        # Model providers and inference routers.
        "anthropic",
        "cohere",
        "google.genai",
        "google.generativeai",
        "google_genai",
        "google_generativeai",
        "groq",
        "huggingface_hub",
        "litellm",
        "mistralai",
        "ollama",
        "openai",
        "replicate",
        "together",
        "transformers",
        "vertexai",
        "vllm",
        # Agent/model orchestration frameworks.
        "autogen",
        "crewai",
        "dspy",
        "langchain",
        "langgraph",
        "llama_index",
        "semantic_kernel",
        # Autonomous or background schedulers. Scheduling belongs to the host.
        "airflow",
        "apache_airflow",
        "apscheduler",
        "celery",
        "dagster",
        "prefect",
        "rq",
        "schedule",
        "temporalio",
    }
)

PROVIDER_CREDENTIAL_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "COHERE_API_KEY",
        "GOOGLE_API_KEY",
        "GROQ_API_KEY",
        "MISTRAL_API_KEY",
        "OPENAI_API_KEY",
        "TOGETHER_API_KEY",
    }
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _is_forbidden_import(module: str) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.") for prefix in FORBIDDEN_IMPORT_PREFIXES
    )


def test_ac18_substrate_has_no_model_scheduler_or_provider_credentials() -> None:
    """The substrate stays deterministic and contains no inference escape hatch."""

    violations: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT)
        for module in sorted(_imports(path)):
            if _is_forbidden_import(module):
                violations.append(f"{relative}: imports {module}")
        source = path.read_text(encoding="utf-8")
        for credential_name in sorted(PROVIDER_CREDENTIAL_NAMES):
            if credential_name in source:
                violations.append(f"{relative}: references {credential_name}")

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"].get("dependencies", [])
    for dependency in dependencies:
        normalized = dependency.split("[", 1)[0].split("=", 1)[0].split("<", 1)[0]
        normalized = normalized.split(">", 1)[0].strip().replace("-", "_")
        if _is_forbidden_import(normalized):
            violations.append(f"pyproject.toml: depends on {dependency}")

    assert violations == []


def test_ac18_external_runner_crosses_the_mcp_boundary() -> None:
    """The simulated agent host must exercise MCP, not reach into domain internals."""

    runner_path = SOURCE_ROOT / "runner.py"
    assert runner_path.is_file(), "Phase 1 requires an external-agent demo runner"

    imports = _imports(runner_path)
    assert any(module == "mcp.client" or module.startswith("mcp.client.") for module in imports), (
        "the external runner must invoke the substrate through an MCP client"
    )
    assert not any(_is_forbidden_import(module) for module in imports)
