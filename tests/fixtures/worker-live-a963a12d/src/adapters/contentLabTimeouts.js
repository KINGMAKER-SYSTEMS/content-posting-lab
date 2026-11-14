// One timeout inventory for every Control Plane Worker -> Content Lab HTTP door.
// Callers may impose a smaller remaining request budget, but no call may exceed
// MAX_TIMEOUT_MS in contentLabClient.js or PostRenderClient's validated 60s cap.
export const CONTENT_LAB_CALL_TIMEOUT_MS = Object.freeze({
  capabilities: 8_000,
  formatContracts: 8_000,
  dossierIngredients: 12_000,
  roster: 8_000,
  rosterRefresh: 15_000,
  sourceImport: 30_000,
  createJob: 15_000,
  publishRecipe: 15_000,
  jobStatus: 8_000,
  artifacts: 8_000,
  visualAdmission: 10_000,
  postRenderStatus: 15_000,
  postRenderArtifact: 45_000,
  fontProxy: 8_000,
});
