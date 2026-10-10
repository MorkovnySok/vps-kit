#!/usr/bin/env python3
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'src' / 'vpsctl.py'
with tempfile.TemporaryDirectory() as temp:
    p = Path(temp)
    (p/'projects').mkdir()
    (p/'config.json').write_text(json.dumps({'public_ip':'198.51.100.10','ssh_port':2222}))
    env = dict(os.environ, VPSKIT_ALLOW_NONROOT='1',
        VPSKIT_CONFIG=str(p/'config.json'), VPSKIT_PROJECTS=str(p/'projects'),
        VPSKIT_APPS=str(p/'apps'), VPSKIT_KEY_DIR=str(p/'keys'),
        VPSKIT_AUTH_KEYS=str(p/'ssh'/'authorized_keys'),
        VPSKIT_CADDY_DIR=str(p/'caddy'),
        PATH=f'{ROOT}/tests/mockbin:' + os.environ['PATH'])
    def cli(*args, **kwargs):
        return subprocess.run([sys.executable,str(BASE),*args],env=env,text=True,capture_output=True,**kwargs)
    result=cli('project','add','myapp','--repo','Example/MyApp','--port','8090',
               '--compose','deploy/compose.yaml','--no-caddy')
    assert result.returncode==0, result.stderr
    assert (p/'apps/myapp/shared/.env').exists()
    assert 'restrict' in (p/'ssh/authorized_keys').read_text()
    assert not (p/'projects/myapp.json').stat().st_mode & 0o002
    workflow=cli('workflow','myapp')
    assert workflow.returncode==0, workflow.stderr
    txt=workflow.stdout
    doc=yaml.safe_load(txt)
    assert doc['jobs']['deploy']['runs-on']=='ubuntu-latest'
    build=doc['jobs']['deploy']['steps'][3]['run']
    deploy=doc['jobs']['deploy']['steps'][4]['run']
    assert '${#build_services[@]}' in build
    assert '${GITHUB_REPOSITORY,,}' in build
    assert '${build_services[@]}' in build
    assert '${DEPLOY_PORT:-22}' in deploy
    assert '${{ secrets.DEPLOY_SSH_KEY }}' in txt
    for script in [build, deploy]:
        check=subprocess.run(['bash','-n'], input=script, text=True, capture_output=True)
        assert check.returncode == 0, check.stderr
    duplicate=cli('project','add','myapp','--repo','Example/MyApp','--port','8090','--no-caddy')
    assert duplicate.returncode !=0
    dup_port=cli('project','add','another','--repo','Example/Another','--port','8090','--no-caddy')
    assert dup_port.returncode !=0
    invalid=cli('project','add','../escape','--repo','Example/MyApp','--port','8091','--no-caddy')
    assert invalid.returncode !=0
    # Extractor accepts ordinary files, refuses .env and traversal archives.
    def extract(tar_bytes):
        dest=p/'bundle'; dest.mkdir(exist_ok=True)
        return subprocess.run([sys.executable,str(ROOT/'src/extract-bundle.py'),str(dest)],
                              input=tar_bytes,capture_output=True)
    def archive(filename,content):
        f=io.BytesIO()
        with tarfile.open(fileobj=f,mode='w:gz') as t:
            info=tarfile.TarInfo(filename)
            info.size=len(content)
            t.addfile(info,io.BytesIO(content))
        return f.getvalue()
    assert extract(archive('compose.yaml',b'services: {}')).returncode==0
    assert extract(archive('.env',b'TOKEN=secret')).returncode!=0
    assert extract(archive('../escape',b'danger')).returncode!=0
print('All offline tests passed')
