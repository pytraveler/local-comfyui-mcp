"""Introspection and parameter patching for API-format ComfyUI workflows.

An API-format workflow is a flat dict: ``{node_id: {"class_type": str, "inputs": {...}}}``.
An input value is either a literal or a link ``[source_node_id, output_slot]``.

The hard part of driving such a graph from the outside is that the interesting
values are rarely literals on the node you care about: ``KSampler.steps`` is
usually a link to a primitive, possibly through a switch. So the whole module is
built on :func:`resolve_setter`, which walks a link back to the node that
actually holds a writable literal.
"""

from __future__ import annotations

import copy
import difflib
import json
import re
from dataclasses import dataclass, field
from typing import Any

Graph = dict[str, dict[str, Any]]

MAX_DEPTH = 32


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<missing>"


MISSING = _Missing()


PRIMITIVE_VALUE_KEYS: dict[str, str] = {
    "PrimitiveInt": "value",
    "PrimitiveFloat": "value",
    "PrimitiveBoolean": "value",
    "PrimitiveString": "value",
    "PrimitiveStringMultiline": "value",
    "PrimitiveNode": "value",
    "easy int": "value",
    "easy float": "value",
    "easy string": "value",
    "easy seed": "seed",
    "easy positive": "positive",
    "easy negative": "negative",
    "Seed (rgthree)": "seed",
    "ImpactInt": "value",
    "ImpactFloat": "value",
    "String Literal": "string",
    "Int Literal": "int",
    "Float Literal": "float",
    "CR Text": "text",
    "Text Multiline": "text",
    "JWStringMultiline": "text",
}

BOOL_SWITCHES: dict[str, tuple[str, str, str]] = {
    "ComfySwitchNode": ("switch", "on_true", "on_false"),
    "Switch (Any) [Crystools]": ("boolean", "on_true", "on_false"),
    "ImpactConditionalBranch": ("cond", "tt_value", "ff_value"),
    "easy ifElse": ("boolean", "on_true", "on_false"),
}

PASSTHROUGH: dict[str, str] = {
    "PreviewAny": "source",
    "easy showAnything": "anything",
    "Display Any (rgthree)": "source",
}

ANY_SWITCHES = {"Any Switch (rgthree)"}

SAMPLER_CLASSES = (
    "KSampler",
    "KSamplerAdvanced",
    "KSamplerSelect",
    "SamplerCustom",
    "SamplerCustomAdvanced",
    "WanVideoSampler",
    "WanVideoSamplerv2",
    "WanVideoDiffusionForcingSampler",
)

SAMPLER_SIDECAR_INPUTS = ("noise", "guider", "sigmas", "scheduler", "text_embeds", "image_embeds")

SAMPLER_FIELDS: dict[str, str] = {
    "seed": "int",
    "noise_seed": "int",
    "steps": "int",
    "cfg": "float",
    "shift": "float",
    "sampler_name": "combo",
    "scheduler": "combo",
    "denoise": "float",
    "start_at_step": "int",
    "end_at_step": "int",
}

LATENT_INPUTS = ("latent_image", "image_embeds")

LATENT_FIELDS: dict[str, str] = {
    "width": "int",
    "height": "int",
    "batch_size": "int",
    "num_frames": "int",
}

LOADER_FIELDS: dict[str, list[tuple[str, str, str]]] = {
    "UNETLoader": [("unet_name", "model", "combo")],
    "UnetLoaderGGUF": [("unet_name", "model", "combo")],
    "CheckpointLoaderSimple": [("ckpt_name", "checkpoint", "combo")],
    "VAELoader": [("vae_name", "vae", "combo")],
    "CLIPLoader": [("clip_name", "clip", "combo")],
    "DualCLIPLoader": [("clip_name1", "clip1", "combo"), ("clip_name2", "clip2", "combo")],
    "DualCLIPLoaderGGUF": [("clip_name1", "clip1", "combo"), ("clip_name2", "clip2", "combo")],
    "LoraLoaderModelOnly": [("lora_name", "lora", "combo"), ("strength_model", "lora_strength", "float")],
    "LoraLoader": [
        ("lora_name", "lora", "combo"),
        ("strength_model", "lora_strength", "float"),
        ("strength_clip", "lora_strength_clip", "float"),
    ],
    "WanVideoModelLoader": [("model", "model", "combo")],
    "WanVideoVAELoader": [("model_name", "vae", "combo")],
    "LoadWanVideoT5TextEncoder": [("model_name", "text_encoder", "combo")],
    "LoadImage": [("image", "image", "combo")],
    "LoadImageMask": [("image", "image", "combo")],
    "LoadImageOutput": [("image", "image", "combo")],
    "UpscaleModelLoader": [("model_name", "upscale_model", "combo")],
    "ImageScaleBy": [("scale_by", "scale_by", "float"), ("upscale_method", "upscale_method", "combo")],
    "ImageScale": [
        ("width", "width", "int"),
        ("height", "height", "int"),
        ("upscale_method", "upscale_method", "combo"),
    ],
    "easy imageScaleDownToSize": [("size", "max_size", "int")],
    "CRT_QuantizeAndCropImage": [("max_side_length", "max_side_length", "int")],
    "FluxGuidance": [("guidance", "guidance", "float")],
    "VHS_VideoCombine": [("frame_rate", "frame_rate", "float")],
}

TEXT_ENCODER_INPUTS = ("text", "prompt", "positive", "negative")
CONDITIONING_ZERO = "ConditioningZeroOut"

PROMPT_PAIR_FIELDS: dict[str, tuple[str, str]] = {
    "WanVideoTextEncode": ("positive_prompt", "negative_prompt"),
    "WanVideoTextEncodeCached": ("positive_prompt", "negative_prompt"),
}

POWER_LORA_LOADER = "Power Lora Loader (rgthree)"

MULTI_LORA_LOADERS = {"WanVideoLoraSelectMulti"}
LORA_SLOT = re.compile(r"^lora_(\d+)$")
LORA_SLOT_EMPTY = "none"

RESOLUTION_SELECTOR = "ResolutionSelector"

RESOLUTION_FIELDS: dict[str, list[tuple[str, str, str]]] = {
    RESOLUTION_SELECTOR: [
        ("aspect_ratio", "aspect_ratio", "combo"),
        ("megapixels", "megapixels", "float"),
        ("multiple", "multiple", "int"),
    ],
}

TOGGLE_CLASSES = {"PrimitiveBoolean"}

PREVIEW_TO_SAVE = {"PreviewImage": "SaveImage"}


Schemas = dict[str, Any]

LITERAL_KINDS = {"INT": "int", "FLOAT": "float", "BOOLEAN": "bool", "STRING": "string", "COMBO": "combo"}

OPTIONS_SHOWN = 12


@dataclass(frozen=True)
class InputSpec:
    """What ComfyUI says about one node input."""

    kind: str  # int | float | bool | string | combo | link
    options: tuple[Any, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None

    @property
    def is_literal(self) -> bool:
        return self.kind != "link"

    def tightened_by(self, consumer: InputSpec | None) -> InputSpec:
        """Combine this write target's spec with that of the node reading the value.

        A literal written to a primitive still has to satisfy whoever consumes it,
        and the consumer usually holds the meaningful range: `PrimitiveInt.value`
        accepts +/-2**63 while the KSampler reading it declares 1..10000. ComfyUI
        does not check this itself - it only validates literals sitting directly on
        the node - so an unchecked value here means a real 99999-step run.
        """
        if consumer is None or not consumer.is_literal:
            return self
        return InputSpec(
            kind=self.kind if self.is_literal else consumer.kind,
            options=self.options if self.options is not None else consumer.options,
            minimum=_tightest(self.minimum, consumer.minimum, max),
            maximum=_tightest(self.maximum, consumer.maximum, min),
        )


def _tightest(a: float | None, b: float | None, pick: Any) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return pick(a, b)


def _as_number(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def parse_input_spec(entry: Any) -> InputSpec:
    """Parse one /object_info input entry.

    ComfyUI emits several shapes for these and mixes them freely across core and
    custom nodes: ``["INT", {...}]``, ``["COMBO", {"options": [...]}]``, and the
    older ``[[opt, ...]]`` / ``[[opt, ...], {...}]`` for combos. A head that is
    not a known literal type (``"MODEL"``, ``"IMAGE"``, ...) means a link.
    """
    if not isinstance(entry, list) or not entry:
        return InputSpec("link")
    head = entry[0]
    config = entry[1] if len(entry) > 1 and isinstance(entry[1], dict) else {}

    if isinstance(head, list):  
        return InputSpec("combo", options=tuple(head))
    if not isinstance(head, str):
        return InputSpec("link")

    kind = LITERAL_KINDS.get(head)
    if kind is None:
        return InputSpec("link")
    if kind == "combo":
        options = config.get("options")
        return InputSpec("combo", options=tuple(options) if isinstance(options, list) else None)
    return InputSpec(kind, minimum=_as_number(config.get("min")), maximum=_as_number(config.get("max")))


class SchemaIndex:
    """Lazily parsed view over /object_info. Empty when ComfyUI was unreachable."""

    def __init__(self, raw: Schemas | None = None) -> None:
        self._raw = raw or {}
        self._parsed: dict[str, dict[str, InputSpec]] = {}

    def __bool__(self) -> bool:
        return bool(self._raw)

    def for_class(self, class_type: str) -> dict[str, InputSpec]:
        if class_type not in self._parsed:
            entry = self._raw.get(class_type) or {}
            sections = entry.get("input") or {}
            specs: dict[str, InputSpec] = {}
            for section in ("required", "optional"):
                for key, value in (sections.get(section) or {}).items():
                    specs[key] = parse_input_spec(value)
            self._parsed[class_type] = specs
        return self._parsed[class_type]

    def spec(self, graph: Graph, node_id: str, input_key: str) -> InputSpec | None:
        if not self._raw:
            return None
        return self.for_class(_class_of(graph, node_id)).get(input_key)


WIDGET_NOISE = {"control_after_generate", "control_before_generate"}

VALUE_KEY_PREFERENCE = ("value", "text", "string", "number", "int", "float", "boolean", "seed", "prompt")


def is_link(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], (str, int))
        and isinstance(value[1], int)
    )


def _inputs(graph: Graph, node_id: str) -> dict[str, Any]:
    node = graph.get(str(node_id)) or {}
    inputs = node.get("inputs")
    return inputs if isinstance(inputs, dict) else {}


def _class_of(graph: Graph, node_id: str) -> str:
    return (graph.get(str(node_id)) or {}).get("class_type", "")


def _title_of(graph: Graph, node_id: str) -> str:
    meta = (graph.get(str(node_id)) or {}).get("_meta") or {}
    return meta.get("title") or _class_of(graph, node_id)


def _node_title(graph: Graph, node_id: str) -> str:
    """The author's title, or "" when it is just the class name and says nothing."""
    title = _title_of(graph, node_id)
    return "" if title == _class_of(graph, node_id) else title


def resolve_setter(
    graph: Graph, node_id: str, input_key: str, _depth: int = 0
) -> tuple[str, str] | None:
    """Find where a literal for ``node_id.input_key`` can actually be written.

    Returns ``(node_id, input_key)`` of a node holding a literal, following links
    back through primitives, pass-throughs and switches whose selector is itself
    a resolvable literal. Returns ``None`` when the value is produced by real
    computation and therefore is not settable.
    """
    if _depth > MAX_DEPTH:
        return None
    inputs = _inputs(graph, node_id)
    if input_key not in inputs:
        return None
    value = inputs[input_key]
    if not is_link(value):
        return (str(node_id), input_key)
    return _resolve_ref(graph, value, _depth + 1)


def _forwarded_input(graph: Graph, node_id: str, depth: int = 0) -> Any:
    """Which input a node merely forwards, per the tables - the shared hop.

    Every walker that sees through switches and pass-throughs takes this exact
    step, so it lives in one place: a class added to a table extends all of them
    at once. Returns the input key to follow, ``MISSING`` for a forwarder whose
    branch cannot be honestly chosen (a switch with an unresolvable selector),
    and ``None`` for a node that actually computes something.
    """
    cls = _class_of(graph, node_id)
    inputs = _inputs(graph, node_id)

    if cls in BOOL_SWITCHES:
        selector, on_true, on_false = BOOL_SWITCHES[cls]
        cond = literal_value(graph, node_id, selector, depth + 1)
        if isinstance(cond, bool):
            return on_true if cond else on_false
        return MISSING

    if cls in ANY_SWITCHES:
        for key in sorted(k for k in inputs if k.startswith("any_")):
            if is_link(inputs[key]):
                return key
        return MISSING

    if cls in PASSTHROUGH:
        return PASSTHROUGH[cls]

    if cls == "Reroute":
        for key, value in inputs.items():
            if is_link(value):
                return key

    return None


def _resolve_ref(graph: Graph, ref: list[Any], depth: int) -> tuple[str, str] | None:
    if depth > MAX_DEPTH:
        return None
    src_id = str(ref[0])
    if src_id not in graph:
        return None

    if _class_of(graph, src_id) in PRIMITIVE_VALUE_KEYS:
        return resolve_setter(graph, src_id, PRIMITIVE_VALUE_KEYS[_class_of(graph, src_id)], depth + 1)

    hop = _forwarded_input(graph, src_id, depth)
    if hop is MISSING:
        return None
    if hop is not None:
        return resolve_setter(graph, src_id, hop, depth + 1)

    inferred = infer_value_key(graph, src_id)
    if inferred is not None:
        return (src_id, inferred)

    return None


def infer_value_key(graph: Graph, node_id: str) -> str | None:
    """Structurally guess the value input of a node not covered by the tables.

    A node with no incoming links whose literals are a single scalar is, in
    practice, always some node pack's primitive. Recognising that shape keeps an
    unfamiliar pack usable without editing PRIMITIVE_VALUE_KEYS first - the table
    still wins when present, since it encodes the exact key.
    """
    inputs = _inputs(graph, node_id)
    if not inputs or any(is_link(value) for value in inputs.values()):
        return None
    scalars = [
        key
        for key, value in inputs.items()
        if key not in WIDGET_NOISE and isinstance(value, (str, int, float, bool))
    ]
    if len(scalars) == 1:
        return scalars[0]
    for name in VALUE_KEY_PREFERENCE:
        if name in scalars:
            return name
    return None


def literal_value(graph: Graph, node_id: str, input_key: str, _depth: int = 0) -> Any:
    """Evaluate ``node_id.input_key`` down to a literal, or ``MISSING``."""
    target = resolve_setter(graph, node_id, input_key, _depth)
    if target is None:
        return MISSING
    return _inputs(graph, target[0]).get(target[1], MISSING)


def _walk_to_encoder(
    graph: Graph, ref: list[Any], depth: int = 0
) -> tuple[str | None, bool]:
    """Walk a conditioning link back to a text-encoding node.

    Returns ``(node_id, zeroed)``. ``zeroed`` is True when a ConditioningZeroOut
    sits in the chain, which means the branch is an empty/unconditional prompt
    rather than a real negative prompt.
    """
    if depth > MAX_DEPTH or not is_link(ref):
        return (None, False)
    node_id = str(ref[0])
    if node_id not in graph:
        return (None, False)
    cls = _class_of(graph, node_id)
    inputs = _inputs(graph, node_id)

    if cls == CONDITIONING_ZERO:
        inner = inputs.get("conditioning")
        found, _ = _walk_to_encoder(graph, inner, depth + 1) if is_link(inner) else (None, False)
        return (found, True)

    for key in TEXT_ENCODER_INPUTS:
        if key in inputs:
            return (node_id, False)

    if cls in BOOL_SWITCHES:
        selector, on_true, on_false = BOOL_SWITCHES[cls]
        cond = literal_value(graph, node_id, selector, depth + 1)
        if not isinstance(cond, bool):
            return (None, False)
        branch = inputs.get(on_true if cond else on_false)
        if is_link(branch):
            return _walk_to_encoder(graph, branch, depth + 1)
        return (None, False)

    candidates = [k for k in ("conditioning", "source", "input") if is_link(inputs.get(k))]
    candidates += sorted(k for k in inputs if k.startswith("any_") and is_link(inputs[k]))
    if not candidates:
        candidates = [k for k, v in inputs.items() if is_link(v)]
    for key in candidates:
        found, zeroed = _walk_to_encoder(graph, inputs[key], depth + 1)
        if found:
            return (found, zeroed)
    return (None, False)


@dataclass
class Param:
    name: str
    type: str
    value: Any
    node_id: str
    input: str
    origin: str
    via: str = ""
    title: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    spec: InputSpec | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "name": self.name,
            "type": self.type,
            "current": self.value,
            "target": f"{self.node_id}.{self.input}",
            "origin": self.origin,
        }
        if self.title:
            out["node_title"] = self.title
        if self.via:
            out["via"] = self.via
        out.update(self.extra)
        return out


_SLUG_STRIP = re.compile(r"\b(boolean|bool|switch|primitive|node|value|text|string)\b", re.I)


def slugify(title: str) -> str:
    cleaned = _SLUG_STRIP.sub(" ", title)
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", cleaned).strip("_").lower()
    slug = re.sub(r"_+", "_", slug)
    return slug or re.sub(r"[^0-9a-zA-Z]+", "_", title).strip("_").lower()


def _sampler_ids(graph: Graph) -> list[str]:
    ids = [nid for nid, node in graph.items() if node.get("class_type") in SAMPLER_CLASSES]
    return sorted(ids, key=_node_sort_key)


def forwarded_to(graph: Graph, ref: Any, _depth: int = 0) -> str | None:
    """The node a link really comes from, seen through nodes that only forward it.

    Same forwarding tables as :func:`resolve_setter`, stopping one hop earlier: at
    the node rather than at the literal it holds. A sampler's sidecar is routinely
    reached through a switch, and stopping at the switch would hide everything
    behind it.
    """
    if _depth > MAX_DEPTH or not is_link(ref):
        return None
    node_id = str(ref[0])
    if node_id not in graph:
        return None
    hop = _forwarded_input(graph, node_id, _depth)
    if hop is not None and hop is not MISSING:
        return forwarded_to(graph, _inputs(graph, node_id).get(hop), _depth + 1)
    return node_id


def _sampler_group(graph: Graph, samplers: list[str]) -> list[str]:
    """Each sampler followed by the nodes it delegates its own settings to.

    With SamplerCustomAdvanced the seed, conditioning and steps live on separate
    noise/guider/scheduler nodes, so scanning the sampler alone finds neither `prompt`
    nor `seed`. The sidecars are read for the same fields as the sampler itself, which
    is why they come right after it: whoever is scanned first names the parameter.
    """
    group: list[str] = []
    for sampler_id in samplers:
        group.append(sampler_id)
        inputs = _inputs(graph, sampler_id)
        for slot in SAMPLER_SIDECAR_INPUTS:
            sidecar = forwarded_to(graph, inputs.get(slot))
            if sidecar is not None and sidecar not in group:
                group.append(sidecar)
    return group


def _node_sort_key(node_id: str) -> tuple[int, ...]:
    """Sort node ids naturally, including subgraph ids like ``30:12``."""
    return tuple(int(p) if p.isdigit() else 0 for p in str(node_id).split(":"))


class _Namer:
    """Hands out unique param names, suffixing collisions with the node id."""

    def __init__(self) -> None:
        self.used: set[str] = set()

    def take(self, base: str, node_id: str) -> str:
        if base not in self.used:
            self.used.add(base)
            return base
        name = f"{base}@{node_id}"
        self.used.add(name)
        return name


def discover_params(graph: Graph, schemas: Schemas | None = None) -> list[Param]:
    """Best-effort discovery of the knobs worth exposing on a workflow.

    When `schemas` (a raw /object_info payload) is supplied, each parameter's type,
    allowed options and numeric range come from ComfyUI itself rather than from the
    tables above, which only ever encoded a guess.
    """
    index = SchemaIndex(schemas)
    params: list[Param] = []
    namer = _Namer()
    claimed: set[tuple[str, str]] = set()

    def add(base: str, kind: str, node_id: str, input_key: str, origin: str, via: str = "", **extra: Any) -> None:
        target = resolve_setter(graph, node_id, input_key)
        if target is None:
            return
        set_node, set_input = target
        if target in claimed:
            return
        value = _inputs(graph, set_node).get(set_input, MISSING)
        if isinstance(value, _Missing):
            return
        claimed.add(target)

        written = index.spec(graph, set_node, set_input)
        consumer = index.spec(graph, node_id, input_key)
        if written is not None and written.is_literal:
            spec = written.tightened_by(consumer)
        elif written is None and consumer is not None and consumer.is_literal:
            spec = consumer
        else:
            spec = None
        if spec is not None:
            kind = spec.kind
            if spec.options is not None:
                extra["options"] = list(spec.options[:OPTIONS_SHOWN])
                if len(spec.options) > OPTIONS_SHOWN:
                    extra["options_total"] = len(spec.options)
            if spec.minimum is not None:
                extra["min"] = spec.minimum
            if spec.maximum is not None:
                extra["max"] = spec.maximum
        hop = ""
        if (set_node, set_input) != (str(node_id), input_key):
            hop = f"{node_id}.{input_key} -> {set_node}.{set_input} ({_title_of(graph, set_node)})"
        params.append(
            Param(
                name=namer.take(base, set_node),
                type=kind,
                value=value,
                node_id=set_node,
                input=set_input,
                origin=origin,
                via=via or hop,
                title=_node_title(graph, set_node),
                extra=extra,
                spec=spec,
            )
        )

    samplers = _sampler_ids(graph)
    sampler_group = _sampler_group(graph, samplers)

    for sampler_id in sampler_group:
        inputs = _inputs(graph, sampler_id)
        pair = PROMPT_PAIR_FIELDS.get(_class_of(graph, sampler_id))
        if pair is not None:
            add("prompt", "string", sampler_id, pair[0], "prompt")
            add("negative_prompt", "string", sampler_id, pair[1], "prompt")
        for slot, base in (("positive", "prompt"), ("negative", "negative_prompt")):
            ref = inputs.get(slot)
            if not is_link(ref):
                continue
            encoder_id, zeroed = _walk_to_encoder(graph, ref)
            if encoder_id is None or zeroed:
                continue
            enc_inputs = _inputs(graph, encoder_id)
            key = next((k for k in TEXT_ENCODER_INPUTS if k in enc_inputs), None)
            if key is None:
                continue
            add(base, "string", encoder_id, key, "prompt")

    for sampler_id in sampler_group:
        inputs = _inputs(graph, sampler_id)
        for key, kind in SAMPLER_FIELDS.items():
            if key in inputs:
                add("seed" if key == "noise_seed" else key, kind, sampler_id, key, "sampler")
        for slot in LATENT_INPUTS:
            latent_id = forwarded_to(graph, inputs.get(slot))
            if latent_id is None:
                continue
            for key, kind in LATENT_FIELDS.items():
                if key in _inputs(graph, latent_id):
                    add(key, kind, latent_id, key, "latent")

    for node_id, node in sorted(graph.items(), key=lambda kv: _node_sort_key(kv[0])):
        cls = node.get("class_type", "")
        inputs = _inputs(graph, node_id)

        for key, friendly, kind in RESOLUTION_FIELDS.get(cls, []):
            if key in inputs:
                add(friendly, kind, node_id, key, "resolution")

        if cls in TOGGLE_CLASSES:
            add(slugify(_title_of(graph, node_id)), "bool", node_id, PRIMITIVE_VALUE_KEYS.get(cls, "value"), "toggle")

        for key, friendly, kind in LOADER_FIELDS.get(cls, []):
            if key in inputs:
                add(friendly, kind, node_id, key, "loader")

        if cls == POWER_LORA_LOADER or cls in MULTI_LORA_LOADERS:
            entries = _lora_slots(graph, node_id)
            if entries:
                params.append(
                    Param(
                        name=namer.take("loras", node_id),
                        type="loras",
                        value=[e for e in entries if e["on"]],
                        node_id=node_id,
                        input="(lora slots)",
                        origin="lora",
                        title=_node_title(graph, node_id),
                        extra={"available": entries},
                    )
                )

    return params


def _lora_slots(graph: Graph, node_id: str) -> list[dict[str, Any]]:
    """The lora slots of a multi-slot loader, in one shape whatever the storage.

    The Power Lora Loader keeps a dict per slot; WanVideoLoraSelectMulti keeps flat
    `lora_N`/`strength_N` pairs and writes "none" into the slots it does not use.
    """
    inputs = _inputs(graph, node_id)
    if _class_of(graph, node_id) == POWER_LORA_LOADER:
        return [
            {"slot": k, "lora": v.get("lora"), "strength": v.get("strength"), "on": bool(v.get("on"))}
            for k, v in sorted(inputs.items(), key=lambda kv: _lora_slot_index(kv[0]))
            if k.startswith("lora_") and isinstance(v, dict)
        ]
    slots = []
    for key, value in sorted(inputs.items(), key=lambda kv: _lora_slot_index(kv[0])):
        match = LORA_SLOT.match(key)
        if match is None or not isinstance(value, str) or value == LORA_SLOT_EMPTY:
            continue
        strength = inputs.get(f"strength_{match.group(1)}")
        strength = strength if isinstance(strength, (int, float)) else 0.0
        slots.append({"slot": key, "lora": value, "strength": strength, "on": bool(strength)})
    return slots


def _lora_slot_index(key: str) -> tuple[int, str]:
    match = LORA_SLOT.match(key)
    return (int(match.group(1)), "") if match else (-1, key)


def output_nodes(graph: Graph) -> list[dict[str, str]]:
    """Nodes that produce visible output, so callers know where results come from."""
    kinds = ("SaveImage", "PreviewImage", "SaveAnimatedWEBP", "VHS_VideoCombine", "SaveAudio", "SaveVideo")
    return [
        {"node_id": nid, "class_type": node.get("class_type", ""), "title": _title_of(graph, nid)}
        for nid, node in sorted(graph.items(), key=lambda kv: _node_sort_key(kv[0]))
        if node.get("class_type") in kinds
    ]


def required_models(graph: Graph) -> list[dict[str, str]]:
    """Model files the workflow references, for a fast pre-flight sanity check."""
    keys = ("ckpt_name", "unet_name", "vae_name", "clip_name", "clip_name1", "clip_name2", "lora_name", "control_net_name", "style_model_name", "model", "model_name")
    found: list[dict[str, str]] = []
    for node_id, node in sorted(graph.items(), key=lambda kv: _node_sort_key(kv[0])):
        cls = node.get("class_type", "")
        for key, value in _inputs(graph, node_id).items():
            if key in keys and isinstance(value, str) and value:
                found.append({"node_id": node_id, "input": key, "file": value})
        if cls == POWER_LORA_LOADER or cls in MULTI_LORA_LOADERS:
            for entry in _lora_slots(graph, node_id):
                if entry["on"]:
                    found.append({"node_id": node_id, "input": entry["slot"], "file": entry["lora"] or ""})
    return found


class ParamError(ValueError):
    """A requested parameter could not be applied."""


KNOWN_NAMES_SHOWN = 40


def _unknown_param_message(name: str, discovered: dict[str, Param]) -> str:
    """Lead with near-matches: an alphabetical list often cuts off the obvious candidate."""
    names = sorted(discovered)
    close = difflib.get_close_matches(name, names, n=3, cutoff=0.4)
    parts = [f"unknown parameter '{name}'."]
    if close:
        parts.append(f"Did you mean: {', '.join(close)}?")
    listed = ", ".join(names[:KNOWN_NAMES_SHOWN])
    if len(names) > KNOWN_NAMES_SHOWN:
        listed += f", ... (+{len(names) - KNOWN_NAMES_SHOWN} more, see describe_workflow)"
    parts.append(f"Known: {listed}")
    return " ".join(parts)


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.lower() in ("true", "1", "yes", "on"):
            return True
        if value.lower() in ("false", "0", "no", "off"):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    raise ParamError(f"expected a boolean, got {value!r}")


def _to_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ParamError(f"expected an integer, got {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ParamError(f"expected an integer, got {value!r}") from exc


def _to_float(value: Any) -> float:
    if isinstance(value, bool):
        raise ParamError(f"expected a number, got {value!r}")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ParamError(f"expected a number, got {value!r}") from exc


def _coerce(value: Any, current: Any, kind: str) -> Any:
    """Coerce an incoming value to the type a slot expects.

    The declared `kind` wins over the type of the current value: a PrimitiveFloat
    holding a whole number serialises to JSON as `1`, and inferring "int" from
    that would quietly write integers into a float slot.
    """
    if kind == "bool":
        return _to_bool(value)
    if kind == "int":
        return _to_int(value)
    if kind == "float":
        return _to_float(value)
    if kind == "string":
        return value if isinstance(value, str) else str(value)
    if kind == "combo":
        return value

    if isinstance(current, bool):
        return _to_bool(value)
    if isinstance(current, int):
        return _to_int(value)
    if isinstance(current, float):
        return _to_float(value)
    if isinstance(current, str) and not isinstance(value, str):
        return str(value)
    return value


def _apply_loras(graph: Graph, node_id: str, spec: Any) -> str:
    """Apply a lora spec to a multi-slot lora loader.

    Accepts a list of ``{"lora": name, "strength": float, "on": bool}`` (name may
    be a substring of the configured path), or a list of plain names to enable.
    Slots not mentioned are switched off, so the list is the whole selection.

    The two storage shapes are switched off differently. An rgthree slot has an
    `on` flag and keeps its name either way; a flat `lora_N` slot has no such flag,
    so off means strength 0 - writing "none" would erase the name that the next
    call has to match against.
    """
    if not isinstance(spec, list):
        raise ParamError("'loras' expects a list")
    inputs = _inputs(graph, node_id)
    flat = _class_of(graph, node_id) in MULTI_LORA_LOADERS
    slots = {entry["slot"]: entry["lora"] for entry in _lora_slots(graph, node_id)}

    def write(slot: str, on: bool, strength: float | None) -> str:
        if flat:
            key = f"strength_{LORA_SLOT.match(slot).group(1)}"
            if strength is not None:
                inputs[key] = float(strength)
            if not on:
                inputs[key] = 0.0
            return f"{slot}={slots[slot]} (strength {inputs[key]})"
        entry = inputs[slot]
        entry["on"] = on
        if strength is not None:
            entry["strength"] = float(strength)
        return f"{slot}={entry['lora']} (strength {entry['strength']}, on={entry['on']})"

    for slot in slots:
        write(slot, on=False, strength=None)

    applied: list[str] = []
    for item in spec:
        if isinstance(item, str):
            item = {"lora": item, "on": True}
        if not isinstance(item, dict) or "lora" not in item:
            raise ParamError(f"lora entry must be a name or an object with 'lora': {item!r}")
        needle = str(item["lora"]).replace("/", "\\").lower()
        matches = [k for k, name in slots.items() if needle in str(name).replace("/", "\\").lower()]
        if not matches:
            available = ", ".join(str(name) for name in slots.values())
            raise ParamError(f"no lora slot matching {item['lora']!r}. Available: {available}")
        if len(matches) > 1:
            raise ParamError(f"{item['lora']!r} matches several slots: {', '.join(matches)}")
        on = bool(item.get("on", True))
        strength = item.get("strength", 1.0 if flat and on else None)
        applied.append(write(matches[0], on=on, strength=strength))
    return "; ".join(applied) if applied else "all loras disabled"


def _check_range(name: str, value: Any, spec: InputSpec | None) -> None:
    """Enforce numeric bounds. Nothing else checks these once a value is behind a link."""
    if spec is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return
    if spec.minimum is not None and value < spec.minimum:
        raise ParamError(f"{name}: {value} is below the minimum {spec.minimum}")
    if spec.maximum is not None and value > spec.maximum:
        raise ParamError(f"{name}: {value} is above the maximum {spec.maximum}")


def _option_warning(name: str, value: Any, spec: InputSpec | None) -> str | None:
    """Flag a value outside the declared options without rejecting it.

    The declared list is not authoritative: nodes may accept more than they
    advertise. `LoadImage.image` lists only the top level of the input folder, yet
    happily loads `subfolder/name.png` because its own VALIDATE_INPUTS just checks
    that the file exists. Rejecting here would refuse values that work, so ComfyUI
    stays the authority and this is only a hint.
    """
    if spec is None or spec.options is None or value in spec.options:
        return None
    shown = ", ".join(repr(o) for o in spec.options[:OPTIONS_SHOWN])
    more = f" (+{len(spec.options) - OPTIONS_SHOWN} more)" if len(spec.options) > OPTIONS_SHOWN else ""
    return f"note: {name}={value!r} is not among the declared options ({shown}{more}); ComfyUI will decide"


def apply_params(
    graph: Graph,
    params: dict[str, Any],
    schemas: Schemas | None = None,
    discovered: dict[str, Param] | None = None,
) -> list[str]:
    """Apply ``params`` to ``graph`` in place. Returns human-readable change log.

    Names may be discovered parameter names, or a raw ``<node_id>.<input>`` path
    as an escape hatch for anything discovery missed. With `schemas`, values are
    checked against ComfyUI's own declared options and ranges before submission.

    `discovered` lets a caller that just ran :func:`discover_params` on this
    same, unmodified graph hand the result in instead of paying for a second
    pass. It must not be reused across calls: applying a toggle changes where
    later parameters resolve, which is why the default is to rediscover.
    """
    if not params:
        return []
    index = SchemaIndex(schemas)
    if discovered is None:
        discovered = {p.name: p for p in discover_params(graph, schemas)}
    changes: list[str] = []

    def write(name: str, value: Any, node_id: str, input_key: str, current: Any, spec: InputSpec | None, label: Any) -> None:
        coerced = _coerce(value, current, spec.kind if spec and spec.is_literal else "")
        _check_range(name, coerced, spec)
        graph[node_id]["inputs"][input_key] = coerced
        changes.append(label(coerced))
        notice = _option_warning(name, coerced, spec)
        if notice:
            changes.append(notice)

    for name, value in params.items():
        param = discovered.get(name)

        if param is None and "." in name:
            node_id, _, input_key = name.rpartition(".")
            if node_id in graph and input_key in _inputs(graph, node_id):
                current = _inputs(graph, node_id)[input_key]
                if is_link(current):
                    target = resolve_setter(graph, node_id, input_key)
                    if target is None:
                        src_id = str(current[0])
                        raise ParamError(
                            f"'{name}' is not a literal: it is fed by node {src_id} "
                            f"({_class_of(graph, src_id)}, '{_title_of(graph, src_id)}'), whose output "
                            "is computed. Set an input on that node instead, or upstream of it."
                        )
                    consumer = index.spec(graph, node_id, input_key)
                    node_id, input_key = target
                    current = _inputs(graph, node_id)[input_key]
                    written = index.spec(graph, node_id, input_key)
                    spec = written.tightened_by(consumer) if written else consumer
                else:
                    spec = index.spec(graph, node_id, input_key)
                write(name, value, node_id, input_key, current, spec,
                      lambda new, n=name, nid=node_id, key=input_key: f"{n} -> {new!r} (raw path {nid}.{key})")
                continue

        if param is None:
            raise ParamError(_unknown_param_message(name, discovered))

        if param.type == "loras":
            changes.append(f"loras: {_apply_loras(graph, param.node_id, value)}")
            continue

        write(name, value, param.node_id, param.input, param.value,
              param.spec or InputSpec(param.type),
              lambda new, n=name, p=param: f"{n}: {p.value!r} -> {new!r} (at {p.node_id}.{p.input})")

    return changes


def force_save_images(graph: Graph, filename_prefix: str = "mcp") -> list[str]:
    """Turn PreviewImage nodes into SaveImage so results survive in output/."""
    converted = []
    for node_id, node in graph.items():
        target = PREVIEW_TO_SAVE.get(node.get("class_type", ""))
        if target:
            node["class_type"] = target
            node.setdefault("inputs", {})["filename_prefix"] = filename_prefix
            converted.append(node_id)
    return converted


def clone(graph: Graph) -> Graph:
    return copy.deepcopy(graph)


def strip_meta(graph: Graph) -> Graph:
    """Drop ``_meta`` before submitting - ComfyUI ignores it, and it bloats the payload."""
    return {nid: {k: v for k, v in node.items() if k != "_meta"} for nid, node in graph.items()}


SEVERITIES = ("error", "warning", "note")

WILDCARD_TYPES = {"", "*", "none"}


def _type_set(raw: str) -> set[str]:
    """A slot's declared type as the set of types it accepts, lowercased.

    Empty parts are kept rather than dropped: an empty declaration *is* one of
    litegraph's wildcards, and filtering it out would turn "anything goes" into
    "nothing matches".
    """
    return {part.strip().lower() for part in str(raw).split(",")}


def _slot_types_match(produced: str, consumed: str) -> bool:
    """Whether litegraph would allow a link between these two slots.

    `isValidConnection` is what actually judges a link on the canvas, so this has
    to agree with it - a stricter rule here reports a working graph as broken,
    which is the one outcome `diagnose` must not produce. Two things beyond plain
    equality, and one live workflow tripped both on the first run:

    *Case does not count.* A pack may declare `float` where the core nodes say
    `FLOAT`; litegraph lowercases both before comparing.

    *A type may be a comma-separated set of accepted ones.* `ComfyMathExpression`
    takes `FLOAT,INT,BOOLEAN`, and a FLOAT feeding it matches one member rather
    than the whole string.

    `find_node_types` reads this too, which is the other half of the point: the
    search has to offer what the canvas will then accept.
    """
    made, takes = _type_set(produced), _type_set(consumed)
    if made & WILDCARD_TYPES or takes & WILDCARD_TYPES:
        return True
    return bool(made & takes)


def _is_subgraph_boundary(node_id: str) -> bool:
    """Whether an id names a subgraph's own input/output node rather than a real one.

    Those carry reserved negative ids (``30:-10``) and are not in the graph's node
    list, so every link crossing a subgraph boundary looks dangling without this.
    Measured on the Krea workflow: twenty of twenty-five "errors" were this.
    """
    return node_id.rpartition(":")[2].startswith("-")


def _crosses_boundary(origin_id: str, node_id: str) -> bool:
    """Whether a link leaves the part of the graph a report covers.

    Two shapes, neither evidence of anything wrong: a subgraph's own input node
    carries a reserved negative id, and a SubgraphNode's inputs resolve through
    getInputLink to a node one nesting level down, so the two ends describe
    different things and may not even be in the list. Depth is compared rather
    than the `path` field because a non-recursive read has no path to compare.
    """
    return _is_subgraph_boundary(origin_id) or origin_id.count(":") != str(node_id).count(":")


def diagnose(nodes: list[dict[str, Any]], schemas: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Everything wrong with a workspace graph, worst first.

    `nodes` is get_workspace_graph's summary list - ideally `scope="all"`, since a
    workflow built from subgraphs keeps almost everything that can break inside
    them. `schemas` is a raw /object_info payload; without it only the checks that
    need nothing but the graph's own shape run, which is why the caller is told
    whether it had them.
    """
    schemas = schemas or {}
    index = SchemaIndex(schemas)
    by_id = {str(node.get("id")): node for node in nodes}
    found: list[dict[str, Any]] = []

    def report(node: dict[str, Any], severity: str, kind: str, detail: str, fix: str = "") -> None:
        entry = {
            "severity": severity,
            "kind": kind,
            "node": str(node.get("id")),
            "node_type": node.get("type"),
            "node_title": node.get("title"),
            "detail": detail,
        }
        if fix:
            entry["fix"] = fix
        found.append(entry)

    for node in nodes:
        node_type = str(node.get("type") or "")
        schema = schemas.get(node_type)
        declared = index.for_class(node_type) if schema else {}

        if node.get("describe_error"):
            report(node, "error", "describe_failed", str(node["describe_error"]))
            continue

        if node.get("registered") is False:
            report(
                node, "error", "missing_node_type",
                f"'{node_type}' is not installed in this ComfyUI",
                "install the custom node pack that provides it, or replace the node",
            )
            continue
        if schemas and not schema and node.get("backend") and not node.get("subgraph"):
            report(
                node, "warning", "unknown_to_server",
                f"the browser knows '{node_type}' but /object_info does not",
                "ComfyUI may need a restart to pick the node pack up",
            )

        for slot in node.get("inputs") or []:
            name = str(slot.get("name") or "")
            source = slot.get("from") or None

            if source is None:
                if slot.get("required") and not slot.get("widget"):
                    report(
                        node, "error", "unconnected_input",
                        f"required input '{name}' ({slot.get('type')}) has nothing plugged in",
                        f"connect something producing {slot.get('type')} to {node.get('id')}.{name}",
                    )
                continue

            origin_id = str(source.get("node"))
            if _crosses_boundary(origin_id, str(node.get("id"))):
                continue

            origin = by_id.get(origin_id)
            if origin is None:
                report(
                    node, "error", "dangling_link",
                    f"input '{name}' is fed by node {origin_id}, which is not in the graph",
                    f"reconnect or clear {node.get('id')}.{name}",
                )
                continue

            outputs = origin.get("outputs") or []
            slot_index = source.get("slot")
            valid = (
                isinstance(slot_index, int)
                and not isinstance(slot_index, bool)
                and 0 <= slot_index < len(outputs)
            )
            produced = outputs[slot_index].get("type") if valid else None
            if produced is None:
                report(
                    node, "error", "dangling_link",
                    f"input '{name}' is fed by {origin.get('id')} slot {slot_index}, which does not exist",
                    f"reconnect {node.get('id')}.{name}",
                )
            elif not _slot_types_match(str(produced), str(slot.get("type") or "")):
                report(
                    node, "error", "type_mismatch",
                    f"input '{name}' takes {slot.get('type')} but {origin.get('id')} "
                    f"({origin.get('type')}) produces {produced}",
                    f"rewire {node.get('id')}.{name} to something producing {slot.get('type')}",
                )

        for name, value in (node.get("widgets") or {}).items():
            spec = declared.get(name)
            if spec is None:
                continue
            if spec.kind == "combo":
                if spec.options is not None and value not in spec.options:
                    report(
                        node, "note", "value_not_listed",
                        f"'{name}' is '{value}', which is not among the {len(spec.options or ())} listed options",
                    )
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if spec.minimum is not None and value < spec.minimum:
                report(node, "error", "value_out_of_range", f"'{name}' is {value}, below the minimum {spec.minimum}")
            elif spec.maximum is not None and value > spec.maximum:
                report(node, "error", "value_out_of_range", f"'{name}' is {value}, above the maximum {spec.maximum}")

        mode = node.get("mode")
        if mode in ("muted", "bypassed"):
            report(
                node, "note", str(mode),
                f"'{node.get('title') or node_type}' is {mode}",
                "set_workspace_node_modes with 'always' puts it back",
            )

    return sorted(found, key=lambda issue: SEVERITIES.index(issue["severity"]))


DETAIL_LEVELS = ("outline", "links", "full")

_DULL = {"mode": "always", "collapsed": False, "pinned": False, "registered": True, "backend": True}


def _worth_reporting(node: dict[str, Any]) -> dict[str, Any]:
    found = {key: node[key] for key, dull in _DULL.items() if key in node and node[key] != dull}
    for key in ("subgraph", "path", "describe_error"):
        if node.get(key):
            found[key] = node[key]
    return found


def _outline_node(node: dict[str, Any]) -> dict[str, Any]:
    inputs = node.get("inputs") or []
    outputs = node.get("outputs") or []
    item: dict[str, Any] = {"id": node.get("id"), "type": node.get("type")}
    title = node.get("title")
    if title and title != node.get("type"):
        item["title"] = title
    item["in"] = sum(1 for slot in inputs if slot.get("from"))
    item["out"] = sum(int(slot.get("links") or 0) for slot in outputs)
    item.update(_worth_reporting(node))
    return item


def _slim_input(slot: dict[str, Any]) -> dict[str, Any]:
    """One input slot with its falses left out - an absent key means no.

    Worth doing rather than pretty: `"widget":false,"from":null,"required":false`
    is 42 characters, and the graph measured here has about 400 input slots.
    """
    item: dict[str, Any] = {"name": slot.get("name"), "type": slot.get("type")}
    for key in ("label", "localized_name"):
        if slot.get(key):
            item[key] = slot[key]
    if slot.get("from"):
        item["from"] = slot["from"]
    for key in ("widget", "required"):
        if slot.get(key):
            item[key] = True
    return item


def _shape_node(node: dict[str, Any], detail: str) -> dict[str, Any]:
    item = _outline_node(node)
    if detail == "outline":
        return item
    item["pos"] = node.get("pos")
    item["size"] = node.get("size")
    item["inputs"] = [_slim_input(slot) for slot in node.get("inputs") or []]
    item["outputs"] = list(node.get("outputs") or [])
    if detail == "full" and "widgets" in node:
        item["widgets"] = node["widgets"]
    if detail == "full" and node.get("widget_labels"):
        item["widget_labels"] = node["widget_labels"]
    if detail == "full" and node.get("properties"):
        item["properties"] = node["properties"]
    return item


def _fed_by(nodes: list[dict[str, Any]], wanted: set[str]) -> dict[str, list[str]]:
    """Which nodes read from each of `wanted` - the direction the report omits.

    An input names where it came from, so walking upstream from a subset is free.
    Downstream is not in the shape at all (an output carries only a count), and a
    subset you can only walk one way out of is a dead end half the time.
    """
    downstream: dict[str, list[str]] = {}
    for node in nodes:
        for slot in node.get("inputs") or []:
            source = (slot.get("from") or {}).get("node")
            if source is not None and str(source) in wanted:
                downstream.setdefault(str(source), []).append(str(node.get("id")))
    return downstream


def condense_workspace(
    report: dict[str, Any],
    detail: str = "outline",
    only: list[str] | None = None,
    max_chars: int = 0,
) -> dict[str, Any]:
    """Shrink a workspace summary to something an answer can carry.

    `detail` is how much per node; `only` is which nodes at all. Over `max_chars`
    the detail steps down a level at a time and says so - silently returning less
    than was asked for is the one outcome worth ruling out, because the caller
    cannot tell a sparse graph from a truncated report.
    """
    if detail not in DETAIL_LEVELS:
        raise ValueError(f"detail must be one of {', '.join(DETAIL_LEVELS)}, got {detail!r}")

    nodes = report.get("nodes") or []
    kept = nodes
    downstream: dict[str, list[str]] = {}
    if only is not None:
        wanted = {str(node_id) for node_id in only}
        kept = [node for node in nodes if str(node.get("id")) in wanted]
        downstream = _fed_by(nodes, wanted)

    reduced: list[str] = []
    level = detail
    while True:
        shaped = [_shape_node(node, level) for node in kept]
        for item in shaped:
            feeds = downstream.get(str(item.get("id")))
            if feeds:
                item["feeds"] = feeds
        result = _rebuild(report, shaped, level, only, kept, nodes)
        if not max_chars or _weigh(result) <= max_chars:
            break
        cheaper = DETAIL_LEVELS.index(level) - 1
        if cheaper < 0:
            result = _drop_to_fit(report, shaped, level, only, kept, nodes, max_chars, reduced)
            break
        level = DETAIL_LEVELS[cheaper]
        reduced.append(f"detail dropped to '{level}' to fit {max_chars} characters")

    if reduced:
        result["reduced"] = reduced + [
            "ask again with only=[<ids>] for the nodes that matter, or detail='full' on a subset"
        ]
    return result


def _weigh(result: dict[str, Any]) -> int:
    return len(json.dumps(result, ensure_ascii=False))


def _rebuild(
    report: dict[str, Any],
    shaped: list[dict[str, Any]],
    level: str,
    only: list[str] | None,
    kept: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
) -> dict[str, Any]:
    result = {key: value for key, value in report.items() if key != "nodes"}
    result["detail"] = level
    result["nodes"] = shaped
    if only is not None:
        kept_ids = {str(n.get("id")) for n in kept}
        result["node_count"] = len(kept)
        result["of_nodes"] = len(nodes)
        if isinstance(result.get("issues"), list):
            subset = [i for i in result["issues"] if str(i.get("node")) in kept_ids]
            dropped = len(result["issues"]) - len(subset)
            result["issues"] = subset
            if dropped:
                result["issues_elsewhere"] = dropped
        if "link_count" in result:
            result["link_count"] = sum(
                1 for n in kept for s in (n.get("inputs") or []) if s.get("from")
            )
        missing = sorted({str(i) for i in only} - kept_ids, key=_node_sort_key)
        if missing:
            result["not_found"] = missing
    return result


def _drop_to_fit(
    report: dict[str, Any],
    shaped: list[dict[str, Any]],
    level: str,
    only: list[str] | None,
    kept: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
    max_chars: int,
    reduced: list[str],
) -> dict[str, Any]:
    """Last resort: an outline too big to send. Drop nodes, never quietly."""
    keep = len(shaped)
    while keep > 0:
        result = _rebuild(report, shaped[:keep], level, only, kept, nodes)
        if _weigh(result) <= max_chars:
            break
        keep = keep * 9 // 10 if keep > 10 else keep - 1
    else:
        result = _rebuild(report, [], level, only, kept, nodes)
    if keep < len(shaped):
        reduced.append(
            f"only the first {keep} of {len(shaped)} nodes fit; the rest were left out"
        )
    return result


LAYOUT_SPACING_X = 80
LAYOUT_SPACING_Y = 40

DEFAULT_NODE_SIZE = (240, 120)

COLLAPSED_NODE_HEIGHT = 30


def _node_box(node: dict[str, Any]) -> tuple[float, float, float, float]:
    """One node as (x, y, width, height), with sane numbers whatever it reports."""
    pos = node.get("pos") or (0, 0)
    size = node.get("size") or DEFAULT_NODE_SIZE
    x, y = (float(pos[0]), float(pos[1])) if len(pos) >= 2 else (0.0, 0.0)
    w, h = (float(size[0]), float(size[1])) if len(size) >= 2 else DEFAULT_NODE_SIZE
    if node.get("collapsed"):
        h = COLLAPSED_NODE_HEIGHT
    return x, y, max(w, 1.0), max(h, 1.0)


def _layout_edges(nodes: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Who feeds whom, restricted to the nodes given.

    Cross-boundary links are dropped for the reason `diagnose` drops them: a
    subgraph's own input node is not in the list and its id is not comparable
    with the ids that are, so treating one as an edge would place a node against
    something that is not on this canvas.
    """
    present = {str(node.get("id")) for node in nodes}
    feeds: dict[str, list[str]] = {node_id: [] for node_id in present}
    for node in nodes:
        target = str(node.get("id"))
        for slot in node.get("inputs") or []:
            link = slot.get("from")
            if not link:
                continue
            origin = str(link.get("node"))
            if origin not in present or origin == target:
                continue
            if _crosses_boundary(origin, target):
                continue
            if target not in feeds[origin]:
                feeds[origin].append(target)
    return feeds


def _without_back_edges(order: list[str], feeds: dict[str, list[str]]) -> dict[str, list[str]]:
    """The same links with the ones that close a loop dropped.

    Cycles are ordinary here, not a defect: a Power Lora Loader takes the model
    out of a subgraph, changes it and hands it back, so `30 -> 77 -> 30` is how
    the workflow is meant to be wired. But something has to be left of something,
    and a left-to-right layout has to break every loop *somewhere*.

    Where it breaks decides how the result reads, and depth-first order picks the
    edge a person would: the loop is entered from the side the data arrives on,
    so the link that closes it is the one going back. Left to a plain topological
    pass, the remainder is placed in whatever order the nodes happened to be
    listed in - measured, that put a subgraph to the right of its own previews.
    """
    UNVISITED, ON_STACK, DONE = 0, 1, 2
    state = dict.fromkeys(order, UNVISITED)
    forward: dict[str, list[str]] = {node_id: [] for node_id in order}

    indegree = dict.fromkeys(order, 0)
    for targets in feeds.values():
        for target in targets:
            indegree[target] += 1
    roots = [n for n in order if indegree[n] == 0] + [n for n in order if indegree[n] != 0]

    for root in roots:
        if state[root] != UNVISITED:
            continue
        state[root] = ON_STACK
        stack = [(root, iter(feeds[root]))]
        while stack:
            node_id, targets = stack[-1]
            for target in targets:
                if state[target] == ON_STACK:
                    continue
                forward[node_id].append(target)
                if state[target] == UNVISITED:
                    state[target] = ON_STACK
                    stack.append((target, iter(feeds[target])))
                    break
            else:
                state[node_id] = DONE
                stack.pop()
    return forward


def _layer_of(order: list[str], feeds: dict[str, list[str]]) -> dict[str, int]:
    """Column index per node: one further right than everything feeding it.

    Kahn's order first, so a node is only placed once every predecessor has been.
    `feeds` is expected to be acyclic; the leftover pass is a belt-and-braces
    guard so an unexpected loop degrades to a poor layout rather than a missing
    column.
    """
    indegree = {node_id: 0 for node_id in order}
    for targets in feeds.values():
        for target in targets:
            indegree[target] += 1

    ready = [node_id for node_id in order if indegree[node_id] == 0]
    placed: list[str] = []
    while ready:
        node_id = ready.pop(0)
        placed.append(node_id)
        for target in feeds[node_id]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    seen = set(placed)
    placed += [node_id for node_id in order if node_id not in seen]

    layer = {node_id: 0 for node_id in order}
    for node_id in placed:
        for target in feeds[node_id]:
            layer[target] = max(layer[target], layer[node_id] + 1)
    return layer


def _ordered_rows(
    columns: list[list[str]],
    fed_by: dict[str, list[str]],
    current_y: dict[str, float],
    passes: int = 2,
) -> None:
    """Sort each column so links cross as little as possible, in place.

    Barycentre ordering: a node sits opposite the average row of what feeds it.
    Two sweeps is where the returns stop being visible - this is a readability
    pass, not an optimiser, and a stable result matters more than an optimal one.
    """
    for column in columns:
        column.sort(key=lambda node_id: current_y[node_id])
    rows = {node_id: i for column in columns for i, node_id in enumerate(column)}
    for _ in range(passes):
        for column in columns:
            def key(node_id: str) -> tuple[float, float]:
                sources = [rows[src] for src in fed_by.get(node_id, []) if src in rows]
                if not sources:
                    return (rows[node_id], current_y[node_id])
                return (sum(sources) / len(sources), current_y[node_id])

            column.sort(key=key)
            for i, node_id in enumerate(column):
                rows[node_id] = i


def arrange(
    nodes: list[dict[str, Any]],
    spacing_x: float = LAYOUT_SPACING_X,
    spacing_y: float = LAYOUT_SPACING_Y,
    origin: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """Lay nodes out left to right, in the order the data flows through them.

    `nodes` is get_workspace_graph's summary list, or any subset of it - arranging
    a handful of nodes and leaving the rest alone is the common case, and a subset
    simply has fewer edges to honour.

    Returns the positions to write, with nodes that are already where they belong
    left out, so the caller can see how much of a change this is before making it.
    """
    if not nodes:
        return {"positions": {}, "columns": 0, "moved": 0, "unchanged": 0, "bounds": None}

    order = [str(node.get("id")) for node in nodes]
    boxes = {str(node.get("id")): _node_box(node) for node in nodes}
    feeds = _without_back_edges(order, _layout_edges(nodes))
    fed_by: dict[str, list[str]] = {node_id: [] for node_id in boxes}
    for source, targets in feeds.items():
        for target in targets:
            fed_by[target].append(source)

    layer = _layer_of(order, feeds)
    buckets: list[list[str]] = [[] for _ in range(max(layer.values()) + 1)]
    for node in nodes:
        node_id = str(node.get("id"))
        buckets[layer[node_id]].append(node_id)
    columns = [column for column in buckets if column]

    current_y = {node_id: box[1] for node_id, box in boxes.items()}
    _ordered_rows(columns, fed_by, current_y)

    left = origin[0] if origin else min(box[0] for box in boxes.values())
    top = origin[1] if origin else min(box[1] for box in boxes.values())

    heights = [
        sum(boxes[node_id][3] for node_id in column) + spacing_y * (len(column) - 1)
        for column in columns
    ]
    tallest = max(heights)

    positions: dict[str, list[float]] = {}
    unchanged = 0
    x = left
    for column, height in zip(columns, heights):
        y = top + (tallest - height) / 2
        for node_id in column:
            was_x, was_y, _, node_h = boxes[node_id]
            place = [round(x), round(y)]
            if [round(was_x), round(was_y)] == place:
                unchanged += 1
            else:
                positions[node_id] = place
            y += node_h + spacing_y
        x += max(boxes[node_id][2] for node_id in column) + spacing_x

    return {
        "positions": positions,
        "columns": len(columns),
        "moved": len(positions),
        "unchanged": unchanged,
        "bounds": [round(left), round(top), round(x - spacing_x - left), round(tallest)],
    }


ALIGN_EDGES = ("left", "right", "top", "bottom", "centre_x", "centre_y")

DISTRIBUTE_AXES = ("x", "y")

_EDGE_AXIS = {
    "left": "x", "right": "x", "centre_x": "x",
    "top": "y", "bottom": "y", "centre_y": "y",
}


def align(
    nodes: list[dict[str, Any]],
    edge: str = "",
    distribute: str = "",
    spacing: float | None = None,
) -> dict[str, Any]:
    """Snap nodes to a common edge, space them evenly, or both.

    Unlike `arrange`, this never reads a link: it moves the nodes it is given
    along one axis and leaves everything else - including which column anything
    is in - exactly as the author had it. That is the point. Tidying a row of
    loaders should not rearrange the workflow around them.

    `edge` and `distribute` may be combined only across axes: aligning tops while
    spreading horizontally is one intention, while aligning lefts *and* spreading
    horizontally is two contradictory ones.
    """
    if edge and edge not in ALIGN_EDGES:
        raise ValueError(f"edge must be one of {', '.join(ALIGN_EDGES)}, got {edge!r}")
    if distribute and distribute not in DISTRIBUTE_AXES:
        raise ValueError(f"distribute must be one of {', '.join(DISTRIBUTE_AXES)}, got {distribute!r}")
    if not edge and not distribute:
        raise ValueError("nothing to do; pass edge, distribute, or both")
    if edge and distribute and _EDGE_AXIS[edge] == distribute:
        raise ValueError(
            f"edge={edge!r} and distribute={distribute!r} both act on the {distribute} axis: "
            "one puts every node on the same line, the other spreads them along it"
        )
    if len(nodes) < 2:
        raise ValueError(f"{len(nodes)} node(s) given; aligning needs at least two")

    boxes = {str(node.get("id")): _node_box(node) for node in nodes}
    placed = {node_id: [box[0], box[1]] for node_id, box in boxes.items()}

    if edge:
        axis = 0 if _EDGE_AXIS[edge] == "x" else 1
        span = 2 if axis == 0 else 3
        low = min(box[axis] for box in boxes.values())
        high = max(box[axis] + box[span] for box in boxes.values())
        for node_id, box in boxes.items():
            if edge in ("left", "top"):
                placed[node_id][axis] = low
            elif edge in ("right", "bottom"):
                placed[node_id][axis] = high - box[span]
            else:
                placed[node_id][axis] = (low + high) / 2 - box[span] / 2

    if distribute:
        axis = 0 if distribute == "x" else 1
        span = 2 if axis == 0 else 3
        order = sorted(boxes, key=lambda node_id: (boxes[node_id][axis], _node_sort_key(node_id)))
        extent = sum(boxes[node_id][span] for node_id in order)
        if spacing is None:
            first, last = boxes[order[0]], boxes[order[-1]]
            reach = (last[axis] + last[span]) - first[axis]
            gap = (reach - extent) / (len(order) - 1)
        else:
            gap = float(spacing)
        cursor = boxes[order[0]][axis]
        for node_id in order:
            placed[node_id][axis] = cursor
            cursor += boxes[node_id][span] + gap

    positions: dict[str, list[float]] = {}
    unchanged = 0
    for node_id, box in boxes.items():
        point = [round(placed[node_id][0]), round(placed[node_id][1])]
        if [round(box[0]), round(box[1])] == point:
            unchanged += 1
        else:
            positions[node_id] = point

    return {
        "positions": positions,
        "moved": len(positions),
        "unchanged": unchanged,
        "edge": edge or None,
        "distribute": distribute or None,
    }


NODE_FLAGS = ("deprecated", "experimental", "api_node")

HIDDEN_FLAGS = ("deprecated", "api_node")

DESCRIPTION_SHOWN = 140


def _pack_of(entry: dict[str, Any]) -> str:
    """Which pack a node came from, in the form a person would name it."""
    module = str(entry.get("python_module") or "")
    if module.startswith("custom_nodes."):
        return module[len("custom_nodes.") :]
    return module or "?"


def _flags_of(entry: dict[str, Any]) -> list[str]:
    return [flag for flag in NODE_FLAGS if entry.get(flag)]


def _input_slots(entry: dict[str, Any]) -> tuple[list[tuple[str, str, bool]], list[tuple[str, bool]]]:
    """Link inputs and widget inputs, kept apart.

    They answer different questions - a link type decides what can be wired in,
    a widget name decides what can be set - and merging them into one list makes
    both harder to read.
    """
    links: list[tuple[str, str, bool]] = []
    widgets: list[tuple[str, bool]] = []
    sections = (entry or {}).get("input") or {}
    for group in ("required", "optional"):
        for name, raw in (sections.get(group) or {}).items():
            optional = group == "optional"
            head = raw[0] if isinstance(raw, list) and raw else None
            if isinstance(head, str) and head not in LITERAL_KINDS:
                links.append((name, head, optional))
            elif isinstance(head, (str, list)):
                widgets.append((name, optional))
            else:
                links.append((name, "*", optional))
    return links, widgets


def _output_slots(entry: dict[str, Any]) -> list[tuple[str, str]]:
    """(label, type) per output. `output_name` is the author's label for it."""
    types = (entry or {}).get("output") or []
    names = (entry or {}).get("output_name") or []
    out: list[tuple[str, str]] = []
    for index, declared in enumerate(types):
        type_name = declared if isinstance(declared, str) else "COMBO"
        label = names[index] if index < len(names) and isinstance(names[index], str) else type_name
        out.append((label, type_name))
    return out


def _where_matched(needle: str, class_type: str, entry: dict[str, Any]) -> tuple[int, str] | None:
    """Rank a search hit: which field it landed in, and how squarely.

    Returns ``(score, field)`` with lower better, or None for a miss.
    """
    haystacks: list[tuple[str, list[str]]] = [
        ("name", [class_type]),
        ("title", [str(entry.get("display_name") or "")]),
        ("alias", [str(a) for a in (entry.get("search_aliases") or [])]),
        ("category", [str(entry.get("category") or "")]),
        ("description", [str(entry.get("description") or "")]),
    ]
    for rank, (field_name, values) in enumerate(haystacks):
        best: int | None = None
        for value in values:
            lowered = value.lower()
            if not lowered or needle not in lowered:
                continue
            precision = 0 if lowered == needle else 1 if lowered.startswith(needle) else 2
            best = precision if best is None else min(best, precision)
        if best is not None:
            return rank * 3 + best, field_name
    return None


def _accepts(links: list[tuple[str, str, bool]], wanted: str) -> tuple[bool, bool, bool]:
    """``(matches, declares_it, only_optionally)`` for an input of the wanted type.

    A `*` slot matches everything, which is both true and uninformative: a
    Reroute really can carry a NOISE, and is still not the answer to "where do I
    get a NOISE from". Wildcard hits are kept - the search has to agree with the
    validator that will judge the link - but ranked below the nodes that name
    the type outright. There are 166 wildcard-output nodes on this install, and
    unranked they would crowd a bare type query out of its own limit.

    Optional is the same argument one step milder. `Context Big (rgthree)` takes
    an optional LATENT among ten inputs, so it matches "what accepts a LATENT"
    and is not what anyone meant.
    """
    exact = [optional for _, declared, optional in links if _declares(declared, wanted)]
    if exact:
        return True, True, all(exact)
    loose = [optional for _, declared, optional in links if _slot_types_match(wanted, declared)]
    return bool(loose), False, bool(loose) and all(loose)


def _declares(declared: str, wanted: str) -> bool:
    """Whether a slot names the wanted type outright, wildcards aside.

    The same set-and-case rules as `_slot_types_match`, or the ranking disagrees
    with the match: `float` against a core node's FLOAT, and `FLOAT,INT,BOOLEAN`
    naming three types at once, are declarations - sorting them into the
    wildcard tier buries exactly the nodes the ranking exists to surface.
    """
    return wanted.strip().lower() in (_type_set(declared) - WILDCARD_TYPES)


def _produces(outputs: list[tuple[str, str]], wanted: str) -> tuple[bool, bool]:
    if any(_declares(declared, wanted) for _, declared in outputs):
        return True, True
    return any(_slot_types_match(declared, wanted) for _, declared in outputs), False


def _shape_hit(
    class_type: str,
    entry: dict[str, Any],
    matched_on: str,
    flags: list[str],
    links: list[tuple[str, str, bool]],
    widgets: list[tuple[str, bool]],
    outputs: list[tuple[str, str]],
) -> dict[str, Any]:
    """One search result, built only for the entries that survive the cut."""
    result: dict[str, Any] = {"class_type": class_type}
    title = str(entry.get("display_name") or "")
    if title and title != class_type:
        result["title"] = title
    result["category"] = entry.get("category") or None
    result["pack"] = _pack_of(entry)
    result.update(_slot_labels(links, widgets, outputs))
    if flags:
        result["flags"] = flags
    description = " ".join(str(entry.get("description") or "").split())
    if description:
        result["about"] = (
            description if len(description) <= DESCRIPTION_SHOWN else description[:DESCRIPTION_SHOWN] + "..."
        )
    if matched_on:
        result["matched_on"] = matched_on
    return result


def _slot_labels(
    links: list[tuple[str, str, bool]],
    widgets: list[tuple[str, bool]],
    outputs: list[tuple[str, str]],
) -> dict[str, list[str]]:
    """The wiring of a node, compactly. A trailing `?` marks an optional slot."""
    return {
        "inputs": [f"{declared}{'?' if optional else ''}" for _, declared, optional in links],
        "widgets": [f"{name}{'?' if optional else ''}" for name, optional in widgets],
        "outputs": [declared for _, declared in outputs],
    }


def find_node_types(
    schemas: Schemas,
    search: str = "",
    input_type: str = "",
    output_type: str = "",
    category: str = "",
    pack: str = "",
    include_deprecated: bool = False,
    include_api: bool = False,
    limit: int = 60,
) -> dict[str, Any]:
    """Search installed node types by name, by slot type, or by both.

    `schemas` is a whole /object_info payload. Every filter is ANDed, and each is
    optional; with none of them this is a paged listing of what is installed.

    `input_type` / `output_type` match a slot type (`"LATENT"`, `"IMAGE"`),
    honouring litegraph's wildcards so a `*` slot matches anything - the same rule
    `isValidConnection` applies when the link is actually drawn.

    Nodes filtered out by their flags are counted rather than silently dropped,
    because an empty answer and an answer with nine deprecated hits behind it
    call for different next steps.
    """
    needle = search.strip().lower()
    category_needle = category.strip().lower()
    pack_needle = pack.strip().lower()
    allowed = set(HIDDEN_FLAGS)
    if include_deprecated:
        allowed.discard("deprecated")
    if include_api:
        allowed.discard("api_node")

    scored: list[tuple[tuple[int, int, int, int, str], str, dict[str, Any], str, list[str], Any, Any, Any]] = []
    hidden: dict[str, int] = {}
    for class_type, entry in (schemas or {}).items():
        if not isinstance(entry, dict):
            continue
        links, widgets = _input_slots(entry)
        outputs = _output_slots(entry)
        wildcard = 0
        incidental = 0
        if input_type:
            matches, declared, optional_only = _accepts(links, input_type)
            if not matches:
                continue
            wildcard |= not declared
            incidental |= optional_only
        if output_type:
            matches, declared = _produces(outputs, output_type)
            if not matches:
                continue
            wildcard |= not declared
        if category_needle and category_needle not in str(entry.get("category") or "").lower():
            continue
        if pack_needle and pack_needle not in _pack_of(entry).lower():
            continue

        matched_on = ""
        score = 0
        if needle:
            hit = _where_matched(needle, class_type, entry)
            if hit is None:
                continue
            score, matched_on = hit

        flags = _flags_of(entry)
        suppressed = [flag for flag in flags if flag in allowed]
        if suppressed:
            for flag in suppressed:
                hidden[flag] = hidden.get(flag, 0) + 1
            continue

        sort_key = (wildcard, incidental, score, len(links) + len(widgets) + len(outputs), class_type)
        scored.append((sort_key, class_type, entry, matched_on, flags, links, widgets, outputs))

    scored.sort(key=lambda item: item[0])
    results = [
        _shape_hit(class_type, entry, matched_on, flags, links, widgets, outputs)
        for _, class_type, entry, matched_on, flags, links, widgets, outputs in scored[:limit]
    ]
    answer: dict[str, Any] = {
        "installed": len(schemas or {}),
        "matched": len(scored),
        "shown": len(results),
        "results": results,
    }
    if hidden:
        answer["hidden"] = dict(sorted(hidden.items()))
    return answer


def summarise_schema(entry: dict[str, Any], class_type: str = "", options_shown: int = OPTIONS_SHOWN) -> dict[str, Any]:
    """One /object_info entry, readable and bounded.

    The raw entry is not safe to hand back whole: combo inputs carry a full model
    list each, and `easy loraSwitcher` measures 199 069 characters - near fifty
    thousand tokens for one lookup. The option lists are what makes it big and
    the least of what makes it useful, so they are truncated and counted.
    """
    links, _widgets = _input_slots(entry)
    link_types = {name: declared for name, declared, _ in links}
    sections = (entry or {}).get("input") or {}

    first_holder: dict[tuple[Any, ...], str] = {}

    inputs: list[dict[str, Any]] = []
    for group in ("required", "optional"):
        for name, raw in (sections.get(group) or {}).items():
            config = raw[1] if isinstance(raw, list) and len(raw) > 1 and isinstance(raw[1], dict) else {}
            item: dict[str, Any] = {"name": name}
            if group == "optional":
                item["optional"] = True
            if name in link_types:
                item["type"] = link_types[name]
            else:
                spec = parse_input_spec(raw)
                item["type"] = spec.kind
                if spec.options is not None:
                    shared = first_holder.get(spec.options)
                    if shared is not None:
                        item["same_options_as"] = shared
                    else:
                        first_holder[spec.options] = name
                        item["options"] = list(spec.options[:options_shown])
                        if len(spec.options) > options_shown:
                            item["options_total"] = len(spec.options)
                if spec.minimum is not None:
                    item["min"] = spec.minimum
                if spec.maximum is not None:
                    item["max"] = spec.maximum
                if "default" in config:
                    item["default"] = config["default"]
            tooltip = " ".join(str(config.get("tooltip") or "").split())
            if tooltip:
                item["about"] = tooltip
            inputs.append(item)

    summary: dict[str, Any] = {"class_type": class_type or entry.get("name") or ""}
    title = str(entry.get("display_name") or "")
    if title and title != summary["class_type"]:
        summary["title"] = title
    summary["category"] = entry.get("category") or None
    summary["pack"] = _pack_of(entry)
    flags = _flags_of(entry)
    if flags:
        summary["flags"] = flags
    if entry.get("output_node"):
        summary["output_node"] = True
    description = " ".join(str(entry.get("description") or "").split())
    if description:
        summary["about"] = description
    aliases = [str(a) for a in (entry.get("search_aliases") or [])]
    if aliases:
        summary["aliases"] = aliases
    summary["inputs"] = inputs
    summary["outputs"] = [{"name": label, "type": declared} for label, declared in _output_slots(entry)]
    return summary
