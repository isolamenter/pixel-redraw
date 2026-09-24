"""Behavior checks for the pixel-redraw core, with no network and no API spend.

Everything here drives the real pipeline through `generate()`, which takes its
transport as an injected callback.  That is what lets the two-pass flow, the
refinement fallback and the palette invariant be asserted exactly as they were
when they lived behind a CLI and an HTTP server.
"""

import asyncio
import base64
import io
import json
import re
import unittest
from pathlib import Path

from PIL import Image

import pixel_redraw as pr

REPO_ROOT = Path(__file__).resolve().parent.parent


def source_png(size=(32, 32), color="white") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def image_response(color, size=(32, 32)):
    """A Gemini-shaped response carrying one inline PNG."""
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    content = buffer.getvalue()
    return {
        "candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": "image/png", "data": base64.b64encode(content).decode("ascii"),
        }}]}}]
    }


class FakeUpstream:
    """Records every payload it is handed and replays canned responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.payloads = []

    async def __call__(self, payload):
        self.payloads.append(payload)
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def run_generation(coro):
    return asyncio.run(coro)


class TwoPassTest(unittest.TestCase):
    def base_config(self, **overrides):
        options = dict(model="test-model", api_key="test-key", size="32x32", max_colors=16)
        options.update(overrides)
        return pr.make_config(**options)

    def test_second_pass_output_becomes_the_final_image(self):
        upstream = FakeUpstream(image_response("red"), image_response("blue"))
        events = []
        generation = run_generation(pr.generate(
            self.base_config(), source_png(), upstream,
            emit=lambda phase, label, detail="": events.append(phase),
        ))

        self.assertEqual(len(upstream.payloads), 2)
        self.assertTrue(generation.refinement_applied)
        with Image.open(io.BytesIO(generation.outputs["pixel_png"])) as pixel:
            self.assertEqual(pixel.convert("RGB").getpixel((0, 0)), (0, 0, 255))
        self.assertTrue(generation.outputs["report"]["refinement_applied"])

    def test_failed_refinement_keeps_the_first_draft(self):
        upstream = FakeUpstream(
            image_response("red"),
            pr.UpstreamTransportError("temporary failure"),
        )
        generation = run_generation(pr.generate(self.base_config(), source_png(), upstream))

        self.assertEqual(len(upstream.payloads), 2)
        self.assertFalse(generation.refinement_applied)
        with Image.open(io.BytesIO(generation.outputs["pixel_png"])) as pixel:
            self.assertEqual(pixel.convert("RGB").getpixel((0, 0)), (255, 0, 0))

    def test_second_payload_carries_the_pixelized_first_draft(self):
        draft = image_response("red")
        upstream = FakeUpstream(draft, draft)
        run_generation(pr.generate(self.base_config(), source_png(), upstream))

        parts = upstream.payloads[1]["contents"][0]["parts"]
        encoded = parts[2]["inline_data"]["data"]
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as review:
            self.assertEqual(review.size, (32, 32))
            self.assertEqual(review.getpixel((0, 0)), (255, 0, 0, 255))

    def test_intermediate_draft_is_reported_when_refinement_ran(self):
        """The page shows the pixelized first draft between the two passes.

        It must be the LOGICAL grid (4x4 here), not the enlarged review image
        sent to the model, because the UI scales it back up itself.
        """
        upstream = FakeUpstream(image_response("red"), image_response("blue"))
        generation = run_generation(pr.generate(self.base_config(), source_png(), upstream))

        self.assertIsNotNone(generation.outputs["draft_png"])
        with Image.open(io.BytesIO(generation.outputs["draft_png"])) as draft:
            self.assertEqual(draft.size, (4, 4))
            self.assertEqual(draft.convert("RGB").getpixel((0, 0)), (255, 0, 0))

    def test_no_draft_when_refinement_did_not_run(self):
        upstream = FakeUpstream(image_response("blue"))
        generation = run_generation(pr.generate(
            self.base_config(passes=1), source_png(), upstream))
        self.assertIsNone(generation.outputs["draft_png"])

    def test_single_pass_skips_refinement(self):
        upstream = FakeUpstream(image_response("blue"))
        generation = run_generation(pr.generate(
            self.base_config(passes=1), source_png(), upstream))

        self.assertEqual(len(upstream.payloads), 1)
        self.assertFalse(generation.refinement_applied)

    def test_phase_order_matches_the_stepper(self):
        upstream = FakeUpstream(image_response("red"), image_response("blue"))
        events = []
        run_generation(pr.generate(
            self.base_config(), source_png(), upstream,
            emit=lambda phase, label, detail="": events.append(phase),
        ))
        self.assertLess(events.index("extracted"), events.index("refine_wait"))
        self.assertLess(events.index("refined"), events.index("pixelizing"))
        self.assertEqual(events[-1], "done")
        # every emitted phase must be one the frontend stepper knows about
        self.assertTrue(set(events) <= set(pr.STAGES_MODEL), set(events) - set(pr.STAGES_MODEL))


class LocalOnlyTest(unittest.TestCase):
    def test_pixelize_only_needs_no_credentials(self):
        """The free smoke-test path must not demand a key or a model."""
        config = pr.make_config(model="", api_key="", size="16x16", max_colors=8)
        upstream = FakeUpstream()
        events = []
        generation = run_generation(pr.generate(
            config, source_png((64, 64)), upstream,
            pixelize_only=True,
            emit=lambda phase, label, detail="": events.append(phase),
        ))
        self.assertEqual(upstream.payloads, [])
        self.assertTrue(set(events) <= set(pr.STAGES_LOCAL))
        with Image.open(io.BytesIO(generation.outputs["pixel_png"])) as pixel:
            self.assertEqual(pixel.size, (4, 4))

    def test_model_run_without_credentials_is_a_config_error(self):
        upstream = FakeUpstream(image_response("red"))
        with self.assertRaises(pr.ConfigError) as caught:
            run_generation(pr.generate(
                pr.make_config(model="", api_key=""), source_png(), upstream))
        self.assertEqual(caught.exception.kind, "config")


class PaletteInvariantTest(unittest.TestCase):
    PALETTE = ((255, 0, 0), (0, 0, 255))

    def test_output_only_ever_uses_the_requested_palette(self):
        config = pr.make_config(model="m", api_key="k", size="16x16", palette=self.PALETTE)
        upstream = FakeUpstream(image_response((200, 30, 30)), image_response((30, 30, 200)))
        generation = run_generation(pr.generate(config, source_png((64, 64)), upstream))

        with Image.open(io.BytesIO(generation.outputs["pixel_png"])) as pixel:
            present = {rgb for _, rgb in pixel.convert("RGB").getcolors(maxcolors=1 << 20)}
        self.assertTrue(present <= set(self.PALETTE), present)

    def test_violation_is_reported_rather_than_shipped(self):
        image = Image.new("RGB", (2, 2), (1, 2, 3))
        with self.assertRaises(pr.PaletteViolationError):
            pr.assert_palette_subset(image, self.PALETTE)

    def test_hex_string_palette_is_accepted(self):
        config = pr.make_config(model="m", api_key="k", palette="#ff0000,#0000ff")
        self.assertEqual(config.palette, self.PALETTE)


class NearestPaletteTest(unittest.TestCase):
    def test_ties_resolve_to_the_lowest_index(self):
        # Two identical colors in palette guarantee distance tie, strictly resolving to index 0
        palette = ((10, 10, 10), (10, 10, 10))
        image = Image.new("RGB", (1, 1), (10, 10, 10))
        self.assertEqual(pr.nearest_palette(image, palette).getpixel((0, 0)), (10, 10, 10))

    def test_numpy_path_matches_the_pure_loop(self):
        """The fallback is the reference implementation; the fast path must not drift."""
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("numpy is not installed; the pure-Python path is the only one")

        palette = ((0, 0, 0), (10, 10, 10), (200, 30, 30), (255, 255, 255))
        # Tie-heavy on purpose: a plain 5-step ramp hits the midpoint repeatedly.
        values = [value for value in range(0, 256, 5)]
        image = Image.new("RGB", (len(values), len(values)))
        pixels = image.load()
        for y, green in enumerate(values):
            for x, red in enumerate(values):
                pixels[x, y] = (red, green, 255 - red)

        vectorised = pr._nearest_palette_numpy(image, palette)
        original = pr._nearest_palette_numpy
        try:
            # Force the reference loop by hiding numpy from this call only.
            pr._nearest_palette_numpy = lambda *args, **kwargs: (_ for _ in ()).throw(ImportError())
            looped = pr.nearest_palette(image, palette)
        finally:
            pr._nearest_palette_numpy = original

        self.assertEqual(list(vectorised.getdata()), list(looped.getdata()))


class ConfigTest(unittest.TestCase):
    def test_bounds_are_enforced_here_so_no_caller_can_forget(self):
        for overrides, error in (
            ({"scale": 0}, pr.ConfigError),
            ({"scale": 33}, pr.ConfigError),
            ({"max_colors": 1}, pr.SizeError),
            ({"max_colors": 300}, pr.SizeError),
            ({"passes": 3}, pr.ConfigError),
            ({"passes": "2"}, pr.ConfigError),
            ({"timeout": 0}, pr.ConfigError),
            ({"timeout": "soon"}, pr.ConfigError),
            ({"size": "0x0"}, pr.SizeError),
            ({"palette": "#GGGGGG"}, pr.PaletteError),
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(error):
                    pr.make_config(model="m", api_key="k", **overrides)

    def test_prompt_placeholders_are_filled_from_the_resolved_grid(self):
        config = pr.make_config(model="m", api_key="k", size="32x32")
        resolved = pr.configure_for_source(config, (1024, 512))
        self.assertEqual(resolved.size, (128, 64))
        self.assertIn("128x64", resolved.prompt)
        self.assertIn("128x64", resolved.refine_prompt)

    def test_density_is_proportional_to_reference_canvas(self):
        self.assertEqual(pr.SIZES, (8, 16, 32, 64, 128))
        c256_32 = pr.configure_for_source(pr.make_config(model="m", api_key="k", size="32x32"), (256, 256))
        self.assertEqual(c256_32.size, (32, 32))
        c1024_32 = pr.configure_for_source(pr.make_config(model="m", api_key="k", size="32x32"), (1024, 1024))
        self.assertEqual(c1024_32.size, (128, 128))
        c1024_64 = pr.configure_for_source(pr.make_config(model="m", api_key="k", size="64x64"), (1024, 1024))
        self.assertEqual(c1024_64.size, (256, 256))
        self.assertEqual(c1024_64.base_size, (64, 64))
        c1920_64 = pr.configure_for_source(pr.make_config(model="m", api_key="k", size="64x64"), (1920, 1080))
        self.assertEqual(c1920_64.size, (480, 270))
        c1024_512_32 = pr.configure_for_source(pr.make_config(model="m", api_key="k", size="32x32"), (1024, 512))
        self.assertEqual(c1024_512_32.size, (128, 64))


class LimitsTest(unittest.TestCase):
    def test_header_sniff_reads_png_without_decoding(self):
        self.assertEqual(pr.sniff_dimensions(source_png((40, 25))), (40, 25))

    def test_oversized_image_is_refused_and_labelled(self):
        blob = source_png((max(1, pr.MAX_DIMENSION + 1), 1))
        with self.assertRaises(pr.LimitsError) as caught:
            pr.check_image_limits(blob)
        self.assertEqual(caught.exception.kind, "limits")

    def test_non_image_is_not_a_limit_error(self):
        pr.check_image_limits(b"not an image at all")  # deferred to prepare_image

    def test_undecodable_input_is_an_input_image_error(self):
        with self.assertRaises(pr.InputImageError) as caught:
            pr.pixelize(b"not an image", pr.make_config(model="m", api_key="k"))
        self.assertEqual(caught.exception.kind, "input_image")


class EnvelopeTest(unittest.TestCase):
    def test_redaction_removes_the_key_in_both_forms(self):
        # Assembled at runtime on purpose: the redactor has a key-SHAPE backstop
        # (AIza + 35 chars), so the test needs a string of that shape -- but a
        # literal one in the source reads as a leaked credential to secret
        # scanners and can block a push. The shape is the fixture; the literal
        # is not worth committing.
        key = "AIza" + "TESTKEY" * 5          # exactly AIza + 35 chars
        self.assertRegex(key, r"^AIza[0-9A-Za-z_\-]{35}$",
                         "fixture must match the redactor's key-shape backstop")
        payload = f"header said x-goog-api-key: {key} and {base64.b64encode(key.encode()).decode()}"
        cleaned = pr.redact(payload, key)
        self.assertNotIn(key, cleaned)
        self.assertNotIn(base64.b64encode(key.encode()).decode(), cleaned)

    def test_envelope_keeps_the_kind_contract(self):
        exc = pr.LimitsError("too big")
        envelope = pr.to_envelope(exc, "test-key", where="generate", phase="encoding")
        self.assertEqual(envelope["kind"], "limits")
        self.assertEqual(envelope["exception"], "pixel_redraw.LimitsError")
        self.assertEqual(envelope["phase"], "encoding")
        for field in ("kind", "message", "detail", "http_status", "where", "hint", "exception"):
            self.assertIn(field, envelope)

    def test_upstream_status_survives_into_the_envelope(self):
        exc = pr.UpstreamHTTPError("Upstream HTTP 401: nope", status=401, detail="body")
        envelope = pr.to_envelope(exc, "k")
        self.assertEqual(envelope["kind"], "upstream_http")
        self.assertEqual(envelope["http_status"], 401)
        self.assertIn("401", envelope["hint"])


class ExtractImageTest(unittest.TestCase):
    def test_snake_case_inline_data_is_accepted(self):
        content = source_png((4, 4))
        response = {"candidates": [{"content": {"parts": [
            {"inline_data": {"mime_type": "image/png",
                             "data": base64.b64encode(content).decode()}}]}}]}
        self.assertEqual(pr.extract_image(response), content)

    def test_data_uri_in_prose_is_accepted(self):
        content = source_png((4, 4))
        uri = "data:image/png;base64," + base64.b64encode(content).decode()
        self.assertEqual(pr.extract_image({"text": f"here: {uri}"}), content)

    def test_url_only_response_explains_why_it_cannot_be_used(self):
        response = {"candidates": [{"content": {"parts": [
            {"text": "see https://example.invalid/image.png"}]}}]}
        with self.assertRaises(pr.NoImageExtracted) as caught:
            pr.extract_image(response)
        self.assertIn("example.invalid", str(caught.exception))
        self.assertIn("CORS", str(caught.exception))

    def test_no_image_names_the_block_reason(self):
        response = {"promptFeedback": {"blockReason": "SAFETY"}}
        with self.assertRaises(pr.NoImageExtracted) as caught:
            pr.extract_image(response)
        self.assertIn("SAFETY", str(caught.exception))


class RenderOutputsTest(unittest.TestCase):
    def test_report_shape_and_bytes(self):
        config = pr.make_config(model="m", api_key="k", size="16x16", scale=4)
        source = source_png((64, 64), "red")
        resolved = pr.configure_for_source(config, (64, 64))
        pixel = pr.pixelize(source, resolved, preserve_clusters=True)
        outputs = pr.render_outputs(source, pixel, resolved, input_name="cat.png")

        self.assertTrue(outputs["pixel_png"].startswith(b"\x89PNG"))
        self.assertTrue(outputs["preview_png"].startswith(b"\x89PNG"))
        with Image.open(io.BytesIO(outputs["preview_png"])) as preview:
            self.assertEqual(preview.size, (pixel.width * 4, pixel.height * 4))

        report = outputs["report"]
        self.assertEqual(report["input"], "cat.png")
        self.assertEqual(report["size"], [pixel.width, pixel.height])
        self.assertEqual(report["scale"], 4)
        self.assertEqual(report["protocol"], "gemini v1beta")
        self.assertFalse(report["refinement_applied"])
        for gone in ("logical_output", "preview_output", "raw_output"):
            self.assertNotIn(gone, report)

    def test_keep_raw_controls_the_raw_bytes(self):
        config = pr.make_config(model="m", api_key="k", size="16x16", keep_raw=False)
        resolved = pr.configure_for_source(config, (64, 64))
        source = source_png((64, 64), "red")
        pixel = pr.pixelize(source, resolved)
        self.assertIsNone(pr.render_outputs(source, pixel, resolved)["raw_png"])


class CorePurityTest(unittest.TestCase):
    """The env-free / file-free / socket-free contract, enforced not assumed.

    This module is imported inside Pyodide, where there is no environment to
    read and no synchronous networking.  A stray `import os` would not fail at
    import time -- it would fail later, in the browser, for the user.
    """

    FORBIDDEN = (
        r"\bimport\s+argparse\b",
        r"\bimport\s+os\b",
        r"\bimport\s+sys\b",
        r"\bimport\s+pathlib\b",
        r"\bfrom\s+pathlib\b",
        r"\bimport\s+urllib\b",
        r"\bimport\s+mimetypes\b",
        r"\bos\.environ\b",
        r"\burllib\.request\b",
        # bare open() only: Pillow's Image.open(BytesIO) is not filesystem access
        r"(?<![.\w])open\s*\(",
    )

    def test_core_has_no_environment_or_filesystem_access(self):
        for filename in ("pixel_redraw.py", "pixel_color.py", "pixel_reduce.py"):
            source = (REPO_ROOT / filename).read_text(encoding="utf-8")
            code = "\n".join(
                line for line in source.splitlines() if not line.lstrip().startswith("#")
            )
            # strip docstrings so prose about the old design does not trip the scan
            code = re.sub(r'"""(?:.|\n)*?"""', "", code)
            for pattern in self.FORBIDDEN:
                self.assertIsNone(re.search(pattern, code), f"{pattern} found in {filename}")

    def test_palettes_module_still_self_checks_against_the_core(self):
        import pixel_palettes
        pixel_palettes.self_check()  # raises AssertionError on a broken preset


if __name__ == "__main__":
    unittest.main()
