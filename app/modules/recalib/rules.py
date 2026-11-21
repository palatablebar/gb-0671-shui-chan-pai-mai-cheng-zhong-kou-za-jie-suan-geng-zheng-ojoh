from decimal import Decimal, ROUND_HALF_UP
from app.coerce import money, instant, number
from app.errors import BusinessError
from app.modules.correction.rules import select_versions

def parse_event(raw):
    if not isinstance(raw, dict):
        raise BusinessError('BAD_CALIBRATION', '校准事件须为对象')
    scale = str(raw.get('scale_id', '')).strip()
    if not scale:
        raise BusinessError('BAD_CALIBRATION', '校准秤号缺失')
    try:
        start = instant(raw.get('window_start', ''))
        end = instant(raw.get('window_end', ''))
    except BusinessError:
        raise BusinessError('BAD_CALIBRATION', '校准时间窗格式无效')
    if not start < end:
        raise BusinessError('BAD_CALIBRATION', '校准时间窗起止颠倒')
    try:
        deviation = number(raw.get('deviation_kg', ''))
    except BusinessError:
        raise BusinessError('BAD_CALIBRATION', '偏差值缺失或格式错误')
    basis = str(raw.get('basis', '')).strip()
    if not basis:
        raise BusinessError('BAD_CALIBRATION', '校准依据缺失')
    tickets = raw.get('tickets')
    if not isinstance(tickets, list) or not all(isinstance(t, str) and t.strip() for t in tickets):
        raise BusinessError('BAD_CALIBRATION', '受影响票清单须为非空字符串列表')
    tickets = sorted({t.strip() for t in tickets})
    if not tickets:
        raise BusinessError('BAD_CALIBRATION', '受影响票清单为空')
    return {'scale_id': scale, 'shift': str(raw.get('shift', '')).strip(),
            'window_start': str(raw.get('window_start', '')).strip(),
            'window_end': str(raw.get('window_end', '')).strip(),
            'start': start, 'end': end, 'deviation': deviation,
            'correction': -deviation, 'basis': basis, 'tickets': tickets}

def pending_row(ticket, code, detail):
    return {'ticket': ticket, 'code': code, 'detail': detail}

def recalculate(row, correction):
    gross = Decimal(row['gross_kg']) + correction
    if gross <= 0:
        raise BusinessError('CALIB_SWITCH_FAILED', '校准后毛重非正')
    net = gross - Decimal(row['tare_kg'])
    if net < 0:
        raise BusinessError('CALIB_SWITCH_FAILED', '校准后净重为负')
    billable = (net * (1 - Decimal(row['rate']))).quantize(Decimal('.001'), rounding=ROUND_HALF_UP)
    amount = money(billable * Decimal(row['price']))
    due = money(Decimal(amount) - Decimal(row['paid']))
    new_row = {**row, 'version': row['version'] + 1, 'replaces': str(row['version']),
               'gross_kg': format(gross, 'f'), 'net_kg': format(net, 'f'),
               'billable_kg': str(billable), 'amount': amount, 'due': due}
    evidence = {'ticket': row['ticket'], 'version': new_row['version'],
                'gross_before': row['gross_kg'], 'gross_after': new_row['gross_kg'],
                'net_before': row['net_kg'], 'net_after': new_row['net_kg'],
                'rate': row['rate'], 'amount_before': row['amount'], 'amount_after': amount,
                'paid': row['paid'], 'delta': due}
    return new_row, evidence

def event_view(event):
    if event is None:
        return None
    return {'scale_id': event['scale_id'], 'shift': event['shift'],
            'window_start': event['window_start'], 'window_end': event['window_end'],
            'deviation_kg': format(event['deviation'], 'f'),
            'correction_kg': format(event['correction'], 'f'),
            'basis': event['basis'], 'tickets': event['tickets']}

def apply(state):
    raw = state['context'].get('calibration')
    applied = []; pending = []; event = None
    if raw is not None:
        event = parse_event(raw)
        final, conflicts, _ = select_versions(state['settlements'])
        blocked = {c['ticket'] for c in conflicts}
        current = {row['ticket']: row for row in final}
        log = state['context'].get('weigh_log', {})
        grosses = {}
        for s in state['settlements']:
            grosses.setdefault(s['ticket'], set()).add(s['gross_kg'])
        additions = []
        for ticket in event['tickets']:
            if ticket in blocked:
                pending.append(pending_row(ticket, 'CALIB_VERSION_BLOCKED', '票存在版本冲突，保持旧版结算'))
                continue
            row = current.get(ticket)
            if row is None:
                pending.append(pending_row(ticket, 'CALIB_TICKET_UNKNOWN', '票不存在或无生效结算'))
                continue
            entry = log.get(ticket)
            if not isinstance(entry, dict):
                pending.append(pending_row(ticket, 'CALIB_NO_TRACE', '缺少称重轨迹，无法核对秤号与时间窗'))
                continue
            scale = str(entry.get('scale', '')).strip()
            if scale != event['scale_id']:
                pending.append(pending_row(ticket, 'CALIB_SCALE_MISMATCH', f"轨迹秤号{scale or '空'}与校准秤{event['scale_id']}不符"))
                continue
            try:
                moment = instant(entry.get('time', ''))
            except BusinessError:
                pending.append(pending_row(ticket, 'CALIB_NO_TRACE', '称重轨迹时间无效'))
                continue
            if not event['start'] <= moment <= event['end']:
                pending.append(pending_row(ticket, 'CALIB_WINDOW_MISMATCH', '称重时间不在校准时间窗内'))
                continue
            if len(grosses.get(ticket, set())) > 1:
                pending.append(pending_row(ticket, 'CALIB_REWEIGHED', '票已复秤，偏差修正会与复秤重复'))
                continue
            try:
                new_row, evidence = recalculate(row, event['correction'])
            except BusinessError as error:
                pending.append(pending_row(ticket, 'CALIB_SWITCH_FAILED', f'{error.message}，已恢复旧版'))
                continue
            additions.append(new_row)
            applied.append(evidence)
        state['settlements'].extend(additions)
    state['recalib'] = {'event': event_view(event), 'applied': applied, 'pending': pending}
    state['views']['recalib'] = {'title': '校准重算', 'rows': applied,
        'metrics': {'重算票': len(applied), '待处理': len(pending)}}
