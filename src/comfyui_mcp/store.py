"""Loading API-format workflow files from disk."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import Config
from .graph import Graph


class WorkflowError(ValueError):
    pass


GUIDE_SUFFIX = ".md"


def _is_api_format(data: Any) -> bool:
    return isinstance(data, dict) and any(
        isinstance(node, dict) and "class_type" in node for node in data.values()
    )


def list_workflows(cfg: Config) -> list[dict[str, Any]]:
    if not cfg.workflows_dir.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(cfg.workflows_dir.rglob("*.json")):
        entry: dict[str, Any] = {
            "name": path.relative_to(cfg.workflows_dir).with_suffix("").as_posix(),
            "path": str(path),
            "size_kb": round(path.stat().st_size / 1024, 1),
        }
        guide = path.with_suffix(GUIDE_SUFFIX)
        if guide.is_file():
            entry["guide"] = guide.name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            entry["error"] = f"unreadable: {exc}"
            items.append(entry)
            continue
        if _is_api_format(data):
            entry["nodes"] = len(data)
        else:
            entry["error"] = (
                "not API format (looks like a UI workflow export). "
                "Re-export from ComfyUI with 'Export (API)'."
            )
        items.append(entry)
    return items


def resolve_path(cfg: Config, name: str) -> Path:
    """Resolve a workflow name to a file inside the workflows directory."""
    candidate = Path(name)
    if candidate.is_absolute():
        path = candidate
    else:
        path = cfg.workflows_dir / name
        if path.suffix.lower() != ".json":
            path = path.with_suffix(".json")
        root = cfg.workflows_dir.resolve()
        if not str(path.resolve()).startswith(str(root)):
            raise WorkflowError(f"workflow name escapes the workflows directory: {name}")
    if not path.exists():
        known = ", ".join(w["name"] for w in list_workflows(cfg)) or "(none)"
        raise WorkflowError(f"workflow '{name}' not found. Available: {known}")
    return path


def load_workflow(cfg: Config, name: str) -> tuple[Graph, Path]:
    path = resolve_path(cfg, name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"{path} is not valid JSON: {exc}") from exc
    if not _is_api_format(data):
        raise WorkflowError(
            f"{path} is not an API-format workflow. In ComfyUI use "
            "Workflow -> Export (API) and save the result here."
        )
    return data, path


def guide_path(cfg: Config, name: str) -> Path | None:
    """The instruction file sitting next to a workflow, or None if it has none."""
    try:
        path = resolve_path(cfg, name)
    except WorkflowError:
        return None
    guide = path.with_suffix(GUIDE_SUFFIX)
    return guide if guide.is_file() else None


def load_guide(cfg: Config, name: str) -> tuple[str, Path]:
    """Read a workflow's instruction file.

    Raises WorkflowError naming the workflows that do have one, since a missing
    guide is not an error in itself - most workflows need no explaining.
    """
    path = resolve_path(cfg, name)  
    guide = path.with_suffix(GUIDE_SUFFIX)
    if not guide.is_file():
        documented = ", ".join(w["name"] for w in list_workflows(cfg) if w.get("guide"))
        raise WorkflowError(
            f"workflow '{name}' has no instruction file ({guide.name} does not exist). "
            f"Workflows that ship one: {documented or '(none)'}"
        )
    return guide.read_text(encoding="utf-8"), guide


def _write_json(root: Path, name: str, data: Any, overwrite: bool, what: str) -> Path:
    path = root / name
    if path.suffix.lower() != ".json":
        path = path.with_suffix(".json")
    if not str(path.resolve()).startswith(str(root.resolve())):
        raise WorkflowError(f"{what} name escapes {root}: {name}")
    if path.exists() and not overwrite:
        raise WorkflowError(f"{path} already exists; pass overwrite=True to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def save_workflow(cfg: Config, name: str, graph: Graph, overwrite: bool = False) -> Path:
    return _write_json(cfg.workflows_dir, name, graph, overwrite, "workflow")


def save_export(cfg: Config, name: str, data: Any, overwrite: bool = False) -> Path:
    """Write a graph the browser produced, outside the API-format directory.

    Deliberately not `workflows/`: `list_workflows` walks that tree recursively and
    reports anything that is not API format as an error, which is right - nothing
    there can be run otherwise. A UI export is a different kind of file with a
    different use, so it gets a different place rather than a special case.
    """
    return _write_json(cfg.export_dir, name, data, overwrite, "export")


def _saved_names(cfg: Config) -> list[str]:
    """Every graph file either directory holds, by the name that finds it again."""
    found: list[str] = []
    for root in (cfg.workflows_dir, cfg.export_dir):
        if root.exists():
            found += [p.relative_to(root).with_suffix("").as_posix() for p in sorted(root.rglob("*.json"))]
    return found


def resolve_graph_file(cfg: Config, name: str) -> Path:
    """A saved graph by name, from the workflows directory or the export directory.

    Two roots rather than one because the two formats live apart on purpose - API
    in `workflows/`, UI in `exports/` - and whoever names a file should not have to
    remember which of the two it landed in. Workflows win a tie: those are the
    curated, runnable ones, and an export is a snapshot.
    """
    candidate = Path(name)
    if candidate.is_absolute():
        if not candidate.is_file():
            raise WorkflowError(f"file not found: {candidate}")
        return candidate

    for root in (cfg.workflows_dir, cfg.export_dir):
        path = root / name
        if path.suffix.lower() != ".json":
            path = path.with_suffix(".json")
        if not str(path.resolve()).startswith(str(root.resolve())):
            raise WorkflowError(f"name escapes {root}: {name}")
        if path.is_file():
            return path

    known = ", ".join(_saved_names(cfg)) or "(none)"
    raise WorkflowError(
        f"no saved graph '{name}' in {cfg.workflows_dir} or {cfg.export_dir}. Available: {known}"
    )


def load_graph_file(cfg: Config, name: str) -> tuple[Any, Path, str]:
    """Read a saved graph and say which of the two formats it is in.

    Unlike `load_workflow` this accepts both, because the caller loading one into
    the browser can use either - and refusing a UI export here would refuse the
    file `save_workspace` writes, which is the one most likely to be loaded back.
    """
    path = resolve_graph_file(cfg, name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkflowError(f"{path} holds a {type(data).__name__}, not a workflow")
    return data, path, "api" if _is_api_format(data) else "ui"
