#!/usr/bin/env python3
"""Validate and render a Flint ChartAssemblyInput through flint-chart-mcp."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import unquote, urlparse


TOTAL_LABELS = {"all", "total", "subtotal", "全部", "总计", "合计", "小计"}


class ChartError(ValueError):
    pass


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ChartError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ChartError(f"invalid JSON in {path}: {exc}") from exc


def _resolve_data_path(raw: str, base_dir: Path) -> Path:
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        raise ChartError("Flint cannot read remote data.url values")
    if parsed.scheme == "file":
        return Path(unquote(parsed.path)).expanduser().resolve()
    if parsed.scheme:
        raise ChartError(f"unsupported data.url scheme: {parsed.scheme}")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _load_rows(data: dict, base_dir: Path) -> tuple[list[dict], Path | None]:
    has_values = "values" in data
    has_url = "url" in data
    if has_values == has_url:
        raise ChartError("data must contain exactly one of values or url")

    if has_values:
        rows = data["values"]
        data_path = None
    else:
        data_path = _resolve_data_path(data["url"], base_dir)
        suffix = data_path.suffix.lower()
        if suffix == ".json":
            rows = _load_json(data_path)
            if isinstance(rows, dict) and isinstance(rows.get("values"), list):
                rows = rows["values"]
        elif suffix in {".csv", ".tsv"}:
            delimiter = "\t" if suffix == ".tsv" else ","
            try:
                with data_path.open("r", encoding="utf-8-sig", newline="") as fh:
                    rows = list(csv.DictReader(fh, delimiter=delimiter))
            except FileNotFoundError as exc:
                raise ChartError(f"file not found: {data_path}") from exc
        else:
            raise ChartError("data.url must point to a local JSON, CSV, or TSV file")

    if not isinstance(rows, list) or not rows:
        raise ChartError("chart data must be a non-empty array of rows")
    if not all(isinstance(row, dict) for row in rows):
        raise ChartError("every chart data row must be an object")
    return rows, data_path


def _encoding_fields(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        fields = []
        for item in value:
            fields.extend(_encoding_fields(item))
        return fields
    if isinstance(value, dict) and isinstance(value.get("field"), str):
        return [value["field"]]
    return []


def validate(payload: dict, base_dir: Path, require_provenance: bool) -> tuple[list[str], Path | None]:
    if not isinstance(payload, dict):
        raise ChartError("chart input must be a JSON object")

    data = payload.get("data")
    chart_spec = payload.get("chart_spec")
    semantics = payload.get("semantic_types")
    if not isinstance(data, dict):
        raise ChartError("missing object: data")
    if not isinstance(chart_spec, dict):
        raise ChartError("missing object: chart_spec")
    if not isinstance(semantics, dict):
        raise ChartError("missing object: semantic_types")
    if not isinstance(chart_spec.get("chartType"), str):
        raise ChartError("chart_spec.chartType must be a string")
    encodings = chart_spec.get("encodings")
    if not isinstance(encodings, dict) or not encodings:
        raise ChartError("chart_spec.encodings must be a non-empty object")

    rows, data_path = _load_rows(data, base_dir)
    encoded_fields = []
    for encoding in encodings.values():
        encoded_fields.extend(_encoding_fields(encoding))
    encoded_fields = list(dict.fromkeys(encoded_fields))
    if not encoded_fields:
        raise ChartError("encodings do not reference any fields")

    available = set().union(*(row.keys() for row in rows))
    missing_fields = [field for field in encoded_fields if field not in available]
    if missing_fields:
        raise ChartError(f"encoded fields missing from data: {', '.join(missing_fields)}")
    missing_semantics = [field for field in encoded_fields if field not in semantics]
    if missing_semantics:
        raise ChartError(
            "semantic_types missing encoded fields: " + ", ".join(missing_semantics)
        )

    warnings = []
    incomplete = [
        field for field in encoded_fields if any(field not in row for row in rows)
    ]
    if incomplete:
        warnings.append("some rows omit encoded fields: " + ", ".join(incomplete))
    if len(rows) < 3:
        warnings.append("fewer than 3 rows; confirm that a chart adds value")
    if not chart_spec.get("title"):
        warnings.append("chart_spec.title is missing; use a finding as the headline")

    if require_provenance:
        missing_source = [i + 1 for i, row in enumerate(rows) if not row.get("source_url")]
        if missing_source:
            preview = ", ".join(str(i) for i in missing_source[:8])
            raise ChartError(f"rows missing source_url: {preview}")

    for field in encoded_fields:
        labels = {
            str(row[field]).strip().casefold()
            for row in rows
            if field in row and row[field] is not None
        }
        if labels & TOTAL_LABELS and len(labels) > 1:
            warnings.append(
                f"field {field!r} mixes a total/subtotal label with component rows"
            )

    return warnings, data_path


def _render(payload: dict, base_dir: Path, backend: str, fmt: str) -> tuple[bytes, str]:
    npx = shutil.which("npx")
    if not npx:
        raise ChartError("npx is required to run flint-chart-mcp")

    request_payload = json.loads(json.dumps(payload, ensure_ascii=False))
    data = request_payload["data"]
    if "url" in data:
        data["url"] = str(_resolve_data_path(data["url"], base_dir))
    arguments = {**request_payload, "backend": backend, "format": fmt}

    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "sf-reader-all-chart", "version": "0.1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "render_chart", "arguments": arguments},
        },
    ]
    wire = "\n".join(json.dumps(message, ensure_ascii=False) for message in messages) + "\n"
    package = os.getenv("FLINT_CHART_MCP_PACKAGE", "flint-chart-mcp@0.5.0")
    try:
        completed = subprocess.run(
            [npx, "-y", package],
            input=wire,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=base_dir,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ChartError("Flint rendering timed out after 120 seconds") from exc

    responses = []
    for line in completed.stdout.splitlines():
        try:
            responses.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    response = next((item for item in responses if item.get("id") == 2), None)
    if response is None:
        detail = completed.stderr.strip().splitlines()[-1:] or ["no MCP response"]
        raise ChartError(f"Flint did not return a render result: {detail[0]}")
    if "error" in response:
        raise ChartError(f"Flint MCP error: {response['error']}")

    result = response.get("result", {})
    content = result.get("content", [])
    if result.get("isError"):
        text_blocks = [item.get("text", "") for item in content if item.get("type") == "text"]
        raise ChartError("Flint rejected the chart: " + " ".join(text_blocks))

    if fmt == "svg":
        svg = next(
            (
                item.get("text", "")
                for item in content
                if item.get("type") == "text" and item.get("text", "").lstrip().startswith("<svg")
            ),
            None,
        )
        if svg is None:
            raise ChartError("Flint response did not contain SVG data")
        return svg.encode("utf-8"), "image/svg+xml"

    image = next((item for item in content if item.get("type") == "image"), None)
    if image is None or not image.get("data"):
        raise ChartError("Flint response did not contain PNG data")
    try:
        return base64.b64decode(image["data"], validate=True), image.get("mimeType", "image/png")
    except ValueError as exc:
        raise ChartError("Flint returned invalid base64 image data") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="ChartAssemblyInput JSON file")
    parser.add_argument("--output", type=Path, help="Output .svg or .png file")
    parser.add_argument("--backend", choices=("vegalite", "echarts", "chartjs"), default="vegalite")
    parser.add_argument("--format", choices=("svg", "png"))
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--require-provenance", action="store_true")
    args = parser.parse_args()

    input_path = args.input.expanduser().resolve()
    try:
        payload = _load_json(input_path)
        warnings, _ = validate(payload, input_path.parent, args.require_provenance)
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        if args.validate_only:
            print(f"valid: {input_path}")
            return 0
        if args.output is None:
            raise ChartError("--output is required unless --validate-only is used")

        output = args.output.expanduser().resolve()
        fmt = args.format or output.suffix.lower().lstrip(".")
        if fmt not in {"svg", "png"}:
            raise ChartError("output suffix must be .svg or .png, or pass --format")
        if args.backend == "chartjs" and fmt == "svg":
            raise ChartError("the chartjs backend supports PNG only")

        artifact, mime_type = _render(payload, input_path.parent, args.backend, fmt)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(artifact)
        print(f"rendered: {output} ({mime_type}, {len(artifact)} bytes)")
        return 0
    except ChartError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
