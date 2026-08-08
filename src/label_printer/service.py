"""HTTP service for remote printing.

`/render` returns a PNG (or a JSON body with a base64-encoded PNG when the
request sets ``format="base64"`` — useful for Home Assistant `rest_command`,
which can only capture text/JSON responses). `/print` returns the raster
command bytes by default (dry-run) or drives the configured network transport
when ``send=true``. The printer host is resolved the same way the CLI resolves
it: the ``LABEL_PRINTER_HOST`` environment variable, then the value persisted
by ``lp printer set <ip>``.
"""

from __future__ import annotations

import base64
import io
import json
import os
from typing import Any, Literal

try:
    from fastapi import FastAPI, Header, HTTPException
    from fastapi.responses import FileResponse, Response
    from pydantic import BaseModel
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "Install service extras: pip install -e '.[service]'"
    ) from e

from label_printer import RasterOptions, encode_batch, encode_job
from label_printer import state as state_mod
from label_printer.engine.batch import (
    BatchEntry,
    BatchSpecError,
    build_batch_images,
    stack_preview,
)
from label_printer.engine.compose import compose_extras, strip_template_handled
from label_printer.engine.icons import IconNotFoundError
from label_printer.engine.icons import registry as _icon_registry
from label_printer.status import (
    StatusQueryError,
    TapeMismatchError,
    check_tape_or_warn,
)
from label_printer.tape import TapeWidth
from label_printer.templates import default_registry
from label_printer.transport.base import StatusUnavailable
from label_printer.transport.network import NetworkTransport

app = FastAPI(title="label-printer", version="0.1.0")
_REGISTRY = default_registry()
_TOKEN_ENV = "LABEL_PRINTER_TOKEN"


def _require_token(authorization: str | None) -> None:
    expected = os.environ.get(_TOKEN_ENV)
    if not expected:
        return  # auth disabled if no token set (local dev)
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    if authorization.split(" ", 1)[1] != expected:
        raise HTTPException(403, "bad token")


class RenderRequest(BaseModel):
    template: str
    tape_mm: int = 12
    fields: dict[str, Any] = {}
    # Optional post-render extras composed onto the right edge of any label.
    link: str | None = None
    image: str | None = None
    # Response format: binary PNG (default) or JSON with a base64-encoded PNG.
    format: Literal["png", "base64"] = "png"


def _render_body_with_extras(template, fields: dict, tape: TapeWidth,
                             link: str | None, image: str | None):
    extras = {k: v for k, v in {"link": link, "image": image}.items() if v}
    extras = strip_template_handled(extras, template)
    body = template.render(template.validate(fields), tape)
    return compose_extras(body, extras, tape)


class PrintRequest(RenderRequest):
    # Dry-run by default — opt in explicitly to drive the hardware transport.
    send: bool = False


def _resolve_printer_host() -> str:
    """Pick a printer host: LABEL_PRINTER_HOST env → saved state.

    Raises HTTPException(503) if neither is set — the service can't reach any
    printer without one.
    """
    resolved = state_mod.resolve_printer_host()
    if resolved:
        return resolved
    raise HTTPException(
        503,
        "no printer host configured. Set LABEL_PRINTER_HOST or run "
        "`lp printer set <ip>` on the service host.",
    )


def _verify_tape(transport: NetworkTransport, tape: TapeWidth) -> str | None:
    """Check the loaded tape matches the job. Returns a warning string if the
    check was skipped (SNMP unavailable), None on success. Raises
    HTTPException(409) on a real mismatch or printer error, 502 if the status
    query itself fails.
    """
    try:
        return check_tape_or_warn(transport, tape)
    except StatusQueryError as e:
        raise HTTPException(502, f"could not query printer status: {e}") from e
    except TapeMismatchError as e:
        raise HTTPException(409, str(e)) from e


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "printer_configured": bool(state_mod.resolve_printer_host())}


@app.get("/templates")
def templates(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    _require_token(authorization)
    return [
        {
            "qualified": t.meta.qualified,
            "summary": t.meta.summary,
            "default_tape_mm": int(t.meta.default_tape),
            "fields": [
                {
                    "name": f.name,
                    "description": f.description,
                    "required": f.required and f.default is None,
                    "default": f.default,
                    "example": f.example,
                }
                for f in t.meta.fields
            ],
        }
        for t in _REGISTRY
    ]


@app.get("/status")
def printer_status(authorization: str | None = Header(default=None)) -> Response:
    """Report the physically loaded tape width and any printer error state.

    Uses the same SNMP status path as the /print pre-check. Returns
    ``ok: false`` with a warning when the printer can't report status (e.g.
    SNMP disabled) so callers can fall back gracefully instead of failing.
    ``tape_mm`` is the real-world width — the printer reports 3.5mm tape as
    the protocol sentinel 4, which we map back here.
    """
    _require_token(authorization)
    host = _resolve_printer_host()
    transport = NetworkTransport(host)
    try:
        status = transport.query_status()
    except StatusUnavailable as e:
        return Response(
            json.dumps({"ok": False, "host": host, "warning": str(e)}),
            media_type="application/json",
        )
    except Exception as e:
        raise HTTPException(502, f"could not query printer status: {e}") from e
    return Response(
        json.dumps({
            "ok": True,
            "host": host,
            "has_media": status.has_media,
            "tape_mm": 3.5 if status.media_width_mm == 4 else status.media_width_mm,
            "errors": status.describe_errors(),
        }),
        media_type="application/json",
    )


@app.get("/icons/{name}.svg")
def icon_svg(name: str, authorization: str | None = Header(default=None)) -> Response:
    """Serve a bundled icon SVG by name (e.g. ``/icons/wifi.svg``).

    Raw file serve — no rasterization, so this works without the optional
    cairosvg dependency. Browsers render the SVG directly; the label engine
    rasterizes its own copy at print time.
    """
    _require_token(authorization)
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, "invalid icon name")
    try:
        path = _icon_registry().find(name)
    except IconNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    return FileResponse(
        path,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.post("/render")
def render(req: RenderRequest, authorization: str | None = Header(default=None)) -> Response:
    _require_token(authorization)
    try:
        template = _REGISTRY.get(req.template)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    tape = TapeWidth(4 if req.tape_mm in (3, 4) else req.tape_mm)
    image = _render_body_with_extras(template, req.fields, tape, req.link, req.image)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    png = buf.getvalue()
    if req.format == "base64":
        return Response(
            json.dumps({
                "png_b64": base64.b64encode(png).decode("ascii"),
                "bytes": len(png),
                "tape_mm": int(tape),
                "template": template.meta.qualified,
            }),
            media_type="application/json",
        )
    return Response(png, media_type="image/png")


@app.post("/print")
def print_(req: PrintRequest, authorization: str | None = Header(default=None)) -> Response:
    """Encode a label. Dry-run by default; set ``send=true`` to drive the printer."""
    _require_token(authorization)
    try:
        template = _REGISTRY.get(req.template)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    tape = TapeWidth(4 if req.tape_mm in (3, 4) else req.tape_mm)
    image = _render_body_with_extras(template, req.fields, tape, req.link, req.image)
    data = encode_job(image, tape)

    if not req.send:
        return Response(
            data,
            media_type="application/octet-stream",
            headers={"X-Dry-Run": "true", "X-Bytes": str(len(data))},
        )

    host = _resolve_printer_host()
    transport = NetworkTransport(host)
    warning = _verify_tape(transport, tape)
    try:
        transport.send(data)
    except OSError as e:
        raise HTTPException(502, f"could not reach printer at {host}: {e}") from e

    body: dict[str, Any] = {
        "sent": True,
        "host": host,
        "bytes": len(data),
    }
    if warning:
        body["warning"] = warning
    return Response(
        json.dumps(body),
        media_type="application/json",
        headers={"X-Dry-Run": "false", "X-Bytes": str(len(data))},
    )


# --- Batch endpoints ---------------------------------------------------------


class BatchLabel(BaseModel):
    fields: dict[str, Any] = {}
    template: str | None = None  # overrides the request-level template
    link: str | None = None
    image: str | None = None
    copies: int = 1


class BatchRequest(BaseModel):
    """Same-template batch: one template, N field-set variations.

    Mirrors the CLI's spec-v2 object form. Per-label ``template`` overrides
    allow mixed templates as long as the tape width stays uniform.
    """

    template: str
    tape_mm: int | None = None
    labels: list[BatchLabel]
    link: str | None = None
    image: str | None = None
    half_cut: bool = True
    gap_dots: int = 0
    cut_every: int | None = None
    format: Literal["png", "base64"] = "png"  # /render/batch only


class BatchPrintRequest(BatchRequest):
    # Dry-run by default — opt in explicitly to drive the hardware transport.
    send: bool = False


def _build_batch(req: BatchRequest):
    """Validate the request and render all labels. Returns (images, tape)."""
    if not req.labels:
        raise HTTPException(400, "batch must contain at least one label")
    entries = [
        BatchEntry(
            template=label.template or req.template,
            fields=label.fields,
            tape_mm=req.tape_mm,
            link=label.link or req.link,
            image=label.image or req.image,
            copies=label.copies,
        )
        for label in req.labels
    ]
    try:
        return build_batch_images(entries, _REGISTRY)
    except BatchSpecError as e:
        msg = str(e)
        if "No such template" in msg:
            raise HTTPException(404, msg) from e
        raise HTTPException(400, msg) from e


@app.post("/render/batch")
def render_batch(req: BatchRequest, authorization: str | None = Header(default=None)) -> Response:
    """Render a whole batch as a single vertically stacked strip preview PNG."""
    _require_token(authorization)
    images, tape = _build_batch(req)
    buf = io.BytesIO()
    stack_preview(images).save(buf, format="PNG")
    png = buf.getvalue()
    if req.format == "base64":
        return Response(
            json.dumps({
                "png_b64": base64.b64encode(png).decode("ascii"),
                "bytes": len(png),
                "labels": len(images),
                "tape_mm": int(tape),
            }),
            media_type="application/json",
        )
    return Response(png, media_type="image/png")


@app.post("/batch")
def print_batch(req: BatchPrintRequest, authorization: str | None = Header(default=None)) -> Response:
    """Encode a chained multi-label job. Dry-run unless ``send=true``."""
    _require_token(authorization)
    images, tape = _build_batch(req)
    options = RasterOptions(
        half_cut=req.half_cut, gap_dots=req.gap_dots, cut_every=req.cut_every,
    )
    data = encode_batch(images, tape, options)

    if not req.send:
        return Response(
            data,
            media_type="application/octet-stream",
            headers={"X-Dry-Run": "true", "X-Bytes": str(len(data))},
        )

    host = _resolve_printer_host()
    transport = NetworkTransport(host)
    warning = _verify_tape(transport, tape)
    try:
        transport.send(data)
    except OSError as e:
        raise HTTPException(502, f"could not reach printer at {host}: {e}") from e

    body: dict[str, Any] = {
        "sent": True,
        "host": host,
        "bytes": len(data),
        "labels": len(images),
    }
    if warning:
        body["warning"] = warning
    return Response(
        json.dumps(body),
        media_type="application/json",
        headers={"X-Dry-Run": "false", "X-Bytes": str(len(data))},
    )
