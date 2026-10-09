import hashlib
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/cognilode-decisionx-refresh-queued-packet'


def load():
    loader = importlib.machinery.SourceFileLoader('decisionx_refresh_queued_packet_test', str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_packet_is_deterministic_and_hashes_exact_deployed_source(monkeypatch, tmp_path):
    module = load()
    sdk_sha, site_sha = 'a' * 40, 'b' * 40
    monkeypatch.setattr(module, 'git', lambda repo, *args:
                        (sdk_sha + '\n').encode() if args[0] == 'rev-parse' else b'')
    monkeypatch.setattr(module, 'source', lambda repo, revision, path:
                        (revision + ':' + path + '\n').encode())
    release = {'schema': 'cognilode.customer-release.v4-current-surface',
               'source_sha': site_sha}
    first = module.build_packet(tmp_path, tmp_path, release, tmp_path)
    second = module.build_packet(tmp_path, tmp_path, release, tmp_path)
    assert first['sha256'] == second['sha256']
    with zipfile.ZipFile(first['path']) as archive:
        manifest = json.loads(archive.read('MANIFEST.json'))
        assert manifest['sdk_revision'] == sdk_sha
        assert manifest['deployed_site_revision'] == site_sha
        assert len(manifest['members']) == len(module.SITE_PATHS) + len(module.SDK_PATHS) + 2
        for name, expected in manifest['members'].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == expected
        source_path = 'source/site/' + module.SITE_PATHS[0]
        assert archive.read(source_path) == (site_sha + ':' + module.SITE_PATHS[0] + '\n').encode()
