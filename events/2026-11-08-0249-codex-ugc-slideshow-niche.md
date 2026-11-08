time: [02:49] [08-11-26]
agent: [codex] [gpt-6]
worktree: [codex/ugc-slideshow-niche-20261108]
type: [feature-request]
area: [backend]

Made the existing page-scoped UGC slideshow format available to non-Coffee,
non-meme pages with a sourced_slideshow engine, including pages whose niche is
not literally slideshow or XMAS. Dossier candidates, selected catalog versions,
and recipe material profiles now use one format-aware admission check. The
page-bound image-video and single-image-video plans remain restricted to their
exact PenPal and Twin page tuples. No account configuration or media changed.

Before the fix, three focused assertions failed: image-video leaked into XMAS
candidates, TRUCK had no UGC candidate, and a non-PenPal page could select the
image-video catalog version. An initial broad admission also caused two meme
Dossier tests to fail because it fetched an extra Syzygy library; the final
scope excludes meme and Coffee. The final focused Dossier/slideshow/image
execution suite passed 137 tests with one skip; the format, recipe, generation,
and source suite passed 335; production image imports passed 2. These are
local tests, not provider, deployed, or live-page acceptance.

Devlog pass: root AGENTS.md records the stable format-admission contract. No
child index or further owning doc changed.
