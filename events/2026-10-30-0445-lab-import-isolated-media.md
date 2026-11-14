# Isolate import attempt media

Each source-import retry writes to a new private attempt directory. An old child can only write its old path, not the current attempt. Import subprocess cancellation kills and joins its owned process group before returning, and retention recognizes the grouped attempt layout while preserving unconfirmed current clips.
