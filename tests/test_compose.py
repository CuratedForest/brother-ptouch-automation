"""Tests for the post-render compose decorator."""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest
from PIL import Image

from label_printer import TapeWidth
from label_printer.engine.compose import (
    compose_extras,
    image_from_base64,
    load_and_fit_image,
    strip_template_handled,
)
from label_printer.tape import geometry_for
from label_printer.templates import default_registry


@pytest.fixture(scope="module")
def registry():
    return default_registry()


def _render_body(qualified: str, data: dict, tape: TapeWidth, registry) -> Image.Image:
    template = registry.get(qualified)
    return template.render(template.validate(data), tape)


def test_link_appends_qr_on_the_right(registry):
    tape = TapeWidth.MM_12
    body = _render_body("kitchen/pantry_jar",
                        {"name": "FLOUR", "purchased": "2026-04-17"}, tape, registry)
    composed = compose_extras(body, {"link": "vault:kitchen/flour"}, tape)

    # QR is sized to print height, so composed image is wider by at least that much.
    geom = geometry_for(tape)
    assert composed.height == body.height == geom.print_pins
    assert composed.width > body.width + geom.print_pins // 2


def test_image_appends_bitmap_on_the_right(tmp_path: Path, registry):
    tape = TapeWidth.MM_12

    # Create a small bitmap to attach.
    icon = Image.new("RGB", (40, 40), "black")
    icon_path = tmp_path / "icon.png"
    icon.save(icon_path)

    body = _render_body("kitchen/pantry_jar",
                        {"name": "FLOUR", "purchased": "2026-04-17"}, tape, registry)
    composed = compose_extras(body, {"image": str(icon_path)}, tape)

    assert composed.height == body.height
    assert composed.width > body.width


def test_no_extras_returns_body_unchanged(registry):
    tape = TapeWidth.MM_12
    body = _render_body("kitchen/pantry_jar",
                        {"name": "FLOUR", "purchased": "2026-04-17"}, tape, registry)
    composed = compose_extras(body, {}, tape)
    assert composed is body


def test_strip_template_handled_removes_template_owned_keys(registry):
    qr_template = registry.get("utility/qr")
    extras = {"link": "anything", "image": "/tmp/unused.png"}
    stripped = strip_template_handled(extras, qr_template)
    assert "link" not in stripped  # utility/qr handles link internally
    assert stripped["image"] == "/tmp/unused.png"


def test_strip_passes_through_for_plain_template(registry):
    plain = registry.get("kitchen/pantry_jar")
    extras = {"link": "vault:x", "image": "/tmp/y.png"}
    assert strip_template_handled(extras, plain) == extras


@pytest.mark.parametrize(
    "qualified,data",
    [
        ("kitchen/spice", {"name": "Paprika", "origin": "Spain", "best_by": "2027-01"}),
        ("three_d_printing/filament_spool", {
            "material": "PLA", "color": "Black", "brand": "Bambu", "opened": "2026-04-01",
        }),
        ("electronics/component_bin", {"value": "10k", "footprint": "0805"}),
        ("pet/collar_backup", {"name": "Rex", "contact": "+1 555 0100"}),
    ],
)
def test_link_works_across_template_packs(qualified: str, data: dict, registry):
    tape = TapeWidth.MM_12
    body = _render_body(qualified, data, tape, registry)
    composed = compose_extras(body, {"link": f"vault:test/{qualified}"}, tape)
    assert composed.width > body.width
    assert composed.height == body.height


# --- icon extra -------------------------------------------------------------


@pytest.mark.parametrize("tape", [TapeWidth.MM_12, TapeWidth.MM_24])
def test_icon_extra_appends_on_the_right(tape: TapeWidth, registry):
    body = _render_body("kitchen/spice", {"name": "Paprika"}, tape, registry)
    composed = compose_extras(body, {"icon": "lucide:wheat"}, tape)
    assert composed.height == body.height
    assert composed.width > body.width


def test_icon_extra_unknown_icon_raises(registry):
    from label_printer.engine.icons import IconNotFoundError

    tape = TapeWidth.MM_12
    body = _render_body("kitchen/spice", {"name": "Paprika"}, tape, registry)
    with pytest.raises(IconNotFoundError):
        compose_extras(body, {"icon": "mdi:this-icon-does-not-exist"}, tape)


def test_icon_extra_resolves_mdi_namespace(tmp_path: Path, registry, monkeypatch):
    # A fake mdi source on LABEL_PRINTER_ICON_PATH — no need for the real
    # ~7000-icon repo in tests.
    mdi_dir = tmp_path / "mdi"
    mdi_dir.mkdir()
    (mdi_dir / "fridge.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
        '<rect width="24" height="24" fill="black"/></svg>'
    )
    monkeypatch.setenv("LABEL_PRINTER_ICON_PATH", str(tmp_path))

    tape = TapeWidth.MM_12
    body = _render_body("kitchen/spice", {"name": "Paprika"}, tape, registry)
    composed = compose_extras(body, {"icon": "mdi:fridge"}, tape)
    assert composed.width > body.width


def test_preset_with_icon_field_strips_icon_extra(registry):
    # pantry_jar renders its own icon from the `icon` field — the trailing-edge
    # icon extra must not double-render.
    template = registry.get("kitchen/pantry_jar")
    extras = {"icon": "lucide:wheat", "link": "vault:x"}
    stripped = strip_template_handled(extras, template)
    assert "icon" not in stripped
    assert stripped["link"] == "vault:x"


# --- image extras from non-path sources --------------------------------------


def test_load_and_fit_image_accepts_pil_image(registry):
    img = Image.new("RGB", (40, 40), "black")
    fitted = load_and_fit_image(img, 64)
    assert fitted.height == 64
    assert fitted.mode == "RGB"


def test_image_extra_accepts_pil_image(registry):
    tape = TapeWidth.MM_12
    body = _render_body("kitchen/spice", {"name": "Paprika"}, tape, registry)
    composed = compose_extras(body, {"image": Image.new("RGB", (40, 40), "black")}, tape)
    assert composed.width > body.width
    assert composed.height == body.height


def _tiny_png_b64() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "black").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_image_from_base64_roundtrip():
    img = image_from_base64(_tiny_png_b64())
    assert img.size == (8, 8)


def test_image_from_base64_rejects_bad_base64():
    with pytest.raises(ValueError, match="base64"):
        image_from_base64("not valid base64 !!!")


def test_image_from_base64_rejects_non_image():
    payload = base64.b64encode(b"this is not an image").decode("ascii")
    with pytest.raises(ValueError, match="readable image"):
        image_from_base64(payload)
