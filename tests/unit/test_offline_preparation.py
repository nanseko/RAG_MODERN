import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts import verify_offline_bundle

ROOT = Path(__file__).resolve().parents[2]


def test_bundle_verifier_rejects_empty_inventory(tmp_path):
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'files': []}), encoding='utf8')
    with pytest.raises(ValueError, match='no files'):
        verify_offline_bundle.verify_manifest(manifest, tmp_path)


def test_bundle_verifier_checks_hash_and_confines_paths(tmp_path):
    import hashlib
    artifact = tmp_path / 'artifact.whl'
    artifact.write_bytes(b'wheel-content')
    record = {'path': 'artifact.whl', 'bytes': 13,
              'sha256': hashlib.sha256(b'wheel-content').hexdigest()}
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'files': [record]}), encoding='utf8')
    assert verify_offline_bundle.verify_manifest(manifest, tmp_path)['verified_files'] == 1
    artifact.write_bytes(b'brokencontent')
    with pytest.raises(ValueError, match='hash'):
        verify_offline_bundle.verify_manifest(manifest, tmp_path)
    record['path'] = '../outside.whl'
    manifest.write_text(json.dumps({'files': [record]}), encoding='utf8')
    with pytest.raises(ValueError, match='path'):
        verify_offline_bundle.verify_manifest(manifest, tmp_path)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows installer execution')
@pytest.mark.parametrize('installer', ['offline-setup.bat', 'setup-cuda.bat'])
def test_offline_installers_target_project_venv_and_never_global_pip(tmp_path, installer):
    # Only the pip package is substituted: the actual batch and venv interpreter run.
    # Running global pip would modify a user's Python; an explicit trap prevents that.
    subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(tmp_path / '.venv')], check=True)
    shutil.copyfile(ROOT / installer, tmp_path / installer)
    (tmp_path / 'pip').mkdir()
    (tmp_path / 'pip' / '__init__.py').write_text('', encoding='utf8')
    (tmp_path / 'pip' / '__main__.py').write_text(
        "import json,sys\nfrom pathlib import Path\n"
        "with Path('pip-calls.jsonl').open('a') as f: f.write(json.dumps({'python':sys.executable,'args':sys.argv[1:]})+'\\n')\n",
        encoding='utf8')
    trap = tmp_path / 'trap'
    trap.mkdir()
    (trap / 'pip.cmd').write_text('@echo off\necho GLOBAL_PIP_CALLED>"%CD%\\global-pip.txt"\nexit /b 1\n', encoding='utf8')
    env = dict(os.environ, PATH=str(trap) + os.pathsep + os.environ['PATH'], PYTHONPATH=str(tmp_path))
    result = subprocess.run(['cmd', '/c', str(tmp_path / installer)], cwd=tmp_path,
                            env=env, input='\n', text=True, capture_output=True, timeout=30)
    assert not (tmp_path / 'global-pip.txt').exists(), result.stdout
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in (tmp_path / 'pip-calls.jsonl').read_text().splitlines()]
    assert len(calls) == 2
    assert all(Path(call['python']).is_relative_to(tmp_path / '.venv') for call in calls)
    assert all('--no-index' in call['args'] for call in calls)
    if installer == "setup-cuda.bat":
        assert "torch==2.10.0+cu128" in calls[0]["args"]
        assert any("wheels-windows-ocr-cu128" in arg for arg in calls[0]["args"])



def test_online_model_preparation_clears_inherited_offline_flags(monkeypatch, tmp_path):
    from scripts import download_ocr_model
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    monkeypatch.setenv('TRANSFORMERS_OFFLINE', '1')
    for name in ('HF_HOME', 'HF_XET_CACHE', 'HF_HUB_DISABLE_IMPLICIT_TOKEN'):
        monkeypatch.setenv(name, '')
    monkeypatch.setattr(sys, 'argv', ['download_ocr_model.py', '--manifest-only', '--destination', str(tmp_path)])
    # Replace the network boundary; inspect the environment delivered to Hub preparation.
    seen = {}
    def prepare(destination):
        seen.update({name: os.getenv(name) for name in ('HF_HUB_OFFLINE', 'TRANSFORMERS_OFFLINE')})
        return {'verified_files': 1}
    monkeypatch.setattr(download_ocr_model, 'prepare_manifest', prepare)
    download_ocr_model.main()
    assert seen == {'HF_HUB_OFFLINE': None, 'TRANSFORMERS_OFFLINE': None}
