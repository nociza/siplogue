# Configuration

Siplogue uses one trusted JSON file to describe a Git-backed static site. Store it outside the website repository. Set `SIPLOGUE_CONFIG` to its absolute path or pass `--config` on each `doctor` and `publish` command.

```json
{
  "schema_version": 1,
  "site": {
    "repository": "/srv/website",
    "collection_file": "public/data/sips.json",
    "media_dir": "public/images/sips",
    "public_media_prefix": "/images/sips",
    "entry_url_template": "https://example.com/sips/{slug}",
    "setup_collection_file": "public/data/brew-setups.json",
    "setup_media_dir": "public/images/brew-setups",
    "public_setup_media_prefix": "/images/brew-setups",
    "setup_url_template": "https://example.com/sips/setups/{slug}",
    "current_ttl_days": 14,
    "validate_command": ["npm", "test"]
  },
  "git": {
    "commit": true,
    "push": true,
    "sync_before_publish": true,
    "require_clean_worktree": true,
    "remote": "origin",
    "branch": "main"
  }
}
```

## Fields

- `site.repository`: absolute path to the root of an existing Git worktree.
- `site.collection_file`: repository-relative JSON file. It is created as an array when absent; an existing file must contain an array.
- `site.media_dir`: repository-relative directory for sanitized public images.
- `site.public_media_prefix`: root-relative URL corresponding to `media_dir`.
- `site.entry_url_template`: absolute HTTP(S) URL. It may contain `{slug}` and `{id}`.
- `site.setup_collection_file`: repository-relative JSON array containing public brew setups.
- `site.setup_media_dir`: repository-relative directory for sanitized setup photographs.
- `site.public_setup_media_prefix`: root-relative URL corresponding to `setup_media_dir`.
- `site.setup_url_template`: absolute HTTP(S) setup URL. It may contain `{slug}` and `{id}`.
- `site.current_ttl_days`: optional current-drinking window from 1 to 365 days. Defaults to 14.
- `site.validate_command`: optional argv-style command array executed from the repository root after files are written and before commit. No shell interpolation occurs.
- `git.commit`: normally `true`. Set `false` only for a separate reviewed commit workflow.
- `git.push`: set `true` for one-message automatic publishing. It requires `git.commit: true`.
- `git.sync_before_publish`: set `true` for a long-lived production checkout. Siplogue fetches and fast-forwards the configured branch before it writes; divergence fails closed.
- `git.require_clean_worktree`: keep `true` in production so a publication cannot absorb or validate against unrelated changes.
- `git.remote` and `git.branch`: push destination. Credentials belong in the host's Git credential or SSH setup, never in this file.

The four setup fields are optional as a group for older Sip-only sites. Configure all four to enable the brew-setup shelf; partial setup configuration fails `doctor`.

Run `doctor` after configuration and after moving the website checkout. A healthy automatic setup reports `"ok": true`, an empty `git_status`, the intended sip and setup paths, and both URL templates.

## Private state

The default is `$XDG_STATE_HOME/siplogue`, or `~/.local/state/siplogue` when `XDG_STATE_HOME` is unset. Override it with `SIPLOGUE_STATE_DIR` or the global `--state-dir` option. The state directory must not be inside the site repository or any directory served by the web server.

Back up the private state if retaining originals matters. Git backs up the published content, not the raw intake ledger.
