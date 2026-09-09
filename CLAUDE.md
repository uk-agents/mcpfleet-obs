# mcpfleet-obs

Fleet-shared observability for the FastMCP servers in `mcpfleet/`: client
tracking, Prometheus counters, and the `/metrics` route, installed with one
call — `install(mcp, prefix="<server_prefix>")`.

Consumers: `uk-due-diligence-mcp` and `uk-business-mcp`, both pinned `==`.

## Releasing — use twine, not the workflow

`.github/workflows/release.yml` has **never successfully published**. Both
releases to date were hand-uploaded:

| Version | Tag/release | Workflow run | How it actually reached PyPI |
|---------|-------------|--------------|------------------------------|
| 0.1.0   | 2026-07-29  | failed       | hand-uploaded 11 min *before* the run failed |
| 0.2.0   | 2026-09-09  | failed       | `twine upload` after the run failed |

Both runs failed identically, at `pypa/gh-action-pypi-publish`:

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
upload by minutes:

```bash
curl -s https://pypi.org/simple/mcpfleet-obs/ | grep -o 'mcpfleet_obs-[0-9.]*'
uv run --refresh-package mcpfleet-obs --with mcpfleet-obs --no-project \
  -- python -c "from importlib.metadata import version; print(version('mcpfleet-obs'))"
```

## TODO

- **Delete `release.yml`**, or fix it. It is currently a trap: it looks like the
  release path, takes ten minutes to fail, and has never once published. If it
  is fixed rather than deleted, register the trusted publisher on PyPI first —
  owner `uk-agents`, repo `mcpfleet-obs`, workflow `release.yml`, environment
  `pypi` — then re-run a release to confirm before trusting it.
- **Expand this file.** It was created 2026-09-09 holding only the release
  process, because that was the thing actively costing time. Architecture, the
  middleware ordering contract, and the fastmcp version-matrix policy
  (`3.2.3/3.2.4/3.3.0/3.4.5`, plus `4.0.0b1` as an allowed failure) are not
  written down anywhere yet.

## CI

`ci.yml` runs pytest across the fastmcp matrix on every push. Keep it green
before tagging — it is the only thing that catches the exception-class move
between 3.2.x (pydantic `ValidationError`) and 3.3+ (FastMCP's own).
