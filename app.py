import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from pathlib import Path
from flask import Flask, jsonify, request, session, send_from_directory
from data import DATA, USERS, AS_OF
from query import Sandbox, METRICS, scoped_rows, QueryError
from agent import run_agent, enabled
import dispatch

ROOT = Path(__file__).parent
app = Flask(__name__, static_folder='static')
app.config.update(SECRET_KEY=os.getenv('SECRET_KEY') or secrets.token_hex(32), MAX_CONTENT_LENGTH=24000,
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=os.getenv('RENDER') == 'true')
STATE = Path(os.getenv('STATE_DIR', str(ROOT / 'runtime')))
STATE.mkdir(parents=True, exist_ok=True)
DB = STATE / 'app.sqlite3'


@contextmanager
def state_db():
    connection = sqlite3.connect(DB, timeout=10)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


with state_db() as db:
    db.executescript('''CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, principal TEXT, messages TEXT, updated REAL);
    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, principal TEXT, at TEXT, question TEXT, status TEXT, details TEXT);''')
    dispatch.initialize(db)
limiter = defaultdict(deque)
lock = threading.Lock()
active = set()


def make_sandbox():
    with state_db() as db:
        cases, events = dispatch.snapshot(db,session['user'])
    return Sandbox(session['user'],{'dispatch_cases':cases,'dispatch_events':events})


def limited(key, maximum, seconds=60):
    now = time.monotonic()
    with lock:
        queue = limiter[key]
        while queue and queue[0] < now-seconds:
            queue.popleft()
        if len(queue) >= maximum:
            return True
        queue.append(now)
        if len(limiter) > 5000:
            stale = [k for k,v in limiter.items() if not v or v[-1] < now-3600]
            for k in stale:
                del limiter[k]
    return False


@app.before_request
def protect():
    if request.path.startswith('/api/') and request.method == 'POST':
        csrf = session.get('csrf','')
        if not csrf or not hmac.compare_digest(csrf, request.headers.get('X-CSRF-Token','')):
            return jsonify(error='会话验证失效，请刷新页面。'), 403
        if not request.is_json:
            return jsonify(error='请求必须使用JSON。'), 415
    if request.path.startswith('/api/') and request.path not in ['/api/bootstrap','/api/login']:
        if session.get('user') not in USERS:
            return jsonify(error='请先选择演示身份。'), 401


@app.after_request
def headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
    if request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/')
def index():
    return send_from_directory(app.static_folder,'index.html')


@app.get('/healthz')
def health():
    return jsonify(status='ok')


@app.get('/api/bootstrap')
def bootstrap():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_hex(24)
    return jsonify(csrf=session['csrf'], user=session.get('user'), users=USERS, mode='llm' if enabled() else 'demo',
                   snapshot=str(AS_OF), counts={k:len(v) for k,v in DATA.items()},
                   password_required=bool(os.getenv('DEMO_ACCESS_CODE')))


@app.post('/api/login')
def login():
    if limited('login:'+request.remote_addr, 15):
        return jsonify(error='尝试过于频繁，请稍后再试。'), 429
    body = request.get_json()
    user = body.get('user') if isinstance(body,dict) else None
    code = os.getenv('DEMO_ACCESS_CODE','')
    supplied = body.get('code','') if isinstance(body,dict) else ''
    if code and (not isinstance(supplied,str) or not hmac.compare_digest(code,supplied)):
        return jsonify(error='演示访问码不正确。'), 403
    if user not in USERS:
        return jsonify(error='未知身份。'), 400
    session.clear()
    session.update(user=user, csrf=secrets.token_hex(24), sid=secrets.token_hex(24))
    return jsonify(csrf=session['csrf'], user=user)


@app.get('/api/overview')
def overview():
    box = make_sandbox()
    try:
        result = box.execute("SELECT count(*) AS total,sum(CASE WHEN status='已办结' THEN 1 ELSE 0 END) AS closed,sum(overdue) AS overdue,round(avg(handling_hours),1) AS avg_hours FROM tickets")
        categories = box.execute('SELECT category,count(*) AS total FROM tickets GROUP BY category ORDER BY total DESC')
        trend = box.execute("SELECT strftime('%Y-%m',created_at) AS month,count(*) AS total FROM tickets GROUP BY month ORDER BY month")
        return jsonify(stats=result['rows'][0], scope=USERS[session['user']]['scope'], metrics=METRICS,
                       schema=box.schema(), visible_counts=box.counts, categories=categories, trend=trend)
    finally:
        box.close()


def read_history():
    with state_db() as db:
        row = db.execute('SELECT messages FROM conversations WHERE id=? AND principal=?', (session['sid'],session['user'])).fetchone()
    return json.loads(row[0]) if row else []


@app.get('/api/history')
def history():
    return jsonify(messages=read_history())


@app.post('/api/new')
def new_chat():
    session['sid'] = secrets.token_hex(24)
    return jsonify(ok=True)


@app.get('/api/audit')
def audit():
    with state_db() as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('SELECT id,principal,at,question,status FROM audit WHERE principal=? ORDER BY id DESC LIMIT 30',(session['user'],)).fetchall()
    return jsonify(rows=[dict(r) for r in rows])


@app.post('/api/chat')
def chat():
    sid = session['sid']
    if limited('chat:'+sid, 12) or limited('global', 120):
        return jsonify(error='查询较频繁，请稍后再试。'), 429
    body = request.get_json()
    q = body.get('question') if isinstance(body,dict) else None
    if not isinstance(q,str) or not 1 <= len(q.strip()) <= 1500:
        return jsonify(error='请输入1至1500字的问题。'), 400
    with lock:
        if sid in active:
            return jsonify(error='当前会话已有查询运行中。'), 409
        active.add(sid)
    box = None
    status, detail = 'error', ''
    try:
        box = make_sandbox()
        previous = read_history()
        result = run_agent(box,q.strip(),previous)
        result['scope'] = USERS[session['user']]['scope']
        result['metrics'] = METRICS
        messages = previous + [{'role':'user','content':q.strip()}, {'role':'assistant','content':result['title']+'：'+result['summary']}]
        with state_db() as db:
            db.execute('INSERT OR REPLACE INTO conversations VALUES (?,?,?,?)',(sid,session['user'],json.dumps(messages[-16:],ensure_ascii=False),time.time()))
            db.execute('DELETE FROM conversations WHERE updated<?',(time.time()-7*86400,))
        status, detail = 'ok', json.dumps(result['trace'],ensure_ascii=False)
        return jsonify(result)
    except QueryError as exc:
        detail = str(exc)
        return jsonify(error=detail), 422
    except Exception:
        logging.exception('Agent execution failed')
        detail = '服务或模型调用异常'
        return jsonify(error='模型服务调用失败或返回格式异常。请检查服务端模型地址、密钥、模型工具调用支持情况后重试；系统未伪造结果。'), 502
    finally:
        if box:
            box.close()
        with lock:
            active.discard(sid)
        with state_db() as db:
            db.execute('INSERT INTO audit(principal,at,question,status,details) VALUES (?,?,?,?,?)',
                       (session['user'],time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),q,status,detail))
            db.execute('DELETE FROM audit WHERE id < (SELECT coalesce(max(id),0)-10000 FROM audit)')


@app.errorhandler(dispatch.DispatchError)
def dispatch_error(exc):
    return jsonify(error=str(exc)),exc.status


def dispatch_body():
    body=request.get_json()
    if not isinstance(body,dict):
        raise dispatch.DispatchError('请求格式必须是JSON对象。')
    return body


@app.get('/api/dispatch')
def dispatch_list():
    with state_db() as db:
        rows,events=dispatch.snapshot(db,session['user'])
    return jsonify(rows=rows[:100],total=len(rows),event_count=len(events),can_dispatch=session['user'] in ('city','district'),
        districts=[USERS[session['user']]['district']] if USERS[session['user']]['district'] else dispatch.DISTRICTS,
        departments=[{'id':i+1,'name':name} for i,name in enumerate(dispatch.DEPTS)],
        model_available=enabled(),scope=USERS[session['user']]['scope'])


@app.get('/api/dispatch/<cid>')
def dispatch_detail(cid):
    with state_db() as db:
        row=dispatch.get_case(db,session['user'],cid)
        events=[dict(e) for e in db.execute('SELECT * FROM dispatch_events WHERE case_id=? ORDER BY id',(cid,))]
    return jsonify(case=row,events=events)


@app.post('/api/dispatch/create')
def dispatch_create():
    body=dispatch_body()
    if limited('intake:'+session['user'],20):
        return jsonify(error='录入过于频繁，请稍后重试。'),429
    with state_db() as db:
        case=dispatch.create_case(db,session['user'],body)
    return jsonify(case=case)


@app.post('/api/dispatch/recommend')
def dispatch_recommend():
    body=dispatch_body()
    cid=body.get('case_id')
    if not isinstance(cid,str) or type(body.get('use_model',False)) is not bool or type(body.get('automatic',False)) is not bool:
        raise dispatch.DispatchError('工单编号或派单参数格式错误。')
    if limited('recommend:'+session['sid'],10):
        return jsonify(error='推荐过于频繁，请稍后重试。'),429
    with state_db() as db:
        case=dispatch.get_case(db,session['user'],cid,write=True)
        if case['status']=='已派单':
            raise dispatch.DispatchError('该工单已派单。',409)
    try:
        recommendation=dispatch.recommend(case,body.get('use_model',False))
    except QueryError as exc:
        return jsonify(error=str(exc)),422
    with state_db() as db:
        db.execute('BEGIN IMMEDIATE')
        latest=dispatch.get_case(db,session['user'],cid,write=True)
        if latest['version']!=case['version'] or latest['status']=='已派单':
            raise dispatch.DispatchError('工单已更新，请刷新后重试。',409)
        proposal=dispatch.save_proposal(db,session['user'],case,recommendation)
        if body.get('automatic') and recommendation['auto_eligible']:
            case=dispatch.execute(db,session['user'],cid,proposal['proposal_id'],automatic=True)
        else:
            case=dispatch.get_case(db,session['user'],cid)
    return jsonify(case=case,proposal=proposal)


@app.post('/api/dispatch/execute')
def dispatch_execute():
    body=dispatch_body()
    if not isinstance(body.get('case_id'),str) or not isinstance(body.get('proposal_id'),str):
        raise dispatch.DispatchError('缺少工单或建议编号。')
    with state_db() as db:
        db.execute('BEGIN IMMEDIATE')
        case=dispatch.execute(db,session['user'],body['case_id'],body['proposal_id'],
            manual_department=body.get('department_id'),reason=body.get('reason',''))
    return jsonify(case=case)


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.getenv('PORT','8000')), debug=False)
