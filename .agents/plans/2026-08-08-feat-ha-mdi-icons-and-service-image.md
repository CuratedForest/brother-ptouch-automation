# Plan: Home Assistant icon support (mdi: references) + service image upload

## Goal

Let Home Assistant (and any remote HTTP caller) put icons/bitmaps on labels:

1. **mdi: icon references** resolved server-side from the Material Design Icons pack — HA's native icon namespace. Works in template icon fields (already supported by `engine/icons.py`) and as a new global `--icon` extra composable onto any template.
2. **mdi pack baked into the Docker image** so the container service resolves `mdi:*` out of the box.
3. **`image_b64` on the HTTP service** — the remote equivalent of the CLI's `--image` flag: callers (HA `rest_command`, etc.) POST a base64-encoded PNG/JPEG instead of a server-local path.

Out of scope: passing raw SVG bytes, fetching icons from HA over the network, `ha:` alias prefix, USB/BT transports.

## Current state (verified)

- `engine/icons.py`: `IconRegistry.find("mdi:wifi")` already works; searches `LABEL_PRINTER_ICON_PATH` → `~/.config/label-printer/icons/mdi/` → bundled. `lp icons install-mdi` clones the mdi repo into `USER_ROOT/mdi/` (subdir `svg/` — see `_install_icon_repo` in `cli.py`).
- `engine/compose.py`: `EXTRA_KEYS = ("link", "image")`; `compose_extras(body, extras, tape)` appends to right edge; `load_and_fit_image(path, target_h)` loads from filesystem path only.
- `service.py`: `RenderRequest.image` / `BatchLabel.image` are **server-local paths** — useless for remote callers today.
- `engine/batch.py`: per-entry extras `link`/`image` flow through `build_batch_images` → `compose_extras`.
- `containers/brother-ptouch-automation/Dockerfile`: keeps `git` in the image explicitly for `lp icons install-*`; runs as user `labelprinter` (uid 65532, home `/home/labelprinter`). `_USER_ROOT` resolves via `LABEL_PRINTER_ICON_HOME` env or `XDG_CONFIG_HOME`/`~/.config`.
- Preset templates with `icon_field` render their icon internally via `render_two_line_label(icon=...)`; these already accept `mdi:*` values with zero changes.

## Tasks (ordered)

### 1. `icon` as a global compose extra
- `engine/compose.py`:
  - Add `"icon"` to `EXTRA_KEYS`.
  - In `compose_extras`, handle `icon`: resolve via `icons.load_icon(name, size)` where `size = geom.print_pins - 4` (match `layout.py`'s inset) and append like the image extra. Import `load_icon` lazily inside the function (mirrors `layout.py`) so the base install without cairosvg still imports.
  - Docstring: document the `icon` key.
- Preset templates that render an icon field internally must not double-render when the caller passes the `icon` extra. Check how `handles_extras` is declared on preset-backed templates (`templates/presets.py`); if presets with `icon_field` don't already declare it, add `handles_extras = frozenset({"icon"})` for those. Do the same for `utility/image` (already handles `image`) — no icon change needed there.
- CLI (`cli.py`): add `--icon NAME` option to `render`, `print`, and `batch` (batch-wide default; per-label `icon` key in spec JSON, same pattern as `link`/`image` in `engine/batch.py` `_entry_from_raw`). Thread through `_render_with_extras`.
- Service (`service.py`): add `icon: str | None = None` to `RenderRequest` and `BatchLabel`; include in the extras dict in `_render_body_with_extras` and batch entry building.
- `IconNotFoundError` from a bad `mdi:` name must surface as the existing 4xx client-error path in the service (verify it's already mapped; add mapping if not).

### 2. `image_b64` service field (remote `--image`)
- `engine/compose.py`: widen `load_and_fit_image` to accept `str | Path | Image.Image` — if given a `PIL.Image`, skip `Image.open`. Keep behavior identical otherwise.
- `service.py`:
  - Add `image_b64: str | None = None` to `RenderRequest` and `BatchLabel`.
  - Decode helper: `base64.b64decode` → `Image.open(BytesIO(...))` → pass the PIL image through as the `image` extra value. Bad base64 / unreadable image → 400 with a clear message.
  - `image_b64` and `image` are mutually exclusive per request/label — reject both-set with 400.
- `engine/batch.py`: allow per-label `image_b64` in spec JSON too (decode at entry build time) so `lp batch spec.json` accepts the same shape the service does. CLI batch spec: support `image_b64` key identically (decode in `_entry_from_raw` path).

### 3. Dockerfile: bake in the mdi pack
- `containers/brother-ptouch-automation/Dockerfile`, after user creation:
  ```dockerfile
  RUN su -s /bin/sh labelprinter -c "lp icons install-mdi"
  ```
  (or set `LABEL_PRINTER_ICON_HOME=/home/labelprinter/.config/label-printer/icons` and run before `USER`, then `chown -R`). Verify the install lands under `/home/labelprinter/.config/label-printer/icons/mdi/` — that path must survive the volume-mount pattern documented in the README (mounting `/home/labelprinter/.config/label-printer` would shadow baked icons; note this in the README's Docker section: either bake succeeds only when no volume is mounted, or document `LABEL_PRINTER_ICON_PATH` as override).
- Do **not** install full Lucide — bundled curated Lucide covers it.

### 4. Tests (`tests/`)
- Compose: `icon` extra appends to any two-line label; preset with icon field + `icon` extra does not double-render; `load_and_fit_image` accepts a PIL Image.
- Service: `POST /render` with `icon="mdi:wifi"` (monkeypatch/temp `LABEL_PRINTER_ICON_PATH` with a tiny fixture SVG so tests don't need the real mdi repo); `image_b64` round-trip (tiny PNG fixture → appears in output, right edge wider than body); 400s for bad b64 and both-image-fields.
- Batch: per-label `icon` and `image_b64` in spec JSON.
- Follow AGENTS.md: cover new behavior at 12mm and 24mm where template rendering is involved; no printer mocks beyond the existing dry-run boundary.

### 5. Docs
- `README.md`: HTTP service section — document `icon` and `image_b64` request fields with an HA `rest_command` curl example (`-d '{"template":"kitchen/pantry_jar","fields":{"name":"Flour","icon":"mdi:barrel"}}'`). Docker section — mdi pack is baked in; volume-shadowing caveat. Highlights bullet: mdi via `mdi:` names.
- `skill/SKILL.md`: one line that `mdi:` icon names work anywhere `lucide:` does.
- `docs/creating-a-preset.md`: icon field examples can use `mdi:*` (one sentence).
- `AGENTS.md`: no structural change needed.

## Failure modes to handle
- mdi pack not installed + `mdi:` requested → existing `IconNotFoundError` with search paths in the message (already good); service maps to 4xx.
- cairosvg missing → `IconEngineUnavailable` (already loud).
- `image_b64` decodes to non-image or zero-height → 400, not 500.
- Docker build without network access to github.com → mdi clone fails the build; acceptable (git clone is already the documented install path), but keep the `RUN` as its own layer so it can be commented out easily.

## Validation
- `.venv/bin/pytest` — full suite green.
- `.venv/bin/ruff check src tests`
- Manual smoke: `lp render kitchen/pantry_jar -f name=Flour --icon mdi:barrel --png-out /tmp/ha_icon.png` (with mdi installed) and eyeball; `lp serve` + curl `/render` with `image_b64` and with `icon`.
- Docker: build the image locally (`docker build -f containers/brother-ptouch-automation/Dockerfile .`), run, `GET /health`, `POST /render` with an `mdi:` icon.
