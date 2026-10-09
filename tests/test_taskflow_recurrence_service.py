from __future__ import annotations
import importlib.machinery, importlib.util, json, os, stat, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
SERVICE=ROOT/'scripts/cognilode-taskflow-phase-controller-service'
UNIT=ROOT/'deploy/systemd/user/cognilode-taskflow-phase-controller.service'

def load():
    loader=importlib.machinery.SourceFileLoader('recurrence_service',str(SERVICE))
    spec=importlib.util.spec_from_loader(loader.name,loader)
    mod=importlib.util.module_from_spec(spec); loader.exec_module(mod); return mod

class RecurrenceTests(unittest.TestCase):
    def test_unit_stays_active_and_uses_bounded_service_host(self):
        text=UNIT.read_text()
        self.assertIn('Type=simple',text)
        self.assertIn('cognilode-taskflow-phase-controller-service',text)
        self.assertIn('Restart=on-failure',text)
        self.assertNotIn('.timer',text)
        self.assertNotIn('chatgpt.com',text)

    def test_service_run_invokes_controller_once_and_records_receipt(self):
        mod=load()
        with tempfile.TemporaryDirectory() as td:
            td=Path(td); fake=td/'controller'
            fake.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps({"project":"critical-path-autonomy","actions":[{"phase":"research","state":"queued"}],"argv":sys.argv[1:]}))\n')
            fake.chmod(fake.stat().st_mode|stat.S_IXUSR)
            mod.CONTROLLER=fake; mod.STATE_ROOT=td/'state'; mod.LOG=mod.STATE_ROOT/'recurrence.jsonl'; mod.LOCK=mod.STATE_ROOT/'recurrence.lock'; mod.RUN_TIMEOUT=10
            env={
                'COGNILODE_TASKFLOW_PLAN':'/tmp/critical-path-autonomy.plan',
                'COGNILODE_TASKFLOW_RESEARCH_EMPLOYEE':'Elliot Mercer',
                'COGNILODE_TASKFLOW_EXTERNAL_EMPLOYEE':'Nadia Brooks',
                'COGNILODE_TASKFLOW_SEED_STEP':'CP-4-6',
            }
            with patch.dict(os.environ,env,clear=False): mod.run_once()
            rows=[json.loads(x) for x in mod.LOG.read_text().splitlines()]
            self.assertEqual(rows[-1]['returncode'],0)
            out=json.loads(rows[-1]['stdout_tail'])
            self.assertIn('--plan',out['argv']); self.assertIn('CP-4-6',out['argv'])
            self.assertIn('Elliot Mercer',out['argv']); self.assertIn('Nadia Brooks',out['argv'])

    def test_service_requires_two_distinct_named_phase_employees(self):
        mod=load()
        with patch.dict(os.environ,{
            'COGNILODE_TASKFLOW_PLAN':'/tmp/p.plan',
            'COGNILODE_TASKFLOW_RESEARCH_EMPLOYEE':'Elliot Mercer',
            'COGNILODE_TASKFLOW_EXTERNAL_EMPLOYEE':'Nadia Brooks',
        },clear=True):
            argv=mod.command()
            self.assertIn('Elliot Mercer',argv); self.assertIn('Nadia Brooks',argv)
            self.assertNotEqual(os.environ['COGNILODE_TASKFLOW_RESEARCH_EMPLOYEE'],os.environ['COGNILODE_TASKFLOW_EXTERNAL_EMPLOYEE'])

if __name__=='__main__': unittest.main()
