# Sip

Sip is an open-source agent skill for turning a tea or coffee photograph and a
few rough thoughts into a polished, Git-backed journal entry. It also maintains
a current-drinking rotation and a photographed inventory of the brewing setups,
methods, and equipment available to the user.

No database, CMS, or publishing cron is required. Public content is ordinary
JSON and sanitized media in a static-site repository; original photographs,
captions, receipts, and deduplication records remain private on the agent host.

## What Sip does

- Polishes informal notes instead of publishing a caption verbatim.
- Records when the message was received and keeps source bookkeeping private.
- Makes new coffee current by default for a configurable period, normally 14
  days. Tea is archived by default unless the user says it is current.
- Refreshes an existing drink into a new current window without duplicating the
  journal entry, or archives it explicitly.
- Publishes photographed brew setups with supported methods and an inventory of
  tools or machinery.
- Links a cup to the exact setup used through stable setup IDs.
- Removes common JPEG and PNG metadata before copying media into the public
  repository.
- Validates, commits, and optionally pushes only the intended public files.
- Refuses dirty or divergent worktrees, unsupported media, unsafe paths,
  duplicate records, and near-verbatim public copy.

The accompanying implementation at [nociza.com/sips](https://www.nociza.com/sips/)
shows the current rotation, brew shelf, archive, setup detail pages, and links
between cups and the setups used to make them.

## Data boundaries

| Data | Location | Visibility |
| --- | --- | --- |
| Polished cup entries | Configured `sips.json` | Public Git |
| Brew setups and tool inventory | Configured `brew-setups.json` | Public Git |
| Sanitized cup and setup images | Configured site media directories | Public Git |
| Original images and rough notes | Sip state directory | Private |
| Receipts and append-only events | Sip state directory | Private |
| Credentials and Git configuration | Agent host | Private |

Each cup can include `receivedAt`, an `activity` window, and `setupIds`. A static
site can compare `activity.expiresAt` with the current time to move an expired
cup into its archive without a scheduled data mutation. Setup records keep a
stable ID even when their description, tools, methods, or photograph changes.

## Install

Sip is an AgentSkills-style repository and can be placed directly in an
OpenClaw workspace:

```sh
git clone https://github.com/nociza/siplogue.git \
  "$HOME/.openclaw/workspace/skills/sip"
```

The runtime requires Linux or macOS, Python 3.10 or newer, Git, and a trusted
Git checkout of the target static website. The target site decides its own JSON
shape and build process; Sip supplies the publication workflow and example
contracts.

For another agent runtime, load [`SKILL.md`](SKILL.md) as the skill entrypoint
and expose `SIPLOGUE_CONFIG` and `SIPLOGUE_STATE_DIR` to that agent.

## Configure

Start with [`examples/siplogue.example.json`](examples/siplogue.example.json).
The configuration identifies one trusted website checkout, its cup and setup
collections, media directories, public URL templates, validation command, and
Git behavior.

```sh
export SIPLOGUE_CONFIG=/absolute/path/to/siplogue.json
export SIPLOGUE_STATE_DIR=/absolute/private/path/to/siplogue-state

python3 scripts/siplogue.py doctor
```

Keep the configuration and state directory outside the public site repository.
For automatic publishing, use a repository-scoped deploy key or equivalent Git
credential and set `git.push` to `true`. Keep
`git.require_clean_worktree` and `git.sync_before_publish` enabled in production.

See [`references/configuration.md`](references/configuration.md) for every
field and deployment invariant.

## Use it with an agent

Typical requests are deliberately conversational:

```text
Use $sip. Here is the coffee I opened today and a few notes about the cup.

Use $sip. I am drinking the Honduras coffee again; bring it back into my
current rotation.

Use $sip. This photograph is my V60 station. It has a hand grinder, scale,
gooseneck kettle, and supports V60 and AeroPress.

Use $sip. Add the new grinder to my V60 setup without replacing its photo.
```

The agent captures the private intake first, writes polished structured public
copy, and invokes the deterministic publisher. It should inspect the existing
setup collection before linking a cup; it must never guess equipment ownership
or a setup ID.

## CLI overview

```sh
# Store an incoming photograph and rough notes privately.
python3 scripts/siplogue.py capture \
  --media /absolute/path/to/photo.jpg \
  --notes-file /absolute/path/to/notes.txt \
  --source-json /absolute/path/to/source.json

# Publish the polished cup payload returned by the agent workflow.
python3 scripts/siplogue.py publish \
  --config /absolute/path/to/siplogue.json \
  --entry-id ENTRY_ID \
  --payload /absolute/path/to/polished-post.json

# Renew the current window, or replace the flag with --state archived.
python3 scripts/siplogue.py refresh \
  --config /absolute/path/to/siplogue.json \
  ENTRY_ID_OR_SLUG

# Publish a photographed setup after a normal capture.
python3 scripts/siplogue.py publish-setup \
  --config /absolute/path/to/siplogue.json \
  --entry-id ENTRY_ID \
  --payload /absolute/path/to/setup.json

# Change setup copy, methods, or tools while preserving its photograph.
python3 scripts/siplogue.py update-setup \
  --config /absolute/path/to/siplogue.json \
  SETUP_ID_OR_SLUG \
  --payload /absolute/path/to/setup-update.json
```

Read [`references/payload-schema.md`](references/payload-schema.md) for cup
payloads and [`references/brew-setups.md`](references/brew-setups.md) for setup
payloads. `list` and `show` inspect bookkeeping receipts; `show` redacts private
fields unless explicitly asked to reveal them.

Publishing is idempotent. If a network push fails after the local commit,
rerunning the same `publish` or `publish-setup` command resumes from the recorded
commit instead of creating another entry.

## Bring your own site

Sip does not require Next.js or the example website. A compatible site needs:

1. A Git repository with JSON arrays for cups and, optionally, brew setups.
2. Public media directories for sanitized images.
3. Pages or components that render those contracts.
4. A non-interactive validation command that exits nonzero for invalid content.
5. A normal Git remote if automatic pushes are enabled.

The setup collection is optional as a complete group of four configuration
fields. A partial setup configuration fails `doctor` so publication cannot
silently target the wrong location.

## Development

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/siplogue.py
```

The tests use temporary Git repositories and cover private capture,
sanitization, polishing enforcement, current rotation, setup publication and
updates, clean-worktree enforcement, fast-forward synchronization, automatic
pushes, and idempotent retries.

## License

[MIT-0](LICENSE)
