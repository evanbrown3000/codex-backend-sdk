from __future__ import annotations
import hashlib, importlib.machinery, importlib.util, json, os, re, stat, subprocess, tempfile, unittest, urllib.error, zipfile
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
CTRL=ROOT/'scripts/cognilode-taskflow-phase-controller'
WORKER=ROOT/'scripts/cognilode-chatmode-queue-worker'
_fixture_dir=tempfile.TemporaryDirectory()
PLAN=Path(_fixture_dir.name)/'plans'/'agent-memory.plan'
PLAN.parent.mkdir(parents=True)
PLAN.write_text('''project Agent Memory / Memory Stock
id agent-memory
[x] AM-0 inherited work
[ ] AM-5 canonical memory integration
    done_when: verified central readback
    effect_probe_command: /usr/bin/printf active
    effect_probe_expected: active
[ ] AM-6 storage migration
[ ] AM-7 citation expansion
[ ] AM-8 foreground rollover
    owner: Nadia Brooks
    depends_on: AM-5
[ ] AM-9 project acceptance
    depends_on: AM-5 AM-6 AM-7 AM-8
''',encoding='utf-8')

def load(name,path):
    loader=importlib.machinery.SourceFileLoader(name,str(path)); spec=importlib.util.spec_from_loader(loader.name,loader)
    mod=importlib.util.module_from_spec(spec); import sys; sys.modules[name]=mod; loader.exec_module(mod); return mod

c=load('ctrl',CTRL)
w=load('worker',WORKER)

def manifest_zip(path:Path, plan_sha:str, files:dict[str,bytes]):
    rows=[]
    for name,data in files.items(): rows.append({'path':name,'sha256':hashlib.sha256(data).hexdigest()})
    manifest=json.dumps({'schema':'test.manifest.v1','plan_sha256':plan_sha,'files':rows},sort_keys=True).encode()
    path.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
        for name,data in files.items(): z.writestr(name,data)
        z.writestr('MANIFEST.json',manifest)

class FakeQueue:
    def __init__(self): self.jobs={}; self.gen=0
    def get(self,jid): return self.jobs.get(jid)
    def enqueue(self,job):
        if job['id'] not in self.jobs:
            x=json.loads(json.dumps(job)); x.setdefault('effect_evidence',[]); self.jobs[x['id']]=x
        return self.jobs[job['id']]
    def _ready(self,j): return all(self.jobs.get(d,{}).get('state')=='complete' for d in j.get('taskflow_dependencies',[]))
    def claim(self,provider,worker_id):
        for j in self.jobs.values():
            if j['provider']==provider and j['state']=='queued' and self._ready(j):
                self.gen+=1; j.update(state='claimed',claimed_by=worker_id,lease_token=f't{self.gen}',lease_generation=self.gen); return j
        return None
    def begin(self,j):
        cur=self.jobs[j['id']]
        if cur.get('state')!='claimed' or cur.get('lease_token')!=j.get('lease_token'): return {'ok':False}
        cur['state']='effect_pending'; return {'ok':True,'job':cur}
    def complete(self,j,evidence,conversation_id=None):
        cur=self.jobs[j['id']]
        if cur.get('state')!='effect_pending': return {'ok':False,'error':'bad_state'}
        cur['state']='complete'; cur['effect_evidence']=list(evidence)
        if conversation_id: cur['conversation_id']=conversation_id
        return {'ok':True,'job':cur}
    def post(self,body):
        if body['operation']=='list_jobs':
            rows=list(self.jobs.values())
            for k in ('provider','state'):
                if body.get(k): rows=[j for j in rows if j.get(k)==body[k]]
            return {'jobs':rows[:body.get('limit',100)]}
        raise AssertionError(body)

class Tests(unittest.TestCase):
    def test_named_employee_receives_slack_role_source(self):
        role=c.slack_role_context('Elliot Mercer')
        self.assertIn('Principal Platform Systems Engineer',role)
        self.assertIn('slack.com/archives/',role)
        with self.assertRaisesRegex(ValueError,'Slack role missing'):
            c.slack_role_context('Unnamed Employee')

    def test_repo_plan_must_match_committed_instruction(self):
        with tempfile.TemporaryDirectory() as td:
            repo=Path(td); (repo/'plans').mkdir(); source=repo/'plans'/'p.plan'
            source.write_text('project P\nid p\n[ ] A example\n',encoding='utf-8')
            subprocess.run(['git','init','-q',str(repo)],check=True)
            subprocess.run(['git','-C',str(repo),'add','plans/p.plan'],check=True)
            subprocess.run(['git','-C',str(repo),'-c','user.name=Test','-c','user.email=test@example.test','commit','-qm','plan'],check=True)
            c.verify_plan_revision(c.parse_plan(source))
            source.write_text('project P\nid p\n[ ] A altered\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'committed project instruction'):
                c.verify_plan_revision(c.parse_plan(source))

    def test_independent_probe_is_required_and_observed(self):
        plan=c.parse_plan(PLAN); step=plan.by_id()['AM-5']
        proof=c.verify_effect_probe(step)
        self.assertEqual(proof['observed'],'active')
        self.assertEqual(len(proof['sha256']),64)
        from dataclasses import replace
        bad=replace(step,fields={'effect_probe_command':'/usr/bin/printf missing','effect_probe_expected':'active'})
        with self.assertRaisesRegex(ValueError,'independent effect probe failed'):
            c.verify_effect_probe(bad)
        missing=replace(step,fields={})
        with self.assertRaisesRegex(ValueError,'effect_probe_command'):
            c.verify_effect_probe(missing)

    def test_exact_get_job_and_taskflow_plan_proof(self):
        class Sender:
            def __init__(self): self.calls=[]
            def operator_memory_post(self,body):
                self.calls.append(body)
                if body['operation']=='get_job':
                    return {'ok':True,'job':{'id':body['job_id'],'state':'complete'}} if body['job_id']=='exact-complete' else {'ok':False,'error':'job_not_found'}
                if body['operation']=='enqueue_job':
                    saved=dict(body, state='queued', prompt_author='taskflow_plan')
                    saved['taskflow_plan_receipt']={'plan_sha256':body['plan_revision'],'plan_source_ref':body['plan_source_ref'],
                                                  'taskflow_step':body['taskflow_step'],'phase':body['phase']}
                    return {'ok':True,'job':saved}
                raise AssertionError(body)
        sender=Sender(); q=c.QueueClient(sender); plan=c.parse_plan(PLAN); step=plan.by_id()['AM-5']
        self.assertEqual(q.get('exact-complete')['state'],'complete')
        job=c.materialize_research_job(q,plan,step,50,role='Elliot Mercer',state_root=Path(_fixture_dir.name))
        body=sender.calls[-1]
        self.assertEqual(body['provider'],'codex.research')
        self.assertEqual(body['prompt_authority'],'taskflow_plan')
        self.assertEqual(body['assigned_employee'],'Elliot Mercer')
        self.assertEqual(body['plan_source_ref'],'file:'+str(PLAN))
        self.assertEqual(body['plan_revision'],hashlib.sha256(PLAN.read_bytes()).hexdigest())
        self.assertEqual(body['plan_text'],PLAN.read_text())
        self.assertIn(PLAN.read_text(),body['prompt'])
        self.assertEqual(job['id'],body['job_id'])
        self.assertFalse(any(row['operation']=='list_jobs' for row in sender.calls))

    def test_get_job_404_is_missing_but_other_errors_fail_closed(self):
        class Sender:
            def __init__(self,code,message): self.code=code; self.message=message
            def operator_memory_post(self,body):
                try: raise urllib.error.HTTPError('https://fixture',self.code,'error',{},None)
                except urllib.error.HTTPError as cause: raise RuntimeError(self.message) from cause
        self.assertIsNone(c.QueueClient(Sender(404,'job_not_found')).get('missing'))
        with self.assertRaises(RuntimeError): c.QueueClient(Sender(500,'job_not_found')).get('missing')

    def test_claim_requires_exact_employee_and_fence(self):
        class Sender:
            def operator_memory_post(self,body):
                return {'jobs':[{'id':'j','provider':body['provider'],'claimed_by':'other',
                                 'lease_token':'t','lease_generation':1}]}
        with self.assertRaisesRegex(RuntimeError,'employee'):
            c.QueueClient(Sender()).claim('codex.research','Elliot Mercer')

    def test_plan_revision_stays_immutable(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'plans'/'p.plan'; path.parent.mkdir()
            path.write_text('project Sample\nid sample\n[ ] A step\n')
            plan=c.parse_plan(path)
            c.verify_plan_revision(plan)
            path.write_text(path.read_text()+'changed\n')
            with self.assertRaisesRegex(ValueError,'plan source changed'):
                c.verify_plan_revision(plan)

    def test_secretary_dispatch_is_constrained_to_installed_codex(self):
        with tempfile.TemporaryDirectory() as td:
            td=Path(td); prompt=td/'prompt.md'; prompt.write_text('full phase instructions')
            secretary=td/'secretary.py'
            secretary.write_text('''#!/usr/bin/env python3
import json,sys
from pathlib import Path
args=sys.argv
assert args[1]=='dispatch' and '--candidates-json' in args and '--prefer-installed' in args and '--shell' in args
assert '--external-effect' in args and args[args.index('--external-effect')+1]=='shell'
assert '--plan-file' in args and Path(args[args.index('--plan-file')+1]).read_text()=='full phase instructions'
candidate=json.loads(Path(args[args.index('--candidates-json')+1]).read_text())
assert len(candidate)==1 and candidate[0]['family']=='installed_codex'
print(json.dumps({'completed':True,'final_candidate':{'family':'chatgpt','name':'wrong-route'}}))
'''); secretary.chmod(secretary.stat().st_mode|stat.S_IXUSR)
            vanilla=td/'vanilla-codex'; vanilla.write_text('#!/bin/sh\nexit 0\n'); vanilla.chmod(0o755)
            with mock.patch.dict(os.environ, {'COGNILODE_CODEX_BIN':str(vanilla)}):
                receipt=c.run_secretary(secretary,prompt,employee='Elliot Mercer',manager='m',repo_path=ROOT,phase='external_effect')
            self.assertEqual(receipt['returncode'],0)
            self.assertFalse(c.secretary_succeeded(receipt))
            self.assertEqual(receipt['taskflow_codex_provenance']['resolved_path'],str(vanilla))

    def test_modified_release_requires_matching_manifest_digest(self):
        with tempfile.TemporaryDirectory() as td:
            release=Path(td); binary=release/'codex'; binary.write_bytes(b'modified release fixture')
            binary.chmod(0o755)
            manifest={'source_commit':'a'*40,'sha256':hashlib.sha256(binary.read_bytes()).hexdigest()}
            (release/'manifest.json').write_text(json.dumps(manifest))
            self.assertEqual(c.codex_binary_identity(str(binary))[0],'modified_codex')
            launcher=release/'launcher'; launcher.write_text('#!/bin/sh\nrelease_dir="$(dirname "$0")"\n')
            linked=release/'linked-launcher'; linked.symlink_to(launcher)
            self.assertEqual(c.codex_binary_identity(str(linked))[0],'installed_codex')
            launcher.write_text('#!/bin/sh\nrelease_dir="$(dirname "$(readlink -f -- "$0")")"\n')
            self.assertEqual(c.codex_binary_identity(str(linked))[0],'modified_codex')
            manifest['sha256']='0'*64; (release/'manifest.json').write_text(json.dumps(manifest))
            self.assertEqual(c.codex_binary_identity(str(binary))[0],'installed_codex')

    def test_source_commit_receipt_cannot_complete_external_effect(self):
        plan=c.parse_plan(PLAN); step=plan.by_id()['AM-5']
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'effect.json'
            path.write_text(json.dumps({'schema':'cognilode.taskflow.external_effect.v1','status':'applied',
                'project_id':plan.project_id,'plan_sha256':plan.sha256,'step_id':step.step_id,
                'work_zip_sha256':'a'*64,'effect_kind':'source_commit','effect_ref':'b'*40,
                'environment':'fixture','checks':[{'command':'git log','exit_code':0,'result':'commit exists'}]}))
            with self.assertRaisesRegex(ValueError,'not an external effect'):
                c.verify_effect_receipt(path,plan=plan,step=step,work_sha256='a'*64)

    def test_actual_plan_graph_and_structural_completion_barrier(self):
        p=c.parse_plan(PLAN); self.assertEqual(p.sha256,hashlib.sha256(PLAN.read_bytes()).hexdigest())
        by=p.by_id(); self.assertEqual(by['AM-8'].dependencies,('AM-5',)); self.assertEqual(set(by['AM-9'].dependencies),{'AM-5','AM-6','AM-7','AM-8'})
        chat=c.phase_job_id(p,'AM-5','chatgpt_sandbox'); effect=c.phase_job_id(p,'AM-5','external_effect')
        self.assertNotEqual(chat,effect)
        self.assertEqual(c.final_dependency_ids(p,by['AM-8']),[effect])
        self.assertNotIn(chat,c.final_dependency_ids(p,by['AM-8']))

    def test_zip_verifier_hashes_every_member_and_requires_external_instructions(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'good.zip'; plan='a'*64
            manifest_zip(path,plan,{'EXTERNAL_EFFECT_INSTRUCTIONS.md':b'deploy carefully','src/a.py':b'print(1)'})
            meta=c.verify_manifest_zip(path,expected_plan_sha256=plan,require_external_instructions=True)
            self.assertEqual(meta['sha256'],hashlib.sha256(path.read_bytes()).hexdigest()); self.assertTrue(meta['instructions_sha256'])
            bad=Path(td)/'bad.zip'; manifest_zip(bad,plan,{'src/a.py':b'x'})
            with self.assertRaisesRegex(ValueError,'EXTERNAL_EFFECT_INSTRUCTIONS'): c.verify_manifest_zip(bad,expected_plan_sha256=plan,require_external_instructions=True)

    def test_external_work_zip_must_keep_queued_sha(self):
        plan=c.parse_plan(PLAN); step=plan.by_id()['AM-5']; q=FakeQueue()
        with tempfile.TemporaryDirectory() as td:
            td=Path(td); path=td/'work.zip'
            manifest_zip(path,plan.sha256,{'EXTERNAL_EFFECT_INSTRUCTIONS.md':b'apply','patch.txt':b'first'})
            meta=c.verify_manifest_zip(path,expected_plan_sha256=plan.sha256,require_external_instructions=True)
            job=c.ensure_external_job(q,plan,step,50,meta,state_root=td,external_employee='Rina Hale')
            manifest_zip(path,plan.sha256,{'EXTERNAL_EFFECT_INSTRUCTIONS.md':b'apply','patch.txt':b'second'})
            with self.assertRaisesRegex(ValueError,'queued SHA-256'):
                c.verified_work_attachment(job,plan)

    def test_external_worker_stages_hash_bound_work_zip_when_original_device_path_is_absent(self):
        plan=c.parse_plan(PLAN)
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'work.zip'
            manifest_zip(path,plan.sha256,{'EXTERNAL_EFFECT_INSTRUCTIONS.md':b'apply', 'patch.txt':b'change'})
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            row={'ref':'file:/first-device/only/work.zip','sha256':digest,
                 'mirrors':[f's3://private-bucket/taskflow-artifacts/sha256/{digest}.zip']}
            job={'attachment_refs':[row]}
            with mock.patch.object(c,'stage_external_work_zip',return_value=path) as stage:
                verified=c.verified_work_attachment(job,plan)
            stage.assert_called_once_with(row,digest)
            self.assertEqual(verified['sha256'],digest)
            with mock.patch.object(c,'stage_external_work_zip',return_value=path):
                with self.assertRaisesRegex(ValueError,'queued SHA-256'):
                    c.verified_work_attachment({'attachment_refs':[{**row,'sha256':'0'*64}]},plan)

    def test_worker_refuses_multiphase_chatgpt_completion_without_instruction_file(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'work.zip'; path.write_bytes(b'not zip')
            value={'assistant_terminal':True,'conversation_id':'c1','central_conversation_store':{'central_readback_verified':True,'conversation_id':'c1'},'downloaded_files':[{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'name':'work.zip'}]}
            self.assertIsNone(w.validated_result(value))
            good=Path(td)/'good.zip'
            with zipfile.ZipFile(good,'w') as z:z.writestr('EXTERNAL_EFFECT_INSTRUCTIONS.md','x')
            value['downloaded_files']=[{'path':str(good),'sha256':hashlib.sha256(good.read_bytes()).hexdigest(),'name':'good.zip'}]
            checked=w.validated_result(value); self.assertIsNotNone(checked)
            self.assertEqual(checked[1][0]['sha256'],hashlib.sha256(good.read_bytes()).hexdigest())

    def test_worker_never_marks_non_sandbox_multiphase_job_complete_from_provider_result(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'good.zip'
            with zipfile.ZipFile(path,'w') as z:z.writestr('EXTERNAL_EFFECT_INSTRUCTIONS.md','apply')
            value={'assistant_terminal':True,'conversation_id':'c1','central_conversation_store':{'central_readback_verified':True,'conversation_id':'c1'},'downloaded_files':[{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'name':'good.zip'}]}
            self.assertFalse(w.selected_for_device({'id':'j','provider':'codex.research','phase':'research',
                                                    'taskflow_multi_phase':True,'claimed_by':'rhythm:evanpc:1',
                                                    'rhythm_tape_sha256':'x','rhythm_slot_index':1}))

    def test_external_effect_pending_is_reconciled_from_durable_receipt_without_resend(self):
        plan=c.parse_plan(PLAN); q=FakeQueue(); step=plan.by_id()['AM-5']
        with tempfile.TemporaryDirectory() as td:
            td=Path(td); work=td/'work.zip'; manifest_zip(work,plan.sha256,{'EXTERNAL_EFFECT_INSTRUCTIONS.md':b'apply','patch.txt':b'x'})
            meta=c.verify_manifest_zip(work,expected_plan_sha256=plan.sha256,require_external_instructions=True)
            job=c.ensure_external_job(q,plan,step,50,meta,state_root=td/'state',external_employee='Rina Hale')
            claimed=q.claim('codex.external-effect','Rina Hale'); self.assertEqual(claimed['id'],job['id']); self.assertTrue(q.begin(claimed)['ok'])
            receipt=td/'state'/plan.project_id/step.step_id/'external_effect'/job['id']/'EXTERNAL_EFFECT_RESULT.json'; receipt.parent.mkdir(parents=True,exist_ok=True)
            receipt.write_text(json.dumps({'schema':'cognilode.taskflow.external_effect.v1','status':'applied','project_id':plan.project_id,'plan_sha256':plan.sha256,'step_id':step.step_id,'work_zip_sha256':meta['sha256'],'effect_kind':'deployment','effect_ref':'deploy:recovered','environment':'fixture','checks':[{'command':'readback','exit_code':0,'result':'effect exists'}],'defects':[]}))
            receipt.with_name('secretary_receipt.json').write_text(json.dumps({'taskflow_route_verified':True}))
            result=c.run_once(queue=q,plan=plan,role='Elliot Mercer',secretary=Path('/bin/false'),state_root=td/'state',worker_id='unused',manager='m',priority=50,external_employee='Rina Hale',seed_step='AM-5')
            self.assertEqual(q.get(job['id'])['state'],'complete')
            self.assertTrue(any(x.get('phase')=='external_effect_reconcile' and x.get('ok') for x in result['actions']))

    def test_end_to_end_mechanical_slice_releases_successor_only_after_external_effect(self):
        plan=c.parse_plan(PLAN); q=FakeQueue()
        with tempfile.TemporaryDirectory() as td:
            td=Path(td); secretary=td/'fake-secretary.py'
            secretary.write_text(r'''#!/usr/bin/env python3
import hashlib,json,re,sys,zipfile
from pathlib import Path
args=sys.argv; prompt=Path(args[args.index('--plan-file')+1]).read_text()
if 'named Codex research employee' in prompt:
    out=Path(re.search(r'ZIP at exactly:\n([^\n]+)',prompt).group(1)); psha=re.search(r'Plan SHA-256: ([0-9a-f]{64})',prompt).group(1)
    out.parent.mkdir(parents=True,exist_ok=True); data=b'researched source'; rows=[{'path':'RESEARCH_REPORT.md','sha256':hashlib.sha256(data).hexdigest()}]
    with zipfile.ZipFile(out,'w') as z:
      z.writestr('RESEARCH_REPORT.md',data); z.writestr('MANIFEST.json',json.dumps({'plan_sha256':psha,'files':rows},sort_keys=True))
elif 'Codex external-effect employee' in prompt:
    out=Path(re.search(r'write JSON to exactly ([^ ]+) with schema',prompt).group(1)); psha=re.search(r'Plan SHA-256: ([0-9a-f]{64})',prompt).group(1); step=re.search(r'Step: ([^ ]+)',prompt).group(1); wsha=re.search(r'Work ZIP SHA-256: ([0-9a-f]{64})',prompt).group(1)
    out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps({'schema':'cognilode.taskflow.external_effect.v1','status':'applied','project_id':'agent-memory','plan_sha256':psha,'step_id':step,'work_zip_sha256':wsha,'effect_kind':'test_deployment','effect_ref':'deploy:test-123','environment':'native-test-fixture','checks':[{'command':'fixture verification','exit_code':0,'result':'observed deployed state'}],'defects':[]}))
candidate=json.loads(Path(args[args.index('--candidates-json')+1]).read_text())[0]
print(json.dumps({'completed':True,'final_candidate':{'family':candidate['family'],'name':candidate['name']},'final_assessment':{'terminal':True}}))
''')
            secretary.chmod(secretary.stat().st_mode|stat.S_IXUSR)
            state=td/'state'
            first=c.run_once(queue=q,plan=plan,role='Elliot Mercer',secretary=secretary,state_root=state,worker_id='test-worker',manager='m',priority=50,external_employee='Rina Hale')
            research=q.get(c.phase_job_id(plan,'AM-5','research')); self.assertEqual(research['state'],'complete')
            chat=q.get(c.phase_job_id(plan,'AM-5','chatgpt_sandbox')); self.assertEqual(chat['state'],'queued')
            self.assertIsNone(q.get(c.phase_job_id(plan,'AM-5','external_effect')))
            self.assertIsNone(q.get(c.phase_job_id(plan,'AM-8','research')), 'AM-8 must not release after research')
            # Simulate the proven B4PT0R chat-mode worker completing only its phase after central readback.
            work=td/'work.zip'; manifest_zip(work,plan.sha256,{'EXTERNAL_EFFECT_INSTRUCTIONS.md':b'apply it','patch.txt':b'change'})
            chat.update(state='complete',effect_evidence=[{'kind':'provider_conversation','ref':'conv-1'},{'kind':'central_conversation_readback','ref':'conv-1'},{'kind':'chatgpt_sandbox_artifact','ref':hashlib.sha256(work.read_bytes()).hexdigest(),'path':str(work)}])
            second=c.run_once(queue=q,plan=plan,role='Elliot Mercer',secretary=secretary,state_root=state,worker_id='test-worker',manager='m',priority=50,external_employee='Rina Hale')
            effect=q.get(c.phase_job_id(plan,'AM-5','external_effect')); self.assertEqual(effect['state'],'complete')
            # One more controller recurrence observes committed effect completion and releases AM-8 research.
            third=c.run_once(queue=q,plan=plan,role='Elliot Mercer',secretary=secretary,state_root=state,worker_id='test-worker',manager='m',priority=50,external_employee='Rina Hale')
            self.assertEqual(q.get(c.phase_job_id(plan,'AM-8','research'))['assigned_employee'],'Nadia Brooks')
            self.assertTrue(any(x.get('phase')=='external_effect_complete' and x.get('ok') for x in second['actions']))

if __name__=='__main__': unittest.main()
