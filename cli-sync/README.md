# gatekey-sync

Keeps a rotated [Gatekey](https://github.com/core-labs-io/gatekey) personal
API key (`gk_pk_...`) synced to your local AI CLI tooling — so automatic key
rotation on the gateway never breaks the tools on your laptop. No background
process: it refreshes the key on demand (with a local cache, so the common
path makes no network call) and injects it wherever your tool reads it from.

Credentials are stored in your OS keychain via
[`keyring`](https://pypi.org/project/keyring/) — never in a plaintext file.

## Install

From a released wheel (attached to each GitHub release), with
[pipx](https://pipx.pypa.io/) so it gets its own isolated environment:

```bash
pipx install ./gatekey_sync-<version>-py3-none-any.whl
```

Or from a source checkout:

```bash
pipx install ./cli-sync          # or: pip install ./cli-sync
```

(If the package is published to PyPI for your organization:
`pipx install gatekey-sync`.)

## Usage

```bash
# One-time: device-code login against your Gatekey deployment
gatekey-sync login --base-url https://gatekey.your-company.com

# One-time: tell it where your AI CLI reads its key from - an env var...
gatekey-sync configure --env-var OPENAI_API_KEY
# ...or a file (with an optional template around the secret)
gatekey-sync configure --write-file ~/.config/my-tool/credentials --template "api_key = {secret}"

# Then run your tool through the wrapper - it injects the current key,
# refreshing it first only if the cached one is stale or rejected:
gatekey-sync exec -- my-ai-cli "explain this diff"

# Or just print the current key, for scripting:
export OPENAI_API_KEY=$(gatekey-sync get-key)
```

## Fleet / MDM preconfiguration

The gateway URL resolves in this order:

1. `--base-url` flag
2. `GATEKEY_SYNC_BASE_URL` environment variable
3. Saved config (written by `login` / `configure`)
4. `http://localhost:8000`

Set `GATEKEY_SYNC_BASE_URL=https://gatekey.your-company.com` machine-wide
(MDM profile, login script, `/etc/environment`, ...) and users only ever run
`gatekey-sync login` — no URL to distribute or mistype.

## Notes

- Works with personal keys (`gk_pk_...`), which support rotation with a
  fetch-current-key mechanism. Service-account keys (`gk_sk_...`) have no
  sync path — see the main repository's known-limitations doc.
- Config and cache live under your platform's user data directory; the
  refresh credential itself lives in the OS keychain.
- Development: `pip install -e ./cli-sync pytest && pytest cli-sync/tests`.
