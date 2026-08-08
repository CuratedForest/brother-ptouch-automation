# Proposal 0002 — Same-template batches

**Status**: implemented
**Opened**: 2026-08-07
**Updated**: 2026-08-07
**Requested by**: @harteWired

## One-line summary

Make "print N variations of the same template" a first-class workflow: a concise batch spec, CSV input, per-entry copies, whole-strip previews, and batch support in the HTTP service — all on top of the existing `encode_batch` chaining core.

## Motivation

The byte-level machinery for chained multi-label jobs already exists and works (`encode_batch` — job-level prologue, per-page `ESC i z` with `n9` page-position bytes, `0x0C` separators, half-cut between labels, `0x1A` feed-and-cut). What's missing is the ergonomics. Today, printing ten spice jars means writing a JSON array of ten near-identical objects, each repeating `"template": "kitchen/spice"`. And:

- `lp print` has no `--copies` — duplicates of one label can't be chained at all.
- Batch entries silently drop `link` / `image` extras (the batch loop calls `template.render` directly instead of `_render_with_extras`).
- There's no way to preview the whole strip — you can eyeball one label's PNG, not the sequence you're about to run through the cutter.
- No spacing control between chained labels.
- The HTTP service has no batch endpoint — `/print` is strictly single-label, so Home Assistant / chat clients can't batch.

Real workflows this unlocks: labelling a whole spice rack from a CSV, printing three copies of every cable flag for a rewire, a HA rest_command that prints today's leftover labels in one strip.

## Design

### 1. Shared batch builder — `engine/batch.py` (new)

A small pure module used by both the CLI and the service. Owns:

- `BatchEntry` — `{template, fields, link?, image?, copies?}`. `copies` expands inline (one entry → N images) before encoding.
- `build_batch_images(entries, registry) -> (list[Image], TapeWidth)` — renders each entry **with extras** (fixes the current extras gap), expands copies, and enforces the single-tape-width rule in one place (deduplicating the inline logic in `cli.py`).
- `stack_preview(images, tape) -> Image` — renders the whole batch as one preview PNG, **labels stacked vertically** (top-to-bottom, in print order) with a 1-px separator between them — matching how the strip physically comes off the printer.

Stays pure: images in, images out. No transport, no encoding.

### 2. Encoder — `engine/raster.py`

Two additions, both defaulting to current behavior so existing goldens stay valid:

- `RasterOptions.gap_dots: int = 0` — extra blank feed between chained labels. Implemented by padding each non-final page image with blank raster lines before encoding, so the byte-level chaining logic is untouched.
- `RasterOptions.cut_every: int | None = None` — full cut after every N labels within one job (re-emit `ESC i K` at group boundaries). Default `None` = current "half-cut between, full cut at end". Useful when a 30-label strip is unwieldy.

If either ends up changing default output bytes, goldens regenerate via `REGEN_GOLDENS=1 pytest tests/test_raster_encoder.py` — but the intent is zero change to the default stream.

### 3. CLI — `cli.py`

**Batch spec v2** (backwards-compatible — the current array form keeps working). New object shorthand for same-template batches:

```json
{
  "template": "kitchen/spice",
  "tape_mm": 12,
  "labels": [
    {"fields": {"name": "Paprika"}},
    {"fields": {"name": "Cumin"}, "copies": 3},
    {"fields": {"name": "Oregano"}, "link": "vault:kitchen/spices/oregano"}
  ]
}
```

**CSV input** — the real "label ten jars" workflow:

```bash
lp batch kitchen/spice --csv names.csv --field name
# columns → template fields; repeat --field col=field to map more
```

**Copies sugar:**

```bash
lp print kitchen/spice -f name=Paprika --copies 4   # routes through encode_batch
```

**Strip preview** (vertical stacking, per this proposal):

```bash
lp batch rack.json --preview-out strip.png   # whole strip, top-to-bottom
```

**Batch-wide flags:** `--gap-dots N`, `--cut-every N`, existing `--no-half-cut`, `--send`, `--bin-out`.

### 4. HTTP service — `service.py`

- `POST /batch` — accepts the spec-v2 object (including `labels[].copies`, `link`, `image`) plus `send`, `gap_dots`, `cut_every`, `half_cut`. Dry-run returns the chained raster bytes (`application/octet-stream`, `X-Dry-Run: true`); `send: true` goes through the same SNMP tape-match gate as `/print` (409 on mismatch, 502 on transport failure, 503 on missing host).
- `POST /render/batch` — returns the vertically stacked strip PNG (binary by default, JSON + base64 with `"format": "base64"`, mirroring `/render`) so chat/HA clients can preview batches too.

### 5. Docs & skill

- README batch section: spec v2, CSV, `--copies`, `/batch` curl example.
- `docs/cutting-and-batches.md`: note on `gap_dots` / `cut_every` if they touch encoder bytes.
- `skill/SKILL.md`: batch workflow — same-template shorthand + `--preview-out` before `--send` (the skill's "always dry-render first" rule extends to strips).

## Settings surface

| Setting | Where | Default |
|---|---|---|
| `copies` per entry | spec v2, CSV rows, `--copies` | 1 |
| `gap_dots` between labels | `--gap-dots` / API | 0 |
| `cut_every N` (full cut per group) | `--cut-every` / API | off |
| `half_cut` on/off | `--no-half-cut` / API | on |
| `link` / `image` per entry | spec v2 / API | — |
| tape width (one per batch) | spec / `--tape` | template default |
| whole-strip preview (vertical) | `--preview-out` / `/render/batch` | — |
| `send` vs dry-run | unchanged | dry-run |

## Testing

- `tests/test_batch.py`: gap / cut_every byte expectations; spec-v2 parsing; copies expansion; extras per entry.
- `tests/test_cli.py`: CSV mapping, `--copies`, `--preview-out` vertical layout, mixed-tape rejection via the shared builder.
- `tests/test_service.py`: `/batch` dry-run + send paths, tape-mismatch 409, `/render/batch` PNG shape (width = tape print width, height = sum of labels).
- No printer mocks — byte streams into buffers, diffed against goldens (per project convention).

## Implementation order

1. `engine/batch.py` + spec-v2 parser + tests
2. Encoder `gap_dots` / `cut_every` + golden tests
3. CLI wiring (`--csv`, `--copies`, `--preview-out`, batch flags)
4. `/batch` + `/render/batch` service endpoints + tests
5. README / cutting-and-batches / SKILL.md updates

Each step lands green independently.

## Explicitly out of scope

- **Mixed-tape batches.** Chained jobs can't switch tape mid-job; the single-width rule stays.
- **Per-label cut modes.** Cut behavior is a batch-wide setting, not per entry.
- **Reordering / collation.** Labels print in spec order; no sorting logic in the engine.
