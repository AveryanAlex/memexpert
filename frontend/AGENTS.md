# Frontend Architecture Notes

This SvelteKit app uses Svelte 5, pnpm 10.28.0, Tailwind CSS v4 through `@tailwindcss/vite`, Bits UI 2.18.1 for accessible compound primitives, and `@lucide/svelte` when an icon clarifies an action.

## Conventions

- Prefer Tailwind utility classes and small component composition over new global CSS or broad `@apply` blocks.
- Keep `src/app.css` limited to Tailwind import, theme tokens, font/body resets, and unavoidable app-wide base styles.
- Put reusable visual primitives in `src/lib/ui`. These components should be small, prop-forwarding, and independent of MemeExpert domain data.
- Put composed product UI in `src/lib/features/<area>`, for example `memes`, `trends`, `collections`, `profile`, and `admin`.
- Use Bits UI through local wrappers for repeated primitives. Dropdown menu wrappers live in `src/lib/ui/dropdown-menu` and own the Portal/Content styling.
- Dialog, Popover, and Tooltip wrappers should stay thin, forward useful props, and support `bind:open` where the underlying primitive does.
- Keep `Tooltip.Provider` near root layout. Tooltips are supplemental desktop help only; essential content belongs in Popover, Dialog, or visible text.
- Preserve server actions and route URLs. Refactors should thin route markup without changing form names, actions, query params, or data contracts.
- Use warm MemeExpert tokens (`paper`, `cream`, `ink`, `line`, `soft`) to preserve the existing product feel.

## Checks

Run from `frontend` with the pinned package manager. If `pnpm` is not on PATH in this container, use `npx pnpm@10.28.0 --config.store-dir=/home/ubuntu/.hermes/profiles/coder/home/.local/share/pnpm/store/v10 run <script>`.

- `pnpm check`
- `pnpm test`
- `pnpm build`
- `pnpm test:smoke` when browser smoke coverage is relevant and the mock API/browser dependencies are available.
