# MemeXpert Frontend

Minimal SvelteKit MVP for the public meme catalog. It uses the existing FastAPI catalog API and keeps search/detail calls in server load functions so public browsing works without auth while SSR requests forward cookies when present.

## Configuration

- `API_BASE_URL`: backend origin for SSR API calls. Defaults to `http://localhost:8000`.

## Local Commands

```sh
pnpm install
pnpm dev
pnpm check
pnpm test
pnpm build
```

## CI Commands

Run these from `frontend/`:

```sh
pnpm install --frozen-lockfile
pnpm check
pnpm test
pnpm build
```
