# Brew setup payloads

A brew setup is a photographed, named combination of methods and tools. Store it in the configured public setup collection; keep its raw caption and original photo in Sip's private state.

## New or replacement-photo payload

`publish-setup` accepts one UTF-8 JSON object:

- `name`: required public name, at most 120 characters.
- `slug`: optional stable lowercase identifier. When omitted it is derived from `name`.
- `summary`: required short description, at most 320 characters.
- `description`: required polished Markdown, at most 12,000 characters.
- `alt_text`: required concrete photo description, at most 500 characters.
- `methods`: array of up to 20 method names, such as `espresso`, `V60`, or `gongfu`.
- `tools`: array of up to 40 objects. Each requires `name` and may include `role` and `notes`.
- `tags`: array of up to 20 strings.

```json
{
  "name": "Morning Espresso Station",
  "summary": "A compact daily station for espresso and Americanos.",
  "description": "This is the counter setup I use for short espresso and longer Americanos.",
  "alt_text": "A compact espresso machine and hand grinder arranged on a kitchen counter.",
  "methods": ["espresso", "americano"],
  "tools": [
    {"name": "Compact espresso machine", "role": "brewer"},
    {"name": "Hand grinder", "role": "grinder", "notes": "Dialed for espresso."}
  ],
  "tags": ["espresso", "countertop"]
}
```

Capture the photo and raw description first, then publish:

```sh
python3 {baseDir}/scripts/siplogue.py publish-setup \
  --config /absolute/path/to/siplogue.json \
  --entry-id ENTRY_ID \
  --payload /absolute/path/to/setup.json
```

Publishing the same slug replaces the setup record while preserving its stable ID. The new sanitized image is added normally; old public images are retained rather than deleted automatically.

## Metadata-only updates

`update-setup` accepts any non-empty subset of `name`, `slug`, `summary`, `description`, `methods`, `tools`, or `tags`. It preserves the existing photograph and unspecified fields.

```sh
python3 {baseDir}/scripts/siplogue.py update-setup \
  --config /absolute/path/to/siplogue.json \
  morning-espresso-station \
  --payload /absolute/path/to/setup-update.json
```

Treat the setup collection as an inventory, not a recommendation engine. Record only tools and capabilities the user actually owns or can access.
