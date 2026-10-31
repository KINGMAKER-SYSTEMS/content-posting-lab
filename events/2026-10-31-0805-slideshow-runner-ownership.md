# Keep live slideshow reservations during recovery

New slideshow jobs bind a private attempt and hold their runner claim through durable writes. Foreign-runtime status and idempotency recovery can terminalize a marked job only after acquiring its claim; legacy unmarked active jobs remain reserved. Already exposed partial slideshow clips retain their plans and digests through failure and compaction. Jobs that fail before exposing a clip can release their plans.

This covers cooperating updated runtimes. Mixed old runtimes and unknown legacy ownership remain unqualified; no artifact API or remote renderer protocol changes.
