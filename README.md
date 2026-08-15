# comfyui-mcp

**An MCP server that drives a local ComfyUI - including the workflow you have open in the browser.**

[English](README.md) | [Русский](README.ru.md) | [Changelog](CHANGELOG.md)

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.13-blue)
![Tools](https://img.shields.io/badge/tools-45-green)

---

Point your assistant at ComfyUI and it can list your workflows, work out what
parameters each one takes, run them, watch the progress, and show you the result.
With the optional bridge node installed it can also read and edit **the canvas on
screen**, unsaved changes and all.

```
you: run the ideogram workflow at 1.5 megapixels, portrait
     -> describe_workflow  finds `megapixels` and `aspect_ratio` and where they land
     -> run_workflow       submits, streams progress, returns the file
     -> show_image         you look at it
```

## Why the bridge is the interesting half

ComfyUI's HTTP API knows about files, models and the queue. It knows nothing
about the workflow being edited: that lives in litegraph, inside the page, and
the only copy of its unsaved state is in the tab's memory.

The bridge turns the WebSocket ComfyUI already holds open to every client into a
request/response channel, so an assistant can read the live graph, change widget
values, rewire links, tidy the layout, take a screenshot of the canvas, and queue
the workflow the same way the Queue button does.

Everything else works without it, and the tools say plainly which of the two
setup steps is missing rather than failing vaguely.

## Requirements

- **ComfyUI** - a portable build or any local install.
- **Nothing else.** The installer fetches [uv](https://github.com/astral-sh/uv),
  which then builds an isolated `.venv` with the right Python. No global Python,
  no system packages.
- An MCP client. Eleven are supported out of the box; see below.

## Install

**Windows**

```
install.bat
```

**Linux and macOS**

```
./install.sh
```

Five steps: fetch uv, build the `.venv`, seed `.env` from the template, check the
server imports, run the test suite. Safe to run again - each step checks whether
it is already done.

Prefer a menu to remembering names? `launcher.bat` / `./launcher.sh` opens one
window over every script in the folder, and installs first if there is no `.venv`
yet.

## Tell it where ComfyUI is

```
configure_comfy.bat        ./configure_comfy.sh
```

Root folder, port, launch script, the model and export directories, the download
token. The installer opens this window by itself if it cannot resolve
`COMFYUI_ROOT`. Everything it writes goes to `.env`; see `.env.example` for the
full list of settings.

## Connect your MCP client

```
configure_clients.bat      ./configure_clients.sh
```

Generates a ready config for **Claude Code, Cursor, Kilo Code, OpenCode, LM
Studio, Cherry Studio, MiMo Code, OpenClaw, Hermes, Codex and llama.cpp** - the
right shape, the right container key, the right file format, with the absolute
paths into this checkout already filled in. An existing config is merged rather
than replaced, and a file with comments in it is never rewritten.

## Install the bridge node

```
install_node.bat           ./install_node.sh
uninstall_node.bat         ./uninstall_node.sh
```

A directory junction (symlink on Unix) into `custom_nodes`, so there is no second
copy to drift. No administrator rights needed.

**Restart ComfyUI afterwards** - custom nodes are imported once at startup. After
that, opening a tab is all it takes; `workspace_status` says whether both halves
are in place.

## What the 45 tools cover

| Group | What it is for |
|---|---|
| **Status** | is ComfyUI up, what is the VRAM and the queue doing |
| **Workflows and reference** | list workflow files, work out their parameters, search the node catalogue, read a node's schema |
| **Logs** | ComfyUI's own console, and the browser console - where a failed extension is the only place it says anything |
| **Canvas: reading** | the live graph, a screenshot of it, which workflow tabs are open, a diagnosis of what is wrong with it |
| **Canvas: editing** | widget values, properties, on-screen labels, links, node modes, layout, groups, undo |
| **Running** | run a file or the canvas, follow the progress, fetch the result, show an image |
| **Downloading models** | fetch a model to the directory ComfyUI actually reads, with progress |
| **Process and tab** | start, stop or restart ComfyUI; reload the browser tab |

Not all of them have to be offered. `configure.bat` writes one line to `.env` that
narrows the set - a switched-off tool is not registered at all, so its schema
never reaches the model's context. What is off is reported by `comfy_status`, so
the assistant does not conclude the server cannot do it.

## Security

The server, ComfyUI and the browser tab all run on your own machine, under your
own account, over stdio. Nothing leaves it except the model downloads you ask
for. What follows is what that still leaves open, because "it runs locally" is
not the whole answer.

**The download is the sharp end.** A model URL routinely arrives from somebody
else's document - a "Model Links" note inside a shared workflow, or a loader's
`properties.models` - reaches the assistant as ordinary text, and the assistant
has a downloader. So `download_model` refuses three things before it opens a
socket, and refuses a `dry_run` the same way:

- **A host you did not allow.** `COMFYUI_DOWNLOAD_ALLOW_HOSTS` ships non-empty.
  An injected instruction can name any URL; it cannot name a host that is not on
  the list.
- **A format that executes as it loads.** `.ckpt .pt .pth .bin .pkl` are pickle;
  `.safetensors` and `.gguf` are data. Refused unless
  `COMFYUI_DOWNLOAD_ALLOW_PICKLE` says otherwise.
- **A file the volume cannot hold**, plus a ceiling of your own if you want one.

A download token, if you set one, reaches the origin host and no further: the
redirect chain is walked by hand so that a signed CDN link never sees it.

**There is no authentication by default, and ComfyUI has none either.** On
localhost that is the posture you already have. If ComfyUI listens wider than
that, set `COMFYUI_BRIDGE_TOKEN` here and `COMFYUI_MCP_BRIDGE_TOKEN` in
ComfyUI's own environment - the bridge adds routes to ComfyUI's server, and one
of them restarts the process.

**The tool switch is not a security boundary**, and the tool descriptions say so.
`configure.bat` decides what is offered, which is a coarse grid: the question
about `download_model` is not whether it exists but where it may point, and that
is what the settings above are for. Your MCP client's own permission prompts are
the other half.


## Workflows

Workflow files live in `workflows/` and must be **API format** -
`Workflow -> Export (API)` in ComfyUI. A UI-format export is refused with an
explanation rather than half-read.

A workflow may ship a same-named `.md` beside it: an instruction file for whoever
fills the parameters in, served verbatim by `get_workflow_guide`. Some graphs
need input in a shape the node list cannot express, and prose gets you nowhere.

## Two languages

Every window, the installer and `.env.example` speak English or Russian.
`COMFYUI_LANG` decides; left empty it asks the machine. `--lang=en` opens any
window in the other language for one run.

The tool descriptions the model reads stay English throughout, deliberately: they
describe an interface to a program.

## When something is wrong

- A node type cannot be found, or a pack behaves as though half of it is missing:
  read `get_comfy_log`. A custom node whose import failed is simply absent from
  `/object_info`, and absent looks exactly like never installed.
- A node is on the canvas but behaves wrongly: read `get_console_log`. The
  frontend catches an extension's import error and only logs it to the browser
  console, so that is the only place it exists.
- A workspace tool fails: `workspace_status` names which of the two things is
  missing - the node, or an open tab. Neither is worth retrying.

## Licence

[GPL-3.0](LICENSE). ComfyUI itself is GPL-3.0, and the bridge half of this
project is a custom node that runs inside it, so the same terms are the honest
fit: use it, change it, distribute it - and pass the source and the credit along
with it.

Copyright (C) 2026 pytraveler
