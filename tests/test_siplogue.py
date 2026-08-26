from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY / "scripts" / "siplogue.py"


def jpeg_with_exif() -> bytes:
    exif = b"Exif\x00\x00GPS=37.7749,-122.4194"
    app1 = b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif
    comment = b"owner name"
    com = b"\xff\xfe" + struct.pack(">H", len(comment) + 2) + comment
    scan = b"\xff\xda\x00\x02\x01\x02\x03\xff\xd9"
    return b"\xff\xd8" + app1 + com + scan


class SiplogueCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.site = self.root / "site"
        self.state = self.root / "state"
        self.site.mkdir()
        self.run_git("init", "-q")
        self.run_git("config", "user.name", "Siplogue Test")
        self.run_git("config", "user.email", "siplogue@example.invalid")
        (self.site / "public" / "data").mkdir(parents=True)
        (self.site / "public" / "data" / "sips.json").write_text("[]\n", encoding="utf-8")
        (self.site / "README.md").write_text("test site\n", encoding="utf-8")
        self.run_git("add", ".")
        self.run_git("commit", "-qm", "Initial site")

        self.config = self.root / "siplogue.json"
        self.config.write_text(json.dumps({
            "schema_version": 1,
            "site": {
                "repository": str(self.site),
                "collection_file": "public/data/sips.json",
                "media_dir": "public/images/sips",
                "public_media_prefix": "/images/sips",
                "entry_url_template": "https://example.com/sips/{slug}",
                "validate_command": [sys.executable, "-c", "import json; json.load(open('public/data/sips.json'))"],
            },
            "git": {
                "commit": True,
                "push": False,
                "require_clean_worktree": True,
                "remote": "origin",
                "branch": "main",
            },
        }), encoding="utf-8")
        self.media = self.root / "photo.jpg"
        self.media.write_bytes(jpeg_with_exif())
        self.notes = self.root / "notes.txt"
        self.raw_notes = "Bright jasmine and peach. Sweeter as it cooled. Brewed at 92 C."
        self.notes.write_text(self.raw_notes, encoding="utf-8")
        self.source = self.root / "source.json"
        self.source.write_text(json.dumps({"channel": "telegram", "channel_id": "me", "message_id": "42"}), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_git(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *arguments], cwd=self.site, check=True, text=True, capture_output=True)

    def run_cli(self, *arguments: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--state-dir", str(self.state), *arguments],
            check=False,
            text=True,
            capture_output=True,
        )
        self.assertEqual(expected, result.returncode, msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        return result

    def capture(self, entry_id: str | None = None) -> dict[str, object]:
        arguments = [
            "capture",
            "--media", str(self.media),
            "--notes-file", str(self.notes),
            "--source-json", str(self.source),
            "--captured-at", "2026-08-26T15:30:00Z",
        ]
        if entry_id:
            arguments.extend(["--entry-id", entry_id])
        return json.loads(self.run_cli(*arguments).stdout)

    def polished_payload(self, body: str | None = None) -> Path:
        payload = self.root / f"payload-{len(list(self.root.glob('payload-*')))}.json"
        payload.write_text(json.dumps({
            "title": "Jasmine After the First Pour",
            "kind": "tea",
            "excerpt": "A floral cup that traded its early brightness for a lingering peach sweetness.",
            "body": body or (
                "The first pour opened with a clean jasmine lift and a ripe-peach edge. "
                "As the cup cooled, the sweetness settled in and the fruit became rounder. "
                "At 92 °C, it felt expressive without turning sharp."
            ),
            "alt_text": "A brewed cup of tea beside its leaves in soft afternoon light.",
            "observed_at": "2026-08-26T15:30:00Z",
            "subject": {"name": "Afternoon tea", "style": "floral"},
            "brew": {"temperature_c": 92},
            "tasting_notes": ["jasmine", "peach", "honeyed finish"],
            "rating": 8.5,
            "tags": ["tea", "tasting-note"],
        }), encoding="utf-8")
        return payload

    def test_capture_publish_and_bookkeep_without_database(self) -> None:
        capture = self.capture()
        entry_id = str(capture["entry_id"])
        duplicate = self.capture()
        self.assertEqual(entry_id, duplicate["entry_id"])
        self.assertTrue(duplicate["idempotent"])

        receipt_path = Path(str(capture["receipt"]))
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(receipt_path.stat().st_mode), 0o600)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(self.raw_notes, receipt["intake"]["raw_notes"])

        result = json.loads(self.run_cli(
            "publish",
            "--config", str(self.config),
            "--entry-id", entry_id,
            "--payload", str(self.polished_payload()),
        ).stdout)
        self.assertEqual("committed", result["status"])

        collection = json.loads((self.site / "public" / "data" / "sips.json").read_text(encoding="utf-8"))
        self.assertEqual(1, len(collection))
        public_dump = json.dumps(collection)
        self.assertNotIn(self.raw_notes, public_dump)
        self.assertNotIn("raw_notes", public_dump)
        self.assertEqual("tea", collection[0]["kind"])
        public_media = self.site / "public" / collection[0]["image"]["src"].lstrip("/")
        self.assertTrue(public_media.exists())
        self.assertNotIn(b"Exif", public_media.read_bytes())
        self.assertNotIn(b"GPS", public_media.read_bytes())
        self.assertEqual("", self.run_git("status", "--porcelain").stdout)
        self.assertTrue(self.run_git("log", "-1", "--pretty=%s").stdout.startswith("Publish sip:"))

        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual("committed", receipt["status"])
        self.assertEqual(self.raw_notes, receipt["intake"]["raw_notes"])
        self.assertTrue(receipt["publication"]["commit_sha"])
        self.assertEqual("https://example.com/sips/2026-08-26-jasmine-after-the-first-pour", receipt["publication"]["url"])
        events = (self.state / "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(["captured", "committed"], [json.loads(line)["event"] for line in events])

    def test_verbatim_public_body_is_rejected_and_site_is_untouched(self) -> None:
        entry_id = str(self.capture("verbatim-test")["entry_id"])
        result = self.run_cli(
            "publish",
            "--config", str(self.config),
            "--entry-id", entry_id,
            "--payload", str(self.polished_payload(body=self.raw_notes)),
            expected=2,
        )
        self.assertIn("appears verbatim", result.stderr)
        self.assertEqual([], json.loads((self.site / "public" / "data" / "sips.json").read_text(encoding="utf-8")))
        self.assertEqual("", self.run_git("status", "--porcelain").stdout)

    def test_dirty_site_is_rejected(self) -> None:
        entry_id = str(self.capture("dirty-test")["entry_id"])
        (self.site / "README.md").write_text("local edit\n", encoding="utf-8")
        result = self.run_cli(
            "publish",
            "--config", str(self.config),
            "--entry-id", entry_id,
            "--payload", str(self.polished_payload()),
            expected=2,
        )
        self.assertIn("worktree is not clean", result.stderr)
        self.assertEqual([], json.loads((self.site / "public" / "data" / "sips.json").read_text(encoding="utf-8")))

    def test_automatic_push_and_rerun_are_idempotent(self) -> None:
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True, text=True, capture_output=True)
        self.run_git("remote", "add", "origin", str(remote))
        config = json.loads(self.config.read_text(encoding="utf-8"))
        config["git"]["push"] = True
        self.config.write_text(json.dumps(config), encoding="utf-8")
        entry_id = str(self.capture("automatic-test")["entry_id"])
        payload = self.polished_payload()

        first = json.loads(self.run_cli(
            "publish",
            "--config", str(self.config),
            "--entry-id", entry_id,
            "--payload", str(payload),
        ).stdout)
        self.assertEqual("published", first["status"])
        remote_head = subprocess.run(
            ["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        self.assertEqual(first["publication"]["commit_sha"], remote_head)

        second = json.loads(self.run_cli(
            "publish",
            "--config", str(self.config),
            "--entry-id", entry_id,
            "--payload", str(payload),
        ).stdout)
        self.assertEqual("published", second["status"])
        self.assertTrue(second["idempotent"])
        collection = json.loads((self.site / "public" / "data" / "sips.json").read_text(encoding="utf-8"))
        self.assertEqual(1, len(collection))


if __name__ == "__main__":
    unittest.main()
