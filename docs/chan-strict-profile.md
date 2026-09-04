# Strict Chan baseline

`strict-v1` is a reproducible benchmark profile, not a claim that it is the
only orthodox interpretation of Chan theory. It is intentionally separate from
the `simplified-v1` daily analyzer so backtests can attribute differences to
explicit rules instead of hidden thresholds.

The profile applies these rules:

- Inclusion is resolved chronologically. A containing bar keeps the prior
  effective direction; an initial tie resolves upward. Upward inclusion keeps
  the higher high and higher low, while downward inclusion keeps the lower high
  and lower low. Equal boundaries are included.
- A fractal requires strict high and low extrema against both neighboring
  processed bars. The right processed bar confirms it. `tradable_at` is the
  next supplied input bar, so a last-bar observation cannot be tradeable.
- A stroke connects alternating confirmed fractals only when they are at least
  three processed bars apart. Same-kind fractals do not silently create a
  stroke. Once a stroke is tradeable, its endpoint is immutable in later
  prefixes; a later same-kind extreme is retained as a fractal but cannot
  rewrite that historical stroke.
- A central area requires overlap across three consecutive confirmed strokes
  and may extend only while each following confirmed stroke overlaps the
  original area.
- This structural baseline emits reason-coded observations and timestamps. It
  does not label observations as investment advice or assert buy/sell points.

Any rule change must create a new profile version and a new comparison run.
