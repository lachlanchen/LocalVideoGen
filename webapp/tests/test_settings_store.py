from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from webapp.settings_store import (
    SettingsStore,
    SettingsStoreError,
    SettingsStoreValidationError,
)


class SettingsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.private = Path(self.temporary.name) / "private"
        self.path = self.private / "h3-studio-settings.json"
        self.store = SettingsStore(self.path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_defaults_and_private_atomic_persistence(self):
        settings = self.store.get()
        self.assertEqual(settings["system_prompt"], "")
        self.assertEqual(settings["preferred_duration"], 2.0)
        self.assertEqual(stat.S_IMODE(self.private.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

        saved = self.store.update(
            system_prompt="Never add subtitles.\nKeep motion calm.",
            preferred_duration=7.5,
        )
        reopened = SettingsStore(self.path).get()
        self.assertEqual(reopened, saved)
        self.assertEqual(reopened["preferred_duration"], 7.5)

    def test_removed_settings_file_returns_to_safe_defaults(self):
        self.store.update(system_prompt="Temporary rule", preferred_duration=5)
        self.path.unlink()
        self.assertEqual(self.store.get()["system_prompt"], "")
        self.assertEqual(self.store.get()["preferred_duration"], 2.0)

    def test_invalid_updates_are_rejected(self):
        for duration in (1.5, 15.5, 2.25, float("nan"), True, "many"):
            with self.subTest(duration=duration):
                with self.assertRaises(SettingsStoreValidationError):
                    self.store.update(preferred_duration=duration)
        with self.assertRaises(SettingsStoreValidationError):
            self.store.update(system_prompt="x" * 2001)

    def test_corrupt_file_and_symlink_fail_closed(self):
        self.path.write_text("{", encoding="utf-8")
        with self.assertRaises(SettingsStoreError):
            self.store.get()

        self.path.unlink()
        target = self.private / "target.json"
        target.write_text(json.dumps({"secret": True}), encoding="utf-8")
        os.symlink(target, self.path)
        with self.assertRaises(SettingsStoreError):
            SettingsStore(self.path)


if __name__ == "__main__":
    unittest.main()
