#!/usr/bin/env python3
"""Private intake ledger and Git-backed publisher for the Siplogue skill."""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
from typing import Any


SCHEMA_VERSION = 1
MAX_NOTES_CHARS = 20_000
MAX_MEDIA_BYTES = 50 * 1024 * 1024
ALLOWED_KINDS = {"coffee", "tea"}


class SiplogueError(RuntimeError):
    """An expected, user-actionable workflow failure."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_state_dir() -> Path:
    configured = os.environ.get("SIPLOGUE_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home).expanduser() / "siplogue"
    return Path.home() / ".local" / "state" / "siplogue"


def json_output(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


def load_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SiplogueError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SiplogueError(f"{label} is not valid JSON: {path}: {exc}") from exc


def atomic_write_json(path: Path, value: Any, mode: int = 0o600, parent_mode: int = 0o700) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=parent_mode)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def ensure_state_dir(path: Path) -> Path:
    path = path.expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    for child in (path / "receipts", path / "originals"):
        child.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(child, 0o700)
    return path


class StateLock:
    def __init__(self, state_dir: Path):
        self.state_dir = ensure_state_dir(state_dir)
        self.handle: Any = None

    def __enter__(self) -> Path:
        lock_path = self.state_dir / ".lock"
        self.handle = lock_path.open("a+", encoding="utf-8")
        os.chmod(lock_path, 0o600)
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self.state_dir

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.handle:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def append_event(state_dir: Path, entry_id: str, event: str, **details: Any) -> None:
    record = {
        "at": utc_now(),
        "entry_id": entry_id,
        "event": event,
        "details": details,
        "schema_version": SCHEMA_VERSION,
    }
    ledger = state_dir / "events.jsonl"
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    fd = os.open(ledger, flags, 0o600)
    try:
        os.write(fd, (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(ledger, 0o600)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())


def validate_iso_datetime(value: str, label: str) -> str:
    candidate = value.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise SiplogueError(f"{label} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise SiplogueError(f"{label} must include a timezone")
    return value


def read_notes(path_value: str) -> str:
    if path_value == "-":
        notes = sys.stdin.read()
    else:
        notes = Path(path_value).expanduser().read_text(encoding="utf-8")
    notes = notes.strip()
    if not notes:
        raise SiplogueError("raw notes cannot be empty")
    if len(notes) > MAX_NOTES_CHARS:
        raise SiplogueError(f"raw notes exceed {MAX_NOTES_CHARS} characters")
    return notes


def receipt_path(state_dir: Path, entry_id: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", entry_id):
        raise SiplogueError("entry ID contains unsafe characters")
    return state_dir / "receipts" / f"{entry_id}.json"


def derive_entry_id(captured_at: str, source: dict[str, Any], media_sha256: str, notes: str) -> str:
    timestamp = dt.datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    day = timestamp.astimezone(dt.timezone.utc).strftime("%Y%m%d")
    message_id = str(source.get("message_id", "")).strip()
    channel_id = str(source.get("channel_id", source.get("channel", ""))).strip()
    if message_id:
        seed = f"{channel_id}\0{message_id}\0{media_sha256}".encode("utf-8")
        suffix = hashlib.sha256(seed).hexdigest()[:12]
    else:
        seed = f"{media_sha256}\0{normalized_text(notes)}".encode("utf-8")
        suffix = hashlib.sha256(seed).hexdigest()[:8] + uuid.uuid4().hex[:4]
    return f"{day}-{suffix}"


def command_capture(args: argparse.Namespace) -> None:
    media = Path(args.media).expanduser().resolve()
    if not media.is_file():
        raise SiplogueError(f"media file not found: {media}")
    if media.stat().st_size > MAX_MEDIA_BYTES:
        raise SiplogueError(f"media exceeds the {MAX_MEDIA_BYTES // (1024 * 1024)} MiB limit")
    notes = read_notes(args.notes_file)
    source: dict[str, Any] = {}
    if args.source_json:
        source_value = load_json(Path(args.source_json).expanduser(), "source metadata")
        if not isinstance(source_value, dict):
            raise SiplogueError("source metadata must be a JSON object")
        source = source_value
    captured_at = validate_iso_datetime(args.captured_at, "captured_at") if args.captured_at else utc_now()
    media_sha256 = sha256_file(media)
    entry_id = args.entry_id or derive_entry_id(captured_at, source, media_sha256, notes)

    with StateLock(Path(args.state_dir)) as state_dir:
        target_receipt = receipt_path(state_dir, entry_id)
        if target_receipt.exists():
            existing = load_json(target_receipt, "receipt")
            same = (
                existing.get("intake", {}).get("raw_notes") == notes
                and existing.get("intake", {}).get("media", {}).get("sha256") == media_sha256
            )
            if not same:
                raise SiplogueError(f"entry ID collision with different intake: {entry_id}")
            json_output({"entry_id": entry_id, "receipt": str(target_receipt), "status": existing["status"], "idempotent": True})
            return

        suffix = media.suffix.lower() if re.fullmatch(r"\.[a-zA-Z0-9]{1,10}", media.suffix) else ".bin"
        original = state_dir / "originals" / f"{entry_id}{suffix}"
        shutil.copyfile(media, original)
        os.chmod(original, 0o600)
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "entry_id": entry_id,
            "status": "captured",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "source": source,
            "intake": {
                "captured_at": captured_at,
                "raw_notes": notes,
                "media": {
                    "original_name": media.name,
                    "private_path": str(original),
                    "sha256": media_sha256,
                    "bytes": media.stat().st_size,
                },
            },
            "public_payload": None,
            "publication": None,
            "last_error": None,
        }
        atomic_write_json(target_receipt, receipt)
        append_event(state_dir, entry_id, "captured", media_sha256=media_sha256)
        json_output({"entry_id": entry_id, "receipt": str(target_receipt), "status": "captured", "idempotent": False})


def slugify(value: str) -> str:
    value = value.casefold().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:80]


def bounded_string(value: Any, label: str, maximum: int, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise SiplogueError(f"{label} must be a string")
    value = value.strip()
    if required and not value:
        raise SiplogueError(f"{label} cannot be empty")
    if len(value) > maximum:
        raise SiplogueError(f"{label} exceeds {maximum} characters")
    return value


def sanitize_flat_mapping(value: dict[str, Any], label: str, allowed: set[str]) -> dict[str, Any]:
    unexpected = set(value) - allowed
    if unexpected:
        raise SiplogueError(f"{label} contains unsupported fields: {', '.join(sorted(unexpected))}")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if item is None:
            result[key] = None
        elif isinstance(item, bool):
            raise SiplogueError(f"{label}.{key} cannot be a boolean")
        elif isinstance(item, (int, float)):
            result[key] = item
        elif isinstance(item, str):
            result[key] = bounded_string(item, f"{label}.{key}", 240)
        else:
            raise SiplogueError(f"{label}.{key} must be a string, number, or null")
    return result


def validate_payload(payload: Any, receipt: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SiplogueError("public payload must be a JSON object")
    title = bounded_string(payload.get("title"), "title", 140, required=True)
    kind = bounded_string(payload.get("kind"), "kind", 20, required=True)
    if kind not in ALLOWED_KINDS:
        raise SiplogueError("kind must be either 'tea' or 'coffee'")
    excerpt = bounded_string(payload.get("excerpt"), "excerpt", 320, required=True)
    body = bounded_string(payload.get("body"), "body", 12_000, required=True)
    alt_text = bounded_string(payload.get("alt_text"), "alt_text", 500, required=True)

    raw_notes = receipt["intake"]["raw_notes"]
    raw_normalized = normalized_text(raw_notes)
    body_normalized = normalized_text(body or "")
    similarity = difflib.SequenceMatcher(None, raw_normalized, body_normalized).ratio()
    if raw_normalized == body_normalized or (len(raw_normalized) >= 30 and similarity >= 0.90):
        raise SiplogueError("public body appears verbatim; polish the intake before publishing")

    observed_at = payload.get("observed_at") or receipt["intake"]["captured_at"]
    validate_iso_datetime(observed_at, "observed_at")
    observed_day = dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00")).date().isoformat()
    slug = payload.get("slug") or f"{observed_day}-{slugify(title or '')}"
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise SiplogueError("slug must contain lowercase letters, digits, and single hyphens")
    if len(slug) > 120:
        raise SiplogueError("slug exceeds 120 characters")

    tags = payload.get("tags", [])
    tasting_notes = payload.get("tasting_notes", [])
    for value, label, limit in ((tags, "tags", 20), (tasting_notes, "tasting_notes", 30)):
        if not isinstance(value, list) or len(value) > limit or any(not isinstance(item, str) or not item.strip() for item in value):
            raise SiplogueError(f"{label} must be a list of at most {limit} non-empty strings")
    subject = payload.get("subject", {})
    brew = payload.get("brew", {})
    if not isinstance(subject, dict) or not isinstance(brew, dict):
        raise SiplogueError("subject and brew must be JSON objects")
    subject = sanitize_flat_mapping(
        subject,
        "subject",
        {"name", "producer", "origin", "variety", "process", "style"},
    )
    brew = sanitize_flat_mapping(
        brew,
        "brew",
        {"method", "temperature_c", "dose_g", "water_g", "steep_seconds", "grind", "infusions"},
    )
    rating = payload.get("rating")
    if rating is not None and (isinstance(rating, bool) or not isinstance(rating, (int, float)) or not 0 <= rating <= 10):
        raise SiplogueError("rating must be a number from 0 to 10")

    return {
        "title": title,
        "slug": slug,
        "kind": kind,
        "excerpt": excerpt,
        "body": body,
        "alt_text": alt_text,
        "observed_at": observed_at,
        "subject": subject,
        "brew": brew,
        "tasting_notes": [item.strip() for item in tasting_notes],
        "rating": rating,
        "tags": [item.strip() for item in tags],
    }


def strip_jpeg_metadata(data: bytes) -> bytes:
    if not data.startswith(b"\xff\xd8"):
        raise SiplogueError("invalid JPEG image")
    output = bytearray(data[:2])
    position = 2
    while position < len(data):
        marker_start = position
        if data[position] != 0xFF:
            raise SiplogueError("malformed JPEG marker stream")
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            raise SiplogueError("truncated JPEG marker")
        marker = data[position]
        position += 1
        if marker == 0xDA:  # Start of scan: entropy-coded image data follows.
            output.extend(data[marker_start:])
            break
        if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
            output.extend(data[marker_start:position])
            if marker == 0xD9:
                break
            continue
        if position + 2 > len(data):
            raise SiplogueError("truncated JPEG segment")
        segment_length = struct.unpack(">H", data[position:position + 2])[0]
        if segment_length < 2 or position + segment_length > len(data):
            raise SiplogueError("invalid JPEG segment length")
        segment_end = position + segment_length
        # APP1 (EXIF/XMP), APP13 (IPTC), and comments may carry location or identity.
        if marker not in {0xE1, 0xED, 0xFE}:
            output.extend(data[marker_start:segment_end])
        position = segment_end
    if not output.endswith(b"\xff\xd9"):
        # JPEG scan data normally includes EOI; reject truncation instead of publishing it.
        raise SiplogueError("JPEG is missing its end marker")
    return bytes(output)


def strip_png_metadata(data: bytes) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(signature):
        raise SiplogueError("invalid PNG image")
    output = bytearray(signature)
    position = len(signature)
    saw_iend = False
    private_chunks = {b"eXIf", b"iTXt", b"tEXt", b"zTXt", b"tIME"}
    while position < len(data):
        if position + 12 > len(data):
            raise SiplogueError("truncated PNG chunk")
        chunk_length = struct.unpack(">I", data[position:position + 4])[0]
        chunk_end = position + 12 + chunk_length
        if chunk_end > len(data):
            raise SiplogueError("invalid PNG chunk length")
        chunk_type = data[position + 4:position + 8]
        if chunk_type not in private_chunks:
            output.extend(data[position:chunk_end])
        if chunk_type == b"IEND":
            saw_iend = True
            break
        position = chunk_end
    if not saw_iend:
        raise SiplogueError("PNG is missing its IEND chunk")
    return bytes(output)


def sanitize_image(source: Path, destination_without_suffix: Path) -> Path:
    data = source.read_bytes()
    if data.startswith(b"\xff\xd8"):
        sanitized = strip_jpeg_metadata(data)
        destination = destination_without_suffix.with_suffix(".jpg")
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        sanitized = strip_png_metadata(data)
        destination = destination_without_suffix.with_suffix(".png")
    else:
        raise SiplogueError("only JPEG and PNG are supported; convert HEIC/WebP before publishing")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(sanitized)
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SiplogueError(f"{label} must be a JSON object")
    return value


def safe_repo_path(repo: Path, relative_value: Any, label: str) -> Path:
    if not isinstance(relative_value, str) or not relative_value.strip():
        raise SiplogueError(f"{label} must be a non-empty relative path")
    relative = Path(relative_value)
    if relative.is_absolute():
        raise SiplogueError(f"{label} must be relative to the site repository")
    resolved = (repo / relative).resolve()
    if not resolved.is_relative_to(repo):
        raise SiplogueError(f"{label} escapes the site repository")
    return resolved


def run_checked(command: list[str], cwd: Path, label: str) -> str:
    try:
        result = subprocess.run(command, cwd=cwd, check=False, text=True, capture_output=True)
    except FileNotFoundError as exc:
        raise SiplogueError(f"{label} executable not found: {command[0]}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SiplogueError(f"{label} failed ({result.returncode}): {detail}")
    # Preserve leading spaces: Git porcelain status uses them as column data.
    return result.stdout.rstrip()


def git(repo: Path, *arguments: str, label: str = "git") -> str:
    return run_checked(["git", *arguments], repo, label)


def validated_remote_branch(git_config: dict[str, Any]) -> tuple[str, str]:
    remote = git_config.get("remote", "origin")
    branch = git_config.get("branch", "main")
    if not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._/-]+", value) for value in (remote, branch)):
        raise SiplogueError("git remote or branch contains unsafe characters")
    return remote, branch


def load_config(path: Path) -> dict[str, Any]:
    config = require_mapping(load_json(path, "configuration"), "configuration")
    if config.get("schema_version") != SCHEMA_VERSION:
        raise SiplogueError(f"configuration schema_version must be {SCHEMA_VERSION}")
    return config


def resolve_site(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], Path, Path, Path]:
    site = require_mapping(config.get("site"), "site configuration")
    git_config = require_mapping(config.get("git", {}), "git configuration")
    repository_value = site.get("repository")
    if not isinstance(repository_value, str) or not repository_value.strip():
        raise SiplogueError("site.repository must be configured")
    repository = Path(repository_value).expanduser().resolve()
    if not repository.is_dir():
        raise SiplogueError(f"site repository not found: {repository}")
    top_level = Path(git(repository, "rev-parse", "--show-toplevel", label="repository check")).resolve()
    if top_level != repository:
        raise SiplogueError("site.repository must point to the Git worktree root")
    collection = safe_repo_path(repository, site.get("collection_file"), "site.collection_file")
    media_dir = safe_repo_path(repository, site.get("media_dir"), "site.media_dir")
    return site, git_config, repository, collection, media_dir


def get_worktree_status(repository: Path) -> list[str]:
    output = git(repository, "status", "--porcelain=v1", "--untracked-files=all", label="worktree check")
    return [line for line in output.splitlines() if line]


def restore_file(path: Path, previous: bytes | None) -> None:
    if previous is None:
        if path.exists():
            path.unlink()
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(previous)


def render_entry_url(template: Any, slug: str, entry_id: str) -> str:
    if not isinstance(template, str) or not template.startswith(("https://", "http://")):
        raise SiplogueError("site.entry_url_template must be an http(s) URL")
    try:
        return template.format(slug=slug, id=entry_id)
    except (KeyError, ValueError) as exc:
        raise SiplogueError("site.entry_url_template may use only {slug} and {id}") from exc


def public_payload_hash(entry: dict[str, Any]) -> str:
    encoded = json.dumps(entry, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def command_publish(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config).expanduser())
    site, git_config, repository, collection_path, media_dir = resolve_site(config)
    state_arg = Path(args.state_dir).expanduser().resolve()
    if state_arg.is_relative_to(repository):
        raise SiplogueError("private state directory must not be inside the public site repository")

    with StateLock(state_arg) as state_dir:
        target_receipt = receipt_path(state_dir, args.entry_id)
        receipt = require_mapping(load_json(target_receipt, "receipt"), "receipt")
        if receipt.get("status") == "published":
            json_output({"entry_id": args.entry_id, "status": "published", "publication": receipt.get("publication"), "idempotent": True})
            return
        push_enabled = bool(git_config.get("push", False)) if args.push is None else args.push
        if receipt.get("status") == "committed":
            if push_enabled:
                recorded_commit = (receipt.get("publication") or {}).get("commit_sha")
                current_commit = git(repository, "rev-parse", "HEAD", label="commit lookup")
                if not recorded_commit or current_commit != recorded_commit:
                    raise SiplogueError("site HEAD no longer matches the committed receipt; refusing an ambiguous retry")
                remote, branch = validated_remote_branch(git_config)
                try:
                    git(repository, "push", remote, f"HEAD:{branch}", label="git push")
                except SiplogueError as exc:
                    receipt["last_error"] = str(exc)
                    receipt["updated_at"] = utc_now()
                    atomic_write_json(target_receipt, receipt)
                    append_event(state_dir, args.entry_id, "push_failed", error=str(exc))
                    raise
                receipt["status"] = "published"
                receipt["publication"]["published_at"] = utc_now()
                receipt["updated_at"] = utc_now()
                receipt["last_error"] = None
                atomic_write_json(target_receipt, receipt)
                append_event(
                    state_dir,
                    args.entry_id,
                    "published",
                    commit_sha=receipt["publication"].get("commit_sha"),
                    url=receipt["publication"].get("url"),
                )
            json_output({"entry_id": args.entry_id, "status": receipt["status"], "publication": receipt.get("publication"), "idempotent": True})
            return
        payload_value = load_json(Path(args.payload).expanduser(), "public payload")
        payload = validate_payload(payload_value, receipt)
        original = Path(receipt["intake"]["media"]["private_path"])
        if not original.is_file() or sha256_file(original) != receipt["intake"]["media"]["sha256"]:
            raise SiplogueError("private original is missing or does not match its recorded hash")

        require_clean = git_config.get("require_clean_worktree", True)
        initial_status = get_worktree_status(repository)
        if require_clean and initial_status:
            sample = "\n".join(initial_status[:10])
            raise SiplogueError(f"site worktree is not clean; refusing to mix changes:\n{sample}")
        if git_config.get("sync_before_publish", False):
            remote, branch = validated_remote_branch(git_config)
            git(repository, "fetch", "--prune", remote, branch, label="git fetch")
            git(repository, "merge", "--ff-only", "FETCH_HEAD", label="git fast-forward")
            synchronized_status = get_worktree_status(repository)
            if synchronized_status:
                sample = "\n".join(synchronized_status[:10])
                raise SiplogueError(f"site worktree changed during synchronization:\n{sample}")

        media_stem = media_dir / f"{payload['slug']}-{args.entry_id[-8:]}"
        # The final suffix is selected from the image bytes, not the input filename.
        expected_candidates = [media_stem.with_suffix(".jpg"), media_stem.with_suffix(".png")]
        if any(path.exists() for path in expected_candidates):
            raise SiplogueError("public media destination already exists for this entry")
        media_prefix = site.get("public_media_prefix")
        if not isinstance(media_prefix, str) or not media_prefix.startswith("/"):
            raise SiplogueError("site.public_media_prefix must start with '/'")

        previous_collection = collection_path.read_bytes() if collection_path.exists() else None
        destination: Path | None = None
        committed = False
        try:
            destination = sanitize_image(original, media_stem)
            published_at = utc_now()
            public_entry = {
                "id": args.entry_id,
                "slug": payload["slug"],
                "title": payload["title"],
                "kind": payload["kind"],
                "excerpt": payload["excerpt"],
                "body": payload["body"],
                "image": {
                    "src": f"{media_prefix.rstrip('/')}/{destination.name}",
                    "alt": payload["alt_text"],
                },
                "observedAt": payload["observed_at"],
                "publishedAt": published_at,
                "subject": payload["subject"],
                "brew": payload["brew"],
                "tastingNotes": payload["tasting_notes"],
                "rating": payload["rating"],
                "tags": payload["tags"],
            }
            if collection_path.exists():
                collection = load_json(collection_path, "site collection")
                if not isinstance(collection, list):
                    raise SiplogueError("site collection must be a JSON array")
            else:
                collection = []
            if any(item.get("id") == args.entry_id or item.get("slug") == payload["slug"] for item in collection if isinstance(item, dict)):
                raise SiplogueError("site collection already contains this entry ID or slug")
            collection.insert(0, public_entry)
            atomic_write_json(collection_path, collection, mode=0o644, parent_mode=0o755)

            validate_command = site.get("validate_command", [])
            if validate_command:
                if not isinstance(validate_command, list) or not all(isinstance(part, str) and part for part in validate_command):
                    raise SiplogueError("site.validate_command must be an argv-style string array")
                run_checked(validate_command, repository, "site validation")

            allowed = {str(collection_path.relative_to(repository)), str(destination.relative_to(repository))}
            unexpected = []
            for line in get_worktree_status(repository):
                changed_path = line[3:].split(" -> ")[-1]
                if changed_path not in allowed:
                    unexpected.append(line)
            if require_clean and unexpected:
                raise SiplogueError("site validation changed unexpected files:\n" + "\n".join(unexpected[:10]))

            commit_enabled = bool(git_config.get("commit", True))
            commit_sha = None
            if commit_enabled:
                relative_collection = str(collection_path.relative_to(repository))
                relative_media = str(destination.relative_to(repository))
                git(repository, "add", "--", relative_collection, relative_media, label="git add")
                message_title = re.sub(r"[\r\n]+", " ", payload["title"]).strip()[:72]
                git(repository, "commit", "-m", f"Publish sip: {message_title}", "--", relative_collection, relative_media, label="git commit")
                committed = True
                commit_sha = git(repository, "rev-parse", "HEAD", label="commit lookup")

            url = render_entry_url(site.get("entry_url_template"), payload["slug"], args.entry_id)
            publication = {
                "repository": str(repository),
                "collection_path": str(collection_path.relative_to(repository)),
                "media_path": str(destination.relative_to(repository)),
                "url": url,
                "commit_sha": commit_sha,
                "payload_sha256": public_payload_hash(public_entry),
                "committed_at": utc_now() if commit_enabled else None,
                "published_at": None,
            }
            receipt["public_payload"] = public_entry
            receipt["publication"] = publication
            receipt["status"] = "committed" if commit_enabled else "written"
            receipt["updated_at"] = utc_now()
            receipt["last_error"] = None
            atomic_write_json(target_receipt, receipt)
            append_event(state_dir, args.entry_id, receipt["status"], commit_sha=commit_sha, url=url)

            if push_enabled:
                if not commit_enabled:
                    raise SiplogueError("git.push requires git.commit to be enabled")
                remote, branch = validated_remote_branch(git_config)
                try:
                    git(repository, "push", remote, f"HEAD:{branch}", label="git push")
                except SiplogueError as exc:
                    receipt["last_error"] = str(exc)
                    receipt["updated_at"] = utc_now()
                    atomic_write_json(target_receipt, receipt)
                    append_event(state_dir, args.entry_id, "push_failed", error=str(exc))
                    raise
                receipt["status"] = "published"
                receipt["publication"]["published_at"] = utc_now()
                receipt["updated_at"] = utc_now()
                atomic_write_json(target_receipt, receipt)
                append_event(state_dir, args.entry_id, "published", commit_sha=commit_sha, url=url)

            json_output({"entry_id": args.entry_id, "status": receipt["status"], "publication": receipt["publication"], "idempotent": False})
        except Exception as exc:
            if not committed:
                if destination:
                    relative_collection = str(collection_path.relative_to(repository))
                    relative_media = str(destination.relative_to(repository))
                    subprocess.run(
                        ["git", "restore", "--staged", "--", relative_collection, relative_media],
                        cwd=repository,
                        check=False,
                        text=True,
                        capture_output=True,
                    )
                restore_file(collection_path, previous_collection)
                if destination and destination.exists():
                    destination.unlink()
            receipt["last_error"] = str(exc)
            receipt["updated_at"] = utc_now()
            atomic_write_json(target_receipt, receipt)
            append_event(state_dir, args.entry_id, "publish_failed", error=str(exc))
            raise


def redact_receipt(receipt: dict[str, Any], include_private: bool) -> dict[str, Any]:
    result = json.loads(json.dumps(receipt))
    if not include_private:
        intake = result.get("intake", {})
        if "raw_notes" in intake:
            intake["raw_notes"] = "[redacted; use --include-private]"
        media = intake.get("media", {})
        if "private_path" in media:
            media["private_path"] = "[redacted; use --include-private]"
        if result.get("source"):
            result["source"] = "[redacted; use --include-private]"
    return result


def command_show(args: argparse.Namespace) -> None:
    with StateLock(Path(args.state_dir)) as state_dir:
        receipt = require_mapping(load_json(receipt_path(state_dir, args.entry_id), "receipt"), "receipt")
        json_output(redact_receipt(receipt, args.include_private))


def command_list(args: argparse.Namespace) -> None:
    with StateLock(Path(args.state_dir)) as state_dir:
        items = []
        for path in sorted((state_dir / "receipts").glob("*.json"), reverse=True):
            receipt = load_json(path, "receipt")
            items.append({
                "entry_id": receipt.get("entry_id"),
                "status": receipt.get("status"),
                "created_at": receipt.get("created_at"),
                "updated_at": receipt.get("updated_at"),
                "url": (receipt.get("publication") or {}).get("url"),
            })
        json_output(items)


def command_doctor(args: argparse.Namespace) -> None:
    config = load_config(Path(args.config).expanduser())
    site, git_config, repository, collection, media_dir = resolve_site(config)
    state_dir = ensure_state_dir(Path(args.state_dir))
    problems = []
    if state_dir.is_relative_to(repository):
        problems.append("private state directory is inside the site repository")
    status = get_worktree_status(repository)
    if git_config.get("require_clean_worktree", True) and status:
        problems.append("site worktree is not clean")
    result = {
        "ok": not problems,
        "problems": problems,
        "repository": str(repository),
        "collection": str(collection),
        "media_dir": str(media_dir),
        "state_dir": str(state_dir),
        "git_status": status,
        "entry_url_template": site.get("entry_url_template"),
    }
    json_output(result)
    if problems:
        raise SiplogueError("doctor found configuration problems")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", default=str(default_state_dir()), help="private state directory")
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser("capture", help="store a private copy of an incoming photo and raw notes")
    capture.add_argument("--media", required=True)
    capture.add_argument("--notes-file", required=True, help="UTF-8 file containing raw notes, or - for stdin")
    capture.add_argument("--source-json", help="optional JSON object with channel/message metadata")
    capture.add_argument("--captured-at", help="ISO 8601 timestamp; defaults to now")
    capture.add_argument("--entry-id", help="explicit idempotency key")
    capture.set_defaults(function=command_capture)

    publish = subparsers.add_parser("publish", help="write a polished post to a Git-backed static site")
    publish.add_argument(
        "--config",
        default=os.environ.get("SIPLOGUE_CONFIG"),
        required=not bool(os.environ.get("SIPLOGUE_CONFIG")),
        help="site configuration JSON; defaults to SIPLOGUE_CONFIG",
    )
    publish.add_argument("--entry-id", required=True)
    publish.add_argument("--payload", required=True, help="JSON file containing polished public fields")
    publish.add_argument("--push", action=argparse.BooleanOptionalAction, default=None, help="override git.push")
    publish.set_defaults(function=command_publish)

    show = subparsers.add_parser("show", help="show one receipt; private fields are redacted by default")
    show.add_argument("entry_id")
    show.add_argument("--include-private", action="store_true")
    show.set_defaults(function=command_show)

    list_parser = subparsers.add_parser("list", help="list receipt statuses")
    list_parser.set_defaults(function=command_list)

    doctor = subparsers.add_parser("doctor", help="validate the site and bookkeeping configuration")
    doctor.add_argument(
        "--config",
        default=os.environ.get("SIPLOGUE_CONFIG"),
        required=not bool(os.environ.get("SIPLOGUE_CONFIG")),
        help="site configuration JSON; defaults to SIPLOGUE_CONFIG",
    )
    doctor.set_defaults(function=command_doctor)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.function(args)
        return 0
    except SiplogueError as exc:
        print(f"siplogue: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("siplogue: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
