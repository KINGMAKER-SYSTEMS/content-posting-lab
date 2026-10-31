# Keep truck recovery masters reserved while the runner owns them

New truck recovery jobs bind a private attempt and hold their runner claim through durable writes. Foreign-runtime status and idempotency recovery can terminalize a marked job only after acquiring that claim; unmarked legacy active jobs remain reserved. Legacy failed recoveries with exposed clips retain their master and digest reservations through compaction. Failed jobs without exposed clips may release their masters.

This covers cooperating updated runtimes. Mixed old runtimes and unknown legacy ownership remain unqualified; no model call, artifact API or provider protocol changes.
