from app.engine import evaluate
from app.errors import BusinessError
from app.modules.recalib.rules import recalculate
from app import store
import pytest

CAL = {'scale_id': 'SCALE-03', 'shift': '早班',
       'window_start': '2026-11-17T06:00:00+08:00', 'window_end': '2026-11-17T08:00:00+08:00',
       'deviation_kg': '-2.5', 'basis': 'M1级500kg标准砝码·检定证书JJG539-2026-0817',
       'tickets': ['00001', '00002', '00003', '00004', '00005', '00006', '00007', '00905']}

@pytest.fixture
def recal():
    return evaluate({'dataset': 'dirty', 'overrides': {'calibration': CAL}})

def test_no_event_passthrough(sample):
    # 无校准事件时结果与旧版完全一致
    assert sample['recalib'] == {'event': None, 'applied': [], 'pending': []}
    assert sample['balance']['amount'] == '23708.91'

def test_applied_recalculated(recal):
    row = next(r for r in recal['recalib']['applied'] if r['ticket'] == '00002')
    assert row['gross_after'] == '104.5' and row['net_after'] == '94.5'
    assert row['amount_after'] == '1104.23' and row['delta'] == '1104.23'

def test_per_ticket_rates_not_distorted():
    # 扣杂率不同的票必须逐票重算，不能按总额平摊
    csv = ('id,ticket,kind,weight,unit,fish,price,rate,version,replaces,paid\n'
           'A1g,00071,gross,100,kg,黄鱼,10,0.1,1,,0\nA1t,00071,tare,10,kg,黄鱼,10,0.1,1,,0\n'
           'B1g,00072,gross,100,kg,黄鱼,10,0.2,1,,0\nB1t,00072,tare,10,kg,黄鱼,10,0.2,1,,0\n')
    log = {t: {'scale': 'SCALE-03', 'time': '2026-11-17T06:10:00+08:00'} for t in ('00071', '00072')}
    cal = dict(CAL, tickets=['00071', '00072'])
    s = evaluate({'csv_text': csv, 'overrides': {'calibration': cal, 'weigh_log': log}})
    deltas = {r['ticket']: r['delta'] for r in s['recalib']['applied']}
    assert deltas == {'00071': '832.50', '00072': '740.00'}

def test_unique_effective_version(recal):
    versions = [s for s in recal['settlements'] if s['ticket'] == '00002']
    assert len(versions) == 2
    final = next(f for f in recal['final'] if f['ticket'] == '00002')
    assert final['version'] == 2 and final['amount'] == '1104.23' and final['replaces'] == '1'
    audit = [a for a in recal['version_audit'] if a['ticket'] == '00002']
    assert [a['amount'] for a in audit] == ['1075.02', '1104.23']

def test_scale_mismatch_pending(recal):
    hit = next(p for p in recal['recalib']['pending'] if p['ticket'] == '00006')
    assert hit['code'] == 'CALIB_SCALE_MISMATCH'
    final = next(f for f in recal['final'] if f['ticket'] == '00006')
    assert final['version'] == 1 and final['amount'] == '1121.76'

def test_window_mismatch_pending(recal):
    hit = next(p for p in recal['recalib']['pending'] if p['ticket'] == '00007')
    assert hit['code'] == 'CALIB_WINDOW_MISMATCH'

def test_reweighed_pending(recal):
    # 复秤与偏差重复修正 -> 待处理，已付票保持旧版
    hit = next(p for p in recal['recalib']['pending'] if p['ticket'] == '00001')
    assert hit['code'] == 'CALIB_REWEIGHED'
    final = next(f for f in recal['final'] if f['ticket'] == '00001')
    assert final['version'] == 2 and final['amount'] == '1285.35' and final['due'] == '116.85'

def test_unknown_ticket_pending(recal):
    hit = next(p for p in recal['recalib']['pending'] if p['ticket'] == '00905')
    assert hit['code'] == 'CALIB_TICKET_UNKNOWN'

def test_no_trace_pending():
    cal = dict(CAL, tickets=['00020'])
    log = {'00001': {'scale': 'SCALE-03', 'time': '2026-11-17T06:12:33+08:00'}}
    s = evaluate({'dataset': 'dirty', 'overrides': {'calibration': cal, 'weigh_log': log}})
    assert s['recalib']['pending'][0]['code'] == 'CALIB_NO_TRACE'

def test_version_blocked_pending():
    csv = ('id,ticket,kind,weight,unit,fish,price,rate,version,replaces,paid\n'
           'A1g,00077,gross,100,kg,黄鱼,10,0.1,1,,0\nA1t,00077,tare,10,kg,黄鱼,10,0.1,1,,0\n'
           'A2g,00077,gross,101,kg,黄鱼,10,0.1,2,9,0\nA2t,00077,tare,10,kg,黄鱼,10,0.1,2,9,0\n')
    log = {'00077': {'scale': 'SCALE-03', 'time': '2026-11-17T06:10:00+08:00'}}
    s = evaluate({'csv_text': csv, 'overrides': {'calibration': dict(CAL, tickets=['00077']), 'weigh_log': log}})
    assert s['recalib']['pending'][0]['code'] == 'CALIB_VERSION_BLOCKED'
    assert s['archive']['status'] == '待处理'

def test_paid_delta_after_recalc():
    csv = ('id,ticket,kind,weight,unit,fish,price,rate,version,replaces,paid\n'
           'A1g,00077,gross,100,kg,黄鱼,10,0.1,1,,500\nA1t,00077,tare,10,kg,黄鱼,10,0.1,1,,0\n')
    log = {'00077': {'scale': 'SCALE-03', 'time': '2026-11-17T06:10:00+08:00'}}
    s = evaluate({'csv_text': csv, 'overrides': {'calibration': dict(CAL, tickets=['00077']), 'weigh_log': log}})
    applied = s['recalib']['applied'][0]
    assert applied['amount_after'] == '832.50' and applied['paid'] == '500.00' and applied['delta'] == '332.50'
    assert s['final'][0]['due'] == '332.50'

def test_switch_failure_rolls_back():
    csv = ('id,ticket,kind,weight,unit,fish,price,rate,version,replaces,paid\n'
           'B1g,00088,gross,12,kg,黄鱼,10,0,1,,0\nB1t,00088,tare,10,kg,黄鱼,10,0,1,,0\n')
    log = {'00088': {'scale': 'SCALE-03', 'time': '2026-11-17T06:20:00+08:00'}}
    cal = dict(CAL, deviation_kg='15', tickets=['00088'])
    s = evaluate({'csv_text': csv, 'overrides': {'calibration': cal, 'weigh_log': log}})
    assert s['recalib']['pending'][0]['code'] == 'CALIB_SWITCH_FAILED'
    final = s['final'][0]
    assert final['version'] == 1 and final['amount'] == '20.00'

def test_recalculate_negative_net(clean):
    row = dict(clean['final'][0], gross_kg='10', tare_kg='20')
    with pytest.raises(BusinessError):
        recalculate(row, __import__('decimal').Decimal('-1'))

@pytest.mark.parametrize('patch', [
    {'tickets': []}, {'tickets': '00002'}, {'scale_id': ' '}, {'basis': ''},
    {'window_start': 'bad'}, {'window_end': '2026-11-17T05:00:00+08:00'}, {'deviation_kg': 'x'}])
def test_bad_event_rejected(patch):
    with pytest.raises(BusinessError) as e:
        evaluate({'dataset': 'dirty', 'overrides': {'calibration': dict(CAL, **patch)}})
    assert e.value.code == 'BAD_CALIBRATION'

def test_rerun_no_duplicate_delta(recal, tmp_path):
    again = evaluate({'dataset': 'dirty', 'overrides': {'calibration': CAL}})
    assert again['manifest']['run_id'] == recal['manifest']['run_id']
    assert sum(1 for s in again['settlements'] if s['ticket'] == '00002') == 2
    db = str(tmp_path / 'r.sqlite')
    store.save(db, recal); store.save(db, again)
    assert len(store.history(db)) == 1
    assert store.get(db, recal['manifest']['run_id'])['balance']['due'] == recal['balance']['due']

def test_money_conservation(recal):
    assert recal['balance']['money_balanced']
    assert recal['balance']['row_balanced']

def test_original_data_preserved(recal, sample):
    assert len(recal['records']) == len(sample['records'])
    assert len(recal['quarantine']) == len(sample['quarantine'])
    assert recal['pairs'] == sample['pairs']
    import base64
    raw = base64.b64decode(recal['input_snapshot']['bytes_b64'])
    assert raw == (__import__('app.config', fromlist=['ROOT']).ROOT / 'sample_data/dirty.csv').read_bytes()
    # 旧版金额仍在版本凭证中，未丢失
    assert any(a['ticket'] == '00002' and a['amount'] == '1075.02' for a in recal['version_audit'])
