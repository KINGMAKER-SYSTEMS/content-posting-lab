# Claim terminal failure reason compatibility

Claim F-LAB-TERMINAL-ERROR-WORKER-BOUND-1031 on current main for a narrow status-contract fix. Real offline Lab status responses with truncation whitespace, astral Unicode, and BOM boundaries are rejected by the current Worker client. Preserve terminal ownership, active/success status shapes, and no-write GET behavior.
