"""Only authorized rows/columns enter the SQL sandbox. No connection to raw tables."""
import sqlite3
import time
import re
from data import DATA, USERS

DDL = {
    'tickets': 'id TEXT, subject TEXT, content TEXT, district TEXT, category TEXT, department_id INTEGER, assignee TEXT, channel TEXT, status TEXT, created_at TEXT, due_at TEXT, closed_at TEXT, overdue INTEGER, transfer_count INTEGER, handling_hours REAL',
    'departments': 'id INTEGER, name TEXT, category TEXT, responsibility TEXT',
    'events': 'id INTEGER, ticket_id TEXT, stage TEXT, occurred_at TEXT, actor TEXT, note TEXT',
    'feedback': 'id INTEGER, ticket_id TEXT, score INTEGER, satisfaction TEXT, comment TEXT, visited_at TEXT',
    'service_targets': 'department_id INTEGER, category TEXT, due_days INTEGER, target_completion_pct REAL, target_satisfaction_pct REAL',
    'knowledge_docs': 'id TEXT, title TEXT, doc_type TEXT, district TEXT, department_id INTEGER, body TEXT, effective_date TEXT',
    'daily_context': 'day TEXT, district TEXT, rainfall_mm REAL, is_weekend INTEGER',
    'district_profiles': 'district TEXT, population_10k INTEGER, street_count INTEGER, area_km2 INTEGER',
    'dispatch_cases': 'id TEXT, subject TEXT, content TEXT, district TEXT, status TEXT, department_id INTEGER, category TEXT, assignee TEXT, created_at TEXT, due_at TEXT, version INTEGER',
    'dispatch_events': 'id INTEGER, case_id TEXT, action TEXT, actor TEXT, department_id INTEGER, detail TEXT, occurred_at TEXT',
}
METRICS = {'办结率': '当前范围已办结数 / 当前范围工单总数；按受理时间归属月份',
           '逾期率': 'overdue=1 的工单数 / 当前范围工单总数，含逾期办结与在办超期',
           '满意率': '回访 score>=4 的数量 / 已回访数量；未回访不进入分母',
           '平均办理时长': '仅已办结工单 handling_hours 的平均值，单位小时',
           '数据时间': '固定演示快照 2026-11-19 12:00，全部为合成数据，非真实城市记录'}


def scoped_rows(user_id):
    u = USERS[user_id]
    return [t for t in DATA['tickets'] if (not u['district'] or t[3] == u['district'])
            and (not u['department_id'] or t[5] == u['department_id'])
            and (not u['assignee'] or t[6] == u['assignee'])]


class QueryError(ValueError):
    pass


class Sandbox:
    def __init__(self, user_id, extra=None):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.user_id = user_id
        tickets = scoped_rows(user_id)
        ids, departments = {t[0] for t in tickets}, {t[5] for t in tickets}
        districts = {t[3] for t in tickets}
        visible = {'tickets': tickets, 'events': [e for e in DATA['events'] if e[1] in ids],
                   'feedback': [f for f in DATA['feedback'] if f[1] in ids],
                   'departments': [d for d in DATA['departments'] if d[0] in departments],
                   'service_targets': [d for d in DATA['service_targets'] if d[0] in departments],
                   'knowledge_docs': [d for d in DATA['knowledge_docs'] if (not d[3] or d[3] in districts) and d[4] in departments],
                   'daily_context': [d for d in DATA['daily_context'] if d[1] in districts],
                   'district_profiles': [d for d in DATA['district_profiles'] if d[0] in districts]}
        extra = extra or {}
        u = USERS[user_id]
        pending = [row for row in extra.get('dispatch_cases',[]) if
                   (not u['district'] or row['district']==u['district']) and
                   (not u['department_id'] or row['department_id']==u['department_id']) and
                   (not u['assignee'] or row['assignee']==u['assignee'])]
        pending_ids = {r['id'] for r in pending}
        visible['dispatch_cases'] = [tuple(row[k.split()[0]] for k in DDL['dispatch_cases'].split(',')) for row in pending]
        visible['dispatch_events'] = [tuple(row[k.split()[0]] for k in DDL['dispatch_events'].split(',')) for row in extra.get('dispatch_events',[]) if row['case_id'] in pending_ids]
        self.counts = {k:len(v) for k,v in visible.items()}
        for table, ddl in DDL.items():
            self.db.execute(f'CREATE TABLE {table} ({ddl})')
            rows = visible[table]
            if rows:
                self.db.executemany(f'INSERT INTO {table} VALUES ({",".join("?" for _ in rows[0])})', rows)
        self.db.commit()
        self.db.execute('PRAGMA query_only=ON')
        self.db.set_authorizer(self.authorize)

    @staticmethod
    def authorize(action, a, b, db, source):
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            return sqlite3.SQLITE_OK if a in DDL else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            allowed = {'count','sum','avg','min','max','round','coalesce','nullif','ifnull','strftime','date','datetime',
                       'julianday','substr','substring','length','lower','upper','trim','abs','like','glob',
                       'row_number','rank','dense_rank','lag','lead','total','iif','replace','instr'}
            return sqlite3.SQLITE_OK if (b or '').lower() in allowed else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY

    def schema(self):
        return {'tables': DDL, 'metrics': METRICS, 'scope': USERS[self.user_id]['scope'],
                'relations': ['tickets.department_id=departments.id', 'events.ticket_id=tickets.id', 'feedback.ticket_id=tickets.id',
                              'service_targets.department_id=departments.id', 'tickets.district=district_profiles.district',
                              'date(tickets.created_at)=daily_context.day AND tickets.district=daily_context.district',
                              'dispatch_events.case_id=dispatch_cases.id', 'dispatch_cases.department_id=departments.id',
                              'dispatch_cases为实时新受理派单数据，tickets为固定历史快照；两者不要混作同一统计口径。派单操作须使用独立工作台，SQL工具只读。'],
                'privacy': '不提供姓名、手机号、详细住址等市民身份字段；禁止推断或补全。仅有授权行，无其他隐藏原始表。'}

    def execute(self, sql):
        if not isinstance(sql, str) or len(sql) > 12000:
            raise QueryError('查询格式或长度不符合要求')
        sql = sql.strip().rstrip(';')
        if not re.match(r'^(select|with)\b', sql, re.IGNORECASE):
            raise QueryError('仅允许 SELECT 或只读 WITH 查询')
        start = time.monotonic()
        budget = [0]
        def progress():
            budget[0] += 1
            return int(budget[0] > 2000 or time.monotonic()-start > 2)
        self.db.set_progress_handler(progress, 1000)
        try:
            cursor = self.db.execute(sql)
            columns = [d[0] for d in cursor.description]
            if len(columns) > 40 or len(set(columns)) != len(columns):
                raise QueryError('查询最多返回40列且列名不能重复，请为同名列设置不同别名。')
            rows = cursor.fetchmany(201)
            # Bound strings and result size as well as row count.
            result = [{k: v[:2000] if isinstance(v, str) else v for k, v in dict(row).items()} for row in rows[:200]]
            return {'columns': columns, 'rows': result, 'truncated': len(rows) > 200,
                    'returned_rows': len(result), 'elapsed_ms': round((time.monotonic()-start)*1000), 'sql': sql}
        except sqlite3.Error as exc:
            raise QueryError('查询被拒绝或无法执行：'+str(exc)) from exc

    def close(self):
        self.db.close()
