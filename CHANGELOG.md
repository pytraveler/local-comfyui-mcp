# Changelog

[Русская версия](CHANGELOG.ru.md)

The version in `pyproject.toml`, the git tag and the release on GitHub always say
the same thing; the release workflow refuses a tag that disagrees with
`pyproject.toml`, or one that neither changelog has a section for. The notes on a
release are these two files, in both languages, and nothing is written by hand at
tag time.

## 0.1.5 - unreleased

### Fixed

- **A name containing a dot no longer loses everything after it.** Saving
  `bench_0.8mp_15s` wrote `bench_0.json`. `Path.with_suffix` reads `.8mp_15s` as
  the extension and *replaces* it instead of appending, and a name carrying a
  resolution or a version number is written with dots all the time. Nothing
  reported it: the call succeeded and answered with the truncated path, so the
  file was merely somewhere other than the name asked for - and a second save
  under a neighbouring name would have silently overwritten it. The three places
  that turn a name into a `.json` file - `resolve_path`, `_write_json` behind
  both `save_workflow` and `save_export`, and `resolve_graph_file` - now share
  one helper that appends. A name already ending in `.json` is untouched, as
  before, and the sidecar guide still resolves beside it.

## 0.1.4 - 2026-08-19

### Added

- **`COMFYUI_USER` and `COMFYUI_PASSWORD`: Basic Auth for a ComfyUI reached
  through a reverse proxy.** The bridge token guards the bridge's own routes; a
  proxy asks for credentials in front of the whole API, so the two compose rather
  than overlap. The pair is sent on the HTTP calls and on the WebSocket from one
  source - two sources drift, and that failure reads as a broken WebSocket rather
  than a credential problem. The password goes in `.env` rather than into the URL,
  where it would surface in process lists, logs and error text.

  **Empty changes nothing.** Both halves or neither: with none set the client is
  built exactly as it always was, which is the case everybody on localhost is in.
  Half a pair authenticates with nothing, so that is named outright instead of
  being left to a 401 that says nothing about the setting somebody was part way
  through filling in.

### Changed

- **`comfy_status` says why it could not reach ComfyUI.** A proxy answering 401
  looked exactly like a ComfyUI that was not running, which sends somebody off to
  start one that is already up, or to open a firewall that was never shut. Three
  cases are now told apart: nothing configured and something is asking,
  credentials sent and rejected, and any other non-200. It also reports
  `auth: "basic"` when it is on - never the credentials themselves.

- **`websockets` now needs 14.0 or newer.** The header parameter was renamed
  `additional_headers` in 14, and the old `extra_headers` is gone rather than
  deprecated: passing it raises `TypeError`, which would have been swallowed into
  a silent fall back to polling. Nothing else about the dependency changed.

## 0.1.3 - 2026-08-17

### Added

- **`close_workspace_tab`, and `switch_workspace_tab(to="new")` for a blank
  one.** Tabs could be listed and switched between; now they can be made and
  closed. "new" runs the command the + button runs. Closing deliberately does
  not use ComfyUI's matching command, because that one raises the unsaved-changes
  dialog and waits for a human - the check is made here instead, so a tab with
  unsaved work is refused with a sentence saying what to do rather than a modal
  nobody on this side can answer. The last remaining tab is refused too, and
  closing the tab on screen moves to a neighbour first so the canvas never shows
  a workflow that is no longer open.

- **`promote_workspace_inputs`: exposing an inner node's inputs on the face of
  the subgraph that holds it.** A subgraph with nothing promoted is a sealed box
  - its values are reachable only from inside, and nothing outside can be wired
  to it. A widget row and a plain socket take the same path, and the id names
  both ends: `98:12` is node 12 inside subgraph node 98, so there is no "which
  subgraph" argument and no navigating first. An input already wired inside is
  refused rather than quietly rewired.

- **`pack_workspace_subgraph`: folding nodes and groups into a subgraph, and
  dissolving one back.** A group can be named instead of listing its nodes.
  Unpacking runs before packing, so one call can take a subgraph apart and
  rebuild it differently. Ids do not survive either direction - packing replaces
  the nodes with one, unpacking hands the contents fresh ones - so read the graph
  again afterwards.

- **A `.ps1` may be the launch script**, along with `.cmd` and `.sh`. Offering
  only `.bat` was never a boundary: the setting already runs a script of the
  user's choosing, and a `.bat` can do everything a `.ps1` can, starting with
  calling PowerShell. What it could not do was carry the multi-GPU flags people
  keep in a `.ps1`. A `.ps1` is run through PowerShell with `-ExecutionPolicy
  Bypass`, which applies to that one process and changes nothing on the machine;
  the alternative would be asking somebody to loosen a machine-wide policy to
  start ComfyUI.

### Changed

- **`load_workspace` says which tab it landed in.** It never replaced the
  workflow on screen - ComfyUI opens the file in a tab of its own - but nothing
  reported that, so tabs accumulated silently and afterwards made a reload
  refuse. The reply now names the tabs and whether one was added.

### Fixed

- **A load could take over a workflow tab that had nothing to do with it.**
  ComfyUI resolves the name against its *own* saved workflows rather than the
  directory the file came from, so a file whose name matched one of the user's
  workflows filled that workflow's tab instead of opening one - leaving it
  marked modified, one Ctrl+S from overwriting real work. That is refused now,
  with `force` for when replacing it is the actual intent.

- **Two loads in quick succession opened `X (2).json` instead of reusing the
  tab.** The active workflow is set behind an await that `loadGraphData` does not
  wait for, so the second load arrived before the first had landed and missed the
  reuse. The same gap made a switch report the tab it had come *from*.

- **The launch script could point outside the ComfyUI folder.** `root / name`
  returns `name` unchanged when it is absolute - pathlib's rule, and a silent one
  - so `COMFYUI_LAUNCH_SCRIPT=C:\anything.bat` named a program outside the
  install and nothing said so. Both the settings window and `comfy_start` refuse
  that now. It was the only real risk in this setting, and it had nothing to do
  with the extension.

## 0.1.2 - 2026-08-15

### Added

- **`switch_workspace_tab`: the workflow tabs along the top of the ComfyUI
  window, reported and switched between.** These are open workflows rather than
  browser tabs - one browser tab holds all of them - and every other workspace
  tool acts on whichever one is on screen, so this is what points them at a
  different workflow. Called with no target it reports and moves nothing, which
  is how to find out what is open before naming one; a name matching two open
  tabs is refused rather than guessed. `next` and `previous` step along the bar,
  while `recent` is ComfyUI's own activation history, and a switch made here is
  written into that history exactly as a click is.

  Switching does what clicking the tab does and no more: the canvas is reloaded
  from that workflow's own stored state, through the same call the frontend
  makes. Unsaved edits in the tab being left behind are not lost - they belong to
  that workflow, which is why the bar can show one tab as modified while another
  is on screen. The store this reads was assumed to be out of reach, since the
  frontend keeps it behind a minified module; it turns out to be public after all
  through `app.extensionManager`, which is the same surface ComfyUI offers every
  extension.
