# MemeXpert PRD E2E

Run the root-level product E2E suite against the containerized real stack:

```sh
python scripts/run_container_e2e.py
```

## Coverage Map

- Public website discovery: guest plain-text search, URL-backed tag/media/language/NSFW filters, NSFW hidden for default guests even when the URL requests it, search opt-in confirmation cancel/confirm, NSFW visibility after persisted opt-in, profile disable back to hidden, detail navigation, and imgproxy-rendered media.
- Guest library actions: favorite/unfavorite through the user-facing detail UI/API proxy, no guest custom-collection creation, and Pin shown as full-account-only.
- Content pipeline loop: deterministic fake-provider upload from the seed path, dual Qdrant/Meilisearch proof, public search/detail API proof, and website search/detail proof.

## Deferred Flows

- Full fake Telegram ingest, private uploads, and inline bot flows are not wired into the container E2E path yet.
- Connected-account settings, private uploads, and account-linking flows are still deferred; the browser suite now covers the NSFW opt-in boundary through guest session state.
- The suite must not call real Telegram or live provider APIs. Compose config keeps OCR, Voyage, and classification in fake mode.

## Artifacts

Playwright writes reports, test results, screenshots, videos, and traces under `E2E_ARTIFACTS_DIR`, which the one-command runner binds to `.artifacts/e2e/<run-id>/`.
