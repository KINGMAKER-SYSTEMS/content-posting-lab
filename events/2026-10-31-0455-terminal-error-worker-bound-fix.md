# Preserve Worker-compatible terminal failure reasons

Normalize failure reasons using the Worker's ECMAScript trim boundary and a scalar-safe 300 UTF-16-unit limit. Trim again after the limit and retain the existing named fallback. The writer and legacy status read share this rule; the GET remains read-only.

The baseline regression failed on whitespace, astral and BOM cases. The focused job-status file passes with explicit trim-corpus and unpaired-surrogate controls. Full integration and independent review remain pending.
