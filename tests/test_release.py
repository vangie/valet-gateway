import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('gateway_release', ROOT / 'tools/release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def test_tarball_is_complete_and_not_symlinks(self):
        import tarfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'adapter.py';source.write_text('fixture')
            target = root / 'plugin.tar.gz'
            release.tarball(target, 'plugin', {'adapter.py': source})
            with tarfile.open(target) as archive:
                self.assertEqual(archive.getnames(), ['plugin/adapter.py'])
                self.assertEqual(archive.extractfile('plugin/adapter.py').read(), b'fixture')
            source.unlink();source.symlink_to(target)
            with self.assertRaises(ValueError):
                release.tarball(root / 'bad.tar.gz', 'plugin', {'adapter.py': source})

    def test_package_receipt_rejects_tampering(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / 'adapter.tgz';payload.write_bytes(b'fixture')
            digest = hashlib.sha256(payload.read_bytes()).hexdigest()
            receipt = {'tag': 'v' + release.VERSION, 'commit_sha': 'a' * 40,
                       'assets': {'adapter.tgz': {'size': 7, 'sha256': digest}}}
            manifest = root / 'release-manifest.json';manifest.write_text(json.dumps(receipt))
            (root / 'SHA256SUMS.txt').write_text(f'{digest}  adapter.tgz\n' +
                hashlib.sha256(manifest.read_bytes()).hexdigest() + '  release-manifest.json\n')
            release.verify(root, 'a' * 40)
            payload.write_text('tampered')
            with self.assertRaises(ValueError):release.verify(root, 'a' * 40)

    def test_versions_independent_and_plugin_ids_unchanged(self):
        package = json.loads((ROOT / 'tools/openclaw/package.json').read_text())
        self.assertEqual(package['name'], 'ai-passport-openclaw')
        self.assertEqual(package['version'], '0.8.0')
        self.assertIn('version: 0.8.0', (ROOT / 'tools/hermes/plugin.yaml').read_text())
        self.assertEqual(release.VERSION, '0.2.0')


if __name__ == '__main__':unittest.main()
