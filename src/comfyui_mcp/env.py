"""Reasoning about a Python environment: what is in it, and what a change would do to it.

Pure, no I/O, fully unit-tested - the same rule `graph.py` and `logs.py` follow, and
for the same reason: everything here decides whether a `pip install` is allowed to
happen, and a decision that can only be tested against a live 286-package install is
a decision nobody will test.

The module exists because of one measured sentence. On the install this was written
against, asking uv for `torchvision==0.25.0` - an ordinary-looking pin, the kind that
sits in a custom node's `requirements.txt` - produces this plan:

     - torch==2.11.0+cu130 (from file:///D:/a/ComfyUI/cu130_python_deps/torch-...whl)
     + torch==2.10.0
     - torchvision==0.26.0+cu130
     + torchvision==0.25.0

`torch 2.10.0` is the PyPI build. It has no CUDA. Nothing in the request said the word
torch, nothing in the output is an error, and the install would succeed. `parse_plan`
reads that, `plan_risks` refuses it, and both run before a single byte is downloaded.

Three facts about a real ComfyUI environment shape everything below, and each was
measured on `V:\\Programs\\Comfy\\Comfyui_portable` rather than assumed:

- **A version's local segment is the whole story.** `torch==2.11.0+cu130` exists only
  on download.pytorch.org. A resolver that has not been pointed there does not fail -
  it finds a plain `torch` on PyPI and treats the move as an upgrade.
- **`direct_url.json` is noise on a portable build, not provenance.** 80 of 286
  distributions record `file:///D:/a/ComfyUI/cu130_python_deps/...`, a path on the
  GitHub Actions runner that built the archive. So PEP 610 cannot be asked which
  packages were built locally; it answers "all of them".
- **Some packages are on no index at all.** `sageattention==2.2.0` and
  `sageattn3==1.0.0` were compiled on that machine - the build scripts are still in
  the install root. A restore that removes them does not put them back, so a restore
  has to name them before it starts rather than after.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

RESTORE_SKIP = frozenset(
    {"pip", "setuptools", "wheel", "distribute", "uv", "pkg-resources", "_distutils-hack"}
)

PINNED_FAMILY = frozenset(
    {"torch", "torchvision", "torchaudio", "xformers", "torchsde", "triton", "triton-windows"}
)

_TORCH_INDEX = "https://download.pytorch.org/whl/%s"

_ENVIRONMENT_LINE = re.compile(r"^Using .*? environment at:\s*(.+?)\s*$", re.M)

_RESOLVED_LINE = re.compile(r"^Resolved (\d+) package", re.M)

_MOVE_LINE = re.compile(r"^\s*([+-])\s+(\S+)==(\S+?)(?:\s+\(from\s.*\))?\s*$", re.M)

_NO_CHANGES = "Would make no changes"

_NAME_END = re.compile(r"[=<>!~\[; ]")


def normalise(name: str) -> str:
    """A distribution name in the one form two spellings can be compared in.

    PEP 503: case is not significant and `-`, `_` and `.` are the same character. The
    same pack is `ComfyUI-KJNodes` in a repository name, `comfyui-kjnodes` in the
    registry and `comfyui_kjnodes` in an import, and a comparison that misses that
    reports a package as newly installed when it merely changed its spelling.
    """
    return re.sub(r"[-_.]+", "-", name.strip().lower())


@dataclass(frozen=True)
class Pin:
    """One `name==version` line, as a checkpoint records it."""

    name: str  # normalised
    version: str
    raw: str = ""

    @property
    def local(self) -> str:
        """The `+cu130` part, without the plus. Empty when there is none.

        PEP 440 calls this the local version segment, and it is the flag that says
        this exact build does not exist on PyPI.
        """
        _, plus, tail = self.version.partition("+")
        return tail if plus else ""

    @property
    def line(self) -> str:
        return f"{self.name}=={self.version}"


def parse_name(line: str) -> str:
    """The distribution name out of any requirement-ish line, normalised."""
    return normalise(_NAME_END.split(line.strip(), maxsplit=1)[0])


def parse_pin(line: str) -> Pin | None:
    """One freeze line, or None for anything that is not a pin.

    Deliberately strict about `==`. `uv pip freeze` on a portable ComfyUI emits

        aiohttp @ file:///D:/a/ComfyUI/cu130_python_deps/aiohttp-...whl

    and a checkpoint written that way cannot be restored anywhere - that path exists
    only on the runner that built the archive. `uv pip list --format=freeze` prints
    plain `name==version` for the same environment, which is what `packages.freeze`
    asks for and what this parses. A line arriving in the other shape is dropped
    rather than half-understood.
    """
    text = line.strip()
    if not text or text.startswith("#") or "==" not in text:
        return None
    name, _, version = text.partition("==")
    version = version.split(" ")[0].split(";")[0].strip()
    name = name.strip()
    if not name or not version or " " in name:
        return None
    return Pin(name=normalise(name), version=version, raw=text)


def parse_freeze(text: str) -> list[Pin]:
    """Every pin in a freeze listing, sorted by name, duplicates dropped."""
    seen: dict[str, Pin] = {}
    for line in text.splitlines():
        pin = parse_pin(line)
        if pin is not None:
            seen.setdefault(pin.name, pin)
    return sorted(seen.values(), key=lambda p: p.name)


def format_freeze(pins: list[Pin]) -> str:
    """The text a checkpoint stores. One pin per line, newline-terminated."""
    return "\n".join(p.line for p in pins) + ("\n" if pins else "")


def torch_index_url(pins: list[Pin]) -> str:
    """Which PyTorch index this environment came from, read off its own version tags.

    `+cu130` names the index that can supply it, and without that index a restore does
    not fail - it finds a plain `torch` on PyPI and silently installs the CPU build.
    Empty when nothing carries a local segment, which is the plain-CPU case and needs
    no extra index at all.
    """
    for pin in pins:
        if pin.name in ("torch", "torchvision", "torchaudio") and pin.local:
            return _TORCH_INDEX % pin.local
    return ""


def unrestorable(pins: list[Pin]) -> list[Pin]:
    """Pins whose exact build no ordinary index can supply.

    A local version segment says so outright. It is a lower bound and not the whole
    answer - `sageattention==2.2.0` carries no segment and is on no index either - so
    the honest test is a dry run against the index, which needs the network and
    therefore lives in `packages.py`. This is what can be said for nothing, and it is
    said before a restore rather than after it.
    """
    return [p for p in pins if p.local]


@dataclass(frozen=True)
class Move:
    """One line of a plan: what a package is now, and what it would become.

    `before` empty means an install, `after` empty means a removal, both present means
    the version moves. uv prints a move as two lines and they are paired here by name,
    because the pair is the thing worth judging: a `-` on its own is data loss and a
    `-`/`+` pair that drops a local segment is the CUDA build going away.
    """

    name: str
    before: str = ""
    after: str = ""

    @property
    def kind(self) -> str:
        if not self.before:
            return "install"
        if not self.after:
            return "remove"
        return "change"

    @property
    def loses_local(self) -> bool:
        """The measured failure: `2.11.0+cu130` becoming `2.10.0`."""
        before_local = self.before.partition("+")[2]
        after_local = self.after.partition("+")[2]
        return bool(before_local) and before_local != after_local


@dataclass(frozen=True)
class Plan:
    """What `uv pip install --dry-run` says it would do."""

    moves: tuple[Move, ...] = ()
    resolved: int = 0
    environment: str = ""
    no_changes: bool = False
    raw: str = ""

    @property
    def installs(self) -> list[Move]:
        return [m for m in self.moves if m.kind == "install"]

    @property
    def removals(self) -> list[Move]:
        return [m for m in self.moves if m.kind == "remove"]

    @property
    def changes(self) -> list[Move]:
        return [m for m in self.moves if m.kind == "change"]

    def as_dict(self) -> dict:
        return {
            "no_changes": self.no_changes,
            "resolved": self.resolved,
            "environment": self.environment,
            "install": [{"name": m.name, "version": m.after} for m in self.installs],
            "change": [
                {"name": m.name, "from": m.before, "to": m.after} for m in self.changes
            ],
            "remove": [{"name": m.name, "version": m.before} for m in self.removals],
        }


def parse_plan(text: str) -> Plan:
    """Read a `uv pip install --dry-run` report.

    The format is four counted headings followed by the `-`/`+` lines themselves, and
    only the lines are parsed: the counts restate them, and a count without its lines
    would be a number nobody can act on. `Would make no changes` is its own answer and
    not an empty list, because "nothing to do" and "could not tell" must not look alike.
    """
    env_match = _ENVIRONMENT_LINE.search(text)
    resolved_match = _RESOLVED_LINE.search(text)

    before: dict[str, str] = {}
    after: dict[str, str] = {}
    order: list[str] = []
    for sign, name, version in _MOVE_LINE.findall(text):
        key = normalise(name)
        if key not in order:
            order.append(key)
        (before if sign == "-" else after)[key] = version

    moves = tuple(
        Move(name=key, before=before.get(key, ""), after=after.get(key, ""))
        for key in order
    )
    return Plan(
        moves=moves,
        resolved=int(resolved_match.group(1)) if resolved_match else 0,
        environment=env_match.group(1) if env_match else "",
        no_changes=_NO_CHANGES in text and not moves,
        raw=text,
    )


@dataclass(frozen=True)
class Risk:
    """Why a plan is refused, in the words the caller needs to act on."""

    name: str
    rule: str  # pinned | loses_cuda | removed | unrestorable
    detail: str


def plan_risks(plan: Plan, installed: list[Pin] | None = None, protect: frozenset[str] | None = None) -> list[Risk]:
    """Everything in this plan that must not happen silently.

    Four rules, most specific first, and **at most one fires per move**. They overlap
    heavily on a real plan - a torch downgrade is pinned *and* unrestorable *and*
    arguably a removal - and reporting all three is three sentences about one fact,
    which reads as three problems. Measured on the plan this module was written against:
    two moves produced four risks before the rules were made exclusive, and the second
    two said nothing the first two had not.

    A rule that fires on a healthy plan is worse than no rule - that is `diagnose`'s
    constraint applied to a guard - so none of these fires on an install that only adds
    packages, which is what installing a custom node's requirements normally is.
    """
    protected = PINNED_FAMILY if protect is None else (PINNED_FAMILY | protect)
    have = {p.name: p for p in (installed or [])}
    out: list[Risk] = []

    for move in plan.moves:
        if move.kind == "install":
            continue

        if move.name in protected:
            out.append(
                Risk(
                    name=move.name,
                    rule="pinned",
                    detail=(
                        f"{move.name} {move.before or '?'} -> {move.after or 'removed'}. "
                        "Its version is decided by which CUDA build this install has, not by "
                        "what a requirement asks for."
                    ),
                )
            )
        elif move.loses_local:
            out.append(
                Risk(
                    name=move.name,
                    rule="loses_cuda",
                    detail=(
                        f"{move.name} {move.before} -> {move.after}: the +{move.before.partition('+')[2]} "
                        "build would be replaced by an ordinary index build, which is a different "
                        "binary for a different accelerator."
                    ),
                )
            )
        elif move.kind == "remove":
            out.append(
                Risk(
                    name=move.name,
                    rule="removed",
                    detail=f"{move.name} {move.before} would be uninstalled and nothing replaces it.",
                )
            )
        elif (pin := have.get(move.name)) is not None and pin.local:
            out.append(
                Risk(
                    name=move.name,
                    rule="unrestorable",
                    detail=(
                        f"{move.name} is installed as {pin.version}, a build no ordinary index "
                        "carries; if this move is wrong there is nothing to install back from."
                    ),
                )
            )

    return out


def removal_losses(plan: Plan, installed: list[Pin]) -> list[Pin]:
    """Of the packages this plan removes, the ones nothing could put back.

    Asked before an exact restore - `uv pip sync`, the form that also uninstalls what is
    not on the recorded list - and it is the reason that form is offered rather than
    assumed. Everything it removes was installed after the checkpoint, which is usually
    a node pack's dependency and occasionally something that took an hour to compile.

    Measured here: `sageattention==2.2.0` and `sageattn3==1.0.0` were built on the
    machine, and the build scripts are still in the install root. A sync that removes
    them does not put them back; PyPI has nothing under those names for this pair of
    Python and CUDA. A local version segment says the same thing about `torch`.

    This is a lower bound and says so: the only complete answer is asking an index for
    every name, and a rule that costs 286 network round trips before a rollback is a
    rule nobody waits for. What is caught is the class of package that is expensive to
    lose, which is what the refusal is for.
    """
    have = {p.name: p for p in installed}
    out = []
    for move in plan.removals:
        pin = have.get(move.name)
        if pin is not None and pin.local:
            out.append(pin)
    return out


def plan_is_additive(plan: Plan) -> bool:
    """Whether this plan only adds packages. The common, uninteresting, safe case.

    Installing a node pack's requirements into an environment that already has most of
    them is normally exactly this, and saying so plainly is what stops the refusals from
    reading as though every install were dangerous.
    """
    return bool(plan.moves) and all(m.kind == "install" for m in plan.moves)


@dataclass(frozen=True)
class Change:
    """One line of a before/after comparison of two environments."""

    name: str
    before: str = ""
    after: str = ""


@dataclass(frozen=True)
class Diff:
    added: tuple[Change, ...] = ()
    removed: tuple[Change, ...] = ()
    changed: tuple[Change, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def as_dict(self) -> dict:
        return {
            "added": [{"name": c.name, "version": c.after} for c in self.added],
            "removed": [{"name": c.name, "version": c.before} for c in self.removed],
            "changed": [
                {"name": c.name, "from": c.before, "to": c.after} for c in self.changed
            ],
        }


def diff(before: list[Pin], after: list[Pin]) -> Diff:
    """What happened to an environment between two freezes.

    Reported after every operation that touches packages, because the plan is what uv
    *said* it would do and this is what it did. The two disagree whenever a post-install
    script of a node pack runs its own pip, which is common enough to be the normal case
    rather than the exception.
    """
    b = {p.name: p.version for p in before}
    a = {p.name: p.version for p in after}
    return Diff(
        added=tuple(Change(n, after=a[n]) for n in sorted(set(a) - set(b))),
        removed=tuple(Change(n, before=b[n]) for n in sorted(set(b) - set(a))),
        changed=tuple(
            Change(n, before=b[n], after=a[n])
            for n in sorted(set(a) & set(b))
            if a[n] != b[n]
        ),
    )


def restorable(pins: list[Pin]) -> list[Pin]:
    """The pins an `install` restore writes: everything but the packaging tools.

    Omitting them is how `install` is told to leave them alone. Pinning pip back is the
    way an environment ends up unable to install its way out of the state the restore
    was meant to fix.
    """
    return [p for p in pins if p.name not in RESTORE_SKIP]


def sync_list(recorded: list[Pin], installed: list[Pin]) -> list[Pin]:
    """The pins a `sync` restore writes: the recorded set, plus the packaging tools as
    they are *now*.

    `RESTORE_SKIP` means "leave these alone", and the two verbs hear that from opposite
    directions. For `install`, omitting a name says nothing about it. For `sync`,
    omitting a name says **uninstall it** - so the same list that protects pip under one
    verb deletes it under the other.

    Measured on a real checkpoint of this install: a sync of the 282 restorable pins
    planned `- setuptools==81.0.0` and `- wheel==0.47.0`, which is how an environment
    loses the ability to build anything at install time and how a node pack's next
    install starts failing for a reason nobody connects to a rollback.

    They are carried at their installed versions rather than their recorded ones, which
    is what "leave them alone" actually means: neither removed nor moved.
    """
    keep = {p.name for p in recorded}
    tools = [p for p in installed if p.name in RESTORE_SKIP and p.name not in keep]
    return sorted(recorded + tools, key=lambda p: p.name)


def installed_since(recorded: list[Pin], installed: list[Pin]) -> list[Pin]:
    """What is installed now and was not in the checkpoint, packaging tools aside.

    Named after a default restore, which puts the recorded versions back and stops - so
    these are still here. The packaging tools are excluded because they are deliberately
    not in the recorded list at all, and reporting pip as "installed since the
    checkpoint" is a claim about the user's environment that is simply untrue.
    """
    known = {p.name for p in recorded} | RESTORE_SKIP
    return [p for p in installed if p.name not in known]


def python_matches(candidate_version: str, reported_version: str) -> bool:
    """Whether the interpreter found on disk is the one ComfyUI is running on.

    Compared whole and then, failing that, on the leading dotted number. The second
    form is the fallback for a ComfyUI old enough to report something trimmed; it is
    weaker on purpose and `python_mismatch` says which of the two answered.
    """
    a, b = candidate_version.strip(), reported_version.strip()
    if not a or not b:
        return False
    if a == b:
        return True
    return _leading_version(a) == _leading_version(b) != ""


def _leading_version(text: str) -> str:
    match = re.match(r"\s*(\d+(?:\.\d+)*)", text)
    return match.group(1) if match else ""


def python_mismatch(candidate_version: str, reported_version: str) -> str:
    """The refusal text when the two disagree. Empty when they do not.

    A refusal rather than a warning: everything downstream of this writes into an
    interpreter, and the cost of being wrong is a package installed where nothing will
    ever import it, plus a caller convinced the problem is elsewhere.
    """
    if python_matches(candidate_version, reported_version):
        return ""
    return (
        f"the interpreter found on disk reports {candidate_version.splitlines()[0].strip()!r} "
        f"but the running ComfyUI reports {reported_version.splitlines()[0].strip()!r}. "
        "Those are different interpreters, and installing into the wrong one changes nothing "
        "that ComfyUI can see. Set COMFYUI_PYTHON to the interpreter ComfyUI actually runs on."
    )


COMFY_METADATA = "pyproject.toml"

DISABLED_DIR = ".disabled"
DISABLED_SUFFIX = ".disabled"

NOT_A_PACK = frozenset({"__pycache__", DISABLED_DIR, ".git", "node_modules"})


@dataclass
class Pack:
    """One entry in custom_nodes, as a checkpoint records it.

    Recorded but never restored automatically, which is the safe-updater's rule and is
    kept for its reason: those folders are the user's, several are gigabytes of models,
    and a pack put back at a commit while its dependencies stayed where they are is a
    combination nobody chose.
    """

    name: str
    enabled: bool = True
    registry_id: str = ""
    version: str = ""
    repo: str = ""
    commit: str = ""
    files: bool = True  # a directory, rather than a bare .py in custom_nodes
    dependencies: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        out = {"name": self.name, "enabled": self.enabled}
        for key in ("registry_id", "version", "repo", "commit"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.dependencies:
            out["dependencies"] = list(self.dependencies)
        if not self.files:
            out["single_file"] = True
        return out


def parse_pyproject(text: str) -> dict:
    """A node pack's own account of itself, out of its `pyproject.toml`.

    Every pack measured on this install carries one, because the registry requires it:
    `[project] name/version/dependencies` beside `[tool.comfy] PublisherId`. That is the
    same rule a loader node's `properties.models` follows - the thing already states
    what it is in machine-readable form, so nothing here keeps a table of pack names
    that would go stale the first time somebody publishes.

    Total by construction. A pack with no pyproject, or with a broken one, is an
    ordinary case rather than an error: plenty of older packs are a bare `__init__.py`
    in a git clone, and they are still installed.
    """
    try:
        import tomllib

        data = tomllib.loads(text)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}

    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    urls = project.get("urls") if isinstance(project.get("urls"), dict) else {}
    comfy = data.get("tool", {}).get("comfy") if isinstance(data.get("tool"), dict) else {}
    comfy = comfy if isinstance(comfy, dict) else {}

    repo = ""
    for key in ("Repository", "repository", "Homepage", "homepage", "Source"):
        value = urls.get(key)
        if isinstance(value, str) and value.strip():
            repo = value.strip()
            break

    deps = project.get("dependencies")
    return {
        "registry_id": str(project.get("name", "") or ""),
        "version": str(project.get("version", "") or ""),
        "repo": repo,
        "publisher": str(comfy.get("PublisherId", "") or ""),
        "display_name": str(comfy.get("DisplayName", "") or ""),
        "dependencies": clean_requirements(deps) if isinstance(deps, list) else [],
    }


def clean_requirements(items: list) -> list[str]:
    """A pack's declared dependencies, with the lines that are not dependencies removed.

    **Measured, and the reason this function exists:** `ComfyUI-MMAudio` declares

        dependencies = ["accelerate>=0.33.0", ..., "# for Image utils", "imagesize>=1.4.1",
                        ..., "# for T5XXL tokenizer (SD3/FLUX)", "sentencepiece>=0.2.0"]

    - a `requirements.txt` converted to TOML by something that kept the comment lines as
    list entries. The registry mirrors the pack's own metadata, so it arrives that way
    from both sources, and `plan_packages` is documented as taking this list: handing uv
    `# for Image utils` fails the whole plan over a line that was never a requirement.

    Only a leading `#` counts as a comment. A `#` further along is routinely part of a
    direct reference (`pkg @ https://host/x.whl#sha256=...`), and truncating there would
    quietly produce a requirement that installs the wrong bytes.
    """
    out = []
    for item in items:
        text = str(item).strip()
        if text and not text.startswith("#"):
            out.append(text)
    return out


def same_pack(a: Pack, b: Pack) -> bool:
    """Whether two entries are the same pack under different names.

    Asked before installing, because a duplicate is not a folder-name collision - it is
    two copies of the same nodes registering the same class names, and ComfyUI resolves
    that by whichever imported last. The two machine-readable identities are the
    registry id and the git remote; the folder name is the weakest of the three and is
    only consulted when neither is known, since `ComfyUI-KJNodes` and `comfyui-kjnodes`
    are routinely the same thing checked out twice.
    """
    if a.registry_id and b.registry_id:
        return normalise(a.registry_id) == normalise(b.registry_id)
    if a.repo and b.repo:
        return repo_key(a.repo) == repo_key(b.repo)
    return normalise(a.name) == normalise(b.name)


def repo_key(url: str) -> str:
    """`owner/name` out of any GitHub URL spelling, lowercased.

    `git@github.com:kijai/ComfyUI-KJNodes.git`, `https://github.com/kijai/ComfyUI-KJNodes`
    and the same with a trailing slash are one repository, and a comparison on the raw
    string calls them three.
    """
    text = url.strip().rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    for marker in ("github.com", "gitlab.com", "codeberg.org"):
        if marker in text:
            text = text.split(marker, 1)[1]
            break
    parts = [p for p in re.split(r"[:/]", text) if p]
    return "/".join(parts[-2:]).lower() if len(parts) >= 2 else text.lower()


REGISTRY_URL = "https://api.comfy.org"


@dataclass
class Listing:
    """One node pack as the Comfy Registry describes it.

    `dependencies` is the field worth the whole call: the registry states a pack's
    Python requirements *before* anything is cloned, so `plan_packages` can be asked
    what installing it would move while the pack is still somebody else's problem.
    Measured on `comfyui-kjnodes`: `["pillow>=10.3.0", "color-matcher", "matplotlib"]`.
    """

    id: str
    name: str = ""
    repo: str = ""
    version: str = ""
    description: str = ""
    publisher: str = ""
    downloads: int = 0
    stars: int = 0
    deprecated: bool = False
    banned: bool = False
    dependencies: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        out: dict = {"id": self.id, "name": self.name}
        for key in ("repo", "version", "publisher", "description"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.downloads:
            out["downloads"] = self.downloads
        if self.stars:
            out["github_stars"] = self.stars
        if self.dependencies:
            out["dependencies"] = list(self.dependencies)
        if self.deprecated:
            out["deprecated"] = True
        if self.banned:
            out["banned"] = True
        return out


def parse_listing(data: dict) -> Listing | None:
    """One `/nodes` entry, keeping the handful of fields that decide anything.

    A search result is 25 fields wide and most of them are empty on every pack
    measured - `banner_url`, `category`, `supported_os`, `tags`, two separate status
    fields. Passing that through would be a page of nothing per hit, so this is
    `summarise_schema`'s rule applied to somebody else's payload: report a value only
    when it is not the boring answer.

    Total by construction. The registry adds fields without warning, and a search that
    raises because one pack has a null where a string was is worse than one that
    reports the pack with a blank.
    """
    if not isinstance(data, dict):
        return None
    node_id = str(data.get("id") or "").strip()
    if not node_id:
        return None

    latest = data.get("latest_version")
    latest = latest if isinstance(latest, dict) else {}
    deps = latest.get("dependencies")
    publisher = data.get("publisher")
    publisher = publisher if isinstance(publisher, dict) else {}

    def whole(value: object) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    return Listing(
        id=node_id,
        name=str(data.get("name") or node_id),
        repo=str(data.get("repository") or ""),
        version=str(latest.get("version") or ""),
        description=str(data.get("description") or "").strip(),
        publisher=str(publisher.get("name") or publisher.get("id") or ""),
        downloads=whole(data.get("downloads")),
        stars=whole(data.get("github_stars")),
        deprecated=bool(latest.get("deprecated")),
        banned=str(data.get("status") or "") == "NodeStatusBanned",
        dependencies=clean_requirements(deps) if isinstance(deps, list) else [],
    )


def parse_search(data: dict) -> tuple[list[Listing], dict]:
    """The `/nodes/search` payload: the hits, and where they sit in the whole result.

    The paging half is reported rather than dropped because the count is the answer to
    a question the hits cannot settle: `wan` matches 135 packs here, and a caller shown
    ten of them with no total has no way to tell a narrow query from a truncated one.
    """
    if not isinstance(data, dict):
        return [], {}
    rows = data.get("nodes")
    found = [parse_listing(row) for row in rows] if isinstance(rows, list) else []
    listings = [row for row in found if row is not None]

    page: dict = {}
    for key, out in (("total", "total"), ("page", "page"), ("totalPages", "pages")):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            page[out] = value
    return listings, page


def as_pack(listing: Listing) -> Pack:
    """A registry hit in the shape `same_pack` compares, so the two can be asked about
    each other. The registry id *is* the `[project] name` a pack writes into its own
    `pyproject.toml`, which is what makes that comparison exact rather than a guess."""
    return Pack(
        name=listing.name or listing.id,
        registry_id=listing.id,
        version=listing.version,
        repo=listing.repo,
        dependencies=list(listing.dependencies),
    )


def find_packs(packs: list[Pack], query: str) -> list[Pack]:
    """Every installed pack a caller could mean by `query`, exact matches first.

    Naming a pack is harder than it looks and getting it wrong is silent: the registry
    calls it `comfyui-kjnodes`, `pyproject.toml` agrees, the folder on this machine is
    `comfyui-kjnodes` and on the next one `ComfyUI-KJNodes`, and the person says
    "kjnodes". An exact match on either machine-readable identity wins outright; a
    substring is the fallback and returns everything it matched rather than picking,
    because guessing between two packs is how the wrong one gets disabled.
    """
    want = normalise(query)
    if not want:
        return []

    exact = [p for p in packs if want in {normalise(p.name), normalise(p.registry_id)}]
    if not exact and "/" in query:
        key = repo_key(query)
        exact = [p for p in packs if p.repo and repo_key(p.repo) == key]
    if exact:
        return exact
    return [p for p in packs if want in normalise(p.name) or want in normalise(p.registry_id)]


def conflicts(packs: list[Pack], candidate: Pack) -> list[Pack]:
    """The installed packs that would collide with `candidate` - **enabled ones only**.

    A disabled copy is not a conflict, and that is not a nicety: measured on this
    install, `ComfyUI-WanVideoWrapper` is present twice, enabled and as a disabled
    `@nightly` checkout, both carrying the same registry id. A duplicate check counting
    the second would refuse to touch the pack the user actually runs, and the reason
    would be a folder they deliberately switched off.

    What makes a real collision is two copies *registering the same class names*, which
    ComfyUI resolves as whichever imported last - and a disabled pack imports never.
    """
    return [p for p in packs if p.enabled and same_pack(p, candidate)]


def toggle_target(root, pack: Pack, enable: bool):
    """Where turning `pack` on or off moves it, following ComfyUI-Manager exactly.

    **Manager reads two spellings and writes one, and that asymmetry is the whole
    rule.** Disabling a directory always produces `custom_nodes/.disabled/<name>` -
    `unified_manager` moves it there and never writes the older `<name>.disabled`
    form. Enabling looks for both, in that order, because installs made years apart
    are still out there. A single `.py` pack is the exception in both directions:
    there is nowhere to move a file to, so it is renamed in place with the suffix,
    which is what Manager's `copy_set_active` does - and its name carries the `.py`,
    because `Pack.name` is the entry as it sits on disk rather than a display name.

    Agreeing here is the point of the function. A pack this server calls enabled while
    Manager's UI calls it disabled is a disagreement the user meets as a node that is
    on the canvas and does not work.
    """
    name = pack.name
    if not pack.files:
        current = root / name
        hidden = root / f"{name}{DISABLED_SUFFIX}"
        return (hidden, current) if enable else (current, hidden)

    in_dir = root / DISABLED_DIR / name
    suffixed = root / f"{name}{DISABLED_SUFFIX}"
    if enable:
        source = in_dir if in_dir.exists() else suffixed
        return source, root / name
    return root / name, in_dir


def update_available(installed: Pack, listing: Listing) -> bool:
    """Whether the registry knows a version this pack is not on.

    A string comparison, deliberately. A pack's version is whatever its author typed
    into `pyproject.toml`, and the ones here carry `1.5.0`, `2.0.0`, `0.1` and nine
    empty strings. Ordering those needs a rule none of them agreed to, so the question
    asked is "is it the same one", and the answer to "is it newer" is the two version
    strings printed side by side for a person to read.
    """
    if not listing.version or not installed.version:
        return False
    return normalise(installed.version) != normalise(listing.version)


CUSTOM_NODE_MODULE = "custom_nodes."

NODE_TYPES_SHOWN = 20


def pack_of_module(python_module: str) -> str:
    """The custom_nodes folder a registered node type came from, or "" for a core node.

    `/object_info` carries `python_module` on every entry, and on this install it is
    exactly `custom_nodes.<folder name>` - 208 distinct modules across 2823 types.
    That is the join between the three sources this concept reads: the disk says what
    is installed, `/object_info` says what registered, and until this field there was
    no way to say *which pack* a registered type belonged to, so the two could only be
    counted against each other rather than compared.

    Core nodes report `nodes` or `comfy_extras.nodes_*` and answer "" here.
    """
    text = python_module.strip()
    if not text.startswith(CUSTOM_NODE_MODULE):
        return ""
    return text[len(CUSTOM_NODE_MODULE) :].split(".", 1)[0]


def registered_by(schemas: dict, pack: Pack) -> list[str]:
    """The node types `pack` actually registered, out of a whole `/object_info` payload.

    The question this answers is the one a caller really has: a pack that is installed
    and enabled and registers nothing did not merely fail to load - it failed in a way
    that leaves every workflow using it with holes where its nodes were, and nothing on
    the canvas says why. `get_comfy_log` says why; this says that.
    """
    want = normalise(pack.name)
    found = []
    for name, entry in schemas.items():
        if not isinstance(entry, dict):
            continue
        owner = pack_of_module(str(entry.get("python_module", "")))
        if owner and normalise(owner) == want:
            found.append(str(name))
    return sorted(found)
