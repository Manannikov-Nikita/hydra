---
name: Hydra
description: A privacy-safe evidence desk for multi-project Codex telemetry.
colors:
  primary: "#1d5fd1"
  primary-soft: "#dce9ff"
  primary-foreground: "#ffffff"
  canvas: "#f7f8fa"
  surface: "#ffffff"
  surface-subtle: "#eef1f5"
  ink: "#18212f"
  ink-muted: "#526071"
  border: "#cbd3dd"
  track: "#e5e9ef"
  dark-canvas: "#101318"
  dark-surface: "#171c23"
  dark-surface-subtle: "#232a34"
  dark-ink: "#edf2f7"
  dark-ink-muted: "#b6c0cc"
  dark-border: "#3b4654"
  dark-track: "#29313c"
  dark-primary: "#7db2ff"
  dark-primary-soft: "#1c3558"
  dark-primary-foreground: "#101318"
  phase-understand: "#63aef2"
  phase-implement: "#f59e62"
  phase-test: "#6fcf8c"
  phase-review: "#9b8cf2"
  phase-fix: "#ec6f72"
  phase-unclassified: "#9299a3"
typography:
  headline:
    fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    fontSize: "1.5rem"
    fontWeight: 500
    lineHeight: 1.25
    letterSpacing: "-0.02em"
  title:
    fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    fontSize: "1.125rem"
    fontWeight: 500
    lineHeight: 1.3
  body:
    fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 500
    lineHeight: 1.25
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.4
rounded:
  sm: "6px"
  md: "10px"
  lg: "14px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
  xxl: "32px"
components:
  button-primary:
    backgroundColor: "{colors.primary}"
    textColor: "{colors.primary-foreground}"
    rounded: "{rounded.sm}"
    padding: "8px 14px"
    height: "36px"
  button-ghost:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "8px 12px"
    height: "36px"
  metric-card:
    backgroundColor: "{colors.surface-subtle}"
    textColor: "{colors.ink}"
    rounded: "{rounded.lg}"
    padding: "20px"
  status-chip:
    backgroundColor: "{colors.primary-soft}"
    textColor: "{colors.ink}"
    rounded: "{rounded.pill}"
    padding: "3px 9px"
---

# Design System: Hydra

## Overview

**Creative North Star: "Evidence Desk"**

Hydra is a calm working surface for examining demanding Codex tasks. It should
feel closer to a carefully arranged analyst's desk than to a monitoring wall:
the current project and freshness are obvious, the most useful evidence is near
at hand, and deeper records stay organized until requested.

The interface is information-dense without being compressed. A wide, mostly
unframed workspace carries the task; tonal layers and dividers establish
structure. Three headline summaries are the only repeated cards. Everything
else uses navigation, tables, bars, and disclosure rather than decorative
containers.

It explicitly rejects a generic SaaS KPI wall, a terminal or evidence dump, a
neon observability control room, and AI-styled glass or gradient decoration.

**Key Characteristics:**

- Evidence-first hierarchy with visible provenance and freshness.
- Restrained neutrals with one interaction accent.
- Wide desktop workspace with structural narrow-window adaptation.
- Fast state transitions and no decorative choreography.
- Privacy-safe identifiers and progressive detail.

## Colors

The palette is neutral and restrained. Blue marks interaction and current
selection; phase colors belong only to data marks and their matching labels.

### Primary

- **Verification Blue:** the only action and selection color. It appears on the
  Refresh action, active navigation, links, focus indicators, and selected data.
- **Verification Wash:** a quiet selected or informational surface, never a
  replacement for readable text.

### Secondary

- **Phase Spectrum:** the six named phase colors are persistent categorical
  encodings for charts. They are paired with labels and never used as body text
  or decoration.

### Neutral

- **Day Canvas and Surface:** low-contrast page and content layers for bright
  working environments.
- **Night Canvas and Surface:** near-black neutral layers suited to working
  beside a dark editor without turning the product into a neon dashboard.
- **Ink and Muted Ink:** primary and supporting text. Muted ink remains readable
  and is never used for an essential value.
- **Border and Track:** quiet structure for dividers, tables, and inactive chart
  geometry.

**The One Accent Rule.** Verification Blue is limited to actions, current
selection, focus, and links. Its scarcity is part of the hierarchy.

**The Data Color Rule.** Phase colors may color marks and swatches only. Labels
and numeric values remain Ink.

## Typography

**Display Font:** System UI
**Body Font:** System UI
**Label/Mono Font:** Native UI monospace

**Character:** A single familiar system family keeps the tool fast and quiet.
Monospace is reserved for opaque references, evidence IDs, commands, and schema
names rather than used as an aesthetic layer.

### Hierarchy

- **Headline:** page identity and selected-project headings; balanced but never
  promotional.
- **Title:** section and master-detail headings.
- **Body:** explanations, status detail, and table content; prose is capped at a
  comfortable reading measure while data tables may run wider.
- **Label:** navigation, table headers, controls, and secondary metadata; sentence
  case is mandatory.
- **Mono:** short public references and machine-oriented vocabulary.

**The Tabular Evidence Rule.** Numeric columns use tabular figures and end
alignment so differences can be scanned without visual jitter.

## Elevation

Hydra is flat by default. Depth comes from tonal layering, dividers, sticky
positioning, and focus treatment rather than ambient shadows. Temporary overlays
may use one restrained structural shadow, but content surfaces and metric cards
do not combine borders with wide shadows.

**The Flat Desk Rule.** Resting surfaces never float. If a region looks like a
detached marketing card, the hierarchy is wrong.

## Components

### Buttons

- **Shape:** compact and gently rounded using the small radius.
- **Primary:** Verification Blue with a high-contrast foreground; limited to one
  main action in a control group.
- **Hover / Focus:** a tonal change plus a clearly visible focus ring. State
  transitions are fast and disabled under reduced motion.
- **Ghost:** neutral by default and used for theme, navigation, and secondary
  actions.

### Chips

- **Style:** compact status display with pill geometry and plain-language text.
- **State:** chips never impersonate buttons. Interactive filters use native
  buttons with pressed state instead.

### Cards / Containers

- **Corner Style:** the large radius is reserved for the three headline metric
  summaries.
- **Background:** a subtle tonal layer with no decorative border or shadow.
- **Internal Padding:** enough to separate label, value, and one provenance line.
- **Rule:** cards are never nested and never used for ordinary sections.

### Inputs / Fields

- **Style:** native controls with a neutral surface, structural border, and small
  radius.
- **Focus:** Verification Blue ring and border; focus is never removed.
- **Error / Disabled:** explicit text and state styling; color alone is
  insufficient.

### Navigation

The desktop shell uses a stable project rail and a small set of product
destinations. Active state combines text weight, background, and an accessible
current-page attribute. In a narrow desktop window the rail becomes a native
project selector; Hydra does not introduce a separate mobile information
architecture.

### Phase Allocation

One stacked horizontal bar shows working-token allocation. Every rendered phase
has a stable color, visible label, value, percentage, and textual alternative.
Unclassified intervals are always shown rather than absorbed into another phase.

## Do's and Don'ts

### Do:

- **Do** show freshness, provenance, lower bounds, and caveats adjacent to the
  fact they qualify.
- **Do** use exactly three headline metric summaries when those metrics are
  decision-relevant.
- **Do** keep long task collections and evidence in quiet tables with progressive
  disclosure.
- **Do** preserve system theme as the initial state and persist an explicit
  light or dark user choice.
- **Do** use semantic HTML, visible focus, text alternatives, and color-independent
  labels.

### Don't:

- **Don't** build a generic SaaS KPI wall made from interchangeable cards.
- **Don't** present a terminal or evidence dump before its meaning.
- **Don't** turn Hydra into a neon observability control room with decorative
  alerts and saturated color.
- **Don't** use glass surfaces, gradient text, ornamental grids, oversized
  radii, or decorative motion.
- **Don't** turn unavailable, lower-bound, or incomparable evidence into a
  confident score or conclusion.
- **Don't** expose raw paths, prompts, commands, session identifiers, or tool
  output in browser-visible UI.
