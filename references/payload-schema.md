# Polished payload schema

Write one UTF-8 JSON object. The publisher adds the internal ID, public image path, and publication timestamps.

## Required fields

- `title`: specific title, at most 140 characters.
- `kind`: `tea` or `coffee`.
- `excerpt`: one polished sentence, at most 320 characters.
- `body`: polished Markdown, at most 12,000 characters. It must not be a verbatim or near-verbatim copy of the raw notes.
- `alt_text`: concrete image description, at most 500 characters. Do not repeat the caption or begin with “image of.”

## Optional fields

- `slug`: lowercase letters, digits, and single hyphens. When omitted, the publisher derives `YYYY-MM-DD-title`.
- `observed_at`: ISO 8601 timestamp with timezone. Defaults to the intake timestamp.
- `current`: boolean. Coffee defaults to `true`; tea defaults to `false`. Use an explicit value only when the user indicates a different state.
- `setup_ids`: array of stable IDs from the configured brew setup collection. Include only setups actually used for this cup.
- `subject`: object using only `name`, `producer`, `origin`, `variety`, `process`, and `style`.
- `brew`: object using only `method`, `temperature_c`, `dose_g`, `water_g`, `steep_seconds`, `grind`, and `infusions`.
- `tasting_notes`: array of up to 30 short strings.
- `rating`: number from 0 to 10. Omit it unless the user gave a rating or clearly asked the agent to assign one.
- `tags`: array of up to 20 short strings.

Unknown optional fields should be omitted or represented by an empty object/array. Never use placeholders such as “unknown.” Subject and brew values must be strings, numbers, or null; nested objects are rejected.

## Example

```json
{
  "title": "Jasmine After the First Pour",
  "kind": "tea",
  "excerpt": "A floral cup that traded its early brightness for a lingering peach sweetness.",
  "body": "The first pour opened with a clean jasmine lift and a ripe-peach edge. As the cup cooled, the sweetness settled in and the fruit became rounder. At 92 °C, it felt expressive without turning sharp.",
  "alt_text": "A brewed cup of tea beside its leaves in soft afternoon light.",
  "observed_at": "2026-08-26T15:30:00Z",
  "subject": {"name": "Afternoon tea", "style": "floral"},
  "brew": {"temperature_c": 92},
  "current": true,
  "setup_ids": ["desk-gongfu-station"],
  "tasting_notes": ["jasmine", "peach", "honeyed finish"],
  "rating": 8.5,
  "tags": ["tea", "tasting-note"]
}
```
