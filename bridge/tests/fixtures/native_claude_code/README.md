# Native Claude Code fixture

`django__django-11477.jsonl` is a byte-for-byte copy of
`tests/fixtures/native_claude_code/django__django-11477.jsonl` from TrajViz
(`agent-trajviz` 0.2.0, source commit `655a9ba3557d5195c5c9c6e00e7e852b147949eb`;
sha256 `78deb63f66bb0f652b1df1b452bbd9f5b372fa32bfcd8ba3c30103bfe49e4a25`).
It is a reconstructed native saved-session JSONL of a redacted Claude Code run
on the public SWE-bench task `django__django-11477`.

**License:** MIT, Copyright (c) 2026 Rui Shu (TrajViz's `LICENSE`).

**Sanitization** (from TrajViz's fixture README): session/message UUIDs and
request ids are deterministic placeholders, signature/encrypted-content blobs
are emptied, and absolute paths are rewritten under `/home/user`. TrajViz checks
its fixtures for credential-shaped strings. Do not add fixture content from
non-public runs, and keep this file byte-stable: copy a new version from
TrajViz rather than editing it here.
