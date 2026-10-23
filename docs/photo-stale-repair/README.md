# Photo repair: blocked Lab handoff

This is a draft handoff, not a working Lab repair. Do not merge it for release.

The patch is only the four-file repair delta from control-plane commit
`e87e4112a1847800c1926e5f794ef5f24a4e0f05` over
`87099ac1e75450f6652b2790ca589d5f0087413f`. No inherited substrate is imported.
It keeps old batch files, joins matching current reviews, ignores reviews for
old photo bytes, and refuses reviews that disagree. Missing reviews stay held.

Lab main `4b4d432` plus pinned PR 248 head
`ee33b90512aaa3e7d283093027bc778f972d1da4` contains neither
`tools/photo-library/publish.py` nor the rest of that publisher. Applying the
patch fails because the existing files are missing. Lab has no photo-library
caller in services, routers or tests. A separate decision about ownership and
substrate is needed before a repair-only integration can be built.

Real-media storage readback and renderer/admission receipts for this repair are
not available. Existing Lab rendering tests cannot prove this missing path.
No production data, old manifests, owner holds, settings or activation changes.

The attached gap runner is inert and must not be started. Besides the pinned
head and PR files, it requires a future `lab-photo-ready.json` for that same head
with status `READY` and all four gates true: `composed_review`, `base_head_suite`,
`real_media_storage_readback`, `renderer_admission_receipts`. This task does not
supply that receipt. It checks HOLDs and the time window, pins the merge, then
watches Railway without starting a build. No merge/build success is claimed.
