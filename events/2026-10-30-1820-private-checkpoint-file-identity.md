# Refuse ambiguous private checkpoint filesystem identities

A private manifest that names an existing directory or a file with a conflicting
byte length is not a valid produced-file reference. Hold the entire job rather
than recursively treating the directory's contents as strays. Missing files
remain allowed for successful public assets already removed after R2 proof.
The existing alias/path/manifest identity checks remain unchanged.

The two additional malformed controls and the complete focused preservation
set pass155 checks. No provider, ledger, compaction or production changes.
