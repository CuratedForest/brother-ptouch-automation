"""Multi-label batch encoding with half-cut between pages."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from PIL import Image

from label_printer import RasterOptions, TapeWidth, encode_batch, encode_job
from label_printer.cli import main
from label_printer.constants import CMD_PRINT_AND_FEED
from label_printer.engine.batch import (
    BatchEntry,
    BatchSpecError,
    build_batch_images,
    entries_from_csv,
    parse_spec,
    stack_preview,
)
from label_printer.tape import geometry_for
from label_printer.templates import default_registry


def _test_image(tape: TapeWidth, length: int = 40) -> Image.Image:
    return Image.new("1", (length, geometry_for(tape).print_pins), 1)


def test_batch_of_one_degrades_to_single_job():
    """A 1-image batch is byte-for-byte identical to encode_job — different
    control structures (auto-cut on for single, off for batch) need single-
    label paths to behave normally."""
    tape = TapeWidth.MM_12
    img = _test_image(tape)
    single = encode_job(img, tape)
    batched = encode_batch([img], tape)
    assert single == batched


def test_batch_rejects_empty():
    with pytest.raises(ValueError, match="at least one"):
        encode_batch([], TapeWidth.MM_12)


def test_batch_emits_one_set_of_control_codes_plus_terminating_kick():
    """ESC i M / d are job-level — emit once. ESC i K appears twice: once
    at the job-level prologue (chain on, half-cut on) and once right before
    the last page (no-chain on) so the terminating 0x1A produces a real cut.
    Per philpem / rasterprynt / py-brotherlabel."""
    tape = TapeWidth.MM_12
    data = encode_batch([_test_image(tape)] * 3, tape)
    assert data.count(b"\x1b\x69\x4d") == 1, "ESC i M (Mode) must appear once"
    assert data.count(b"\x1b\x69\x64") == 1, "ESC i d (Margin) must appear once"
    # ESC i K appears at job start AND before the final page.
    assert data.count(b"\x1b\x69\x4b") == 2
    # ESC i A must NOT appear — auto-cut is off at the job level, so
    # cut-every-N is meaningless. A regression introducing it (e.g. an
    # encode_batch refactor that calls build_prologue) is exactly what
    # this assertion guards against.
    assert data.count(b"\x1b\x69\x41") == 0, "ESC i A must not appear in batch output"
    # Three ESC i z (one per page) and the final terminator.
    assert data.count(b"\x1b\x69\x7a") == 3
    assert data.endswith(CMD_PRINT_AND_FEED)


def test_batch_advanced_mode_starts_chain_on_then_flips_for_last_page():
    """Job-level ESC i K = 0x04 (half-cut, chain on). The second ESC i K
    just before the last page = 0x0C (half-cut + no-chain) so the
    terminating 0x1A fires a feed-and-cut."""
    tape = TapeWidth.MM_12
    data = encode_batch([_test_image(tape)] * 3, tape)
    positions = []
    pos = 0
    while True:
        idx = data.find(b"\x1b\x69\x4b", pos)
        if idx < 0:
            break
        positions.append(data[idx + 3])
        pos = idx + 4
    assert positions == [0x04, 0x0C]


def test_batch_disables_auto_cut_at_job_level():
    """The single Mode byte must have auto-cut bit (0x40) cleared. With
    auto-cut on, the printer full-cuts every page regardless of half-cut
    bit — the documented Brother failure mode for batch + half-cut."""
    tape = TapeWidth.MM_12
    data = encode_batch([_test_image(tape)] * 2, tape)
    idx = data.find(b"\x1b\x69\x4d")
    assert idx >= 0
    mode_byte = data[idx + 3]
    assert mode_byte & 0x40 == 0, f"auto-cut must be off in batch mode, got 0x{mode_byte:02x}"


def test_batch_per_page_n9_indicates_position():
    """ESC i z's n9 byte (offset +11 from the prefix start) per-page marks
    starting page (0), middle pages (1), and last page (2)."""
    tape = TapeWidth.MM_12
    data = encode_batch([_test_image(tape, 20)] * 3, tape)
    n9_values = []
    pos = 0
    while True:
        idx = data.find(b"\x1b\x69\x7a", pos)
        if idx < 0:
            break
        # ESC i z (3) + n1..n8 (8) = 11; n9 sits at offset +11.
        n9_values.append(data[idx + 11])
        pos = idx + 13
    assert n9_values == [0, 1, 2]


def test_batch_no_half_cut_flag_clears_half_cut_bit():
    """With half_cut=False the job-level ESC i K drops the half-cut bit;
    the pre-last kick still flips no-chain on for the terminating cut."""
    tape = TapeWidth.MM_12
    data = encode_batch(
        [_test_image(tape)] * 2, tape, RasterOptions(half_cut=False)
    )
    positions = []
    pos = 0
    while True:
        idx = data.find(b"\x1b\x69\x4b", pos)
        if idx < 0:
            break
        positions.append(data[idx + 3])
        pos = idx + 4
    assert positions == [0x00, 0x08]  # no half-cut + chain on, then no-chain


def test_batch_has_single_session_prologue():
    tape = TapeWidth.MM_12
    data = encode_batch([_test_image(tape)] * 3, tape)
    # Invalidate + initialize happen once at the very start of the job.
    assert data[:100] == b"\x00" * 100
    assert data[100:102] == b"\x1b\x40"
    # Dynamic-mode and status-notify only appear once.
    assert data.count(b"\x1b\x69\x61\x01") == 1
    assert data.count(b"\x1b\x69\x21\x00") == 1
    # ESC i z print-information appears once per page.
    assert data.count(b"\x1b\x69\x7a") == 3


# --- CLI `lp batch` ---------------------------------------------------------

def test_cli_batch_dry_run(tmp_path: Path):
    spec = [
        {"template": "kitchen/spice", "tape_mm": 12,
         "fields": {"name": "Paprika"}},
        {"template": "kitchen/spice", "tape_mm": 12,
         "fields": {"name": "Cumin"}},
        {"template": "kitchen/spice", "tape_mm": 12,
         "fields": {"name": "Oregano"}},
    ]
    spec_path = tmp_path / "batch.json"
    spec_path.write_text(json.dumps(spec))
    out = tmp_path / "out.bin"
    result = CliRunner().invoke(
        main, ["batch", str(spec_path), "--bin-out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "batched 3 label" in result.output
    assert "dry-run" in result.output
    assert out.exists()
    data = out.read_bytes()
    # Three pages → three print-information commands; one final 0x1A.
    assert data.count(b"\x1b\x69\x7a") == 3
    assert data.endswith(CMD_PRINT_AND_FEED)


def test_cli_batch_rejects_mixed_tape(tmp_path: Path):
    spec = [
        {"template": "kitchen/spice", "tape_mm": 12, "fields": {"name": "a"}},
        {"template": "kitchen/spice", "tape_mm": 24, "fields": {"name": "b"}},
    ]
    spec_path = tmp_path / "batch.json"
    spec_path.write_text(json.dumps(spec))
    result = CliRunner().invoke(main, ["batch", str(spec_path)])
    assert result.exit_code != 0
    assert "tape width" in result.output.lower()


def test_cli_batch_send_requires_configured_host(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LABEL_PRINTER_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("LABEL_PRINTER_HOST", raising=False)
    import importlib

    from label_printer import state as state_mod
    importlib.reload(state_mod)

    spec = [{"template": "kitchen/spice", "tape_mm": 12, "fields": {"name": "x"}}]
    spec_path = tmp_path / "batch.json"
    spec_path.write_text(json.dumps(spec))
    result = CliRunner().invoke(main, ["batch", str(spec_path), "--send"])
    assert result.exit_code != 0
    assert "no printer host configured" in result.output


# --- Encoder: gap_dots and cut_every ------------------------------------------

def _raster_line_counts(data: bytes) -> list[int]:
    """Extract the declared raster-line count (n4..n7) from each ESC i z."""
    counts = []
    pos = 0
    while True:
        idx = data.find(b"\x1b\x69\x7a", pos)
        if idx < 0:
            break
        counts.append(int.from_bytes(data[idx + 7 : idx + 11], "little"))
        pos = idx + 13
    return counts


def test_gap_dots_extends_non_final_pages():
    """gap_dots adds blank raster lines to every page except the last, and
    the per-page ESC i z line count includes the gap."""
    tape = TapeWidth.MM_12
    img = _test_image(tape, length=10)
    plain = encode_batch([img] * 3, tape)
    gapped = encode_batch([img] * 3, tape, RasterOptions(gap_dots=7))
    assert _raster_line_counts(plain) == [10, 10, 10]
    assert _raster_line_counts(gapped) == [17, 17, 10]
    # 14 extra blank lines total, each collapsing to a 0x5A Z-shortcut.
    assert gapped.count(b"\x5a") - plain.count(b"\x5a") == 14


def test_gap_dots_default_is_byte_identical():
    tape = TapeWidth.MM_12
    imgs = [_test_image(tape)] * 2
    assert encode_batch(imgs, tape) == encode_batch(
        imgs, tape, RasterOptions(gap_dots=0)
    )


def test_cut_every_splits_into_sub_jobs():
    """cut_every=2 over 5 labels → three sub-jobs (2+2+1), each with its own
    session prologue and terminating 0x1A."""
    tape = TapeWidth.MM_12
    data = encode_batch(
        [_test_image(tape)] * 5, tape, RasterOptions(cut_every=2)
    )
    assert data.count(b"\x1b\x40") == 3, "each sub-job re-initializes"
    assert data.count(CMD_PRINT_AND_FEED) == 3
    assert data.count(b"\x1b\x69\x7a") == 5, "five pages total"


def test_cut_every_larger_than_batch_is_single_job():
    tape = TapeWidth.MM_12
    imgs = [_test_image(tape)] * 3
    assert encode_batch(imgs, tape, RasterOptions(cut_every=10)) == \
        encode_batch(imgs, tape)


def test_cut_every_none_is_byte_identical():
    tape = TapeWidth.MM_12
    imgs = [_test_image(tape)] * 2
    assert encode_batch(imgs, tape) == encode_batch(
        imgs, tape, RasterOptions(cut_every=None)
    )


# --- Spec parsing (v1 array + v2 object) ---------------------------------------

def test_parse_spec_v1_array():
    raw = [
        {"template": "kitchen/spice", "tape_mm": 12, "fields": {"name": "a"}},
        {"template": "kitchen/spice", "fields": {"name": "b"}, "copies": 2},
    ]
    entries = parse_spec(raw)
    assert [e.template for e in entries] == ["kitchen/spice"] * 2
    assert entries[0].tape_mm == 12
    assert entries[1].tape_mm is None
    assert entries[1].copies == 2


def test_parse_spec_v2_object_inherits_defaults():
    raw = {
        "template": "kitchen/spice",
        "tape_mm": 12,
        "link": "vault:kitchen",
        "labels": [
            {"fields": {"name": "Paprika"}},
            {"fields": {"name": "Cumin"}, "copies": 3, "link": "vault:cumin"},
        ],
    }
    entries = parse_spec(raw)
    assert all(e.template == "kitchen/spice" for e in entries)
    assert all(e.tape_mm == 12 for e in entries)
    assert entries[0].link == "vault:kitchen"
    assert entries[1].link == "vault:cumin"
    assert entries[1].copies == 3


def test_parse_spec_v2_requires_labels():
    with pytest.raises(BatchSpecError, match="labels"):
        parse_spec({"template": "kitchen/spice"})


def test_parse_spec_rejects_garbage():
    with pytest.raises(BatchSpecError):
        parse_spec("not a spec")
    with pytest.raises(BatchSpecError):
        parse_spec([])
    with pytest.raises(BatchSpecError, match="template"):
        parse_spec([{"fields": {"name": "x"}}])
    with pytest.raises(BatchSpecError, match="copies"):
        parse_spec([{"template": "kitchen/spice", "copies": 0}])


# --- CSV input ------------------------------------------------------------------

def test_entries_from_csv_default_column_mapping(tmp_path: Path):
    csv_path = tmp_path / "spices.csv"
    csv_path.write_text("name\nPaprika\nCumin\n")
    entries = entries_from_csv("kitchen/spice", csv_path)
    assert [e.fields for e in entries] == [{"name": "Paprika"}, {"name": "Cumin"}]
    assert all(e.template == "kitchen/spice" for e in entries)


def test_entries_from_csv_column_rename_and_empty_skip(tmp_path: Path):
    csv_path = tmp_path / "spices.csv"
    csv_path.write_text("spice,origin\nPaprika,Spain\nCumin,\n")
    entries = entries_from_csv(
        "kitchen/spice", csv_path, {"spice": "name"}, tape_mm=12,
    )
    assert entries[0].fields == {"name": "Paprika", "origin": "Spain"}
    assert entries[1].fields == {"name": "Cumin"}  # empty column dropped
    assert all(e.tape_mm == 12 for e in entries)


def test_entries_from_csv_rejects_empty(tmp_path: Path):
    csv_path = tmp_path / "empty.csv"
    csv_path.write_text("name\n")
    with pytest.raises(BatchSpecError, match="no data rows"):
        entries_from_csv("kitchen/spice", csv_path)


# --- build_batch_images ---------------------------------------------------------

def test_build_batch_images_expands_copies_and_extras():
    reg = default_registry()
    entries = [
        BatchEntry(template="kitchen/spice", fields={"name": "A"}, copies=2),
        BatchEntry(template="kitchen/spice", fields={"name": "B"},
                   link="vault:b"),
    ]
    images, tape = build_batch_images(entries, reg)
    assert tape == TapeWidth.MM_12
    assert len(images) == 3
    assert images[0].tobytes() == images[1].tobytes(), "copies are identical"
    # The link extra grows the label (QR appended on the right edge).
    plain, _ = build_batch_images(
        [BatchEntry(template="kitchen/spice", fields={"name": "B"})], reg,
    )
    assert images[2].width > plain[0].width


def test_build_batch_images_rejects_mixed_tape():
    reg = default_registry()
    entries = [
        BatchEntry(template="kitchen/spice", fields={"name": "a"}, tape_mm=12),
        BatchEntry(template="kitchen/spice", fields={"name": "b"}, tape_mm=24),
    ]
    with pytest.raises(BatchSpecError, match="tape width"):
        build_batch_images(entries, reg)


def test_build_batch_images_unknown_template_names_entry():
    reg = default_registry()
    entries = [BatchEntry(template="nope/nope", fields={})]
    with pytest.raises(BatchSpecError, match="entry 0.*No such template"):
        build_batch_images(entries, reg)


# --- stack_preview ---------------------------------------------------------------

def test_stack_preview_stacks_vertically_with_separators():
    imgs = [
        Image.new("RGB", (30, 10), "white"),
        Image.new("RGB", (20, 10), "white"),
        Image.new("RGB", (25, 10), "white"),
    ]
    preview = stack_preview(imgs)
    # Width = widest label; height = sum + 1px separator between labels.
    assert preview.size == (30, 32)
    # Separator rows are black across the full width.
    for y in (10, 21):
        assert all(preview.getpixel((x, y)) == (0, 0, 0) for x in range(30))


def test_stack_preview_single_label_has_no_separator():
    preview = stack_preview([Image.new("RGB", (12, 8), "white")])
    assert preview.size == (12, 8)


# --- CLI: spec v2, CSV, copies, preview ------------------------------------------

def test_cli_batch_spec_v2_with_copies(tmp_path: Path):
    spec = {
        "template": "kitchen/spice",
        "labels": [
            {"fields": {"name": "Paprika"}},
            {"fields": {"name": "Cumin"}, "copies": 3},
        ],
    }
    spec_path = tmp_path / "rack.json"
    spec_path.write_text(json.dumps(spec))
    out = tmp_path / "out.bin"
    result = CliRunner().invoke(
        main, ["batch", str(spec_path), "--bin-out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "batched 4 label" in result.output
    assert out.read_bytes().count(b"\x1b\x69\x7a") == 4


def test_cli_batch_csv_mode(tmp_path: Path):
    csv_path = tmp_path / "spices.csv"
    csv_path.write_text("name\nPaprika\nCumin\nOregano\n")
    out = tmp_path / "out.bin"
    result = CliRunner().invoke(
        main, ["batch", str(csv_path), "--csv", "--template", "kitchen/spice",
               "--bin-out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "batched 3 label" in result.output
    assert out.read_bytes().count(b"\x1b\x69\x7a") == 3


def test_cli_batch_csv_requires_template(tmp_path: Path):
    csv_path = tmp_path / "spices.csv"
    csv_path.write_text("name\nPaprika\n")
    result = CliRunner().invoke(main, ["batch", str(csv_path), "--csv"])
    assert result.exit_code != 0
    assert "--template" in result.output


def test_cli_batch_preview_out_stacks_vertically(tmp_path: Path):
    spec = {
        "template": "kitchen/spice",
        "labels": [{"fields": {"name": n}} for n in ("A", "B", "C")],
    }
    spec_path = tmp_path / "rack.json"
    spec_path.write_text(json.dumps(spec))
    preview = tmp_path / "strip.png"
    result = CliRunner().invoke(
        main, ["batch", str(spec_path), "--preview-out", str(preview),
               "--bin-out", str(tmp_path / "out.bin")]
    )
    assert result.exit_code == 0, result.output
    img = Image.open(preview)
    # Vertical stacking: height = 3 × label height + 2 separator rows,
    # and the label height matches the 12mm tape print area (70 pins).
    label_h = geometry_for(TapeWidth.MM_12).print_pins
    assert img.height == 3 * label_h + 2


def test_cli_batch_gap_and_cut_every_flags(tmp_path: Path):
    spec = [{"template": "kitchen/spice", "fields": {"name": f"n{i}"}}
            for i in range(5)]
    spec_path = tmp_path / "rack.json"
    spec_path.write_text(json.dumps(spec))
    out = tmp_path / "out.bin"
    result = CliRunner().invoke(
        main, ["batch", str(spec_path), "--gap-dots", "5", "--cut-every", "2",
               "--bin-out", str(out)]
    )
    assert result.exit_code == 0, result.output
    data = out.read_bytes()
    assert data.count(CMD_PRINT_AND_FEED) == 3  # 2+2+1 sub-jobs


def test_cli_print_copies_chains():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main, ["print", "kitchen/spice", "-f", "name=Paprika",
                   "--copies", "3", "--bin-out", "copies.bin"]
        )
        assert result.exit_code == 0, result.output
        data = Path("copies.bin").read_bytes()
        assert data.count(b"\x1b\x69\x7a") == 3
        assert data.endswith(CMD_PRINT_AND_FEED)


def test_cli_print_single_copy_matches_encode_job():
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            main, ["print", "kitchen/spice", "-f", "name=Paprika",
                   "--bin-out", "one.bin"]
        )
        assert result.exit_code == 0, result.output
        assert Path("one.bin").read_bytes().count(b"\x1b\x69\x7a") == 1


# --- Batch extras: icon / image_b64 ------------------------------------------


def _tiny_png_b64() -> str:
    import base64
    import io

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "black").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_parse_spec_per_label_icon_and_image_b64():
    spec = {
        "template": "kitchen/spice",
        "tape_mm": 12,
        "labels": [
            {"fields": {"name": "Paprika"}, "icon": "lucide:wheat"},
            {"fields": {"name": "Cumin"}, "image_b64": _tiny_png_b64()},
        ],
    }
    entries = parse_spec(spec)
    assert entries[0].icon == "lucide:wheat"
    assert entries[1].image_b64


def test_parse_spec_rejects_image_and_image_b64_together():
    spec = [{
        "template": "kitchen/spice",
        "fields": {"name": "Paprika"},
        "image": "/tmp/x.png",
        "image_b64": _tiny_png_b64(),
    }]
    with pytest.raises(BatchSpecError, match="mutually exclusive"):
        parse_spec(spec)


def test_build_batch_images_applies_icon_and_image_b64():
    reg = default_registry()
    plain, _ = build_batch_images(
        [BatchEntry(template="kitchen/spice", fields={"name": "Paprika"},
                    tape_mm=12)], reg,
    )
    with_extras, _ = build_batch_images(
        [BatchEntry(template="kitchen/spice", fields={"name": "Paprika"},
                    tape_mm=12, icon="lucide:wheat"),
         BatchEntry(template="kitchen/spice", fields={"name": "Cumin"},
                    tape_mm=12, image_b64=_tiny_png_b64())], reg,
    )
    assert with_extras[0].width > plain[0].width
    assert with_extras[1].width > plain[0].width
    assert with_extras[0].height == plain[0].height


def test_build_batch_images_bad_image_b64_raises():
    reg = default_registry()
    with pytest.raises(BatchSpecError, match="base64"):
        build_batch_images(
            [BatchEntry(template="kitchen/spice", fields={"name": "Paprika"},
                        tape_mm=12, image_b64="not valid base64 !!!")], reg,
        )


def test_cli_batch_icon_default_applies_to_all_labels(tmp_path: Path):
    spec = {
        "template": "kitchen/spice",
        "tape_mm": 12,
        "labels": [{"fields": {"name": "Paprika"}}],
    }
    spec_path = tmp_path / "batch.json"
    spec_path.write_text(json.dumps(spec))
    result = CliRunner().invoke(
        main, ["batch", str(spec_path), "--icon", "lucide:wheat",
               "--bin-out", str(tmp_path / "out.bin")]
    )
    assert result.exit_code == 0, result.output
