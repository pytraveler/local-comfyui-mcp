# Changelog

[Русская версия](CHANGELOG.ru.md)

The version in `pyproject.toml`, the git tag and the release on GitHub always say
the same thing; the release workflow refuses a tag that disagrees with
`pyproject.toml`, or one that neither changelog has a section for. The notes on a
release are these two files, in both languages, and nothing is written by hand at
tag time.

## 0.1.8 - 07.09.2026

### Added

- **The Python environment: seven tools that make installing something a decision
  rather than an accident.** The first half of a concept that continues in
  extensions; what is here is reading, planning, auditing and undoing.

  **The reason it exists is that a caller with no tool for a job does not stop
  wanting the job done** - it reaches for the shell, where nothing knows what a
  ComfyUI install is. This is `download_model`'s argument one layer down: the
  narrowest point of the chain running from a node pack's `requirements.txt` to
  `pip install` on somebody's machine.

  **`plan_packages` says what an install would change, and refuses when the answer
  is torch.** Measured on the install this was built against: asking for
  `torchvision==0.25.0` - an ordinary pin out of a pack's requirements - plans to
  replace `torch 2.11.0+cu130` with `torch 2.10.0`, the build with no CUDA. Nothing
  in the request mentions torch, nothing in the output is an error, and the install
  would succeed. Refused rather than warned about, because the cost of being wrong
  is a multi-gigabyte download and a machine that has stopped using its GPU.

  **`create_checkpoint` and `restore_checkpoint` are the undo.** A checkpoint is
  text files - every `name==version`, what is in custom_nodes and at which commit,
  which PyTorch index this install came from - so it costs kilobytes and needs no
  disk-space check. Models, inputs, outputs and the contents of custom_nodes are
  never copied; it records what was installed, not the files.

  The list is captured as `uv pip list --format=freeze`, never `uv pip freeze`: the
  latter reports a wheel install as `name @ file:///D:/a/ComfyUI/...`, the path on
  the runner that built the portable archive - 80 of 286 packages here - and a
  checkpoint written that way cannot be restored on any machine.

  **Restoring writes a checkpoint of the current state first** and names it in the
  reply, which is `load_workspace`'s guard: the tool cannot ask whether it is about
  to destroy something, so it makes the answer not matter. It is refused while
  ComfyUI is running, because that process holds the DLLs being replaced. Two
  strengths - the default puts the recorded versions back and leaves everything else
  alone, `exact=True` also uninstalls what is not on the list and names by name what
  it cannot put back before it starts. `sageattention` and `sageattn3` on this
  machine were compiled locally and are on no index at all.

  Two things about restoring were learned from the first one run against a real
  install rather than a test fixture. It installs with `--no-deps`, because a real
  ComfyUI environment is routinely not resolvable as a whole - packs install their
  requirements one at a time and pip lets a later one break an earlier one's
  constraint in silence, so uv refuses the recorded set as unsatisfiable and the
  restore does nothing. And the exact form carries the packaging tools explicitly:
  omitting pip and setuptools tells `install` to leave them alone but tells `sync` to
  delete them, which it duly planned to do.

  **The interpreter is confirmed against the running ComfyUI, not merely found.** A
  portable root routinely holds more than one python, and a package installed into
  the wrong one is a silent no-op: uv reports success, the import still fails, and
  the wrong conclusion is easy. `sys.version` from `/system_stats` is compared
  whole. A restore cannot use that, ComfyUI having to be stopped for one, so it
  checks the checkpoint's own recorded version instead.

  **`audit_packages` runs pip-audit through `uv tool run`**, so nothing is added to
  this project's dependencies - the same principle as the vendored `uv.exe`.
  Measured: 118 advisories across 19 of 286 packages, which makes a non-empty answer
  the normal state of a ComfyUI environment rather than an emergency. There is no
  `--fix` and there will not be one.

  It reads the installed distributions by path rather than resolving a requirements
  file, because a ComfyUI environment routinely holds packages that are on no index -
  `cstr`, installed from a git URL by was-node-suite, failed an entire audit by
  itself. The advisories come from OSV rather than PyPI's own service: broader
  coverage, and a different host, so the audit still answers when PyPI is what is
  down.

  **`describe_environment` reads three sources because no one of them is the
  answer.** The disk says what is installed in custom_nodes, `/object_info` says
  what registered, and `get_comfy_log` says why a pack did neither - a disabled pack
  and a pack whose import died are both simply absent from ComfyUI's own view.
  Measured here: 38 packs, two of them disabled and therefore invisible. Both of
  ComfyUI-Manager's disabled spellings are recognised, because a pack this server
  calls installed while Manager's UI calls it disabled is a disagreement the user
  experiences as a node that is present and does not work.

  Two new tool groups in `configure.bat`, both on by default. Switching the
  checkpoints off makes the rest less safe rather than more, which is what the
  warning beside it says.

### Configuration

- `COMFYUI_PYTHON` and `COMFYUI_UV` - the escape hatch for a layout the search does
  not know. Both empty by default and both found automatically.
- `COMFYUI_CHECKPOINT_DIR` - empty means `<COMFYUI_ROOT>/mcp_checkpoints`, beside
  the install it describes rather than inside this checkout.
- `COMFYUI_CHECKPOINT_KEEP`, `COMFYUI_PACKAGE_TIMEOUT`, `COMFYUI_AUDIT_TIMEOUT`.

## 0.1.7 - 2026-08-24

### Added

- **`ask_workspace` and `confirm_workspace`: put a question on the ComfyUI screen
  and wait for the person to answer it.** The first tools here whose answer comes
  from a human rather than from the graph, for the step that turns on something
  only they know - which of two results they preferred, whether a value looks
  right - instead of guessing and building on the guess. They open the frontend's
  own dialogs through `app.extensionManager.dialog`, so nothing is drawn by hand.

  **The question goes below the canvas, not over it.** A dialog is modal and closes
  on a click beside it, so on the question most worth asking - "look at this and
  tell me" - looking is what cancels it. The default surface is a bottom panel tab,
  which covers nothing and dismisses on nothing, so the graph can be panned and
  inspected while the question waits. `modal=True` asks in a dialog instead, for
  something that should interrupt rather than wait to be noticed. The panel draws
  its own buttons, so yes, no and dismiss are all reachable there whatever `kind`
  says.

  **`choices` makes it a pick rather than a typing exercise.** One click instead of
  a sentence, and the answer arrives as one of the strings that were offered rather
  than as something to parse - with `choice_index` beside it, since two options can
  read alike once phrased and an index cannot. Six or fewer are buttons, more become
  a dropdown, and `allow_other` adds a free-text box answering with `-1`. Only the
  panel can offer them: ComfyUI's dialog service has `prompt` and `confirm` and
  nothing that picks from a list, and `confirm`'s `itemList` renders a plain `<ul>`
  that cannot be clicked - so `modal=True` with choices is refused rather than
  quietly ignored.

  **One question at a time, refused rather than queued.** `prompt` and `confirm`
  both open through `dialogStore.showDialog` with the *same* key, and showDialog
  handed a key already in the stack re-shows the dialog that is there and drops
  the options it was given - including the callbacks the promise is waiting on. A
  second question would therefore neither appear nor ever be answered, so it is
  refused, naming the one in the way. ComfyUI's own prompts share that key too.

  **A deadline that leaves nothing dangling.** The wait is bounded and reports
  `timed_out` with `still_on_screen`, because a question we stopped waiting for is
  still in front of the user and still holds the dialog. The ordering that matters
  is dialog < bridge < httpx < the client's own per-call cap.

  **Dismissal is an answer, and not the one you want.** Clicking beside the box
  closes it, so `null` arrives easily; it is neither yes nor no and is reported as
  `dismissed` rather than folded into either. For `confirm_workspace` a plain
  `false` needs a deny button, which ComfyUI draws for `kind="dirtyClose"` alone -
  every other kind offers Cancel and Confirm, so Cancel lands on `null`. Passing
  `deny_label` with any other kind is refused instead of doing nothing quietly.

  The `ask` group is switched on by default and can be turned off like any other;
  a tab that predates it answers `unknown method`, which already means "reload".

### Fixed

- **`arrange_workspace` no longer makes a workflow harder to read than it was.**
  Measured against the layouts their authors had made by hand, it was producing
  three to nine times the link crossings and up to five times the total link
  length - on a 133-node workflow, 527 crossings against the author's 59. Three
  things were wrong and each is now the other way round.

  A node's column was one past everything feeding it, which drags every source to
  the far left however deep the thing that reads it sits: 71 of those 133 nodes
  landed in column 0, and one link was stretched across seventeen columns. A node
  now goes as far right as its consumers allow, so a loader sits beside the
  sampler that reads it.

  Row order was decided by looking only at what fed a node, so a node's own
  consumers had no say and the last sweep won whether or not it helped. It now
  sweeps both ways and keeps the round with the fewest crossings, and a link
  spanning several columns gets a placeholder in each one it passes through -
  without which crossing minimisation cannot see it at all.

  Heights were not chosen: each column was packed from its own top and the columns
  centred against one another, which honours the order and ignores the links. Every
  node is now placed as near as it can get to the middle of what it connects to,
  which is what the vertical travel measures - down from 114k to 11k on that same
  workflow.

  Nodes with no links at all get a column of their own rather than a row in a flow
  they are not part of: a note that sat far below the graph used to keep its height
  and strand the canvas reaching for it.

  **Groups are laid out as groups.** A group box has no membership - what is in it
  is whatever falls inside - so arranging a grouped canvas flat scatters each
  group's nodes across the columns and its box stretches to follow them. On a real
  workflow that turned a 670x670 group into 6130x900 and left eight of them as
  overlapping sheets covering the canvas: the links were measurably tidier and the
  result was unreadable, which is the whole lesson about optimising a number. The
  layout now runs inside each group first and then over the blocks they form, with
  ungrouped nodes travelling together as one more block. `only` still ignores them,
  since a caller naming the nodes has already said what they mean.

  The same four workflows now come out at 7, 4, 32 and 65 crossings against 21, 7,
  59 and 95 by hand. Worth saying plainly: fewer crossings is not the same as
  prettier, and a layered layout still does not group related nodes the way a
  person does.

## 0.1.6 - 2026-08-23

### Fixed

- **A ComfyUI orphaned by a hard kill of this server can be stopped again.**
  Stopping on a graceful exit covers the tidy case; a killed server still leaves
  ComfyUI running, and until now that orphan was also unreachable, because
  ownership lived only in memory - `comfy_stop` refused the very process it had
  started, while the port stayed taken and the model stayed resident. The pid is
  now recorded when ComfyUI is launched, and a starting server takes it back.

  **It adopts; it does not reap.** Killing whatever the record names at startup is
  the obvious shortcut and is wrong twice: a generation outlives an editor crash
  and can still be running - twenty minutes and more is ordinary here - and two MCP
  clients open at once would each shoot down the other's ComfyUI on launch. So
  ownership is restored and the decision is left to whoever is sitting there.

  **A pid alone is a number, so the start time is recorded with it.** Pids are
  reused, readily on Windows and on every platform after a reboot, so a record that
  only named a pid could later name somebody's editor. Adoption requires the start
  time to match too, and a record that fails to verify is deleted rather than kept.
  Where that stamp cannot be read the record is not written at all, since acting on
  it later could not be made safe. The check also has to mean *running* rather than
  *findable*: a process object outlives the process while any handle to it survives,
  so reading a start time succeeds for one that has already exited - which showed up
  live as `comfy_status` reporting a ComfyUI of ours that was long gone. Note that `tempfile`'s auto-deleting kinds are
  unusable for this: such a file is removed when the process is killed, which is
  precisely the case it would exist to survive.

- **A ComfyUI this server started is no longer left running when the server
  exits, and stopping it now reaches ComfyUI itself.** Two faults met here. The
  process held is the `cmd.exe` or shell the launch script runs in, and python is
  its *child*, so the POSIX path - a bare `terminate()` on that shim - killed the
  wrapper and left python holding the port and the GPU; only the Windows path
  walked the tree. And nothing stopped anything on the way out: `main`'s `finally`
  closed the HTTP client and never touched the process, so even a clean shutdown
  orphaned it. `stop` now signals the whole tree on both platforms - a process
  group on POSIX, `taskkill /T` on Windows - and a graceful exit stops it.

  **Asked before it is forced.** The old path went straight to a hard kill, which
  costs ComfyUI the chance to finish what it had open. It escalates immediately
  rather than waiting out the grace period when the ask cannot be delivered, which
  on Windows is the ordinary case: taskkill's polite form talks to windows, and a
  ComfyUI started with `CREATE_NO_WINDOW` has none to talk to.

  **A ComfyUI you started yourself is still never touched.** That gate is
  ownership, not whether something answers the port - a hand-started instance
  answers it too. For the same reason the pid is deliberately *not* written to
  disk to survive a server restart: pids are reused, and adopting one by number
  risks signalling a process that is not ours. A hard kill of the server therefore
  still leaves ComfyUI running, which is the safe direction to fail in - tying its
  lifetime to this process would take a running generation down with an IDE
  restart.

## 0.1.5 - 2026-08-19

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
