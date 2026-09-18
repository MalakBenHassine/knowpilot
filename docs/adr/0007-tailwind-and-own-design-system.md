# ADR-0007: Tailwind CSS with our own design system

- **Status:** Accepted
- **Date:** 2026-09-17

## Context

The interface must look like a credible product, support light and dark themes,
stay consistent across screens, and remain maintainable by one person. It also
has to communicate uncommon states — indexing stages, "insufficient evidence" —
which no component library ships.

## Options considered

| Option | Pros | Cons |
| ------ | ---- | ---- |
| Plain CSS / CSS Modules | No dependency | A second file per component; design tokens and dark mode implemented by hand |
| **Tailwind CSS v4 + our own primitives** | Tokens centralised in `@theme`; dark mode is a redefinition of variables; no parallel stylesheet; widely requested skill | Verbose class lists in JSX; a build step |
| Component library (MUI, shadcn/ui) | Fast to assemble | The result looks like every other project; heavier bundle; fighting the library for custom states |

## Decision

Tailwind CSS v4 with semantic design tokens (`surface`, `line`, `ink`, `accent`,
plus state colours) defined once in `src/index.css`, and a small set of our own
primitives: `Button`, `Card`, `Badge`, `Alert`, `Spinner`, `EmptyState`,
`ProgressBar`.

## Consequences

- No component hard-codes a colour, radius or spacing value; dark mode
  redefines the same variables instead of inverting colours.
- Verbose class names in JSX are contained by reusing the primitives.
- No web font is loaded from a third party: better privacy, one less network
  dependency, and a simpler Content Security Policy.
- The visual identity is ours, which is the point for a portfolio project.

## Revisit when

The number of primitives grows to the point where maintaining them costs more
than adopting a headless library (for example for accessible dialogs, menus and
comboboxes, which are genuinely hard to build correctly).
