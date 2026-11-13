const COUNTS = Object.freeze({ dual: 2, triptych: 3, both: 5 });
export function validateCropDeliveryGroups(artifacts, { expectedMode = null } = {}) {
  const fail = () => { throw Object.assign(new Error('Video outputs do not form the crop groups bound to the published recipe'), { code: 'ARTIFACT_CROP_CONTRACT_INVALID' }); };
  if (expectedMode !== null && !Object.hasOwn(COUNTS, expectedMode)) fail();
  const groups = new Map();
  for (const artifact of artifacts) {
    if (expectedMode && artifact.type === 'video' && !artifact.delivery) fail();
    if (!artifact.delivery) continue;
    const crop = artifact.delivery.crop;
    if (!crop || !Object.hasOwn(COUNTS, crop.mode) || crop.count !== COUNTS[crop.mode]
      || (expectedMode && crop.mode !== expectedMode) || !Number.isSafeInteger(crop.index)
      || crop.index < 0 || crop.index >= crop.count || !/^[a-f0-9]{64}$/.test(crop.sourceSha256 ?? '')
      || crop.groupId !== `sha256:${crop.sourceSha256}`) fail();
    const group = groups.get(crop.groupId) ?? { mode: crop.mode, count: crop.count, indices: new Set(), outputs: new Set() };
    if (group.mode !== crop.mode || group.count !== crop.count || group.indices.has(crop.index)
      || !/^[a-f0-9]{64}$/.test(artifact.sha256 ?? '') || group.outputs.has(artifact.sha256)) fail();
    group.indices.add(crop.index); group.outputs.add(artifact.sha256); groups.set(crop.groupId, group);
  }
  for (const group of groups.values()) if (group.indices.size !== group.count) fail();
}
