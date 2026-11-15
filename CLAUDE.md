# mcpfleet-obs

Fleet-shared observability for the FastMCP servers in `mcpfleet/`: client
tracking, Prometheus counters, and the `/metrics` route, installed with one
call — `install(mcp, prefix="<server_prefix>")`.

Consumers: `uk-due-diligence-mcp`, `uk-legal-mcp`, and `uk-business-mcp`,
all pinned `==`.

## Releasing — use twine; there is no workflow

Releases are hand-uploaded. `release.yml` was deleted in Nov 2026 — it had
**never successfully published**:

| Version | Tag/release | Workflow run | How it actually reached PyPI |
|---------|-------------|--------------|------------------------------|
| 0.1.0   | 2026-07-29  | failed       | hand-uploaded 11 min *before* the run failed |
| 0.2.0   | 2026-09-09  | failed       | `twine upload` after the run failed |
| 0.3.0   | 2026-09-10  | not run      | `twine upload`, workflow deliberately skipped |
| 0.4.0   | (untagged)  | not run      | `twine upload` |
| 0.4.1   | v0.4.1        | not run      | `twine upload` |
| 0.5.0   | v0.5.0        | deleted      | `twine upload` |

The 0.1.0 and 0.2.0 runs failed identically, at `pypa/gh-action-pypi-publish`:

```
invalid-publisher: valid token, but no corresponding publisher
  (Publisher with matching claims was not found)
```

GitHub mints a valid OIDC token; PyPI has no trusted publisher registered to
match it. The `pypi` environment also carries a **10-minute wait timer**, so
each failure costs ten minutes before it reports.

The working path:

```bash
uv version --bump minor      # bumps pyproject.toml AND locks/syncs
uv build
uvx --from twine twine check dist/*
uvx --from twine twine upload --non-interactive --skip-existing dist/*
```

`uv publish` will **not** work here: it reads `--token`/`UV_PUBLISH_TOKEN`,
`--username`/`--password`, or keyring — it does not read `~/.pypirc`, which is
where the token lives. `twine` does read it, so the credential never has to be
passed on a command line.

Bump with `uv version`, not by editing `pyproject.toml`. A hand edit leaves
`uv.lock` behind at the old version (this happened at 0.2.0, fixed in
`393dc8c`); `uv version` locks and syncs in one step.

After publishing, verify against the **simple index**, not the JSON API — the
aggregate `/pypi/<pkg>/json` endpoint is CDN-cached and lagged behind a real
upload by minutes.

Use `--refresh`, not `--refresh-package <name>`. At 0.3.0 the simple index
already listed the new version while `--refresh-package mcpfleet-obs` still
resolved the previous one — it refreshes the package but not the cached index
page it is chosen from, so it can report a stale version with total confidence:

```bash
curl -s https://pypi.org/simple/mcpfleet-obs/ | grep -o 'mcpfleet_obs-[0-9.]*'
uv run --refresh --with mcpfleet-obs --no-project \
  -- python -c "from importlib.metadata import version; print(version('mcpfleet-obs'))"
```

## TODO

- **Reinstate automated releases, if wanted.** `release.yml` was deleted
  because it looked like the release path, took ten minutes to fail, and never
  once published. To bring it back: register the trusted publisher on PyPI
  first — owner `uk-agents`, repo `mcpfleet-obs`, workflow `release.yml`,
  environment `pypi` — then re-run a release to confirm before trusting it.
  Also delete the orphaned `pypi` GitHub environment if it is never reinstated
  (it carries a 10-minute wait timer).
- **Expand this file.** It was created 2026-09-09 holding only the release
  process, because that was the thing actively costing time. Architecture, the
  middleware ordering contract, and the fastmcp version-matrix policy
  (`3.2.3/3.2.4/3.3.0/3.4.5/4.1.3`) are not written down anywhere yet.

## CI

`ci.yml` runs pytest across the fastmcp matrix
(`3.2.3/3.2.4/3.3.0/3.4.5/4.1.3`, all on Python 3.12 — there is no
Python-version axis, so nothing verifies the `>=3.11` floor against older
interpreters) on every push and PR. Keep it green before tagging — it is the
only thing that catches the exception-class move between 3.2.x (pydantic
`ValidationError`) and 3.3+ (FastMCP's own).

The `4.0.0b1` allowed-failure job was removed in Nov 2026: `4.1.3` covers the
4.x line, and the beta failed the initialize handshake on every run — pure
noise. The `tests/fixtures/error-shapes-4.0.0b1.json` contract fixture stays:
it records a wire shape the library still claims to parse.
