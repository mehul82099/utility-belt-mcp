# utility-belt-mcp

A zero-dependency, single-file MCP stdio server (protocol version
`2026-07-28`) exposing a belt of genuinely useful, fully local developer
tools - with the complete tools surface done right:

- `tools/list` with JSON Schema **inputSchema AND outputSchema**, titles, and
  behavioral annotations (`readOnlyHint`, `idempotentHint`, ...)
- `tools/call` keeping the spec's two error channels distinct:
  - protocol errors (unknown tool, malformed request) -> JSON-RPC `-32602`
  - execution errors (input validation failures, bad data) ->
    `isError: true` with an actionable, model-correctable message
- `structuredContent` on every successful call, self-checked by the server
  against the tool's `outputSchema` before returning (the spec says servers
  MUST conform), plus the serialized JSON mirrored in a `TextContent` block
  for backwards compatibility (spec SHOULD)

Everything runs locally: no network, no filesystem, no environment access.
No packages, no build step.

## The belt

| Tool | What it does |
| --- | --- |
| `hash_text` | sha256 / sha512 / sha1 / md5 of a UTF-8 string |
| `base64_convert` | base64 encode/decode of UTF-8 text |
| `json_format` | validate + pretty-print JSON, optional key sorting |
| `uuid_generate` | 1-100 random v4 UUIDs |
| `text_stats` | characters, UTF-8 bytes, words, lines |
| `timestamp_convert` | Unix s/ms -> UTC ISO-8601 + weekday |
| `regex_test` | Python regex against text; matches + groups, capped at 50 |
| `url_convert` | percent-encode / decode |

## Usage

```bash
python3 utility_belt_server.py
```

Example session:

```json
→ {"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"hash_text","arguments":{"algorithm":"sha256","text":"abc"}}}
← {"jsonrpc":"2.0","id":1,"result":{"resultType":"complete","isError":false,"content":[{"type":"text","text":"{\"algorithm\": \"sha256\", ...}"}],"structuredContent":{"algorithm":"sha256","hex":"ba7816bf...","input_bytes":3}}}

→ {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"hash_text","arguments":{"algorithm":"crc32","text":"x"}}}
← {"jsonrpc":"2.0","id":2,"result":{"resultType":"complete","isError":true,"content":[{"type":"text","text":"Invalid arguments: $.algorithm: must be one of [\"sha256\", \"sha512\", \"sha1\", \"md5\"]"}]}}

→ {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"ghost_tool","arguments":{}}}
← {"jsonrpc":"2.0","id":3,"error":{"code":-32602,"message":"Unknown tool: ghost_tool"}}
```

## Error semantics (the part most servers get wrong)

The 2026-07-28 spec splits errors into two channels:

- **Protocol errors** - the request itself is broken (unknown tool,
  `arguments` not an object): standard JSON-RPC error `-32602`.
- **Tool execution errors** - the tool ran but the input failed validation
  (wrong type, value out of range, unparseable JSON, invalid regex): a
  normal result with `isError: true`, because "language models can use
  [it] to self-correct and retry with adjusted parameters".

This server validates every call's arguments against the tool's declared
`inputSchema` (a small built-in JSON Schema subset: type/required/properties/
additionalProperties/enum/ranges/lengths) and routes violations to
`isError: true` with paths like `$.algorithm` in the message.

## Interpretation notes (the spec leaves these open)

- The tool set is static, so `capabilities.tools` is `{}` - declaring
  `listChanged` would promise change notifications that never come, and
  `subscriptions/listen` is intentionally not implemented here.
- `tools/list` pagination (`cursor`) is not implemented; the belt is 8 tools.
- The JSON Schema validator is a subset, sized to exactly the schemas this
  server declares - not a general 2020-12 implementation.
- Unexpected internal exceptions inside a tool are reported as
  `isError: true` execution errors rather than crashing the server.
- `x-mcp-header` schema annotations are not used: they exist for the
  Streamable HTTP transport, and this is a stdio server.

## Tests

```bash
python3 -m unittest -v
```

25 stdio integration tests (Python 3.9+, stdlib only) spawn the server as a
subprocess and cover: capability shape, list schema/annotation presence, the
protocol-vs-execution error split across both channels (6 distinct
`isError: true` paths, 3 distinct `-32602` paths), known-answer vectors
(sha256 of "abc", timestamp 1763424000 -> 2025-11-18T00:00:00Z Tuesday),
round trips (base64, url), the structuredContent/text mirror, an
every-tool end-to-end outputSchema conformance pass, and clean shutdown.
CI runs the suite on ubuntu-24.04 and windows-latest against Python 3.9
and 3.13.

## Limits (honest)

- stdio transport only; text structured results only (no image/audio/resource
  content types).
- Tested against the 2026-07-28 spec text with subprocess-level stdio
  integration tests; not smoke-tested against a specific desktop MCP client.
- Python 3.9+. No third-party packages.
- `regex_test` uses Python's `re`: no catastrophic-backtracking guard, and
  matches are capped at 50.

## Why this exists (gap check)

GitHub repository searches for `"structuredContent" mcp server python
zero-dependency` and `mcp server "outputSchema" utilities hash base64`
(api.github.com, November 12, 2026) returned only this author's earlier
unrelated repo and zero results respectively. This repo is a use case for the
2026-07-28 tools surface: a minimal, tested reference showing inputSchema
validation, outputSchema-backed structuredContent, annotations, and the
two-channel error model in one readable file.

## Sources

- Tools:
  https://modelcontextprotocol.io/specification/2026-07-28/server/tools
- MCP specification index: https://modelcontextprotocol.io/llms.txt

## License

MIT (c) 2026 Mehul Jhabak
