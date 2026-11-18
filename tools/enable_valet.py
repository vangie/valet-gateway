"""Enable the E-Ink4 signed identity in an existing Passport adapter checkout.

Run on the gateway host with --apply. Backs up only changed files, leaves auth
and Passport compatibility intact, and fails closed on an unfamiliar source.
"""
import argparse
import ast
from pathlib import Path
import shutil

HERMES_OLD = 'client.get("platform") != "esp32c3" or client.get("deviceFamily") != "ai-passport"'
HERMES_NEW = '(client.get("platform"), client.get("deviceFamily")) not in [("esp32c3", "ai-passport"), ("esp32s31", "e-ink4-plus")]'
OPENCLAW_OLD = "params?.client?.deviceFamily !== 'ai-passport'"
OPENCLAW_NEW = "!['ai-passport', 'e-ink4-plus'].includes(params?.client?.deviceFamily)"

def prepare(directory):
    directory = Path(directory)
    for filename, before, after in [('passport_auth.py', HERMES_OLD, HERMES_NEW), ('passport-proxy.mjs', OPENCLAW_OLD, OPENCLAW_NEW)]:
        file = directory / filename
        if not file.is_file():
            continue
        source = file.read_text()
        if after in source:
            return file, source, False
        if source.count(before) != 1:
            raise ValueError(f'Unrecognized adapter source: {file}; no changes made')
        updated = source.replace(before, after)
        if file.suffix == '.py':
            ast.parse(updated)
        return file, updated, True
    raise ValueError('Point to an existing tools/hermes or tools/openclaw adapter directory')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('adapter', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    file, source, changed = prepare(args.adapter)
    if not changed:
        print('Already accepts Valet signed identities'); return
    if not args.apply:
        print(f'Ready: {file}. Add --apply to save with a .before-valet backup.'); return
    backup = file.with_name(file.name + '.before-valet')
    if backup.exists():
        raise ValueError(f'Backup already exists: {backup}; inspect before applying')
    shutil.copy2(file, backup)
    file.write_text(source)
    print(f'Updated {file}. Backup: {backup}. Reload the adapter using its normal lifecycle.')

if __name__ == '__main__':
    main()
