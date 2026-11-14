# Execute the intended cross-backend gain guard

pytest.approx rejects nested tuple sequences, so the existing ducking parity
test raised TypeError before checking any gains. Keep an explicit three-point
outer count and two-scalar pair shape, then compare each ordered pair with the
same default pytest scalar tolerances. Both backend checks remain intact; no
runtime code, expected gain, interpolation or envelope policy changes.

The exact parity case passes. Independent in-memory mutations of gain scaling,
point count and point order each fail the repaired assertion. Full broader
qualification on the combined candidate remains required.
