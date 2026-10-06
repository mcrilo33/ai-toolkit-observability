# ai-toolkit-observability
Observability and evaluation for ai-toolkit: Langfuse stack, OpenTelemetry collector, scores and dashboards

## Langfuse stack

One shared, self-hosted [Langfuse v4][langfuse-compose] for every project that uses the
toolkit: web, worker, Postgres, ClickHouse, Redis and MinIO, defined in `compose.yaml`
with pinned images and named volumes. Only the web UI is published, on
`127.0.0.1:3000`.

```sh
scripts/langfuse.sh up      # first run creates the secrets, the org, the project, the keys
scripts/langfuse.sh status  # prints "up" (exit 0) or "down" (exit 1)
scripts/langfuse.sh down    # stops the stack, keeps the data volumes
```

Requires Docker Compose, `openssl` and the macOS Keychain. `up` warns when Docker has
less than 16 GB of memory, the docs' recommendation for this stack.

### Secrets

Every secret is generated once and kept in the macOS Keychain, account `$USER`, service
`ai-toolkit-langfuse-<NAME>`: the project key pair (`LANGFUSE_INIT_PROJECT_PUBLIC_KEY`,
`LANGFUSE_INIT_PROJECT_SECRET_KEY`), the admin password (`LANGFUSE_INIT_USER_PASSWORD`),
the datastore passwords, the encryption key and the salt. They reach Docker through the
environment only. A second `up` reuses them. If the data volumes exist but the Keychain
items are gone, `up` stops instead of generating passwords the databases would reject.

Sign in to the UI at <http://localhost:3000> as `admin@ai-toolkit.local` with:

```sh
security find-generic-password -a "$USER" -s ai-toolkit-langfuse-LANGFUSE_INIT_USER_PASSWORD -w
```

### Send the collector's traces here

Restart the collector from the `ai-toolkit` checkout with the same key pair, so traces
flow to the `ai-toolkit` project. Nothing in that checkout is edited:

```sh
kc() { security find-generic-password -a "$USER" -s "ai-toolkit-langfuse-$1" -w; }
LANGFUSE_PUBLIC_KEY="$(kc LANGFUSE_INIT_PROJECT_PUBLIC_KEY)" \
LANGFUSE_SECRET_KEY="$(kc LANGFUSE_INIT_PROJECT_SECRET_KEY)" \
  path/to/ai-toolkit/scripts/otel.sh up
```

Each worker then appears as a session named by its `spoke_run_id`. In v4 the legacy
`/api/public/sessions` and `/api/public/observations` endpoints are disabled; read with
`/api/public/v2/observations` instead.

### Limits

MinIO is not published, so browser media uploads and batch-export download links in the
UI do not work. Trace ingestion uses MinIO internally and is unaffected.

## Development

```sh
pip install --group dev
pytest -n auto tests
```

CI runs the same command, plus `shellcheck`, on every push.

[langfuse-compose]: https://langfuse.com/self-hosting/deployment/docker-compose
