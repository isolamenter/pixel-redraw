"""Behavior checks for automatic refinement without spending API calls."""

import base64
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from PIL import Image

import pixel_redraw as pr
import pixel_web as web


def image_response(color):
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buffer, "PNG")
    content = buffer.getvalue()
    return content, {
        "candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": "image/png", "data": base64.b64encode(content).decode("ascii"),
        }}]}}]
    }


class TwoPassTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source.png"
        Image.new("RGB", (32, 32), "white").save(self.source)

    def run_cli(self, responses):
        with mock.patch.dict(os.environ, {
            "GEMINI_API_KEY": "test-key", "GEMINI_MODEL": "test-model",
        }), mock.patch.object(pr, "call_gemini", side_effect=responses) as call:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                exit_code = pr.main([
                    str(self.source), "--passes", "2", "--size", "32",
                    "--out-dir", str(self.root / "out"),
                ])
        report = json.loads((self.root / "out/source.report.json").read_text())
        pixel = Image.open(self.root / "out/source.pixel.png").convert("RGB")
        return exit_code, call, report, pixel.getpixel((0, 0))

    def test_second_image_becomes_final_output(self):
        draft, first = image_response("red")
        _, second = image_response("blue")
        exit_code, call, report, color = self.run_cli([first, second])
        self.assertEqual(exit_code, 0)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(call.call_args.kwargs["draft"], draft)
        self.assertTrue(report["refinement_applied"])
        self.assertEqual(color, (0, 0, 255))

    def test_failed_refinement_keeps_first_draft(self):
        _, first = image_response("red")
        exit_code, call, report, color = self.run_cli([
            first, pr.UpstreamTransportError("temporary failure"),
        ])
        self.assertEqual(exit_code, 0)
        self.assertEqual(call.call_count, 2)
        self.assertFalse(report["refinement_applied"])
        self.assertEqual(color, (255, 0, 0))

    def test_web_worker_emits_two_pass_progress_and_returns_final(self):
        self.source_bytes = self.source.read_bytes()
        _, first = image_response("red")
        _, second = image_response("blue")
        with mock.patch.dict(os.environ, {
            "GEMINI_API_KEY": "test-key", "GEMINI_MODEL": "test-model",
            "PIXEL_GENERATION_PASSES": "2",
        }), mock.patch.object(web, "RUNS_DIR", self.root / "runs"), \
                mock.patch.object(pr, "call_gemini", side_effect=[first, second]) as call:
            run = web.Run("test-run", "model")
            web.work(run, self.source_bytes, "image/png", "source.png", 32, None, 16, False)
        self.assertEqual(run.status, "done", run.error)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(run.artifacts["raw.png"][0], pr.extract_image(second))
        self.assertIn("draft.png", run.artifacts)
        with Image.open(io.BytesIO(run.artifacts["draft.png"][0])) as draft_image:
            self.assertEqual(draft_image.size, (4, 4))
        self.assertEqual(run.result["urls"]["draft"], "/api/runs/test-run/draft.png")
        self.assertTrue(run.result["refinement_applied"])
        phases = [event["phase"] for event in run.events]
        self.assertLess(phases.index("extracted"), phases.index("refine_wait"))
        self.assertLess(phases.index("refined"), phases.index("pixelizing"))
        first_pixel = run.artifacts["pixel.png"][0]
        with mock.patch.dict(os.environ, {
            "GEMINI_API_KEY": "test-key", "GEMINI_MODEL": "test-model",
        }):
            updated = web.repixelize(run, 32, None, 16)
        self.assertEqual(run.artifacts["pixel.png"][0], first_pixel)
        self.assertTrue(updated["refinement_applied"])

    def test_refine_payload_uses_pixelized_first_draft(self):
        draft, _ = image_response("red")
        args = mock.Mock(
            size="32", scale=8, max_colors=16, palette="auto",
            prompt=None, keep_raw=False, pixelize_only=False, passes=None,
        )
        with mock.patch.dict(os.environ, {
            "GEMINI_API_KEY": "test-key", "GEMINI_MODEL": "test-model",
        }):
            config = pr.build_config(args)
            config = pr.configure_for_source(config, (32, 32))
            payload = pr.build_payload(self.source, config, draft=draft)

        encoded = payload["contents"][0]["parts"][2]["inline_data"]["data"]
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as review:
            self.assertEqual(review.size, (32, 32))
            self.assertEqual(review.getpixel((0, 0)), (255, 0, 0, 255))


if __name__ == "__main__":
    unittest.main()
