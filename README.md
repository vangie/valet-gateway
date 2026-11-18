# Valet Gateway · 随行网关

Shared OpenClaw and Hermes adapters for AI Passport and Metalio E-Ink4 Plus. Firmware repositories pin this repository as a Git submodule; adapters are installed on the user's gateway host.

## Compatibility first

This initial extraction preserves the existing `ai-passport-openclaw` and `ai-passport-hermes` plugin IDs, `/passport/v1` endpoint, signed v3 handshake, pairing-code encoding and persisted token stores. Passport users do not need to re-pair merely because the source repository moves. An additional signed device identity, `esp32s31 / e-ink4-plus`, is accepted. Existing Passport sessions and formats remain supported.

Source baseline: `vangie/ai-passport-agent`, commit `8fd7f8b793a3efc0e87be2ecefad0d2bc1fca226` (adapter version 0.7.5). Original copyright and licenses are retained. No user gateway configs, credentials, device tokens or firmware drivers are included.

- [OpenClaw installation](tools/openclaw/README.zh_CN.md)
- [Hermes installation](tools/hermes/README.zh_CN.md)
- [Networking](docs/networking.zh_CN.md)
- [Device protocol](protocol/README.md)

[Valet Gateway v0.1.0](https://github.com/vangie/valet-gateway/releases/tag/v0.1.0)
is the first independent release; adapters retain version 0.7.5. Read the
[release notes](docs/release/v0.1.0.md) and verify `SHA256SUMS.txt` and
`release-manifest.json` before installation. Firmware downloads remain in the
respective device repositories. Do not change deployed plugin IDs or re-pair
existing devices merely because the source moves.

## Firmware consumption

```sh
git submodule add https://github.com/vangie/valet-gateway.git integrations/valet-gateway
git submodule update --init --recursive
```

Update the submodule commit explicitly after compatibility tests. A firmware upgrade does not automatically upgrade or restart a user's gateway. Gateway releases and firmware releases remain independent.

## Tests

```sh
node tests/test_passport_plugin.mjs
node tests/test_passport_relay.mjs
node tests/test_passport_ima.mjs
python3 -m unittest discover -s tests -p 'test_passport_hermes*.py'
```

Python tests require `aiohttp` and `cryptography`. Interoperability with real gateways and physical devices is tracked separately from unit-test results.

## Release packaging

After tests and a reviewed commit, run `python3 tools/release.py package build/release`.
The package command refuses a dirty checkout and allowlists plugin runtime files.
Verify downloaded release files with `python3 tools/release.py verify DIRECTORY --commit FULL_COMMIT`.
Publication and gateway deployment are separate actions.

Development branch `feature/book-context-v1` adds opt-in current-page questions and isolated book sessions. See the protocol contract; deployed v0.1.0 remains unchanged. This extension is not yet a released adapter version.
