_________________________________________________________________________________

time:  [10:37am] [10-28-26]
agent: [codex] [gpt-6.1-sol]
worktree: [codex/lab267-stable-partition]
type:  [bug report]: Pull Request (KINGMAKER-SYSTEMS/content-posting-lab) #267
area:  [backend]: Generic clean-video media reservation

Fixed the generic clean-video plan candidate to exclude media keys from active and archived slideshow reservations before selecting a fresh chunk. New compacted archives retain template and media-key identities; legacy signature-only archives hold generic replenishment because their used media cannot be proven disjoint. Added insertion and archive regression coverage. No provider post, merge, or deployment was performed.
_________________________________________________________________________________
