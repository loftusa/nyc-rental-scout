/* Pure fit math for the rental scout — no DOM. Mirrors renter_fit + its call site in refresh/refresh.py:
   fit = Σ w_k·score_k (soft = quiet for apt / social for room, missing → 5, commute = dual_commute),
   +0.8 if loved, then round(min(10, fit), 1).
   Loaded by index.html (window.FitMath). */
(function () {
  const COMP_KEYS = ["commute", "nice", "aesthetic", "nature", "value", "soft", "gym"];

  function compScores(x) {
    const s = x.scores || {};
    const soft = x.bucket === "apt" ? s.quiet : s.social;
    return {
      commute: x.dual_commute ?? s.commute ?? 5,
      nice: s.nice ?? 5, aesthetic: s.aesthetic ?? 5, nature: s.nature ?? 5,
      value: s.value ?? 5, soft: soft ?? 5, gym: s.gym ?? 5,
    };
  }

  function normWeights(raw) {
    let tot = 0;
    for (const k of COMP_KEYS) tot += Math.max(0, raw[k] ?? 0);
    const out = {};
    for (const k of COMP_KEYS) out[k] = tot > 0 ? Math.max(0, raw[k] ?? 0) / tot : 1 / COMP_KEYS.length;
    return out;
  }

  function fitOf(x, w) {
    const s = compScores(x);
    let f = 0;
    for (const k of COMP_KEYS) f += (w[k] ?? 0) * s[k];
    if (x.loved) f += 0.8;
    return Math.round(Math.min(10, f) * 10) / 10;
  }

  function rankBoard(candidates, w, cap, topN) {
    const passing = candidates.filter(x => cap == null || x.price <= cap);
    const fits = new Map(passing.map(x => [x.id, fitOf(x, w)]));
    const ranked = [...passing].sort((a, b) =>
      (fits.get(b.id) - fits.get(a.id)) || (a.price - b.price));
    const n = topN === "all" ? ranked.length : Math.min(topN, ranked.length);
    return { ranked, boardIds: new Set(ranked.slice(0, n).map(x => x.id)), fits };
  }

  function fieldMeans(candidates) {
    const m = Object.fromEntries(COMP_KEYS.map(k => [k, 0]));
    for (const x of candidates) { const s = compScores(x); for (const k of COMP_KEYS) m[k] += s[k]; }
    const n = Math.max(1, candidates.length);
    for (const k of COMP_KEYS) m[k] /= n;
    return m;
  }

  function driverOf(x, w, means) {
    const s = compScores(x);
    let best = null, bv = -Infinity;
    for (const k of COMP_KEYS) {
      const v = (w[k] ?? 0) * (s[k] - means[k]);
      if (v > bv) { bv = v; best = k; }
    }
    return bv > 0.02 ? best : null;
  }

  const FitMath = { COMP_KEYS, compScores, normWeights, fitOf, rankBoard, fieldMeans, driverOf };
  if (typeof module !== "undefined" && module.exports) module.exports = FitMath;
  if (typeof window !== "undefined") window.FitMath = FitMath;
})();
