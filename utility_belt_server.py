#!/usr/bin/env python3
"""utility-belt-mcp: MCP stdio server (protocol version 2026-07-28) exposing a
belt of genuinely useful, fully local developer tools with the complete tools
surface done right:

  - tools/list with JSON Schema inputSchema AND outputSchema, titles and
    behavioral annotations (readOnlyHint / idempotentHint / ...)
  - tools/call with the spec's two error channels kept distinct:
      * protocol errors (unknown tool, malformed request)  -> JSON-RPC -32602
      * execution errors (input validation, bad data)      -> isError: true
  - structuredContent on every successful call, validated by the server
    against the tool's outputSchema before returning, plus the serialized
    JSON in a TextContent block for backwards compatibility (spec SHOULD)

Zero dependencies, single file, Python 3.9+. All tools are pure local
computation: no network, no filesystem, no environment access.

Tools: hash_text, base64_convert, json_format, uuid_generate, text_stats,
timestamp_convert, regex_test, url_convert.
"""

import base64
import hashlib
import json
import re
import sys
import urllib.parse
import uuid
from datetime import datetime, timezone

LATEST_PROTOCOL_VERSION = "2026-07-28"
PROTOCOL_VERSIONS = ["2026-07-28", "2026-05-12", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]


def _emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _error(rid, code, message):
    _emit({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def _result(rid, result):
    _emit({"jsonrpc": "2.0", "id": rid, "result": result})


# ---------------------------------------------------------------------------
# Minimal JSON Schema validator (subset: type, required, properties,
# additionalProperties:false, enum, minimum/maximum, minLength/maxLength,
# minItems/maxItems). Used for inputSchema argument validation (reported as
# isError tool results, per spec) and for the server's own outputSchema
# conformance self-check.

def validate(value, schema, path="$"):
    errors = []
    t = schema.get("type")
    if t == "object":
        if not isinstance(value, dict):
            return ["%s: expected object" % path]
        for req in schema.get("required", []):
            if req not in value:
                errors.append("%s: missing required property '%s'" % (path, req))
        props = schema.get("properties", {})
        for k, v in value.items():
            if k in props:
                errors += validate(v, props[k], "%s.%s" % (path, k))
            elif schema.get("additionalProperties") is False:
                errors.append("%s: unexpected property '%s'" % (path, k))
    elif t == "string":
        if not isinstance(value, str):
            errors.append("%s: expected string" % path)
        else:
            if "minLength" in schema and len(value) < schema["minLength"]:
                errors.append("%s: shorter than minLength %d" % (path, schema["minLength"]))
            if "maxLength" in schema and len(value) > schema["maxLength"]:
                errors.append("%s: longer than maxLength %d" % (path, schema["maxLength"]))
    elif t == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append("%s: expected integer" % path)
        else:
            if "minimum" in schema and value < schema["minimum"]:
                errors.append("%s: below minimum %s" % (path, schema["minimum"]))
            if "maximum" in schema and value > schema["maximum"]:
                errors.append("%s: above maximum %s" % (path, schema["maximum"]))
    elif t == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            errors.append("%s: expected number" % path)
    elif t == "boolean":
        if not isinstance(value, bool):
            errors.append("%s: expected boolean" % path)
    elif t == "array":
        if not isinstance(value, list):
            errors.append("%s: expected array" % path)
        else:
            if "minItems" in schema and len(value) < schema["minItems"]:
                errors.append("%s: fewer than minItems %d" % (path, schema["minItems"]))
            if "maxItems" in schema and len(value) > schema["maxItems"]:
                errors.append("%s: more than maxItems %d" % (path, schema["maxItems"]))
    if "enum" in schema and not errors and value not in schema["enum"]:
        errors.append("%s: must be one of %s" % (path, json.dumps(schema["enum"])))
    return errors


# ---------------------------------------------------------------------------
# Tool implementations. Each returns a JSON-serializable structured result;
# failures raise ToolError with an actionable, model-correctable message.

class ToolError(Exception):
    pass


READ_ONLY = {"readOnlyHint": True, "destructiveHint": False,
             "idempotentHint": True, "openWorldHint": False}


def _tool(name, title, description, input_schema, output_schema, fn, annotations=None):
    return {"name": name, "title": title, "description": description,
            "inputSchema": input_schema, "outputSchema": output_schema,
            "annotations": annotations or dict(READ_ONLY), "fn": fn}


def t_hash_text(args):
    algo = args["algorithm"]
    try:
        h = hashlib.new(algo)
    except ValueError:
        raise ToolError("Unsupported algorithm: %s" % algo)
    h.update(args["text"].encode("utf-8"))
    return {"algorithm": algo, "hex": h.hexdigest(), "input_bytes": len(args["text"].encode("utf-8"))}


def t_base64_convert(args):
    if args["mode"] == "encode":
        out = base64.b64encode(args["text"].encode("utf-8")).decode("ascii")
    else:
        try:
            out = base64.b64decode(args["text"], validate=True).decode("utf-8")
        except Exception:
            raise ToolError("Input is not valid base64 carrying UTF-8 text")
    return {"mode": args["mode"], "result": out}


def t_json_format(args):
    try:
        parsed = json.loads(args["json"])
    except json.JSONDecodeError as e:
        raise ToolError("Invalid JSON at line %d column %d: %s" % (e.lineno, e.colno, e.msg))
    formatted = json.dumps(parsed, indent=args.get("indent", 2),
                           sort_keys=args.get("sort_keys", False), ensure_ascii=False)
    return {"formatted": formatted, "top_level_type": type(parsed).__name__}


def t_uuid_generate(args):
    count = args.get("count", 1)
    return {"version": 4, "uuids": [str(uuid.uuid4()) for _ in range(count)]}


def t_text_stats(args):
    text = args["text"]
    return {"characters": len(text),
            "bytes_utf8": len(text.encode("utf-8")),
            "words": len(text.split()),
            "lines": len(text.split("\n"))}


def t_timestamp_convert(args):
    seconds = args["timestamp"] / (1000 if args["unit"] == "ms" else 1)
    try:
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ToolError("Timestamp out of representable range")
    return {"iso8601_utc": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "unix_seconds": int(seconds),
            "weekday_utc": dt.strftime("%A")}


def t_regex_test(args):
    try:
        rx = re.compile(args["pattern"])
    except re.error as e:
        raise ToolError("Invalid regular expression: %s" % e)
    matches = rx.findall(args["text"])
    flat = [m if isinstance(m, str) else list(m) for m in matches]
    return {"matched": len(matches) > 0, "count": len(matches), "matches": flat[:50]}


def t_url_convert(args):
    if args["mode"] == "encode":
        out = urllib.parse.quote(args["text"], safe="")
    else:
        out = urllib.parse.unquote(args["text"])
    return {"mode": args["mode"], "result": out}


def _obj(props, required=None, additional=False):
    s = {"type": "object", "properties": props}
    if required:
        s["required"] = required
    if not additional:
        s["additionalProperties"] = False
    return s


_STR = {"type": "string"}

TOOLS = [
    _tool("hash_text", "Hash Text",
          "Compute a cryptographic hash of a UTF-8 string.",
          _obj({"algorithm": {**_STR, "enum": ["sha256", "sha512", "sha1", "md5"]}, "text": _STR},
               ["algorithm", "text"]),
          _obj({"algorithm": _STR, "hex": _STR, "input_bytes": {"type": "integer"}},
               ["algorithm", "hex", "input_bytes"]),
          t_hash_text),
    _tool("base64_convert", "Base64 Convert",
          "Encode UTF-8 text to base64 or decode base64 back to UTF-8 text.",
          _obj({"mode": {**_STR, "enum": ["encode", "decode"]}, "text": _STR}, ["mode", "text"]),
          _obj({"mode": _STR, "result": _STR}, ["mode", "result"]),
          t_base64_convert),
    _tool("json_format", "JSON Format",
          "Validate and pretty-print a JSON document with optional key sorting.",
          _obj({"json": _STR,
                "indent": {"type": "integer", "minimum": 0, "maximum": 8},
                "sort_keys": {"type": "boolean"}},
               ["json"]),
          _obj({"formatted": _STR, "top_level_type": _STR}, ["formatted", "top_level_type"]),
          t_json_format),
    _tool("uuid_generate", "UUID Generate",
          "Generate one or more random version-4 UUIDs.",
          _obj({"count": {"type": "integer", "minimum": 1, "maximum": 100}}, []),
          _obj({"version": {"type": "integer"},
                "uuids": {"type": "array", "items": _STR, "minItems": 1, "maxItems": 100}},
               ["version", "uuids"]),
          t_uuid_generate),
    _tool("text_stats", "Text Stats",
          "Count characters, UTF-8 bytes, words, and lines of a text.",
          _obj({"text": _STR}, ["text"]),
          _obj({"characters": {"type": "integer"}, "bytes_utf8": {"type": "integer"},
                "words": {"type": "integer"}, "lines": {"type": "integer"}},
               ["characters", "bytes_utf8", "words", "lines"]),
          t_text_stats),
    _tool("timestamp_convert", "Timestamp Convert",
          "Convert a Unix timestamp (seconds or milliseconds) to UTC ISO-8601 and weekday.",
          _obj({"timestamp": {"type": "integer"},
                "unit": {**_STR, "enum": ["s", "ms"]}},
               ["timestamp", "unit"]),
          _obj({"iso8601_utc": _STR, "unix_seconds": {"type": "integer"}, "weekday_utc": _STR},
               ["iso8601_utc", "unix_seconds", "weekday_utc"]),
          t_timestamp_convert),
    _tool("regex_test", "Regex Test",
          "Test a Python regular expression against a text; returns matches and groups (capped at 50).",
          _obj({"pattern": _STR, "text": _STR}, ["pattern", "text"]),
          _obj({"matched": {"type": "boolean"}, "count": {"type": "integer"},
                "matches": {"type": "array"}},
               ["matched", "count", "matches"]),
          t_regex_test),
    _tool("url_convert", "URL Convert",
          "Percent-encode text for use in URLs, or decode a percent-encoded string.",
          _obj({"mode": {**_STR, "enum": ["encode", "decode"]}, "text": _STR}, ["mode", "text"]),
          _obj({"mode": _STR, "result": _STR}, ["mode", "result"]),
          t_url_convert),
]
TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


# ---------------------------------------------------------------------------
# tools/list and tools/call

def handle_tools_list(rid):
    _result(rid, {"resultType": "complete", "tools": [
        {k: t[k] for k in ("name", "title", "description", "inputSchema", "outputSchema", "annotations")}
        for t in TOOLS
    ]})


def _tool_error_result(rid, message):
    _result(rid, {"resultType": "complete", "isError": True,
                  "content": [{"type": "text", "text": message}]})


def handle_tools_call(rid, params):
    if not isinstance(params, dict):
        _error(rid, -32602, "Invalid params: tools/call requires a params object")
        return
    name = params.get("name")
    if not isinstance(name, str):
        _error(rid, -32602, "Invalid params: name must be a string")
        return
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        _error(rid, -32602, "Invalid params: arguments must be an object")
        return
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        _error(rid, -32602, "Unknown tool: %s" % name)
        return

    # Input validation failures are execution errors (isError: true), not
    # protocol errors: the model can fix them and retry (spec, Error Handling).
    problems = validate(arguments, tool["inputSchema"])
    if problems:
        _tool_error_result(rid, "Invalid arguments: " + "; ".join(problems))
        return
    try:
        structured = tool["fn"](arguments)
    except ToolError as e:
        _tool_error_result(rid, str(e))
        return
    except Exception as e:  # defensive: unexpected bug is still a tool error
        _tool_error_result(rid, "Tool execution failed: %s" % e)
        return

    # Server MUST return structured results conforming to outputSchema:
    # self-check before returning rather than shipping a violating result.
    out_problems = validate(structured, tool["outputSchema"])
    if out_problems:
        _tool_error_result(rid, "Internal error: tool result failed outputSchema validation: "
                           + "; ".join(out_problems))
        return
    _result(rid, {
        "resultType": "complete",
        "isError": False,
        # Spec SHOULD: mirror the structured result as serialized JSON text
        # for backwards compatibility with text-only clients.
        "content": [{"type": "text", "text": json.dumps(structured, ensure_ascii=False)}],
        "structuredContent": structured,
    })


# ---------------------------------------------------------------------------
# Dispatch

def handle_request(req):
    rid = req.get("id")
    method = req.get("method")
    params = req.get("params")

    if method == "initialize":
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        version = requested if requested in PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        _result(rid, {
            "resultType": "complete",
            "protocolVersion": version,
            # Static tool set: no listChanged is declared (it would be a lie -
            # the list never changes at runtime). subscriptions/listen is
            # therefore not implemented by this server.
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "utility-belt-mcp", "version": "1.0.0"},
        })
    elif method == "ping":
        _result(rid, {"resultType": "complete"})
    elif method == "tools/list":
        handle_tools_list(rid)
    elif method == "tools/call":
        handle_tools_call(rid, params)
    elif method == "notifications/initialized":
        pass
    elif rid is None:
        pass
    else:
        _error(rid, -32601, "Method not found: %s" % method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _error(None, -32700, "Parse error")
            continue
        if not isinstance(req, dict) or "method" not in req:
            _error(req.get("id") if isinstance(req, dict) else None, -32600, "Invalid Request")
            continue
        handle_request(req)


if __name__ == "__main__":
    main()
