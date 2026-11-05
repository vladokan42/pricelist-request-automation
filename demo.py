#!/usr/bin/env python3
"""Offline CLI acceptance demo; simulated import is an explicit opt-in."""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def demo(output, simulate_import=False):
    source = Path(__file__).resolve().parent
    output = Path(output).absolute()
    output.mkdir(parents=True, exist_ok=False)
    fixtures, state = output/'fixtures', output/'state'
    steps = []

    def run(*args, fixture=False):
        command = [sys.executable,'-B',str(source/('examples.py' if fixture else 'pricelist.py'))]
        if not fixture:
            command += ['--state',str(state),'--suppliers',str(fixtures/'suppliers.json')]
        command += list(args)
        p = subprocess.run(command, check=True, capture_output=True, text=True)
        result = p.stdout.strip() if fixture else json.loads(p.stdout)
        steps.append({'argv':command,'result':result})
        return result

    run(str(fixtures),fixture=True)
    initial = run('initial','--today','2026-09-08')[0]['message_id']
    assert run('initial','--today','2026-09-08')[0]['status']=='NO_OP'
    run('mark-sent','--draft-id',initial,'--message-id','<sent-request@office.example>','--sent-at','2026-09-08T09:00:00+00:00')
    first=run('reminders','--today','2026-09-10')[0]
    assert first['number']==1
    assert run('reminders','--today','2026-09-14')[0]['reason']=='pendingDraft'
    run('mark-sent','--draft-id',first['message_id'],'--message-id','<reminder1@office.example>','--sent-at','2026-09-10T12:00:00+00:00')
    # Same validated core, two genuine workbook formats.
    r=run('receive','--input',str(fixtures/'reply.eml'),'--today','2026-09-10')[0]
    assert r['status']=='RECEIVED'
    xr=run('receive','--input',str(fixtures/'reply-xls.eml'),'--today','2026-09-10')[0]
    assert xr['status']=='RECEIVED'
    assert run('receive','--input',str(fixtures/'reply.eml'),'--today','2026-09-10')[0]['status']=='NO_OP'
    assert run('thanks','--message-id','<reply@supplier.example>','--today','2026-09-10')['status']=='REVIEW'
    if simulate_import:
        receipt=dict(receipt_id='synthetic-import-receipt-1',message_id='<reply@supplier.example>',supplier='demo',cycle='2026-09',sha256=r['sha256'],status='SUCCESS')
        receipt_path=output/'simulated-success-receipt.json'
        receipt_path.write_text(json.dumps(receipt,indent=2))
        assert run('ack-import','--receipt',str(receipt_path))['status']=='IMPORTED'
        assert run('ack-import','--receipt',str(receipt_path))['status']=='NO_OP'
        assert run('thanks','--message-id','<reply@supplier.example>','--today','2026-09-10')['kind']=='thanks'
        assert run('thanks','--message-id','<reply@supplier.example>','--today','2026-09-10')['status']=='NO_OP'
        assert run('reminders','--today','2026-09-14')['status']=='NO_OP'
    status=run('status')
    assert len(status['drafts'])==(3 if simulate_import else 2)
    assert len(status['messages'])==2
    for record in status['messages'].values():
        actual=(state/record['attachment_path']).read_bytes()
        assert hashlib.sha256(actual).hexdigest()==record['sha256']
    (output/'steps.json').write_text(json.dumps(steps,ensure_ascii=False,indent=2))
    print(json.dumps({'status':'PASS','cli_steps':len(steps),'workbook_formats':['xls','xlsx'],'simulation_only':True,'successful_import_receipt_simulated':simulate_import,'state':str(state)},ensure_ascii=False,indent=2))
    return status


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('output',type=Path,help='New directory outside source')
    p.add_argument('--simulate-import',action='store_true',help='Explicitly create a SYNTHETIC success receipt; no actual import')
    a=p.parse_args()
    demo(a.output,a.simulate_import)
