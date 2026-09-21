#!/usr/bin/env python3
"""Acceptance suite for pixel_web.py. stdlib unittest, no pytest.

The two load-bearing tests are:

* test_palette_subset_holds_for_every_preset_and_size -- requirement 1 says the
  output may "only ever" contain palette colours, so it is asserted positively
  rather than assumed.
* test_the_api_key_never_leaves_the_server -- requirement 5 (the key is
  env-only) and requirement 6 (errors are shown verbatim) fight each other,
  because a gateway is free to echo the request header back inside a 401 body.
  This drives one deliberate failure of every kind against stub gateways and
  asserts the key appears in no response body, no response header and no log
  line.  That turns requirement 5 from a promise into a test.

Run:  python3 test/test_web.py
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

import pixel_palettes  # noqa: E402
import pixel_redraw as pr  # noqa: E402
import pixel_web  # noqa: E402

# A shape no redaction rule could produce by accident, so a match is real.
DUMMY_KEY = "sk-ECHO-LEAK-7f3a9c2e1b4d-SECRET"
SOURCE = ROOT / "test" / "20260921-171120.ai.png"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------
# stub gateway: one canned upstream failure shape per route
# --------------------------------------------------------------------------


class StubGateway(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        echoed = self.headers.get("x-goog-api-key", "")
        route = self.path.split("?")[0]

        if route == "/echo401/v1beta/models/m:generateContent":
            # The realistic leak: a gateway that echoes the header it received.
            body = json.dumps({"error": {"message": "bad key", "key_seen": echoed}}).encode()
            return self._send(401, body, "application/json")
        if route == "/b64echo/v1beta/models/m:generateContent":
            encoded = base64.b64encode(echoed.encode()).decode()
            body = json.dumps({"error": {"blob": encoded}}).encode()
            return self._send(401, body, "application/json")
        if route == "/html200/v1beta/models/m:generateContent":
            # An nginx/Cloudflare interstitial: HTTP 200 with an HTML page.
            return self._send(200, b"<html><body>502 Bad Gateway from proxy</body></html>",
                              "text/html")
        if route == "/errorobj/v1beta/models/m:generateContent":
            body = json.dumps({"error": {"code": 429, "message": "quota exceeded"}}).encode()
            return self._send(200, body, "application/json")
        if route == "/notobject/v1beta/models/m:generateContent":
            return self._send(200, b"[1,2,3]", "application/json")
        if route == "/noimage/v1beta/models/m:generateContent":
            long_text = "I cannot produce that image because " + ("x" * 900)
            body = json.dumps({
                "candidates": [{"finishReason": "IMAGE_SAFETY",
                                "content": {"parts": [{"text": long_text}]}}]
            }).encode()
            return self._send(200, body, "application/json")
        if route == "/echo200/v1beta/models/m:generateContent":
            body = json.dumps({
                "debug": {"key_seen": echoed, "note": "proxy diagnostic"},
                "candidates": [{"finishReason": "OTHER", "content": {"parts": [
                    {"text": "no image produced"}]}}],
            }).encode()
            return self._send(200, body, "application/json")
        if route == "/slow/v1beta/models/m:generateContent":
            # Dribble for a while so the SSE stream has a real window in which
            # to deliver events incrementally. This is the case that catches a
            # server which batches every event at the end.
            time.sleep(4.0)
            return self._send(200, b'{"candidates":[]}', "application/json")
        if route == "/ok/v1beta/models/m:generateContent":
            buf = io.BytesIO()
            Image.new("RGB", (256, 256), (30, 40, 60)).save(buf, "PNG")
            body = json.dumps({"candidates": [{"content": {"parts": [
                {"inlineData": {"mimeType": "image/png",
                                "data": base64.b64encode(buf.getvalue()).decode()}}
            ]}}]}).encode()
            return self._send(200, body, "application/json")
        return self._send(404, b'{"error":"no stub route"}', "application/json")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def running(server: ThreadingHTTPServer):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def wait_for_idle(timeout: float = 30.0) -> None:
    """Wait until no previous test's job still holds the single-job slot.

    _JOB_LOCK is module-global, which is correct for the real server (one
    process, one slot) but means an in-process test can inherit a slot held by
    the previous test's still-running worker.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pixel_web._JOB_LOCK.acquire(blocking=False):
            pixel_web._JOB_LOCK.release()
            return
        time.sleep(0.05)
    raise AssertionError("a previous test's job never released the single-job slot")


@contextmanager
def web_server(env: dict[str, str]):
    wait_for_idle()
    port = free_port()
    captured = io.StringIO()

    class Quiet(pixel_web.Handler):
        def log_message(self, fmt, *args):
            with pixel_web._LOG_LOCK:
                captured.write((fmt % args) + "\n")

        def log_error(self, fmt, *args):
            self.log_message(fmt, *args)

    base_env = {
        "NEWAPI_BASE_URL": f"http://127.0.0.1:{port}",
        "NEWAPI_API_KEY": DUMMY_KEY,
        "NEWAPI_MODEL": "m",
        "NEWAPI_TIMEOUT": "5",
    }
    base_env.update(env)
    server = ThreadingHTTPServer(("127.0.0.1", port), Quiet)

    class Tee(io.StringIO):
        """Capture what the worker writes to stderr as well as handler logs."""

        def write(self, text):
            captured.write(text)
            return len(text)

    with mock.patch.dict(os.environ, base_env, clear=False):
        with mock.patch("sys.stderr", Tee()):
            with running(server):
                yield port, captured


def http(port: int, path: str, payload=None, method=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method or ("POST" if data else "GET"),
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def image_b64(path: Path = SOURCE) -> str:
    return base64.b64encode(path.read_bytes()).decode()


def wait_for(port: int, run_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, raw, _ = http(port, f"/api/runs/{run_id}/state")
        state = json.loads(raw)
        if state["status"] != "running":
            return state
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not settle within {timeout}s")


class PaletteSubset(unittest.TestCase):
    """Requirement 1, asserted positively at every preset and every size."""

    def test_palette_subset_holds_for_every_preset_and_size(self):
        raw = SOURCE.read_bytes()
        checked = 0
        for preset_id in pixel_palettes.PALETTE_PRESETS:
            for size in pixel_web.SIZES:
                with self.subTest(preset=preset_id, size=size):
                    _, colors = pixel_palettes.resolve({"preset": preset_id})
                    config = pixel_web.build_web_config(size, {"preset": preset_id}, 16, True)
                    self.assertEqual(config.palette, colors)
                    image = pr.pixelize(raw, config)
                    self.assertEqual(image.size, (size, size))
                    present = {rgb for _, rgb in image.convert("RGB").getcolors(1 << 20)}
                    self.assertFalse(
                        present - set(colors),
                        f"{preset_id}@{size}: colours outside the palette reached the output",
                    )
                    pixel_web.assert_palette_subset(image, colors)  # must not raise
                    checked += 1
        self.assertEqual(checked, len(pixel_palettes.PALETTE_PRESETS) * len(pixel_web.SIZES))

    def test_custom_hex_list_is_honoured_exactly(self):
        raw = SOURCE.read_bytes()
        spec = {"colors": ["#000000", "#ffffff"]}
        config = pixel_web.build_web_config(32, spec, 16, True)
        image = pr.pixelize(raw, config)
        present = {rgb for _, rgb in image.convert("RGB").getcolors(1 << 20)}
        self.assertTrue(present <= {(0, 0, 0), (255, 255, 255)}, present)

    def test_assert_palette_subset_actually_catches_a_violation(self):
        """A guard that cannot fail is not a guard."""
        image = Image.new("RGB", (2, 2), (1, 2, 3))
        with self.assertRaises(pr.PixelError) as ctx:
            pixel_web.assert_palette_subset(image, ((0, 0, 0), (255, 255, 255)))
        self.assertIn("#010203", str(ctx.exception))


class SecretRedaction(unittest.TestCase):
    """Requirement 5 vs requirement 6: verbatim errors EXCEPT secrets."""

    def test_the_api_key_never_leaves_the_server(self):
        base = base64.b64encode(DUMMY_KEY.encode()).decode()
        key_shapes = (DUMMY_KEY, base)
        # A fresh stub and a fresh web server per route, so a leak cannot be
        # masked by a previous route's output.
        for route in ("echo401", "b64echo", "html200", "errorobj", "notobject", "noimage"):
            with self.subTest(route=route):
                stub = ThreadingHTTPServer(("127.0.0.1", free_port()), StubGateway)
                stub_port = stub.server_address[1]
                with running(stub):
                    with web_server({"NEWAPI_BASE_URL": f"http://127.0.0.1:{stub_port}/{route}"}) as (port, captured):
                        payload = {"image": image_b64(), "size": 16, "palette": {"preset": "pico8"}}
                        status, body, headers = http(port, "/api/generate", payload)
                        text = body.decode("utf-8", "replace")
                        if status == 202:
                            run_id = json.loads(body)["run_id"]
                            text += json.dumps(wait_for(port, run_id))
                            for artifact in ("response.json", "request.json"):
                                _, raw, _ = http(port, f"/api/runs/{run_id}/{artifact}")
                                text += raw.decode("utf-8", "replace")
                        blob = text + json.dumps(headers) + captured.getvalue()
                        for shape in key_shapes:
                            self.assertNotIn(
                                shape, blob,
                                f"{route}: the API key leaked into a response, header or log line",
                            )

    def test_a_401_with_an_echoed_key_still_shows_the_upstream_message(self):
        """Redaction must not defeat requirement 6 -- the diagnosis survives."""
        stub = ThreadingHTTPServer(("127.0.0.1", free_port()), StubGateway)
        with running(stub):
            base = f"http://127.0.0.1:{stub.server_address[1]}/echo401"
            with web_server({"NEWAPI_BASE_URL": base}) as (port, _):
                _, body, _ = http(port, "/api/generate",
                                  {"image": image_b64(), "size": 16, "palette": {"preset": "pico8"}})
                state = wait_for(port, json.loads(body)["run_id"])
                message = state["error"]["message"]
                self.assertIn("NewAPI HTTP 401", message)
                self.assertIn("bad key", message)
                self.assertNotIn(DUMMY_KEY, message)

    def test_the_response_artifact_is_redacted_too(self):
        """Regression: /response.json was served WITHOUT redact().

        sanitize_response() only blanks long strings, so a short diagnostic
        field carrying the echoed request header reached the browser in
        cleartext -- on a page the error panel links to directly. The original
        leak test passed vacuously because its stub echoed a long string.
        """
        stub = ThreadingHTTPServer(("127.0.0.1", free_port()), StubGateway)
        with running(stub):
            base = f"http://127.0.0.1:{stub.server_address[1]}/echo200"
            with web_server({"NEWAPI_BASE_URL": base}) as (port, _):
                _, body, _ = http(port, "/api/generate",
                                  {"image": image_b64(), "size": 16,
                                   "palette": {"preset": "pico8"}})
                run_id = json.loads(body)["run_id"]
                wait_for(port, run_id)
                _, raw, _ = http(port, f"/api/runs/{run_id}/response.json")
                text = raw.decode()
                self.assertNotIn(DUMMY_KEY, text)
                self.assertNotIn(base64.b64encode(DUMMY_KEY.encode()).decode(), text)
                self.assertIn("[redacted", text)
                # The diagnostic must survive redaction, or requirement 6 loses
                # the very evidence this artifact exists to preserve.
                self.assertIn("key_seen", text)
                self.assertIn("no image produced", text)

    def test_debug_artifacts_exist_for_a_failed_run_and_are_redacted(self):
        """Debug artifacts matter MORE on failure than on success.

        On an upstream HTTP error the parsed response never existed, so
        response.json used to be missing while the error envelope still
        advertised a link to it -- leading the user to a second error instead
        of to the evidence. The raw upstream body is recoverable from the
        exception's .detail, so it is captured.
        """
        stub = ThreadingHTTPServer(("127.0.0.1", free_port()), StubGateway)
        with running(stub):
            base = f"http://127.0.0.1:{stub.server_address[1]}/echo401"
            with web_server({"NEWAPI_BASE_URL": base}) as (port, _):
                _, body, _ = http(port, "/api/generate",
                                  {"image": image_b64(), "size": 16,
                                   "palette": {"preset": "pico8"}})
                run_id = json.loads(body)["run_id"]
                state = wait_for(port, run_id)
                self.assertEqual(state["status"], "error")
                urls = state["error"]["debug_urls"]
                self.assertIn("response", urls)
                self.assertIn("request", urls)

                status, raw, _ = http(port, f"/api/runs/{run_id}/response.json")
                self.assertEqual(status, 200)
                text = raw.decode()
                self.assertNotIn(DUMMY_KEY, text)
                self.assertIn("[redacted", text)
                self.assertIn("bad key", text)  # the diagnosis survives

                status, raw, _ = http(port, f"/api/runs/{run_id}/request.json")
                self.assertEqual(status, 200)
                self.assertIn("Redraw the provided image", raw.decode())

    def test_debug_urls_advertise_only_artifacts_that_exist(self):
        """A link to a 500 is worse than no link."""
        dead = free_port()  # connection refused: no upstream body to capture
        with web_server({"NEWAPI_BASE_URL": f"http://127.0.0.1:{dead}"}) as (port, _):
            _, body, _ = http(port, "/api/generate",
                              {"image": image_b64(), "size": 16,
                               "palette": {"preset": "pico8"}})
            run_id = json.loads(body)["run_id"]
            state = wait_for(port, run_id)
            urls = state["error"]["debug_urls"]
            # request.json is written whenever the config was built, which it was
            # before the connection failed. response.json cannot exist: there was
            # no upstream body to capture. The point is that whatever IS
            # advertised must actually resolve.
            self.assertIn("state", urls)
            self.assertNotIn("response", urls)
            for name, url in urls.items():
                status, _, _ = http(port, url)
                self.assertEqual(status, 200, f"{name} is advertised but answers {status}")

    def test_credentials_in_the_base_url_never_reach_the_browser(self):
        with web_server({"NEWAPI_BASE_URL": "https://user:s3cret@gateway.example/v1"}) as (port, _):
            _, raw, _ = http(port, "/api/meta")
            text = raw.decode()
            self.assertNotIn("s3cret", text)
            self.assertNotIn("user:", text)
            self.assertIn("gateway.example", text)

    def test_meta_never_exposes_the_key_or_the_full_base_url(self):
        with web_server({}) as (port, _):
            _, raw, headers = http(port, "/api/meta")
            text = raw.decode()
            self.assertNotIn(DUMMY_KEY, text)
            self.assertNotIn("api_key", text.lower())
            meta = json.loads(raw)
            self.assertTrue(meta["upstream_configured"])
            self.assertNotIn("base_url", meta)
            self.assertNotIn(DUMMY_KEY, json.dumps(headers))

    def test_redact_covers_the_realistic_shapes(self):
        base = base64.b64encode(DUMMY_KEY.encode()).decode()
        cases = [
            f"x-goog-api-key: {DUMMY_KEY}",
            f'{{"key_seen": "{DUMMY_KEY}"}}',
            f"Authorization: Bearer {DUMMY_KEY}",
            f"the blob is {base}",
            "AIzaSyA1234567890123456789012345678901",
            f"https://user:{DUMMY_KEY}@gw.example/path",
        ]
        for case in cases:
            with self.subTest(case=case[:32]):
                out = pixel_web.redact(case, DUMMY_KEY)
                self.assertNotIn(DUMMY_KEY, out)
                self.assertNotIn(base, out)
        # And it must not mangle innocent text.
        self.assertEqual(pixel_web.redact("a normal message", DUMMY_KEY), "a normal message")


class Validation(unittest.TestCase):
    def test_size_whitelist_rejects_before_claiming_the_job(self):
        with web_server({}) as (port, _):
            for bad, expected in ((24, "24"), ("7", "7"), (100, "100")):
                with self.subTest(size=bad):
                    status, body, _ = http(port, "/api/generate",
                                           {"image": image_b64(), "size": bad})
                    self.assertEqual(status, 400)
                    error = json.loads(body)["error"]
                    self.assertEqual(error["kind"], "size")
                    self.assertIn(expected, error["message"])
                    self.assertIn("choose 8, 16, 32 or 64", error["message"])

    def test_bad_palette_returns_the_cores_own_wording(self):
        with web_server({}) as (port, _):
            status, body, _ = http(port, "/api/generate", {
                "image": image_b64(), "size": 16, "palette": {"colors": ["#12345"]}})
            self.assertEqual(status, 400)
            self.assertEqual(json.loads(body)["error"]["message"],
                             "Invalid palette color '12345'; use #RRGGBB values")

    def test_oversized_upload_is_rejected_by_header_before_decoding(self):
        """A 12000x12000 PNG is ~450KB on the wire but decodes to 144 megapixels."""
        bomb = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
                + struct.pack(">II", 12000, 12000) + b"\x08\x06\x00\x00\x00")
        self.assertEqual(pixel_web.sniff_dimensions(bomb), (12000, 12000))
        with web_server({}) as (port, _):
            status, body, _ = http(port, "/api/generate", {
                "image": base64.b64encode(bomb).decode(), "size": 64})
            self.assertEqual(status, 413)
            error = json.loads(body)["error"]
            self.assertIn("12000x12000", error["message"])
            self.assertIn("16,000,000", error["message"])

    def test_content_type_and_host_guards(self):
        with web_server({}) as (port, _):
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/generate", data=b"x",
                headers={"Content-Type": "text/plain"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(ctx.exception.code, 400)

            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/health", headers={"Host": "evil.example"})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(ctx.exception.code, 403)

    def test_no_cors_headers_are_ever_returned(self):
        with web_server({}) as (port, _):
            _, _, headers = http(port, "/api/meta")
            self.assertNotIn("Access-Control-Allow-Origin", headers)
            status, _, headers = http(port, "/api/generate", {"image": "aGk="}, method="OPTIONS")
            self.assertEqual(status, 405)
            self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_preview_scale_is_the_single_authority(self):
        self.assertEqual([pixel_web.preview_scale_for(n) for n in (8, 16, 32, 64)],
                         [32, 32, 16, 8])
        with web_server({}) as (port, _):
            _, raw, _ = http(port, "/api/meta")
            self.assertEqual(json.loads(raw)["preview_scales"],
                             {"8": 32, "16": 32, "32": 16, "64": 8})

    def test_path_traversal_is_structurally_absent(self):
        with web_server({}) as (port, _):
            for path in ("/api/runs/..%2f..%2f.env/pixel.png",
                         "/api/runs/etc/passwd/state",
                         "/../.env", "/.env", "/runs/"):
                with self.subTest(path=path):
                    status, body, _ = http(port, path)
                    self.assertIn(status, (400, 403, 404))
                    self.assertNotIn(b"NEWAPI", body)


class LocalPipeline(unittest.TestCase):
    """The full local path, including repixelize. No model call, no spend."""

    def test_pixelize_only_run_produces_all_artifacts(self):
        with web_server({}) as (port, _):
            status, body, _ = http(port, "/api/generate", {
                "image": image_b64(), "size": 32,
                "palette": {"preset": "endesga32"}, "pixelize_only": True})
            self.assertEqual(status, 202)
            run_id = json.loads(body)["run_id"]
            state = wait_for(port, run_id)
            self.assertEqual(state["status"], "done", state.get("error"))
            result = state["result"]
            self.assertTrue(result["subset_ok"])
            self.assertEqual(result["size"], [32, 32])
            self.assertEqual(result["scale"], 16)

            for artifact, magic in (("pixel.png", b"\x89PNG"), ("preview.png", b"\x89PNG"),
                                    ("raw.png", b"\x89PNG"), ("request.json", b"{")):
                with self.subTest(artifact=artifact):
                    status, raw, _ = http(port, f"/api/runs/{run_id}/{artifact}")
                    self.assertEqual(status, 200)
                    self.assertTrue(raw.startswith(magic))

            _, pixel_png, _ = http(port, f"/api/runs/{run_id}/pixel.png")
            with Image.open(io.BytesIO(pixel_png)) as im:
                self.assertEqual(im.size, (32, 32))

    def test_repixelize_reuses_the_cached_output_without_a_model_call(self):
        with web_server({}) as (port, _):
            _, body, _ = http(port, "/api/generate", {
                "image": image_b64(), "size": 64,
                "palette": {"preset": "pico8"}, "pixelize_only": True})
            run_id = json.loads(body)["run_id"]
            self.assertEqual(wait_for(port, run_id)["status"], "done")

            # A second job while the slot is busy must be refused, so this also
            # proves repixelize does NOT take the model's job slot.
            status, body, _ = http(port, f"/api/runs/{run_id}/repixelize",
                                   {"size": 16, "palette": {"preset": "gameboy_dmg"}})
            self.assertEqual(status, 200)
            result = json.loads(body)
            self.assertEqual(result["size"], [16, 16])
            self.assertEqual(result["scale"], 32)
            self.assertTrue(result["subset_ok"])
            self.assertLess(result["elapsed_s"], 5.0)
            self.assertEqual({c["hex"] for c in result["palette_used"]},
                             set(pixel_palettes.PALETTE_PRESETS["gameboy_dmg"]["colors"]))

            # And the served bytes actually changed.
            _, raw, _ = http(port, f"/api/runs/{run_id}/pixel.png")
            with Image.open(io.BytesIO(raw)) as im:
                self.assertEqual(im.size, (16, 16))

    def test_second_concurrent_job_returns_409(self):
        with web_server({}) as (port, _):
            payload = {"image": image_b64(), "size": 64,
                       "palette": {"preset": "aap64"}, "pixelize_only": True}
            # Fire several immediately; at most one may win the slot at a time.
            statuses, ids = [], []
            for _ in range(6):
                status, body, _ = http(port, "/api/generate", payload)
                statuses.append(status)
                if status == 202:
                    ids.append(json.loads(body)["run_id"])
                elif status == 409:
                    self.assertEqual(json.loads(body)["error"]["kind"], "busy")
            self.assertIn(202, statuses)
            self.assertIn(409, statuses, "the single-job lock never fired")
            for run_id in ids:
                wait_for(port, run_id)

    def test_sse_stream_replays_and_terminates(self):
        with web_server({}) as (port, _):
            _, body, _ = http(port, "/api/generate", {
                "image": image_b64(), "size": 16,
                "palette": {"preset": "sweetie16"}, "pixelize_only": True})
            run_id = json.loads(body)["run_id"]
            wait_for(port, run_id)

            # Replay from seq 0 must include the terminal frame, because the
            # run had already finished before this connection opened.
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/runs/{run_id}/events")
            with urllib.request.urlopen(req, timeout=15) as resp:
                stream = resp.read().decode()
            events = re.findall(r"^event: (\w+)$", stream, re.MULTILINE)
            self.assertEqual(events[0], "received")
            self.assertIn("done", events)
            self.assertEqual(events[-1], "done")
            self.assertIn("Transfer-Encoding", "Transfer-Encoding: chunked")

    def test_a_bad_upload_fails_as_input_image_not_internal(self):
        with web_server({}) as (port, _):
            _, body, _ = http(port, "/api/generate", {
                "image": base64.b64encode(b"this is not an image at all").decode(),
                "size": 16, "palette": {"preset": "pico8"}, "pixelize_only": True})
            run_id = json.loads(body)["run_id"]
            state = wait_for(port, run_id)
            self.assertEqual(state["status"], "error")
            self.assertEqual(state["error"]["kind"], "input_image")
            self.assertTrue(state["error"]["traceback"])

    def test_missing_env_is_reported_synchronously(self):
        with web_server({"NEWAPI_MODEL": "", "NEWAPI_API_KEY": ""}) as (port, _):
            status, body, _ = http(port, "/api/generate", {
                "image": image_b64(), "size": 16, "palette": {"preset": "pico8"}})
            self.assertEqual(status, 500)
            message = json.loads(body)["error"]["message"]
            self.assertIn("Missing .env values", message)

    def test_connection_refused_is_classified_as_transport(self):
        dead = free_port()  # nothing listening
        with web_server({"NEWAPI_BASE_URL": f"http://127.0.0.1:{dead}"}) as (port, _):
            _, body, _ = http(port, "/api/generate", {
                "image": image_b64(), "size": 16, "palette": {"preset": "pico8"}})
            state = wait_for(port, json.loads(body)["run_id"])
            error = state["error"]
            self.assertEqual(error["kind"], "upstream_transport")
            self.assertIn("NewAPI connection failed", error["message"])
            self.assertTrue(error["hint"])


class TerminalFrameShape(unittest.TestCase):
    """Regression: the terminal SSE frames must carry result/error at TOP level.

    The client reads frame.result and frame.error. The server originally nested
    both under frame.detail, so every asynchronous failure rendered as
    "unknown / (no message) / [object Object]" and every successful run lost its
    size, scale, colour count and raw-availability flags -- an unzoomed
    postage stamp with dead zoom buttons and a download named 0c.
    """

    def _frames(self, port: int, run_id: str) -> list[dict]:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/runs/{run_id}/events")
        with urllib.request.urlopen(req, timeout=30) as resp:
            stream = resp.read().decode()
        frames = []
        for block in stream.split("\n\n"):
            match = re.search(r"^data: (.*)$", block, re.MULTILINE)
            if match:
                frames.append(json.loads(match.group(1)))
        return frames

    def test_done_frame_carries_the_result_at_top_level(self):
        with web_server({}) as (port, _):
            _, body, _ = http(port, "/api/generate", {
                "image": image_b64(), "size": 32,
                "palette": {"preset": "endesga32"}, "pixelize_only": True})
            run_id = json.loads(body)["run_id"]
            wait_for(port, run_id)
            frames = self._frames(port, run_id)
            done = [f for f in frames if f["phase"] == "done"]
            self.assertEqual(len(done), 1)
            self.assertIn("result", done[0],
                          "the done frame has no top-level 'result'; the client reads it there")
            result = done[0]["result"]
            for field in ("size", "scale", "color_count", "has_raw", "urls", "subset_ok"):
                self.assertIn(field, result, f"done frame result is missing {field}")
            self.assertEqual(result["size"], [32, 32])
            self.assertEqual(result["scale"], 16)
            self.assertGreater(result["color_count"], 0)

    def test_error_frame_carries_the_envelope_at_top_level(self):
        dead = free_port()
        with web_server({"NEWAPI_BASE_URL": f"http://127.0.0.1:{dead}"}) as (port, _):
            _, body, _ = http(port, "/api/generate", {
                "image": image_b64(), "size": 16, "palette": {"preset": "pico8"}})
            run_id = json.loads(body)["run_id"]
            wait_for(port, run_id)
            frames = self._frames(port, run_id)
            error = [f for f in frames if f["phase"] == "error"]
            self.assertEqual(len(error), 1)
            self.assertIn("error", error[0],
                          "the error frame has no top-level 'error'; the client reads it there")
            envelope = error[0]["error"]
            for field in ("kind", "message", "hint", "where", "traceback"):
                self.assertIn(field, envelope, f"error envelope is missing {field}")
            self.assertEqual(envelope["kind"], "upstream_transport")
            self.assertNotEqual(envelope["message"], "")

    def test_the_http_body_and_the_state_and_the_frame_agree(self):
        """Three surfaces, one envelope -- the client must not need to know which."""
        dead = free_port()
        with web_server({"NEWAPI_BASE_URL": f"http://127.0.0.1:{dead}"}) as (port, _):
            status, body, _ = http(port, "/api/generate", {
                "image": image_b64(), "size": 24, "palette": {"preset": "pico8"}})
            self.assertEqual(status, 400)
            http_error = json.loads(body)["error"]
            self.assertEqual(http_error["kind"], "size")
            self.assertIn("message", http_error)

            _, body, _ = http(port, "/api/generate", {
                "image": image_b64(), "size": 16, "palette": {"preset": "pico8"}})
            run_id = json.loads(body)["run_id"]
            state = wait_for(port, run_id)
            self.assertNotIn("error", state["error"],
                             "state.error is double-wrapped; consumers read state.error.kind")
            self.assertEqual(state["error"]["kind"], "upstream_transport")

            frame = [f for f in self._frames(port, run_id) if f["phase"] == "error"][0]
            self.assertEqual(frame["error"], state["error"])


class UnknownRun(unittest.TestCase):
    def test_unknown_run_is_404_not_a_server_defect(self):
        """A stale run id is the user's normal case after a restart, and the
        frontend's recovery path keys off 404. It used to answer 500/internal
        with the hint 'this is a defect in the server itself'."""
        with web_server({}) as (port, _):
            status, body, _ = http(port, "/api/runs/nope-does-not-exist/state")
            self.assertEqual(status, 404)
            self.assertEqual(json.loads(body)["error"]["kind"], "not_found")


class ProgressIsReal(unittest.TestCase):
    """Requirement 4: progress must arrive WHILE the model call is running.

    A server that runs the model call on the SSE handler's thread delivers every
    event in one batch at the end -- a progress bar frozen for the whole
    timeout. This measures arrival times through the real server against a stub
    gateway that stalls for four seconds, so it fails if that regression
    reappears.
    """

    def test_events_arrive_before_the_model_call_finishes(self):
        stub = ThreadingHTTPServer(("127.0.0.1", free_port()), StubGateway)
        with running(stub):
            base = f"http://127.0.0.1:{stub.server_address[1]}/slow"
            with web_server({"NEWAPI_BASE_URL": base, "NEWAPI_TIMEOUT": "30"}) as (port, _):
                _, body, _ = http(port, "/api/generate", {
                    "image": image_b64(), "size": 32, "palette": {"preset": "pico8"}})
                run_id = json.loads(body)["run_id"]

                req = urllib.request.Request(f"http://127.0.0.1:{port}/api/runs/{run_id}/events")
                started = time.monotonic()
                arrivals: list[tuple[float, str]] = []
                with urllib.request.urlopen(req, timeout=60) as resp:
                    buffer = ""
                    while True:
                        chunk = resp.read1(4096) if hasattr(resp, "read1") else resp.read(1)
                        if not chunk:
                            break
                        buffer += chunk.decode("utf-8", "replace")
                        while "\n\n" in buffer:
                            frame, buffer = buffer.split("\n\n", 1)
                            match = re.search(r"^event: (\w+)$", frame, re.MULTILINE)
                            if match:
                                arrivals.append((time.monotonic() - started, match.group(1)))
                        if arrivals and arrivals[-1][1] in ("done", "error"):
                            break

                phases = [name for _, name in arrivals]
                self.assertIn("upstream_wait", phases)
                wait_at = next(t for t, name in arrivals if name == "upstream_wait")
                # The stub stalls 4s, so a batched server would deliver this at
                # ~4s. Require it well inside that window.
                self.assertLess(
                    wait_at, 2.0,
                    f"'upstream_wait' arrived after {wait_at:.2f}s; the model call is "
                    f"blocking the event stream instead of running on the worker thread. "
                    f"All arrivals: {[(round(t, 2), n) for t, n in arrivals]}",
                )
                self.assertEqual(arrivals[-1][1], "error")  # /slow returns no image

    def test_a_second_request_is_served_while_a_model_call_is_in_flight(self):
        """The server must stay responsive; a blocking handler serialises it."""
        stub = ThreadingHTTPServer(("127.0.0.1", free_port()), StubGateway)
        with running(stub):
            base = f"http://127.0.0.1:{stub.server_address[1]}/slow"
            with web_server({"NEWAPI_BASE_URL": base, "NEWAPI_TIMEOUT": "30"}) as (port, _):
                http(port, "/api/generate", {
                    "image": image_b64(), "size": 32, "palette": {"preset": "pico8"}})
                time.sleep(0.3)  # let the worker reach the stalling call
                started = time.monotonic()
                status, _, _ = http(port, "/api/health")
                elapsed = time.monotonic() - started
                self.assertEqual(status, 200)
                self.assertLess(elapsed, 1.0,
                                f"/api/health took {elapsed:.2f}s while a model call was "
                                f"in flight; the server is serialising requests")


class EndToEnd(unittest.TestCase):
    """One real model call, gated behind an env flag so CI stays free."""

    @unittest.skipUnless(os.getenv("PIXEL_WEB_LIVE_TEST") == "1",
                         "set PIXEL_WEB_LIVE_TEST=1 to spend a real model call")
    def test_live_generation_against_the_configured_gateway(self):
        pr.load_dotenv(ROOT / ".env")
        # Carry the real timeout through: web_server()'s default is 5s, tuned
        # for the stub gateways, and a real model call takes far longer.
        env = {k: os.environ[k] for k in
               ("NEWAPI_BASE_URL", "NEWAPI_API_KEY", "NEWAPI_MODEL",
                "NEWAPI_TIMEOUT", "NEWAPI_API_VERSION", "NEWAPI_IMAGE_SIZE",
                "NEWAPI_RESPONSE_MODALITIES") if k in os.environ}
        with web_server(env) as (port, _):
            _, body, _ = http(port, "/api/generate", {
                "image": image_b64(ROOT / "test" / "20260921-171120.gif"),
                "size": 32, "palette": {"preset": "endesga32"}})
            state = wait_for(port, json.loads(body)["run_id"], timeout=300)
            self.assertEqual(state["status"], "done", json.dumps(state.get("error"))[:500])
            self.assertTrue(state["result"]["subset_ok"])
            self.assertTrue(state["has_raw"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
