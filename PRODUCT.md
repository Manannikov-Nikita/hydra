# Product

## Register

product

## Users

Hydra is used by developers and technical leads who need to understand how
Codex work behaves across several local projects. They inspect telemetry during
implementation, after difficult tasks, and when deciding whether an observed
cost or workflow change is real.

## Product Purpose

Hydra turns privacy-safe deterministic telemetry and short model-reported
semantics into an evidence-backed view of Codex work. The product should make
task cost, phase allocation, repeated verification, review/fix cycles,
comparability, and instrumentation health understandable without requiring raw
JSONL, SQLite access, or trust in an agent's narrative summary.

Success means a user can move from a multi-project overview to one task or one
piece of evidence, understand freshness and uncertainty, compare only compatible
tasks, and refresh observations without exposing private source data.

## Brand Personality

Calm, precise, trustworthy. Hydra should feel like an expert instrument that
makes evidence legible, not a monitoring wall competing for attention.

## Anti-references

- A generic SaaS KPI wall made from interchangeable cards.
- A terminal or evidence dump that exposes implementation detail before meaning.
- A neon observability control room with decorative alerts and saturated color.
- An AI-styled dashboard with glass surfaces, gradients, oversized radii, or
  motion that does not communicate state.
- A scorecard that turns unavailable or incomparable evidence into confident
  conclusions.

## Design Principles

1. **Evidence before interpretation.** Every conclusion keeps provenance,
   caveats, and a path to the supporting record.
2. **Progressive disclosure.** Start with the decision-relevant overview, then
   reveal task, phase, test, and evidence detail on demand.
3. **Privacy by construction.** Browser-visible contracts contain opaque public
   references and never raw paths, prompts, commands, session IDs, or tool output.
4. **Freshness is part of the fact.** Stale, refreshing, partial, and current
   states are explicit rather than hidden behind a timestamp.
5. **Comparison requires context.** Raw deltas remain visible, but improvement or
   regression language appears only when comparability guards pass.

## Accessibility & Inclusion

The admin interface targets WCAG AA contrast, complete keyboard operation,
visible focus, reduced-motion support, and text alternatives for charts. Color
never carries meaning alone. Numeric zero, unavailable data, lower bounds, and
model-reported values remain distinguishable in text.
