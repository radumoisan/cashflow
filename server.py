#!/usr/bin/env python3
"""Local HTTP API for dated cash-flow forecasts and sparse overrides."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import stat
import tempfile
import threading
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request, send_from_directory
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

from engine import ConfigError, evaluate_config, load_config_bytes, replace_input, report_view


API_VERSION = 3
MAX_REQUEST_BYTES = 1_048_576
IF_MATCH_PATTERN = re.compile(r'^"([0-9a-f]{64})"$')
DECIMAL_PATTERN = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$")
CELL_FIELDS = {"row_id", "month", "value", "currency"}


class StaleRevisionError(Exception):
    """Raised when an external editor changes the scenario before replacement."""


def _error(message: str, status: int, code: str) -> tuple[Response, int]:
    return jsonify(error={"code": code, "message": message}), status


def _decimal_from_json(value: Any, path: str) -> Decimal:
    if not isinstance(value, str) or not DECIMAL_PATTERN.fullmatch(value):
        raise ValueError(f"{path} must be a decimal string")
    try:
        decimal = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{path} must be a decimal string") from exc
    if not decimal.is_finite():
        raise ValueError(f"{path} must be finite")
    return decimal


def _require_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    return value


def _require_keys(value: dict[str, Any], required: set[str], path: str) -> None:
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - required)
    if missing:
        raise ValueError(f"{path} is missing required field(s): {', '.join(missing)}")
    if unknown:
        raise ValueError(f"{path} has unknown field(s): {', '.join(unknown)}")


def _round_trip_yaml(data: bytes, path: Path) -> tuple[YAML, Any]:
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.constructor.add_constructor(
        "tag:yaml.org,2002:float",
        lambda loader, node: Decimal(loader.construct_scalar(node).replace("_", "")),
    )
    yaml.representer.add_representer(
        Decimal,
        lambda representer, value: representer.represent_scalar(
            "tag:yaml.org,2002:float", str(value)
        ),
    )
    try:
        document = yaml.load(data.decode("utf-8"))
    except (UnicodeDecodeError, YAMLError) as exc:
        raise ConfigError(f"could not parse {path} for round-trip persistence") from exc
    if not isinstance(document, dict):
        raise ConfigError("cashflow must be a mapping")
    return yaml, document


def _replace_cell(
    data: bytes,
    path: Path,
    *,
    row_id: str,
    month: str,
    value: Decimal | None,
    months: tuple[str, ...],
) -> bytes:
    if month not in months:
        raise ConfigError("cell editing month must be in the requested report window")
    yaml, document = _round_trip_yaml(data, path)
    document = replace_input(document, row_id=row_id, month=month, value=value)
    output = io.StringIO()
    yaml.dump(document, output)
    return output.getvalue().encode("utf-8")


def _state(data: bytes, config_path: Path, start: str | None = None) -> tuple[dict[str, Any], str]:
    config = load_config_bytes(data, config_path)
    report = evaluate_config(config, config_path, start)
    revision = hashlib.sha256(data).hexdigest()
    return (
        {
            "api_version": API_VERSION,
            "revision": revision,
            "report": asdict(report_view(report, config.settings.ron_per_eur)),
        },
        revision,
    )


def _view_etag(state: dict[str, Any]) -> str:
    """A representation ETag includes the view; PATCH uses the YAML revision."""
    return hashlib.sha256(
        (state["revision"] + ":" + state["report"]["months"][0]).encode("ascii")
    ).hexdigest()


def _write_atomic(path: Path, data: bytes, expected_revision: str) -> None:
    temporary_path: Path | None = None
    try:
        original_mode = stat.S_IMODE(path.stat().st_mode)
        with tempfile.NamedTemporaryFile(
            "wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.chmod(temporary_path, original_mode)
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        # Other processes cannot be included in this in-process lock, so a change
        # after this check and before replace remains an unavoidable narrow race.
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_revision:
            raise StaleRevisionError
        os.replace(temporary_path, path)
        temporary_path = None
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        except (AttributeError, OSError):
            return
        try:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def create_app(
    config_path: Path | str | None = None,
    static_dir: Path | str | None = None,
) -> Flask:
    project_root = Path(__file__).resolve().parent
    scenario_path = Path(config_path or project_root / "cashflow.yaml").resolve()
    frontend_path = Path(static_dir or project_root / "frontend").resolve()
    lock = threading.Lock()
    app = Flask(__name__, static_folder=None)
    app.config.update(
        CONFIG_PATH=scenario_path,
        STATIC_DIR=frontend_path,
        MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
    )

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
            "base-uri 'none'; form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.errorhandler(RequestEntityTooLarge)
    def request_too_large(_: RequestEntityTooLarge) -> Response:
        return _error("request body is too large", 413, "request_too_large")

    @app.get("/")
    def index() -> Response:
        return send_from_directory(app.config["STATIC_DIR"], "index.html")

    @app.get("/assets/<path:asset_path>")
    def assets(asset_path: str) -> Response:
        return send_from_directory(app.config["STATIC_DIR"] / "assets", asset_path)

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status=204)

    @app.get("/api/health")
    def health() -> Response:
        return jsonify(status="ok", api_version=API_VERSION)

    def view_start() -> str | None:
        if set(request.args) - {"start"} or len(request.args.getlist("start")) > 1:
            raise ConfigError("only one optional start query parameter is supported")
        return request.args.get("start")

    def current_state() -> tuple[dict[str, Any], str]:
        try:
            data = app.config["CONFIG_PATH"].read_bytes()
        except OSError as exc:
            raise ConfigError(f"could not read {app.config['CONFIG_PATH']}: {exc}") from exc
        return _state(data, app.config["CONFIG_PATH"], view_start())

    @app.get("/api/state")
    def state() -> Response:
        try:
            response_state, _ = current_state()
        except ConfigError as exc:
            return _error(str(exc), 422, "config_validation")
        etag = _view_etag(response_state)
        if request.if_none_match and request.if_none_match.contains(etag):
            response = Response(status=304)
            response.set_etag(etag)
            return response
        response = jsonify(response_state)
        response.set_etag(etag)
        return response

    def json_payload() -> dict[str, Any]:
        if not request.is_json:
            raise ValueError("request body must be JSON")
        try:
            value = request.get_json()
        except BadRequest as exc:
            raise ValueError("request body must contain valid JSON") from exc
        return _require_mapping(value, "request body")

    @app.patch("/api/cell")
    def persist_cell() -> Response:
        if_match = request.headers.get("If-Match", "")
        match = IF_MATCH_PATTERN.fullmatch(if_match)
        if match is None:
            return _error("If-Match must be a quoted SHA-256 revision", 400, "malformed_payload")
        try:
            payload = json_payload()
            _require_keys(payload, CELL_FIELDS, "request body")
            row_id = payload["row_id"]
            month = payload["month"]
            currency = payload["currency"]
            if not isinstance(row_id, str) or not row_id:
                raise ValueError("row_id must be a non-empty string")
            if not isinstance(month, str):
                raise ValueError("month must be a string")
            if currency != "RON":
                raise ValueError("currency must be RON")
            value = None if payload["value"] is None else _decimal_from_json(payload["value"], "value")
        except ValueError as exc:
            return _error(str(exc), 400, "malformed_payload")

        with lock:
            try:
                data = app.config["CONFIG_PATH"].read_bytes()
                start = view_start()
                response_state, revision = _state(data, app.config["CONFIG_PATH"], start)
            except (OSError, ConfigError) as exc:
                return _error(str(exc), 422, "config_validation")
            if match.group(1) != revision:
                return _error("scenario revision is stale", 409, "stale_revision")
            try:
                updated_data = _replace_cell(
                    data,
                    app.config["CONFIG_PATH"],
                    row_id=row_id,
                    month=month,
                    value=value,
                    months=tuple(response_state["report"]["months"]),
                )
                response_state, _ = _state(updated_data, app.config["CONFIG_PATH"], start)
                _write_atomic(app.config["CONFIG_PATH"], updated_data, revision)
            except StaleRevisionError:
                return _error("scenario revision is stale", 409, "stale_revision")
            except (OSError, ConfigError) as exc:
                return _error(str(exc), 422, "config_validation")
        response = jsonify(response_state)
        response.set_etag(_view_etag(response_state))
        return response

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the local cash-flow API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    project_root = Path(__file__).resolve().parent
    parser.add_argument("--config", type=Path, default=project_root / "cashflow.yaml")
    parser.add_argument("--static-dir", type=Path, default=project_root / "frontend")
    arguments = parser.parse_args()
    create_app(arguments.config, arguments.static_dir).run(
        host=arguments.host, port=arguments.port
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
