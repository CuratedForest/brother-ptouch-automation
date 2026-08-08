"""Batch assembly: turn a batch spec into a list of label images.

This module is the shared core for ``lp batch`` (CLI) and ``POST /batch``
(HTTP service). It owns three things:

* The batch spec parsing — both the original array form (v1) and the
  same-template shorthand (v2).
* :func:`build_batch_images` — render every entry (with extras), expand
  ``copies``, and enforce the single-tape-width rule.
* :func:`stack_preview` — a vertical whole-strip preview PNG so a batch can
  be eyeballed before ``--send``.

It stays pure: images in, images out. No encoding, no transport.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from label_printer.engine.compose import compose_extras, strip_template_handled
from label_printer.tape import TapeWidth


class BatchSpecError(ValueError):
    """Raised when a batch spec is malformed or inconsistent."""


@dataclass(frozen=True)
class BatchEntry:
    """One label in a batch, pre-render.

    ``tape_mm`` of None means "the template's default tape" — resolved during
    :func:`build_batch_images`.
    """

    template: str
    fields: dict[str, Any] = field(default_factory=dict)
    tape_mm: int | None = None
    link: str | None = None
    image: str | None = None
    copies: int = 1


def tape_from_mm(mm: int) -> TapeWidth:
    """Map a millimetre width to a TapeWidth (3/4 both mean 3.5mm tape)."""
    try:
        return TapeWidth(4 if mm in (3, 4) else mm)
    except ValueError as e:
        valid = ", ".join(str(int(t)) for t in TapeWidth)
        raise BatchSpecError(f"tape must be one of: {valid} (got {mm})") from e


# --- Spec parsing -------------------------------------------------------------


def parse_spec(raw: Any) -> list[BatchEntry]:
    """Parse a batch spec (already-decoded JSON) into entries.

    Two forms are accepted:

    * **v1 (array)** — each element is a full entry::

        [{"template": "kitchen/spice", "tape_mm": 12,
          "fields": {"name": "Paprika"}}, ...]

    * **v2 (object)** — same-template shorthand::

        {"template": "kitchen/spice", "tape_mm": 12,
         "labels": [{"fields": {"name": "Paprika"}},
                    {"fields": {"name": "Cumin"}, "copies": 3}]}

      Top-level ``template`` / ``tape_mm`` / ``link`` / ``image`` act as
      defaults; each label may override ``template`` and add its own
      ``link`` / ``image``.
    """
    if isinstance(raw, list):
        entries = [_parse_entry(e, defaults={}, where=f"entry {i}")
                   for i, e in enumerate(raw)]
    elif isinstance(raw, dict):
        defaults = {
            k: raw.get(k) for k in ("template", "tape_mm", "link", "image")
        }
        labels = raw.get("labels")
        if not isinstance(labels, list) or not labels:
            raise BatchSpecError("spec object must contain a non-empty 'labels' array")
        entries = [_parse_entry(e, defaults=defaults, where=f"labels[{i}]")
                   for i, e in enumerate(labels)]
    else:
        raise BatchSpecError("batch spec must be a JSON array or object")

    if not entries:
        raise BatchSpecError("batch spec produced zero labels")
    return entries


def _parse_entry(raw: Any, *, defaults: dict, where: str) -> BatchEntry:
    if not isinstance(raw, dict):
        raise BatchSpecError(f"{where}: expected an object, got {type(raw).__name__}")
    template = raw.get("template", defaults.get("template"))
    if not template:
        raise BatchSpecError(f"{where}: missing 'template'")
    copies = raw.get("copies", 1)
    if not isinstance(copies, int) or copies < 1:
        raise BatchSpecError(f"{where}: 'copies' must be a positive integer")
    tape_mm = raw.get("tape_mm", defaults.get("tape_mm"))
    return BatchEntry(
        template=str(template),
        fields=dict(raw.get("fields", {})),
        tape_mm=int(tape_mm) if tape_mm is not None else None,
        link=raw.get("link", defaults.get("link")),
        image=raw.get("image", defaults.get("image")),
        copies=copies,
    )


def entries_from_csv(template: str, csv_path: Path,
                     column_map: dict[str, str] | None = None,
                     tape_mm: int | None = None) -> list[BatchEntry]:
    """One batch entry per CSV row.

    By default every CSV column maps to the template field of the same name.
    ``column_map`` renames columns: ``{"col_name": "field_name"}``. Columns
    mapped to an empty string are dropped.
    """
    column_map = column_map or {}
    entries: list[BatchEntry] = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise BatchSpecError(f"{csv_path}: CSV has no header row")
        for row_num, row in enumerate(reader, start=2):
            fields: dict[str, str] = {}
            for col, value in row.items():
                target = column_map.get(col, col)
                if target and value is not None and value != "":
                    fields[target] = value
            if not fields:
                raise BatchSpecError(f"{csv_path} row {row_num}: no usable columns")
            entries.append(BatchEntry(
                template=template, fields=fields, tape_mm=tape_mm,
            ))
    if not entries:
        raise BatchSpecError(f"{csv_path}: CSV contained no data rows")
    return entries


# --- Rendering ----------------------------------------------------------------


def render_entry(template, entry: BatchEntry, tape: TapeWidth) -> Image.Image:
    """Render one entry, including any post-render extras (link / image)."""
    extras = {k: v for k, v in {"link": entry.link, "image": entry.image}.items() if v}
    extras = strip_template_handled(extras, template)
    body = template.render(template.validate(dict(entry.fields)), tape)
    return compose_extras(body, extras, tape)


def build_batch_images(entries: Iterable[BatchEntry], registry) -> tuple[list[Image.Image], TapeWidth]:
    """Render all entries to images and return them with the batch tape width.

    Expands ``copies`` inline and enforces the single-tape-width rule (a
    chained job can't switch tape mid-job).
    """
    images: list[Image.Image] = []
    tapes: set[TapeWidth] = set()
    for i, entry in enumerate(entries):
        try:
            template = registry.get(entry.template)
        except KeyError as e:
            raise BatchSpecError(f"entry {i}: {e}") from e
        tape = tape_from_mm(entry.tape_mm) if entry.tape_mm is not None \
            else template.meta.default_tape
        tapes.add(tape)
        if len(tapes) > 1:
            raise BatchSpecError(
                f"all batch entries must share one tape width; "
                f"got {sorted(int(t) for t in tapes)}"
            )
        image = render_entry(template, entry, tape)
        images.extend([image] * entry.copies)

    if not images:
        raise BatchSpecError("batch produced zero labels")
    return images, tapes.pop()


# --- Preview ------------------------------------------------------------------


def stack_preview(images: list[Image.Image], *, separator: int = 1) -> Image.Image:
    """Stack label images vertically (top-to-bottom, in print order).

    The result mirrors how the strip physically comes off the printer:
    first label at the top, last at the bottom. Labels are left-aligned;
    narrower labels are padded with white on the right. A ``separator``-pixel
    black line is drawn between labels to show the cut positions.
    """
    if not images:
        raise ValueError("stack_preview requires at least one image")
    width = max(img.width for img in images)
    height = sum(img.height for img in images) + separator * (len(images) - 1)
    canvas = Image.new("RGB", (width, height), "white")
    y = 0
    for i, img in enumerate(images):
        canvas.paste(img.convert("RGB"), (0, y))
        y += img.height
        if separator and i < len(images) - 1:
            for dy in range(separator):
                for x in range(width):
                    canvas.putpixel((x, y + dy), (0, 0, 0))
            y += separator
    return canvas
