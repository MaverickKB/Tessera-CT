"""Generic seed layers.

These records explain the store. They are not project history. `write_seed`
refuses a store that already has layers so a later run cannot mix this
baseline into real work.
"""

from __future__ import annotations

from .core import AetherMind

SEED_AUTHOR = "seed"

SEED_LAYERS = [
    {
        "type": "discovery",
        "ctx": "guide/what-this-is",
        "conf": 1.0,
        "markers": [],
        "body": (
            "AetherMind is a project-local append-only continuity store. "
            "Records live in .aethermind/layers.aem beside the project they "
            "describe. A record is a layer: who wrote it, when, which context "
            "it belongs to, how sure they were, and the fact itself. Later "
            "sessions read layers. They do not replace them."
        ),
    },
    {
        "type": "load-bearing",
        "ctx": "guide/rules",
        "conf": 1.0,
        "markers": ["!"],
        "body": (
            "!Do not edit layers.aem by hand. Append through the library or "
            "the aethermind command. A correction is a new layer that names "
            "the id it corrects. The old layer stays. Confidence is a float "
            "from 0 to 1. Markers in the body are advisory: ? uncertain, "
            "! load-bearing, ~ impression, > follow up."
        ),
    },
    {
        "type": "discovery",
        "ctx": "guide/bootstrap",
        "conf": 1.0,
        "markers": [],
        "body": (
            "This store was created by aethermind init --seed. These layers "
            "are a generic baseline. They are not project history. Append "
            "your own layers under your own context paths. Delete the "
            ".aethermind directory if you want an empty start instead."
        ),
    },
]

SEED_TEXTURE = (
    "The store should feel like a stack of notes left for the next session: "
    "short, dated, and still there after the conversation ends."
)


def write_seed(project_root) -> dict:
    """Append the generic baseline to an empty store."""
    aether = AetherMind(project_root, create=True)
    existing = aether.read_layers()
    if existing:
        raise ValueError(
            "refusing to write seed into a store that already has layers"
        )
    written_ids = []
    for item in SEED_LAYERS:
        layer = aether.write_layer(
            type=item["type"],
            body=item["body"],
            ctx=item["ctx"],
            conf=item["conf"],
            markers=list(item["markers"]),
            author=SEED_AUTHOR,
        )
        written_ids.append(layer.id)
    aether.write_texture(SEED_TEXTURE, "guide/attention", marker="~", author=SEED_AUTHOR)
    return {"count": len(written_ids), "ids": written_ids, "author": SEED_AUTHOR}
