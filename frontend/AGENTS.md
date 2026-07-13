# Frontend Architecture Notes

This SvelteKit app uses Svelte 5, pnpm, Tailwind CSS v4 through `@tailwindcss/vite`, Bits UI for accessible compound primitives, LayerChart for charts, and `@lucide/svelte` when an icon clarifies an action.

## Conventions

- Prefer Tailwind utility classes and small component composition over new global CSS or broad `@apply` blocks.
- Keep `src/app.css` limited to Tailwind import, theme tokens, font/body resets, and unavoidable app-wide base styles.
- Put reusable visual primitives in `src/lib/ui`. These components should be small, prop-forwarding, and independent of MemeExpert domain data.
- Put composed product UI in `src/lib/features/<area>`, for example `memes`, `trends`, `collections`, `profile`, and `admin`.
- Use Bits UI through local wrappers for repeated primitives. Dropdown menu wrappers live in `src/lib/ui/dropdown-menu` and own the Portal/Content styling.
- Use `src/lib/ui/chart` for LayerChart-backed analytics charts. Product code should import `ChartFrame` and the `LayerChart*` re-exports from `$lib/ui/chart`, not `layerchart` directly.
- Keep reusable chart wrappers and primitives in `src/lib/ui/chart`; put feature/product charts under `src/lib/features/<area>`.
- Prefer LayerChart through the local wrapper for data-driven charts that need scales, axes, tooltips, responsive frames, loading/empty states, or reuse. A tiny inline SVG is acceptable for decorative or static one-off marks that do not need those behaviors.
- Charts should use responsive `ChartFrame` sizing, warm MemeExpert tokens, clear labels/ARIA titles, loading and empty states, and readable fallback data or summaries where appropriate.
- Account-aware client UI reads the context-scoped store in `$lib/auth-state`, seeded and resynchronized by the root layout. Browser auth and user/session mutations must publish their returned session or user projection before route invalidation; never put per-user auth state or tokens in a module-level singleton.
- Viewer/account capability needed by shared meme UI comes from `$lib/viewer-capabilities` provided by the root layout Svelte context; do not prop-drill raw `accountType` through unrelated grid/card/bulk props or use module-level per-user stores.
- Dialog, Popover, and Tooltip wrappers should stay thin, forward useful props, and support `bind:open` where the underlying primitive does.
- Keep `Tooltip.Provider` near root layout. Tooltips are supplemental desktop help only; essential content belongs in Popover, Dialog, or visible text.
- Preserve server actions and route URLs. Refactors should thin route markup without changing form names, actions, query params, or data contracts.
- Use warm MemeExpert tokens (`paper`, `cream`, `ink`, `line`, `soft`) to preserve the existing product feel.

## Checks

Run from `frontend`.

- `pnpm check`
- `pnpm test`
- `pnpm build`
- `pnpm test:smoke` when browser smoke coverage is relevant and the mock API/browser dependencies are available.
