/* Scales and ticks for the chart kit. Pure functions: the same input always
   gives the same string, so chart output can be asserted in tests. */

// One decimal place in CSS pixels; avoids float noise in generated markup.
export function px(v) {
  const r = Math.round(v * 10) / 10;
  return Object.is(r, -0) ? "0" : String(r);
}

export function linear(d0, d1, r0, r1) {
  const span = d1 - d0 || 1;
  return v => r0 + ((v - d0) / span) * (r1 - r0);
}

export function logScale(d0, d1, r0, r1) {
  const l0 = Math.log10(d0), l1 = Math.log10(d1);
  const span = l1 - l0 || 1;
  return v => r0 + ((Math.log10(v) - l0) / span) * (r1 - r0);
}

// Powers of ten covering [min, max], always at least two ticks.
export function logTicks(min, max) {
  const lo = Math.floor(Math.round(Math.log10(min) * 1e9) / 1e9);
  let hi = Math.ceil(Math.round(Math.log10(max) * 1e9) / 1e9);
  if (hi <= lo) hi = lo + 1;
  const out = [];
  for (let e = lo; e <= hi; e += 1) out.push(Number(`1e${e}`));
  return out;
}

// Axis tick label for a money value on a log axis: $0.001, $0.01, $0.1, $1, $10.
export function moneyTick(v) {
  return `$${Number(v.toPrecision(3))}`;
}

// Six-step sequential ramp for mechanical pass rate (DESIGN.md 6.9 #4).
export const RAMP_STEPS = 6;
export function rampStep(v, max = 1) {
  if (v == null || Number.isNaN(v)) return null;
  const top = max > 0 ? max : 1;
  return Math.max(0, Math.min(RAMP_STEPS - 1, Math.floor((v / top) * RAMP_STEPS)));
}
