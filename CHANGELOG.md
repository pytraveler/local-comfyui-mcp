# Changelog

[Русская версия](CHANGELOG.ru.md)

The version in `pyproject.toml`, the git tag and the release on GitHub always say
the same thing; the release workflow refuses a tag that disagrees with
`pyproject.toml`, or one that neither changelog has a section for. The notes on a
release are these two files, in both languages, and nothing is written by hand at
tag time.

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
