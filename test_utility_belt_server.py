#!/usr/bin/env python3
"""Stdio integration tests for utility-belt-mcp (2026-07-28 tools surface).

Spawns the server per test and talks JSON-RPC over pipes. Reader thread +
queue, Windows-safe.
"""

import base64
import json
import os
import queue
import subprocess
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "utility_belt_server.py")


class Session:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=HERE,
        )
        self.q = queue.Queue()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.q.put(json.loads(line))
            except ValueError:
                self.q.put({"_unparsable": line})
        self.q.put(None)

    def send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def recv(self, timeout=5.0):
        return self.q.get(timeout=timeout)

    def initialize(self):
        self.send({"jsonrpc": "2.0", "id": "init", "method": "initialize",
                   "params": {"protocolVersion": "2026-07-28"}})
        return self.recv()

    def call(self, rid, name, arguments=None):
        msg = {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
               "params": {"name": name}}
        if arguments is not None:
            msg["params"]["arguments"] = arguments
        self.send(msg)
        return self.recv()

    def close(self, expect_exit=True):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        err = self.proc.stderr.read()
        if expect_exit and (self.proc.returncode != 0 or err):
            raise AssertionError("server exit=%s stderr=%r" % (self.proc.returncode, err))


class UtilityBeltTests(unittest.TestCase):
    def setUp(self):
        self.session = Session()

    def tearDown(self):
        self.session.close(expect_exit=False)

    # -- handshake / listing ------------------------------------------------

    def test_initialize_declares_tools_capability(self):
        r = self.session.initialize()
        self.assertEqual(r["result"]["protocolVersion"], "2026-07-28")
        self.assertEqual(r["result"]["capabilities"], {"tools": {}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "utility-belt-mcp")

    def test_ping(self):
        self.session.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(self.session.recv()["result"], {"resultType": "complete"})

    def test_tools_list_shape(self):
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = self.session.recv()["result"]["tools"]
        names = {t["name"] for t in tools}
        self.assertEqual(names, {"hash_text", "base64_convert", "json_format",
                                 "uuid_generate", "text_stats", "timestamp_convert",
                                 "regex_test", "url_convert"})
        for t in tools:
            self.assertTrue(t["title"])
            self.assertTrue(t["description"])
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertEqual(t["outputSchema"]["type"], "object")
            self.assertTrue(t["annotations"]["readOnlyHint"])
            self.assertFalse(t["annotations"]["openWorldHint"])

    def test_unknown_method_and_parse_error(self):
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 3, "method": "bogus/method"})
        self.assertEqual(self.session.recv()["error"]["code"], -32601)
        self.session.proc.stdin.write("{broken\n")
        self.session.proc.stdin.flush()
        self.assertEqual(self.session.recv()["error"]["code"], -32700)

    # -- protocol errors (-32602) -------------------------------------------

    def test_unknown_tool_is_protocol_error(self):
        self.session.initialize()
        r = self.session.call(4, "not_a_tool", {})
        self.assertEqual(r["error"]["code"], -32602)
        self.assertIn("Unknown tool", r["error"]["message"])

    def test_malformed_call_is_protocol_error(self):
        self.session.initialize()
        self.session.send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                           "params": {"arguments": {}}})
        self.assertEqual(self.session.recv()["error"]["code"], -32602)
        self.session.send({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                           "params": {"name": "hash_text", "arguments": "nope"}})
        self.assertEqual(self.session.recv()["error"]["code"], -32602)

    # -- execution errors (isError: true) ------------------------------------

    def test_schema_violation_is_tool_error(self):
        self.session.initialize()
        r = self.session.call(7, "hash_text", {"algorithm": "sha256", "text": 5})
        res = r["result"]
        self.assertTrue(res["isError"])
        self.assertIn("$.text", res["content"][0]["text"])
        self.assertNotIn("structuredContent", res)

    def test_enum_violation_lists_choices(self):
        self.session.initialize()
        r = self.session.call(8, "hash_text", {"algorithm": "crc32", "text": "x"})
        text = r["result"]["content"][0]["text"]
        self.assertTrue(r["result"]["isError"])
        self.assertIn("sha256", text)

    def test_missing_required_is_tool_error(self):
        self.session.initialize()
        r = self.session.call(9, "base64_convert", {"mode": "encode"})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("text", r["result"]["content"][0]["text"])

    def test_unexpected_property_is_tool_error(self):
        self.session.initialize()
        r = self.session.call(10, "text_stats", {"text": "hi", "evil": True})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("evil", r["result"]["content"][0]["text"])

    def test_range_violation_is_tool_error(self):
        self.session.initialize()
        r = self.session.call(11, "uuid_generate", {"count": 500})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("maximum", r["result"]["content"][0]["text"])

    def test_invalid_json_input_is_tool_error(self):
        self.session.initialize()
        r = self.session.call(12, "json_format", {"json": "{nope"})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("line", r["result"]["content"][0]["text"])

    def test_invalid_regex_is_tool_error(self):
        self.session.initialize()
        r = self.session.call(13, "regex_test", {"pattern": "([", "text": "x"})
        self.assertTrue(r["result"]["isError"])

    def test_invalid_base64_is_tool_error(self):
        self.session.initialize()
        r = self.session.call(14, "base64_convert", {"mode": "decode", "text": "!!!"})
        self.assertTrue(r["result"]["isError"])

    # -- successful calls ----------------------------------------------------

    def test_hash_text_known_vector(self):
        self.session.initialize()
        r = self.session.call(15, "hash_text", {"algorithm": "sha256", "text": "abc"})
        res = r["result"]
        self.assertFalse(res["isError"])
        self.assertEqual(res["structuredContent"]["hex"],
                         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        self.assertEqual(res["structuredContent"]["input_bytes"], 3)
        # Backwards-compat: text content carries the same serialized JSON.
        self.assertEqual(json.loads(res["content"][0]["text"]), res["structuredContent"])

    def test_base64_round_trip(self):
        self.session.initialize()
        enc = self.session.call(16, "base64_convert", {"mode": "encode", "text": "héllo"})["result"]["structuredContent"]["result"]
        self.assertEqual(enc, base64.b64encode("héllo".encode("utf-8")).decode("ascii"))
        dec = self.session.call(17, "base64_convert", {"mode": "decode", "text": enc})["result"]["structuredContent"]
        self.assertEqual(dec["result"], "héllo")

    def test_json_format_sorts_and_validates(self):
        self.session.initialize()
        r = self.session.call(18, "json_format",
                              {"json": '{"b":1,"a":2}', "sort_keys": True, "indent": 0})
        sc = r["result"]["structuredContent"]
        self.assertEqual(json.loads(sc["formatted"]), {"a": 2, "b": 1})
        self.assertLess(sc["formatted"].find("a"), sc["formatted"].find("b"))
        self.assertEqual(sc["top_level_type"], "dict")

    def test_uuid_generate_count_and_shape(self):
        self.session.initialize()
        r = self.session.call(19, "uuid_generate", {"count": 3})
        sc = r["result"]["structuredContent"]
        self.assertEqual(sc["version"], 4)
        self.assertEqual(len(sc["uuids"]), 3)
        self.assertEqual(len(set(sc["uuids"])), 3)
        for u in sc["uuids"]:
            self.assertRegex(u, r"^[0-9a-f-]{36}$")

    def test_uuid_generate_default_count(self):
        self.session.initialize()
        r = self.session.call(20, "uuid_generate", {})
        self.assertEqual(len(r["result"]["structuredContent"]["uuids"]), 1)

    def test_text_stats_counts(self):
        self.session.initialize()
        r = self.session.call(21, "text_stats", {"text": "héllo wörld\nsecond line"})
        sc = r["result"]["structuredContent"]
        self.assertEqual(sc, {"characters": 23, "bytes_utf8": 25,
                              "words": 4, "lines": 2})

    def test_timestamp_convert_seconds_and_millis(self):
        self.session.initialize()
        r = self.session.call(22, "timestamp_convert", {"timestamp": 1763424000, "unit": "s"})
        sc = r["result"]["structuredContent"]
        self.assertEqual(sc["iso8601_utc"], "2025-11-18T00:00:00Z")
        self.assertEqual(sc["weekday_utc"], "Tuesday")
        r2 = self.session.call(23, "timestamp_convert", {"timestamp": 1763424000000, "unit": "ms"})
        self.assertEqual(r2["result"]["structuredContent"]["iso8601_utc"], "2025-11-18T00:00:00Z")

    def test_regex_test_matches_and_cap(self):
        self.session.initialize()
        r = self.session.call(24, "regex_test", {"pattern": r"(\d+)", "text": "a1 b22 c333"})
        sc = r["result"]["structuredContent"]
        self.assertTrue(sc["matched"])
        self.assertEqual(sc["count"], 3)
        self.assertEqual(sc["matches"], ["1", "22", "333"])
        big = self.session.call(25, "regex_test", {"pattern": r"\d", "text": "9" * 200})
        self.assertEqual(len(big["result"]["structuredContent"]["matches"]), 50)
        self.assertEqual(big["result"]["structuredContent"]["count"], 200)

    def test_url_convert_round_trip(self):
        self.session.initialize()
        enc = self.session.call(26, "url_convert", {"mode": "encode", "text": "a b&c=d"})["result"]["structuredContent"]["result"]
        self.assertEqual(enc, "a%20b%26c%3Dd")
        dec = self.session.call(27, "url_convert", {"mode": "decode", "text": enc})["result"]["structuredContent"]["result"]
        self.assertEqual(dec, "a b&c=d")

    def test_every_tool_output_conforms_to_declared_schema(self):
        """End-to-end: call every tool once and verify the server accepted its
        own structuredContent against its outputSchema (isError false)."""
        self.session.initialize()
        samples = {
            "hash_text": {"algorithm": "md5", "text": "x"},
            "base64_convert": {"mode": "encode", "text": "x"},
            "json_format": {"json": "[1,2]"},
            "uuid_generate": {},
            "text_stats": {"text": "x"},
            "timestamp_convert": {"timestamp": 0, "unit": "s"},
            "regex_test": {"pattern": "x", "text": "x"},
            "url_convert": {"mode": "encode", "text": "x"},
        }
        for i, (name, args) in enumerate(samples.items(), start=100):
            r = self.session.call(i, name, args)
            self.assertFalse(r["result"]["isError"], name)
            self.assertIn("structuredContent", r["result"], name)

    def test_graceful_stdin_eof(self):
        self.session.initialize()
        self.session.proc.stdin.close()
        self.assertIsNone(self.session.recv(timeout=5))
        self.session.proc.wait(timeout=10)
        self.assertEqual(self.session.proc.returncode, 0)
        self.assertEqual(self.session.proc.stderr.read(), "")


if __name__ == "__main__":
    unittest.main()
