# Refuse mixed local originality generations

Publish a verified manifest before the data under a permanent local writer lock, and refuse incomplete local snapshots. The reader hashes the exact bounded data it parses on each return; unchanged data can reuse the parsed index. The manual sync uses the same writer and preserves source age. Stable legacy two-file reads remain supported when no managed marker or manifest exists.

This protects cooperating current local readers and writers. Older readers or writers and arbitrary external filesystem mutation are not qualified by this change.
