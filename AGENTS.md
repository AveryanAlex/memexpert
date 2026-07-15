# Repository Notes

MemeExpert is a semantic meme search/catalog and content-pipeline product: it ingests/crawls memes, processes media through workers, indexes PostgreSQL/Qdrant/Meilisearch/S3, and serves a FastAPI API, SvelteKit web/Mini App, and Telegram bot.

## Product and design docs

- Check `docs/` before changing product behavior or architecture: PRDs live in `docs/prd/`, technical design in `docs/tech-design/`, and runbooks in `docs/ops/`.
- Keep the relevant PRD/technical design/runbook updated when implementation changes invalidate or extend those documents; do not let design docs drift from code.

## Deployment status

- The public launch is not complete, but a live beta environment runs on `whale` at `https://beta.memexpert.net`. Treat it as production operationally even though coordinated breaking API changes and destructive database migrations are still acceptable during this beta phase.

## Live beta deployment and production access

- The live beta is a NixOS/Podman Quadlet deployment, not the example Docker Compose stack. Its runtime source of truth is the sibling dotfiles repo: `../dotfiles/apps/memexpert/default.nix`, imported by `../dotfiles/machines/whale/default.nix`. Reploy configuration is in `../dotfiles/machines/whale/reploy.nix`.
- `../dotfiles/profiles/server/memexpert.nix` and the native `memexpert.service` are the separate legacy `memexpert.net` deployment. Do not confuse that service with the beta Quadlet units at `beta.memexpert.net`.
- Image-only releases normally happen by pushing `main`: CI must pass lint, backend tests, frontend tests/build/smoke, and real-stack E2E; it then publishes the `main`, `worker`, and `frontend` `:main` images to GHCR and invokes Reploy. Follow the GitHub Actions run through the final `deploy` job rather than assuming a successful push is deployed.
- Nix/runtime changes belong in `../dotfiles`, not generated files on `whale`. From the dotfiles root, validate with `nix run nixpkgs#nixfmt-tree -- --ci` and `nix flake check`, then use `./deploy.sh whale build` and `./deploy.sh whale switch` (or `test`). Never build whale's NixOS configuration locally; `deploy.sh` builds on the target host.
- Connect with `ssh whale`. Generated Quadlet definitions are visible under `/etc/containers/systemd/memexpert-*.container` and generated services under `/etc/systemd/system/memexpert-*.service`, but never edit them directly. Secrets are Agenix-managed; do not print environment files, full container inspections, tokens, or credentials into logs or chat.
- High-signal read-only production checks:
  - Units: `ssh whale "systemctl list-units --all 'memexpert*' --no-pager"`
  - Containers: `ssh whale "sudo podman ps -a --filter name=memexpert"`
  - API health: `ssh whale "curl -fsS http://10.90.99.10:8000/health"`
  - Recent API logs: `ssh whale "journalctl -u memexpert-api.service -n 200 --no-pager -o short-iso"`
  - Recent worker logs: `ssh whale "journalctl -u 'memexpert-worker-*.service' --since '1 hour ago' --no-pager -o short-iso"`
  - Follow one worker: `ssh whale "journalctl -fu memexpert-worker-ocr.service -o short-iso"`
  - Deployment logs: `ssh whale "journalctl -u reploy.service --since '1 hour ago' --no-pager -o short-iso"`
- App units are `memexpert-api`, `memexpert-frontend`, `memexpert-scheduler`, `memexpert-telegram-crawler`, `memexpert-bot`, and `memexpert-worker-{media,ocr,enrichment,sync,telegram}`. Infrastructure units include `memexpert-{db,redis,rabbitmq,qdrant,meilisearch,minio,imgproxy}`. `memexpert-migrate` being inactive after a successful one-shot migration and `memexpert-minio-init`/`memexpert-network` being `active (exited)` are expected.
- For an interactive database shell, use `ssh -t whale 'sudo podman exec -it memexpert-db psql -U memexpert -d memexpert'`. Start with read-only queries, avoid broad production dumps, and never mutate production data unless the task explicitly authorizes it.
- Prefer systemd and Reploy over raw `podman restart`. Manual restarts, image rollouts, migrations, and database writes change production state; perform them only when requested and preserve the migration/dependency ordering declared in Nix. Wait until all three application images exist before a manual Reploy rollout to avoid a mixed release.

## High-signal structure

- Backend code is Python 3.14/FastAPI in `memexpert/`; the app factory is `memexpert.api.app:create_app`, `/health` is unversioned, and product routes mount under `/api/v1`.
- Console scripts are registered in `pyproject.toml`: `memexpert-api`, `memexpert-workers`, `memexpert-scheduler`, `memexpert-telegram-crawler`, `memexpert-bot`, plus admin/analytics helpers.
- `docker-compose.yml` starts local infrastructure only; it intentionally does not run app containers.
- Python deps use `uv.lock`. `uv sync --locked` installs the default `dev` and `worker` groups locally; Docker `main` images use `--no-default-groups`, while the `worker` target adds worker deps, FFmpeg/FFprobe, and a separate Python 3.13 PaddleOCR helper venv.
- Keep API/main-image imports free of worker-only media deps (`PIL`, `imagehash`, FFmpeg/PaddleOCR). `tests/test_import_boundaries.py` enforces that the FastAPI app imports without those packages.
- Runtime settings load `.env` via `pydantic-settings` and `get_settings()` is cached; tests that monkeypatch env should clear settings/runtime state like the helpers in `tests/conftest.py`.

## Backend commands

- Install/sync: `uv sync --locked`
- Checks used by CI: `uv run ruff check .`, `uv run ty check`, `uv run pytest -v`
- Focused pytest: `uv run pytest -v tests/path.py::test_name`; add `-n 0` when debugging sequentially because pytest addopts default to `-n 4 --dist loadfile`.
- Integration tests use Docker testcontainers for PostgreSQL 16 and Redis 7; failures can be Docker-daemon/setup issues, not app startup issues.

## Database and migrations

- SQLAlchemy models live under `memexpert/models/`; schema changes need an Alembic revision under `alembic/versions/`.
- Verify migration work with `uv run alembic upgrade head` and, when relevant, `uv run pytest -v tests/integration/test_migrations.py`.

## Frontend

- Frontend is a SvelteKit adapter-node app in `frontend/` using Node 22 and `pnpm`; SSR load/server routes call the backend through `API_BASE_URL` and forward cookies.
- Read `frontend/AGENTS.md` before frontend UI work; it contains the Svelte 5/Tailwind v4/Bits UI/LayerChart conventions that should not be duplicated here.
- From `frontend/`: `pnpm install --frozen-lockfile`, `pnpm check`, `pnpm test`, `pnpm exec playwright install --with-deps chromium`, `pnpm test:smoke`, `pnpm build`.
- `pnpm test:smoke` uses `frontend/playwright.config.ts`, which starts its own mock API and Vite server; it is not the real-stack E2E suite.

## Real-stack E2E and Docker

- Root E2E entrypoint: `python scripts/run_container_e2e.py`. It creates `.artifacts/e2e/<run-id>/`, uses Compose project `memexpert-e2e-<run-id>`, runs fake OCR/Voyage/classification providers, and tears down volumes unless `E2E_KEEP_STACK=1`.
- Use `E2E_SKIP_IMAGE_BUILD=1` only when all four image tags already exist locally: `MEMEXPERT_MAIN_IMAGE`, `MEMEXPERT_WORKER_IMAGE`, `MEMEXPERT_FRONTEND_IMAGE`, and `MEMEXPERT_E2E_RUNNER_IMAGE`.
- Production compose sanity checks from CI: `docker compose --env-file .env.prod.example -f docker-compose.prod.example.yml config` and `python3 scripts/validate_prod_compose_env.py`.
- Build Python images with `docker build --target main -t memexpert-main:local -f Dockerfile .` and `docker build --target worker -t memexpert-worker:local -f Dockerfile .`; build the frontend image with `docker build -t memexpert-frontend:local -f frontend/Dockerfile .`.
