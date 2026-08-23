---
name: carlo-ui-design
description: Use when designing or revising CARLO web interfaces, responsive layouts, navigation, dashboards, cards, chat, Discoveries, Tasks, Actions, or other user-facing frontend behavior.
---

# CARLO UI Design

Use `assets/reference.png` as CARLO's durable visual reference. Derive the language; do not reproduce the source product or its identity.

## Visual language

- Warm white canvas, near-black text, quiet gray secondary text.
- Thin neutral borders, subtle separators, almost no shadows.
- Large calm surfaces with generous whitespace and clear alignment.
- Bold, compact headings; small uppercase section labels with tracking.
- A centered horizontal navigation capsule on desktop. Use pill-shaped selected states with strong black/white contrast.
- Cards use restrained rounded corners and content hierarchy, not decoration.
- Color communicates state or action only. Keep the default interface monochrome.
- Icons are simple outline companions to labels, never ornamental filler.

## CARLO adaptation

Make current activity and the next meaningful action obvious. Keep Board, Actions, and Discoveries at the same navigation level. Preserve lifecycle detail in focused views rather than filling dashboards with metrics.

For Discovery, the conversation is the primary surface. On desktop use list / chat / context with the central chat dominant. On mobile use chat fullscreen, a reachable composer, bottom navigation, and a context sheet. Command output is collapsible; code scrolls horizontally.

## Interaction rules

- Design mobile-first and enhance at wider breakpoints.
- Keep touch targets at least 44px.
- Use visible focus states, semantic controls, labels, and sufficient contrast.
- Keep loading, empty, error, streaming, offline, and update states intentional.
- Prefer native controls and CSS over new UI dependencies.
- Validate responsive behavior and the relevant flow in a real browser.

## Check before finishing

- The page reads clearly in grayscale.
- Navigation and selection states are unmistakable.
- Dense operational evidence remains scannable.
- Mobile is a deliberate composition, not a squeezed desktop layout.
- Styling feels calm, precise, and CARLO-specific while remaining recognizably inspired by `assets/reference.png`.
