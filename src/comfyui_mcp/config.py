"""Runtime configuration.

Every setting comes from an environment variable. Values are read from the process
environment first, then from a `.env` file, then from the defaults below. Only the
defaults that are true for any ComfyUI install live here - machine-specific values
(where ComfyUI is installed, which launch script to use) belong in `.env`; see
`.env.example` for the documented template.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_COMFY_ROOT = PROJECT_ROOT / "ComfyUI"

DEFAULT_DOWNLOAD_HOSTS = "huggingface.co,hf.co,civitai.com,github.com,githubusercontent.com"


@dataclass(frozen=True)
class Config:
    # connection 
    host: str
    port: int

    # filesystem
    comfy_root: Path
    launch_script: str
    workflows_dir: Path
    export_dir: Path

    # timeouts, seconds
    request_timeout: float
    object_info_timeout: float
    startup_timeout: float
    run_timeout: float
    poll_interval: float
    startup_poll_interval: float
    stop_grace: float
    ws_recv_timeout: float
    ws_ping_interval: float
    bridge_timeout: float
    download_timeout: float

    # workspace bridge
    bridge_token: str

    # which tools are offered
    tools: str

    # Language of the settings windows and the installer - `en`, `ru`, or empty
    # for the machine's own. Held raw, the way `tools` is: `i18n.resolve` turns it
    # into a language, and an empty value has to survive that far to mean
    # "ask the machine" rather than "English".
    lang: str

    # downloading models
    download_token: str
    download_retries: int
    download_allow_hosts: str
    download_allow_pickle: bool
    download_max_gb: float

    # limits
    fallback_seed_max: int
    preview_max_edge: int
    screenshot_max_edge: int
    node_list_limit: int
    model_list_limit: int
    log_tail_lines: int
    graph_max_chars: int

    # behaviour
    free_on_switch: bool
    free_vram_min_fraction: float

    # provenance
    env_file: Path | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}/ws"

    @property
    def comfy_dir(self) -> Path:
        return self.comfy_root / "ComfyUI"

    @property
    def models_dir(self) -> Path:
        return self.comfy_dir / "models"

    def media_dir(self, kind: str) -> Path:
        """Map a ComfyUI output `type` ("output"/"temp"/"input") to a directory."""
        return self.comfy_dir / {"output": "output", "temp": "temp", "input": "input"}.get(kind, "output")

    @property
    def readable_roots(self) -> tuple[Path, ...]:
        return (
            self.comfy_dir / "output",
            self.comfy_dir / "temp",
            self.comfy_dir / "input",
            self.workflows_dir,
        )


class ConfigError(ValueError):
    """A COMFYUI_* variable is present but unusable."""


def _str(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


def _num(name: str, default: float, cast: type) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _int(name: str, default: int) -> int:
    return int(_num(name, default, int))


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    if raw.strip().lower() in ("1", "true", "yes", "on"):
        return True
    if raw.strip().lower() in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean (true/false), got {raw!r}")


def _float(name: str, default: float) -> float:
    return float(_num(name, default, float))


def find_env_file() -> Path | None:
    """Locate the .env file: explicit override, then repo root, then cwd."""
    explicit = os.environ.get("COMFYUI_MCP_ENV_FILE")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"COMFYUI_MCP_ENV_FILE points at a missing file: {path}")
        return path
    for candidate in (PROJECT_ROOT / ".env", Path.cwd() / ".env"):
        if candidate.is_file():
            return candidate
    return None


def example_env_file(lang: str = "") -> Path:
    """The template a fresh `.env` is copied from, in the reader's language.

    `.env.example` is mostly comments explaining what each setting is for, and it
    is copied into the user's own `.env` - so an untranslated one is not merely
    unhelpful once, it stays in their working file. A language with no template of
    its own falls back to the English, which is the canonical name and the one the
    documentation refers to.
    """
    localised = PROJECT_ROOT / f".env.example.{lang}"
    return localised if lang and localised.is_file() else PROJECT_ROOT / ".env.example"


def set_in_env_text(text: str, key: str, value: str) -> str:
    """Rewrite one `KEY=value` line in a .env file, textually.

    Textually for the reason the client configs are rewritten that way: a .env is
    mostly comments explaining what each setting is for, and a parse-and-reserialise
    pass throws all of it away. So an active line is replaced in place, and a new
    setting lands directly under the commented example that documents it - which is
    where somebody reading the file will look for it.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    quoted = value if value == value.strip() and "#" not in value else f'"{value}"'
    replacement = f"{key}={quoted}"

    active = [i for i, line in enumerate(lines) if line.strip().startswith(f"{key}=")]
    if active:
        lines[active[-1]] = replacement
        return newline.join(lines) + (newline if text.endswith(("\n", "\r")) else "")

    commented = [
        i
        for i, line in enumerate(lines)
        if line.lstrip().startswith("#") and line.lstrip("# \t").startswith(f"{key}=")
    ]
    if commented:
        lines.insert(commented[-1] + 1, replacement)
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(replacement)
    return newline.join(lines) + newline


def load_config() -> Config:
    env_file = find_env_file()
    if env_file is not None:
        load_dotenv(env_file, override=False)

    return Config(
        host=_str("COMFYUI_HOST", "127.0.0.1"),
        port=_int("COMFYUI_PORT", 8188),
        comfy_root=_path("COMFYUI_ROOT", DEFAULT_COMFY_ROOT),
        launch_script=_str("COMFYUI_LAUNCH_SCRIPT", "run_nvidia_gpu.bat"),
        workflows_dir=_path("COMFYUI_WORKFLOWS_DIR", PROJECT_ROOT / "workflows"),
        export_dir=_path("COMFYUI_EXPORT_DIR", PROJECT_ROOT / "exports"),
        request_timeout=_float("COMFYUI_REQUEST_TIMEOUT", 60),
        object_info_timeout=_float("COMFYUI_OBJECT_INFO_TIMEOUT", 120),
        startup_timeout=_float("COMFYUI_STARTUP_TIMEOUT", 180),
        run_timeout=_float("COMFYUI_RUN_TIMEOUT", 900),
        poll_interval=_float("COMFYUI_POLL_INTERVAL", 1.0),
        startup_poll_interval=_float("COMFYUI_STARTUP_POLL_INTERVAL", 2.0),
        stop_grace=_float("COMFYUI_STOP_GRACE", 20),
        ws_recv_timeout=_float("COMFYUI_WS_RECV_TIMEOUT", 30),
        ws_ping_interval=_float("COMFYUI_WS_PING_INTERVAL", 20),
        bridge_timeout=_float("COMFYUI_BRIDGE_TIMEOUT", 20),
        download_timeout=_float("COMFYUI_DOWNLOAD_TIMEOUT", 60),
        bridge_token=_str("COMFYUI_BRIDGE_TOKEN", ""),
        tools=_str("COMFYUI_TOOLS", "all"),
        lang=_str("COMFYUI_LANG", ""),
        download_token=_str("COMFYUI_DOWNLOAD_TOKEN", ""),
        download_retries=_int("COMFYUI_DOWNLOAD_RETRIES", 5),
        download_allow_hosts=_str("COMFYUI_DOWNLOAD_ALLOW_HOSTS", DEFAULT_DOWNLOAD_HOSTS),
        download_allow_pickle=_bool("COMFYUI_DOWNLOAD_ALLOW_PICKLE", False),
        download_max_gb=_float("COMFYUI_DOWNLOAD_MAX_GB", 0),
        fallback_seed_max=_int("COMFYUI_FALLBACK_SEED_MAX", 2**31 - 1),
        preview_max_edge=_int("COMFYUI_PREVIEW_MAX_EDGE", 1024),
        screenshot_max_edge=_int("COMFYUI_SCREENSHOT_MAX_EDGE", 1400),
        node_list_limit=_int("COMFYUI_NODE_LIST_LIMIT", 60),
        model_list_limit=_int("COMFYUI_MODEL_LIST_LIMIT", 100),
        log_tail_lines=_int("COMFYUI_LOG_TAIL_LINES", 200),
        graph_max_chars=_int("COMFYUI_GRAPH_MAX_CHARS", 40000),
        free_on_switch=_bool("COMFYUI_FREE_ON_SWITCH", True),
        free_vram_min_fraction=_float("COMFYUI_FREE_VRAM_MIN_FRACTION", 0.2),
        env_file=env_file,
    )
