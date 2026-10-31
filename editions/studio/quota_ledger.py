"""Durable per-request quotas; video cards and downloads never create charges."""
from contextlib import contextmanager
from datetime import datetime, timedelta, time as day_time, timezone as fixed_timezone
import re
import sqlite3
import time


QUOTA_TYPES = {
    'ordinary_fast10': {'limit': 1, 'period': 'day', 'timezone': 'Asia/Shanghai', 'reset_hour': 1},
    'expert_fast': {'limit': 4, 'period': 'rolling_7_days', 'window_days': 7},
}


def ordinary_day_window(now):
    """Ordinary quota days run from 01:00 to 01:00 in Beijing (UTC+8)."""
    beijing = fixed_timezone(timedelta(hours=8))
    current = datetime.fromtimestamp(now, beijing)
    start = datetime.combine(current.date(), day_time(hour=1), tzinfo=beijing)
    if current < start:
        start -= timedelta(days=1)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def ordinary_day_key(now):
    start, _ = ordinary_day_window(now)
    return datetime.fromtimestamp(start, fixed_timezone(timedelta(hours=8))).date().isoformat()


def reset_at_from_reply(reply):
    """Only extract an explicit dated GMT offset; never infer a platform week anchor."""
    month_names = {'januar':1,'january':1,'februar':2,'february':2,'märz':3,'march':3,
                   'april':4,'mai':5,'may':5,'juni':6,'june':6,'juli':7,'july':7,
                   'august':8,'september':9,'oktober':10,'october':10,
                   'november':11,'dezember':12,'december':12}
    match = re.search(r'(\d{1,2})\.\s*([A-Za-zÄä]+)\s+(\d{4}),?\s+(\d{1,2}):(\d{2})\s*\(GMT([+-])(\d{1,2}):?(\d{2})\)',str(reply or ''),re.I)
    if not match: return None
    day,month,year,hour,minute,sign,offset_hour,offset_minute = match.groups()
    try:
        offset = timedelta(hours=int(offset_hour),minutes=int(offset_minute)) * (-1 if sign=='-' else 1)
        return datetime(int(year),month_names[month.lower()],int(day),int(hour),int(minute),
                        tzinfo=fixed_timezone(offset)).timestamp()
    except (ValueError,KeyError):
        return None


def quota_type(model, duration=None, profile=None):
    key = str(model or '').lower().replace('_', '-').replace('seedance-v', 'seedance-')
    if key not in ('seedance-2.0', 'seedance-20', 'seedance-v20'):
        return None
    if profile in ('pro-fast15', 'plugin-expert30'):
        return 'expert_fast'
    if profile is None and duration == 10:
        return 'ordinary_fast10'
    return None


class QuotaLedger:
    def __init__(self, connection, *, timezone):
        self.conn, self.timezone = connection, timezone
        with self.conn:
            self.conn.execute('''CREATE TABLE IF NOT EXISTS generation_quota (
                account TEXT NOT NULL, quota_type TEXT NOT NULL, request_id TEXT NOT NULL,
                conversation_id TEXT, state TEXT NOT NULL,
                created_at REAL NOT NULL, sent_at REAL,
                PRIMARY KEY(account,quota_type,request_id),
                CHECK(state IN ('reserved','used','uncertain','released')))''')
            self.conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS generation_quota_conversation
                ON generation_quota(quota_type,conversation_id) WHERE conversation_id IS NOT NULL''')
            self.conn.execute('''CREATE TABLE IF NOT EXISTS generation_quota_blocks (
                account TEXT NOT NULL, quota_type TEXT NOT NULL, until_at REAL NOT NULL,
                reason TEXT NOT NULL, PRIMARY KEY(account,quota_type))''')
            self.conn.execute('''CREATE TABLE IF NOT EXISTS generation_quota_migrations (
                name TEXT PRIMARY KEY, applied_at REAL NOT NULL)''')
            self.conn.execute('''CREATE TABLE IF NOT EXISTS generation_quota_legacy_classified (
                account TEXT NOT NULL, conversation_id TEXT NOT NULL, day TEXT NOT NULL,
                quota_type TEXT NOT NULL, classified_at REAL NOT NULL,
                PRIMARY KEY(account,conversation_id))''')

    @contextmanager
    def _transaction(self):
        # All checks and their claim commit under the same SQLite write lock.
        # A second process cannot consume the last slot between SELECT and INSERT.
        nested = self.conn.in_transaction
        self.conn.execute('SAVEPOINT quota_transaction' if nested else 'BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            if nested:
                self.conn.execute('ROLLBACK TO quota_transaction')
                self.conn.execute('RELEASE quota_transaction')
            else:
                self.conn.rollback()
            raise
        else:
            if nested: self.conn.execute('RELEASE quota_transaction')
            else: self.conn.commit()

    def _window(self, kind, now):
        config = QUOTA_TYPES[kind]
        if config['period'] == 'day':
            return ordinary_day_window(now)
        return now-7*86400, now+7*86400

    def snapshot(self, account, kind, *, now=None):
        now = time.time() if now is None else now
        start, end = self._window(kind, now)
        since = '>' if kind == 'expert_fast' else '>='
        rows = self.conn.execute(f'''SELECT state, COALESCE(sent_at,created_at) AS at
            FROM generation_quota WHERE account=? AND quota_type=? AND state!='released'
            AND COALESCE(sent_at,created_at){since}? AND COALESCE(sent_at,created_at)<=?''',
            (account, kind, start, now)).fetchall()
        counts = {state: sum(row[0] == state for row in rows) for state in ('used','reserved','uncertain')}
        block = self.conn.execute('SELECT until_at,reason FROM generation_quota_blocks WHERE account=? AND quota_type=?',
                                  (account,kind)).fetchone()
        until = block[0] if block and block[0]>now else 0
        limit = QUOTA_TYPES[kind]['limit']
        occupied = len(rows)
        uses = sorted(row[1] for row in rows)
        reset = end if kind == 'ordinary_fast10' else (uses[0]+7*86400 if uses else None)
        capacity_at = now
        if occupied >= limit:
            capacity_at = end if kind == 'ordinary_fast10' else uses[occupied-limit]+7*86400
        next_at = max(until, capacity_at) if until or occupied>=limit else None
        return {**QUOTA_TYPES[kind], **counts, 'occupied':occupied,
                'remaining':max(0,limit-occupied), 'blocked':bool(until), 'blocked_until':until,
                'reason':block[1] if until else '',
                'window_start':start if kind=='ordinary_fast10' else None,
                'window_end':end if kind=='ordinary_fast10' else None,
                'reset_at':reset, 'next_available_at':next_at}

    def reserve(self, account, kind, request_id, *, now=None):
        now = time.time() if now is None else now
        with self._transaction():
            existing = self.conn.execute('SELECT state FROM generation_quota WHERE account=? AND quota_type=? AND request_id=?',
                                        (account,kind,request_id)).fetchone()
            if existing and existing[0] != 'released':
                return True
            state = self.snapshot(account,kind,now=now)
            if state['blocked'] or state['remaining']<=0:
                return False
            self.conn.execute('''INSERT INTO generation_quota(account,quota_type,request_id,state,created_at)
                VALUES (?,?,?,'reserved',?) ON CONFLICT(account,quota_type,request_id)
                DO UPDATE SET state='reserved',created_at=excluded.created_at,sent_at=NULL,conversation_id=NULL''',
                (account,kind,request_id,now))
            return True

    def mark_sent(self, account, kind, request_id, *, now=None):
        now = time.time() if now is None else now
        with self._transaction():
            self.conn.execute('''INSERT INTO generation_quota(account,quota_type,request_id,state,created_at,sent_at)
                VALUES (?,?,?,'uncertain',?,?) ON CONFLICT(account,quota_type,request_id)
                DO UPDATE SET state=CASE WHEN state='used' THEN 'used' ELSE 'uncertain' END,
                sent_at=COALESCE(sent_at,excluded.sent_at)''', (account,kind,request_id,now,now))

    def confirm(self, account, kind, request_id, conversation_id, *, now=None):
        now = time.time() if now is None else now
        cid = str(conversation_id)
        if not cid: raise ValueError('Conversation identity required')
        with self._transaction():
            existing = self.conn.execute('SELECT account FROM generation_quota WHERE quota_type=? AND conversation_id=?',
                                        (kind,cid)).fetchone()
            if existing:
                if existing[0] != account: raise ValueError('Conversation account mismatch')
                self.conn.execute("UPDATE generation_quota SET state='used' WHERE quota_type=? AND conversation_id=? AND state='uncertain'",(kind,cid))
                return False
            # A known legacy conversation already occupied one daily aggregate unit.
            # Resolve that unit's identity instead of billing it a second time on recovery.
            if self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='accepted_videos'").fetchone():
                old=self.conn.execute('SELECT day FROM accepted_videos WHERE account=? AND conversation_id=?',(account,cid)).fetchone()
                if old:
                    ordered=[row[0] for row in self.conn.execute('SELECT conversation_id FROM accepted_videos WHERE account=? AND day=? ORDER BY conversation_id',(account,old[0])).fetchall()]
                    legacy_id=f'legacy-unclassified:{old[0]}:{ordered.index(cid)}'
                    resolved=self.conn.execute("UPDATE generation_quota SET state='used',conversation_id=? WHERE account=? AND quota_type=? AND request_id=? AND state='uncertain'",(cid,account,kind,legacy_id)).rowcount
                    if resolved:
                        self.conn.execute("UPDATE generation_quota SET state='released' WHERE account=? AND quota_type=? AND request_id=? AND state='reserved'",(account,kind,request_id))
                        return False
            self.conn.execute('''INSERT INTO generation_quota(account,quota_type,request_id,conversation_id,state,created_at,sent_at)
                VALUES (?,?,?,?,'used',?,?) ON CONFLICT(account,quota_type,request_id)
                DO UPDATE SET conversation_id=excluded.conversation_id,state='used',sent_at=COALESCE(sent_at,excluded.sent_at)''',
                (account,kind,request_id,cid,now,now))
            return True

    def release(self, account, kind, request_id, *, proven_unconsumed=False):
        with self._transaction():
            return bool(self.conn.execute('''UPDATE generation_quota SET state='released'
                WHERE account=? AND quota_type=? AND request_id=? AND state!='released'
                AND (state='reserved' OR ?)''', (account,kind,request_id,bool(proven_unconsumed))).rowcount)

    def release_conversation(self, account, conversation_id):
        with self._transaction():
            return bool(self.conn.execute("UPDATE generation_quota SET state='released' WHERE account=? AND conversation_id=? AND state!='released'",
                                          (account,str(conversation_id))).rowcount)

    def block(self, account, kind, until, reason, *, now=None):
        now = time.time() if now is None else now
        if until is None:
            until = self._window(kind,now)[1]
        if until<=now: return
        with self._transaction():
            self.conn.execute('''INSERT INTO generation_quota_blocks VALUES (?,?,?,?)
                ON CONFLICT(account,quota_type) DO UPDATE SET until_at=MAX(until_at,excluded.until_at),reason=excluded.reason''',
                (account,kind,until,str(reason)[:400]))

    def migrate_legacy(self, *, now=None):
        """Old daily sums cannot identify route: retain a conservative, visible audit baseline."""
        now = time.time() if now is None else now
        with self._transaction():
            if self.conn.execute("SELECT 1 FROM generation_quota_migrations WHERE name='legacy_daily_v1'").fetchone():
                return
            if self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage'").fetchone():
                tz = self.timezone() if callable(self.timezone) else self.timezone
                for account,day,used in self.conn.execute('SELECT account,day,used FROM usage WHERE used>0').fetchall():
                    try:
                        # Daily aggregates have no timestamp. Use that day's latest possible time,
                        # capped at migration time, so the migration never releases early.
                        when = min(now,datetime.combine(datetime.fromisoformat(day).date()+timedelta(days=1),day_time.min,tzinfo=tz).timestamp()-.001)
                    except (ValueError,TypeError):
                        when = now
                    if when<=now-7*86400: continue
                    for kind in QUOTA_TYPES:
                        for index in range(max(0,int(used))):
                            self.conn.execute('''INSERT OR IGNORE INTO generation_quota
                                (account,quota_type,request_id,state,created_at,sent_at)
                                VALUES (?,?,?,'uncertain',?,?)''', (account,kind,f'legacy-unclassified:{day}:{index}',when,when))
            self.conn.execute("INSERT INTO generation_quota_migrations VALUES ('legacy_daily_v1',?)",(now,))

    def classify_legacy(self, rows, *, now=None):
        """Resolve only exact old accepted conversations, one old aggregate slot per CID.

        Unmatched/ambiguous metadata stays uncertain. This never changes the legacy
        usage sums, accepted-video identities, account counters or engine credits.
        """
        from video_capabilities import (validate_video_parameters, submission_duration,
                                        validate_plugin_expert30_parameters)
        now = time.time() if now is None else now
        groups = {}
        for raw in rows:
            if not isinstance(raw, dict) or raw.get('platform','dola') != 'dola':
                continue
            account, cid = raw.get('account'), str(raw.get('conversation_id') or '')
            if account and cid:
                groups.setdefault((account,cid),[]).append(raw)
        result = {'classified':0,'used_added':0,'uncertain_released':0,'skipped':0}
        with self._transaction():
            if not self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='accepted_videos'").fetchone():
                return result
            ordered = {}
            for (account,cid), candidates in groups.items():
                if self.conn.execute('SELECT 1 FROM generation_quota_legacy_classified WHERE account=? AND conversation_id=?',(account,cid)).fetchone():
                    continue
                accepted = self.conn.execute('SELECT day FROM accepted_videos WHERE account=? AND conversation_id=?',(account,cid)).fetchone()
                if not accepted:
                    result['skipped'] += 1
                    continue
                day = accepted[0]
                routes = set()
                valid = []
                for row in candidates:
                    # Missing duration must not silently receive the API's default10.
                    if row.get('duration') is None or not row.get('model'):
                        continue
                    profile = row.get('duration_profile')
                    try:
                        if profile == 'plugin-expert30':
                            params = validate_plugin_expert30_parameters(row['model'],row['duration'])
                            model, duration = params['model'], params['duration']
                        else:
                            model, duration = validate_video_parameters(row['model'],row['duration'])
                            submission_duration(model,duration,profile)
                    except (ValueError,TypeError):
                        continue
                    kind = quota_type(model,duration,profile) or 'non_fast'
                    routes.add(kind)
                    valid.append(row)
                if len(routes) != 1:
                    result['skipped'] += 1
                    continue
                kind = next(iter(routes))
                existing = self.conn.execute('SELECT account,quota_type,state FROM generation_quota WHERE conversation_id=?',(cid,)).fetchall()
                # Existing explicit identity/proof wins over a newly inferred route.
                if any(item[0]!=account or item[2]=='released' or item[1]!=kind for item in existing):
                    result['skipped'] += 1
                    continue
                key = (account,day)
                if key not in ordered:
                    ordered[key] = [item[0] for item in self.conn.execute('SELECT conversation_id FROM accepted_videos WHERE account=? AND day=? ORDER BY conversation_id',(account,day)).fetchall()]
                slot = 'legacy-unclassified:'+str(day)+':'+str(ordered[key].index(cid))
                placeholders = self.conn.execute('''SELECT quota_type,state,COALESCE(sent_at,created_at)
                    FROM generation_quota WHERE account=? AND request_id=?''',(account,slot)).fetchall()
                # Without an original slot, the old daily sum cannot prove which
                # unknown unit belongs here. Do not release a neighbouring CID's unit.
                if not placeholders:
                    result['skipped'] += 1
                    continue
                tz = self.timezone() if callable(self.timezone) else self.timezone
                when = min((item[2] for item in placeholders),default=now)
                trusted = []
                for row in valid:
                    for field in ('accepted_at','sent_at','submitted_at','started_at'):
                        value = row.get(field)
                        if not isinstance(value,(int,float)) or isinstance(value,bool) or not 0 < value <= now:
                            continue
                        try:
                            if datetime.fromtimestamp(value,tz).date().isoformat() == day:
                                trusted.append(value)
                                break
                        except (ValueError,OverflowError,OSError):
                            continue
                if trusted:
                    when = min(trusted)
                released = self.conn.execute("UPDATE generation_quota SET state='released' WHERE account=? AND request_id=? AND state='uncertain'",(account,slot)).rowcount
                result['uncertain_released'] += released
                if kind != 'non_fast' and not existing:
                    self.conn.execute('''INSERT INTO generation_quota
                        (account,quota_type,request_id,conversation_id,state,created_at,sent_at)
                        VALUES (?,?,?,?,'used',?,?)''',(account,kind,'legacy-classified:'+cid,cid,when,when))
                    result['used_added'] += 1
                self.conn.execute('INSERT INTO generation_quota_legacy_classified VALUES (?,?,?,?,?)',(account,cid,day,kind,now))
                result['classified'] += 1
        return result
