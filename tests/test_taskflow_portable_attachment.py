from __future__ import annotations
import hashlib, importlib.machinery, importlib.util, json, os, sys
from pathlib import Path
SCRIPT=Path(__file__).resolve().parents[1]/'scripts'/'cognilode-taskflow-phase-controller'
def load():
    loader=importlib.machinery.SourceFileLoader('taskflow_portable_test',str(SCRIPT)); spec=importlib.util.spec_from_loader(loader.name,loader); m=importlib.util.module_from_spec(spec); sys.modules[loader.name]=m; loader.exec_module(m); return m

def test_portable_attachment_adds_content_addressed_https_mirror(monkeypatch,tmp_path):
    m=load(); p=tmp_path/'research.zip'; p.write_bytes(b'x'); d='a'*64
    monkeypatch.setenv('COGNILODE_TASKFLOW_ATTACHMENT_MIRROR_BASE','https://launchpad.example/artifacts')
    row=m.portable_attachment_ref(p,d)
    assert row['ref']=='file:'+str(p)
    assert row['sha256']==d
    assert row['mirrors']==['https://launchpad.example/artifacts/'+d]

def test_portable_attachment_rejects_non_https_mirror(monkeypatch,tmp_path):
    m=load(); p=tmp_path/'research.zip'; p.write_bytes(b'x')
    monkeypatch.setenv('COGNILODE_TASKFLOW_ATTACHMENT_MIRROR_BASE','http://lan.invalid')
    try: m.portable_attachment_ref(p,'b'*64)
    except RuntimeError as e: assert 'https://' in str(e)
    else: raise AssertionError('expected rejection')

def test_private_s3_mirror_publishes_and_reads_back_exact_attachment(monkeypatch,tmp_path):
    m=load(); p=tmp_path/'research.zip'; p.write_bytes(b'research packet')
    digest=hashlib.sha256(p.read_bytes()).hexdigest()
    monkeypatch.setenv('COGNILODE_TASKFLOW_ATTACHMENT_S3_BUCKET','private-launchpad')
    monkeypatch.setenv('COGNILODE_AWS_CLI','fake-aws')
    calls=[]
    class Result:
        def __init__(self,returncode=0,stdout=''):self.returncode=returncode;self.stdout=stdout
    def run(argv,**_kwargs):
        calls.append(argv)
        if argv[2]=='head-object' and len(calls)==1:return Result(1)
        if argv[2]=='head-object':return Result(stdout=json.dumps({'ContentLength':p.stat().st_size,'Metadata':{'sha256':digest}}))
        return Result()
    monkeypatch.setattr(m.subprocess,'run',run)
    row=m.portable_attachment_ref(p,digest)
    assert row['mirrors']==[f's3://private-launchpad/taskflow-artifacts/sha256/{digest}.zip']
    assert [x[2] for x in calls]==['head-object','put-object','head-object']
