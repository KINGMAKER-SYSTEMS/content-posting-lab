
## Cross-Tab Workflow Wiring
- Used Zustand store (`workflowStore.ts`) to manage cross-tab state (recent jobs, pending counts).
- Implemented simple "Use in Burn" navigation using `useNavigate`.
- Added toast notifications using a shared `ToastContainer` in `App.tsx` connected to the store.
- Tab badges are updated reactively based on store state.
- `Burn.tsx` refreshes data on mount, which works well with `react-router-dom`'s unmounting behavior.

## Wave 3 Audit Follow-Up
- A remediation gate is required before Wave 4+ due to Task 13/14 acceptance criteria gaps.
- Primary blockers: missing nav-level project selector, Home route replacing Projects root, project API/type mismatch, hardcoded project stats/fallback data.
- Detailed fix list: `.sisyphus/notepads/unified-webapp/wave3-remediation-gate.md`.
