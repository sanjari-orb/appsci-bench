"""Shared deck model — the single source of truth for both the .pptx and the presentation JSON.

Rendering (render_pptx.py) and JSON emission (emit_json.py) both consume this model, which is how the
two deliverables stay in lockstep (same slides, same order) as required by the output contract.

Field aliases spell the JSON keys EXACTLY as the customer's downstream tooling expects
(`slideNumber`, `aspectRatio`, `sourceUrl`, `altText`, ...). Serialise with
`model_dump(by_alias=True, exclude_none=True)`. Runtime-only fields (placeholder targets, raw image
bytes) are marked `exclude=True` so they never reach the JSON.
"""
from __future__ import annotations

from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    # Allow constructing by python name OR json alias; validate assignments defensively.
    model_config = ConfigDict(populate_by_name=True)


class _ElementBase(_Base):
    name: str
    # Runtime-only: which placeholder (by idx) in the chosen layout this element fills.
    # Set by the generate stage, used by render_pptx, never serialised.
    placeholder_idx: Optional[int] = Field(default=None, exclude=True)


class TextElement(_ElementBase):
    type: Literal["text"] = "text"
    content: str


class BulletListElement(_ElementBase):
    type: Literal["bullet_list"] = "bullet_list"
    items: List[str] = Field(default_factory=list)


class TableElement(_ElementBase):
    type: Literal["table"] = "table"
    rows: List[List[str]] = Field(default_factory=list)


class ImageElement(_ElementBase):
    type: Literal["image"] = "image"
    source_url: str = Field(alias="sourceUrl")
    alt_text: str = Field(default="", alias="altText")
    # Runtime-only: raw bytes + media type to embed into the .pptx (never serialised).
    image_bytes: Optional[bytes] = Field(default=None, exclude=True, repr=False)
    media_type: Optional[str] = Field(default=None, exclude=True)


Element = Annotated[
    Union[TextElement, BulletListElement, TableElement, ImageElement],
    Field(discriminator="type"),
]


def element_has_content(el: Element) -> bool:
    """True if the element carries real content (content/items/rows/sourceUrl) per the contract."""
    if isinstance(el, TextElement):
        return bool(el.content and el.content.strip())
    if isinstance(el, BulletListElement):
        return any(bool(i and str(i).strip()) for i in el.items)
    if isinstance(el, TableElement):
        return any(any(bool(c is not None and str(c).strip()) for c in row) for row in el.rows)
    if isinstance(el, ImageElement):
        return bool(el.source_url and el.source_url.strip())
    return False


class Slide(_Base):
    slide_number: int = Field(alias="slideNumber")
    layout: str
    elements: List[Element] = Field(default_factory=list)
    notes: Optional[str] = None

    def has_real_content(self) -> bool:
        return any(element_has_content(e) for e in self.elements)


class Metadata(_Base):
    author: str = "Slides Generator"
    version: str = "1.0"
    date: str = ""


class Presentation(_Base):
    title: str
    theme: str = "default"
    aspect_ratio: str = Field(default="16:9", alias="aspectRatio")
    metadata: Metadata = Field(default_factory=Metadata)
    slides: List[Slide] = Field(default_factory=list)


class Deck(_Base):
    """Top-level wrapper — serialises to `{"presentation": {...}}`."""

    presentation: Presentation

    def to_json_dict(self) -> dict:
        return self.model_dump(by_alias=True, exclude_none=True)
