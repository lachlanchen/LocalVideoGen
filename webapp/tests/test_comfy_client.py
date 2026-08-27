from __future__ import annotations

import unittest
import uuid
from unittest.mock import AsyncMock

from webapp.comfy_client import (
    ComfyClient,
    ComfyError,
    flatten_outputs,
    safe_input_path,
    validate_loopback_url,
)


class ClientSafetyTests(unittest.TestCase):
    def test_only_plain_loopback_base_urls_are_accepted(self) -> None:
        self.assertEqual(validate_loopback_url("http://127.0.0.1:8188"), "http://127.0.0.1:8188")
        self.assertEqual(validate_loopback_url("http://[::1]:8188"), "http://[::1]:8188")
        for value in (
            "http://192.168.1.2:8188",
            "http://example.com",
            "file:///tmp/socket",
            "http://user:pass@127.0.0.1:8188",
            "http://127.0.0.1:8188/view",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_loopback_url(value)

    def test_upload_paths_and_output_locations_are_allowlisted(self) -> None:
        self.assertEqual(safe_input_path("frame.png", "h3/image", "input"), "h3/image/frame.png")
        for name, folder, kind in (
            ("../frame.png", "", "input"),
            ("frame.png", "../outside", "input"),
            ("frame.png", "", "output"),
        ):
            with self.subTest(name=name, folder=folder, kind=kind), self.assertRaises(ComfyError):
                safe_input_path(name, folder, kind)

        outputs = flatten_outputs({
            "outputs": {
                "42": {
                    "images": [
                        {"filename": "movie.mp4", "subfolder": "h3", "type": "output"},
                        {"filename": "../secret", "subfolder": "", "type": "output"},
                    ]
                }
            }
        })
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0]["media_type"], "video")


class ClientSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_identifier_cannot_change_after_ownership_claim(self) -> None:
        client = ComfyClient("http://127.0.0.1:8188")
        claimed = str(uuid.uuid4())
        client._json = AsyncMock(return_value={"prompt_id": str(uuid.uuid4())})  # type: ignore[method-assign]
        with self.assertRaisesRegex(ComfyError, "different job identifier"):
            await client.submit({}, {"mode": "t2v"}, claimed)

    async def test_early_progress_is_not_overwritten_by_queued_state(self) -> None:
        client = ComfyClient("http://127.0.0.1:8188")
        claimed = str(uuid.uuid4())
        client._record_event({
            "type": "progress",
            "data": {"prompt_id": claimed, "node": "42", "value": 3, "max": 10},
        })
        client._json = AsyncMock(return_value={"prompt_id": claimed, "number": 1})  # type: ignore[method-assign]
        await client.submit({}, {"mode": "t2v"}, claimed)
        self.assertEqual(client.job_progress(claimed)["phase"], "sampling")
        self.assertEqual(client.job_progress(claimed)["percent"], 30.0)


if __name__ == "__main__":
    unittest.main()
