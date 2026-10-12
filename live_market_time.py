"""Strict, side-effect-free statistical windows for live market selection."""
import datetime as dt
import re
from zoneinfo import ZoneInfo

UTC = dt.timezone.utc
RULE = re.compile(
    r'\bfrom\s+(\w+)\s+(\d{1,2})\s+(\d{1,2}(?::\d{2})?\s*[AP]M)\s+ET\s+'
    r'to\s+(\w+)\s+(\d{1,2}),?\s+(\d{4})\s+(\d{1,2}(?::\d{2})?\s*[AP]M)\s+ET\b', re.I)


def strict_time(value, timezone='America/New_York'):
    if not isinstance(value, str) or not re.match(r'^\d{4}-\d\d-\d\d[T ]\d\d:\d\d', value.strip()):
        raise ValueError('full_timestamp_required')
    parsed = dt.datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    zone = ZoneInfo(timezone)
    candidates = set()
    for fold in (0, 1):
        aware = parsed.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if aware.astimezone(zone).replace(tzinfo=None) == parsed:
            candidates.add(aware)
    if len(candidates) != 1:
        raise ValueError('ambiguous_or_nonexistent_local_time')
    return candidates.pop()


def live_window_details(event, timezone='America/New_York'):
    result = {'valid': False, 'source': None, 'timezone': timezone,
              'start_utc': None, 'end_utc': None, 'reason': None}
    try:
        markets = event.get('markets') or []
        starts = [m['gameStartTime'] for m in markets if isinstance(m, dict) and m.get('gameStartTime')]
        if event.get('gameStartTime'):
            starts.append(event['gameStartTime'])
        ends = [m['endDate'] for m in markets if isinstance(m, dict) and m.get('endDate')]
        if event.get('endDate'):
            ends.append(event['endDate'])
        starts = {strict_time(v, timezone) for v in starts}
        ends = {strict_time(v, timezone) for v in ends}
        if len(starts) > 1 or len(ends) > 1:
            raise ValueError('conflicting_structured_window')
        rule = None
        match = RULE.search(str(event.get('description') or ''))
        if match:
            sm, sd, st, em, ed, year, et = match.groups()
            smn = dt.datetime.strptime(sm, '%B').month
            emn = dt.datetime.strptime(em, '%B').month
            year = int(year)
            def rule_time(month, day, year, clock):
                if ':' not in clock:
                    clock = re.sub(r'^(\d+)', r'\1:00', clock)
                clock = re.sub(r'\s*([AP]M)$', r' \1', clock.upper())
                parsed = dt.datetime.strptime(f'{month} {day} {year} {clock}', '%B %d %Y %I:%M %p')
                return strict_time(parsed.isoformat(), 'America/New_York')
            rule = (rule_time(sm, sd, year - (smn > emn), st), rule_time(em, ed, year, et))
        start = next(iter(starts), None)
        end = next(iter(ends), None)
        if rule and ((start is not None and start != rule[0]) or (end is not None and end != rule[1])):
            raise ValueError('structured_rule_conflict')
        if start is not None and end is not None:
            source = 'structured'
        elif rule:
            start, end = rule
            source = 'resolution_rule_ET'
            result['timezone'] = 'America/New_York'
        else:
            raise ValueError('statistical_window_missing')
        if end <= start:
            raise ValueError('invalid_window_order')
        result.update(valid=True, source=source, start_utc=start.isoformat(), end_utc=end.isoformat())
    except (ValueError, TypeError, OverflowError) as exc:
        result['reason'] = str(exc)
    return result


def live_event_window(event, timezone='America/New_York'):
    details = live_window_details(event, timezone)
    return ((dt.datetime.fromisoformat(details['start_utc']), dt.datetime.fromisoformat(details['end_utc']))
            if details['valid'] else (None, None))


def classify_live_event(event, now, timezone='America/New_York'):
    if now.tzinfo is None:
        raise ValueError('aware_current_time_required')
    details = live_window_details(event, timezone)
    details['slug'] = str(event.get('slug') or event.get('ticker') or '')
    details['phase'] = 'invalid'
    if details['valid']:
        start, end = map(dt.datetime.fromisoformat, (details['start_utc'], details['end_utc']))
        details['phase'] = 'upcoming' if now < start else ('ended' if now >= end else 'active')
    return details
