// Original source timeline and final clip duration are separate measurements.
export const SOURCE_VIDEO_BOUNDS = Object.freeze({ earliestStartMs: 60_000, minDurationMs: 5_000, maxDurationMs: 10_000 });

// The current source planner uses 5–9 s source and delivered lengths where
// feasible. Its legacy fallback otherwise uses whole-second source lengths
// delivering 6–11 s: below 6/11x it produces 5 s (admissible), but up to
// 5/9x it can also produce 6 s whose final exceeds 10 s. Above 1.8x the
// fallback's source windows exceed 10 s. POV Club has no legacy fallback.
// Match the producer's 1e-6 ms feasibility tolerance for saved fractions.
export function sourcedClipSpeedIsSupported(speed, format = null) {
  const current = speed >= (5000 - 1e-6) / 9000 && speed <= (9000 + 1e-6) / 5000;
  return Number.isFinite(speed) && speed >= 0.5 && speed <= 2
    && (current || (format !== 'pov-club' && speed < 6 / 11));
}

const SHIPSTREAM_PAGE_MASTER_AUTHORITY = 'ShipStream source-manifest.v1 exact page master';
const SHIPSTREAM_HISTORICAL_AUTHORITY_PREFIX = 'ShipStream source-manifest.v1 page-bound historical posted cut';

export function earliestSourceStartMs(source) {
  const authority = source?.master?.provenance?.authority;
  return authority === SHIPSTREAM_PAGE_MASTER_AUTHORITY
    || (typeof authority === 'string' && authority.startsWith(SHIPSTREAM_HISTORICAL_AUTHORITY_PREFIX))
    ? 0
    : SOURCE_VIDEO_BOUNDS.earliestStartMs;
}
