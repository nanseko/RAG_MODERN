"""Safety contracts for the opt-in native Windows service launcher."""
import json
import os
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts' / 'windows_services.ps1'
POWERSHELL = shutil.which('pwsh') or shutil.which('powershell')
pytestmark = pytest.mark.skipif(os.name != 'nt' or not POWERSHELL, reason='Windows PowerShell required')


def invoke(*args):
    return subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(SCRIPT), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)


def test_status_is_read_only_for_uninitialized_state():
    state = ROOT / 'data' / 'windows-services-contract-missing'
    assert not state.exists()
    result = invoke('-Action', 'status', '-Service', 'postgres', '-StateRoot', str(state))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['state'] == 'not_initialized'
    assert not state.exists()


def test_rejects_state_outside_project_before_writing(tmp_path):
    state = tmp_path / 'outside'
    result = invoke('-Action', 'init', '-Service', 'postgres', '-StateRoot', str(state))
    assert result.returncode != 0
    assert 'inside the project' in result.stderr
    assert not state.exists()


def test_postgres_init_does_not_overwrite_existing_data():
    state = ROOT / 'data' / 'windows-services-contract-existing'
    cluster = state / 'postgres' / 'data'
    cluster.mkdir(parents=True, exist_ok=False)
    sentinel = cluster / 'keep.txt'
    sentinel.write_text('preserve')
    try:
        result = invoke('-Action', 'init', '-Service', 'postgres', '-StateRoot', str(state))
        assert result.returncode != 0
        assert 'not empty' in result.stderr
        assert sentinel.read_text() == 'preserve'
    finally:
        shutil.rmtree(state)


def test_postgres_init_requires_a_password_before_creating_state(monkeypatch):
    monkeypatch.delenv('RAG_POSTGRES_PASSWORD', raising=False)
    state = ROOT / 'data' / 'windows-services-contract-no-password'
    assert not state.exists()
    result = invoke('-Action', 'init', '-Service', 'postgres', '-StateRoot', str(state))
    assert result.returncode != 0
    assert 'RAG_POSTGRES_PASSWORD' in result.stderr
    assert not state.exists()



@pytest.mark.skipif(not (ROOT / 'vendor/runtime/opensearch-3.9.0/bin/opensearch.bat').is_file(), reason='bundled OpenSearch runtime required')
def test_opensearch_init_creates_loopback_profile_without_starting_service():
    state = ROOT / 'data' / 'windows-services-contract-opensearch'
    assert not state.exists()
    try:
        result = invoke('-Action', 'init', '-Service', 'opensearch', '-StateRoot', str(state), '-Port', '19201', '-LocalOnly')
        assert result.returncode == 0, result.stderr
        config = (state / 'opensearch' / 'config' / 'opensearch.yml').read_text()
        assert 'network.host: 127.0.0.1' in config
        assert 'http.port: 19201' in config
        assert not (state / 'opensearch' / 'opensearch.pid').exists()
        status = invoke('-Action', 'status', '-Service', 'opensearch', '-StateRoot', str(state))
        assert json.loads(status.stdout)['state'] == 'stopped'
    finally:
        if state.exists():
            shutil.rmtree(state)


def test_postgres_stop_refuses_pid_reused_by_another_process():
    import os
    state = ROOT / 'data' / 'windows-services-contract-pid'
    service = state / 'postgres'
    (service / 'data').mkdir(parents=True, exist_ok=False)
    (service / 'service.json').write_text(json.dumps({'service': 'postgres', 'state_directory': str(service), 'port': 15433}))
    (service / 'data' / 'postmaster.pid').write_text(str(os.getpid()))
    try:
        result = invoke('-Action', 'stop', '-Service', 'postgres', '-StateRoot', str(state))
        assert result.returncode != 0
        assert 'PID identity mismatch' in result.stderr
    finally:
        shutil.rmtree(state)
