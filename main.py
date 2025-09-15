from flask import Flask, request, redirect, url_for, render_template_string, g, session, flash, send_file
from datetime import date, datetime
import os, io, json, random
import psycopg2
import psycopg2.extras
from openpyxl import Workbook

# ------------------ 앱 설정 ------------------
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "7467")
DB_URL = os.environ.get("DATABASE_URL")  # Render Env에 넣은 값

# ------------------ DB 연결/헬퍼 ------------------
def get_db():
    conn = getattr(g, "_db_conn", None)
    if conn is None:
        if not DB_URL:
            raise RuntimeError("DATABASE_URL not set")
        conn = g._db_conn = psycopg2.connect(DB_URL, sslmode="require")
    return conn

def db_execute(sql: str, params=()):
    # sqlite 스타일의 ? 플레이스홀더를 postgres %s 로 치환
    sql = sql.replace("?", "%s")
    cur = get_db().cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    return cur

@app.teardown_appcontext
def close_db(_exc):
    conn = getattr(g, "_db_conn", None)
    if conn is not None:
        conn.close()

def init_db():
    # 스키마 생성
    db_execute("""CREATE TABLE IF NOT EXISTS members(
      name TEXT PRIMARY KEY
    );""")

    db_execute("""CREATE TABLE IF NOT EXISTS deposits(
      id SERIAL PRIMARY KEY,
      dt TEXT NOT NULL,
      name TEXT NOT NULL,
      amount INTEGER NOT NULL,
      note TEXT DEFAULT '',
      CONSTRAINT fk_dep_member FOREIGN KEY(name) REFERENCES members(name) ON DELETE CASCADE
    );""")

    db_execute("""CREATE TABLE IF NOT EXISTS meals(
      id SERIAL PRIMARY KEY,
      dt TEXT NOT NULL,
      entry_mode TEXT NOT NULL DEFAULT 'total',  -- 'total' | 'detailed'
      main_mode TEXT NOT NULL DEFAULT 'custom',  -- 'equal' | 'custom'
      side_mode TEXT NOT NULL DEFAULT 'none',    -- 'equal' | 'custom' | 'none'
      main_total INTEGER NOT NULL DEFAULT 0,
      side_total INTEGER NOT NULL DEFAULT 0,
      grand_total INTEGER NOT NULL DEFAULT 0,
      payer_name TEXT,
      guest_total INTEGER NOT NULL DEFAULT 0,
      CONSTRAINT fk_meal_payer FOREIGN KEY(payer_name) REFERENCES members(name) ON DELETE SET NULL
    );""")

    db_execute("""CREATE TABLE IF NOT EXISTS meal_parts(
      id SERIAL PRIMARY KEY,
      meal_id INTEGER NOT NULL,
      name TEXT NOT NULL,
      main_amount INTEGER NOT NULL DEFAULT 0,
      side_amount INTEGER NOT NULL DEFAULT 0,
      total_amount INTEGER NOT NULL DEFAULT 0,
      CONSTRAINT fk_mp_meal FOREIGN KEY(meal_id) REFERENCES meals(id) ON DELETE CASCADE,
      CONSTRAINT fk_mp_member FOREIGN KEY(name) REFERENCES members(name) ON DELETE CASCADE
    );""")

    db_execute("""CREATE TABLE IF NOT EXISTS notices(
      id SERIAL PRIMARY KEY,
      dt TEXT NOT NULL,
      content TEXT NOT NULL
    );""")

    # 감사 로그
    db_execute("""CREATE TABLE IF NOT EXISTS audit_logs(
      id SERIAL PRIMARY KEY,
      dt TEXT NOT NULL,
      action TEXT NOT NULL,        -- insert/update/delete
      target_table TEXT NOT NULL,  -- deposits/meals/meal_parts/notices
      target_id INTEGER,
      payload TEXT                  -- JSON string
    );""")

    # 게임 기록 & 통계
    db_execute("""CREATE TABLE IF NOT EXISTS games(
      id SERIAL PRIMARY KEY,
      dt TEXT NOT NULL,
      game_type TEXT NOT NULL,     -- dice/ladder/oddcard
      rule TEXT NOT NULL,          -- rule text
      participants TEXT NOT NULL,  -- JSON list of names
      winner TEXT,                 -- optional
      loser TEXT,                  -- main loser
      extra TEXT                   -- JSON for extra info
    );""")

    db_execute("""CREATE TABLE IF NOT EXISTS hogu_stats(
      name TEXT PRIMARY KEY,
      losses INTEGER NOT NULL DEFAULT 0
    );""")

    get_db().commit()

def log_audit(action, table, target_id=None, payload=None):
    db_execute(
        "INSERT INTO audit_logs(dt, action, target_table, target_id, payload) VALUES (?,?,?,?,?);",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action, table, target_id, json.dumps(payload or {}, ensure_ascii=False))
    )

# Flask 3.x 호환: 모듈 임포트 시 테이블 보장
with app.app_context():
    try:
        init_db()
    except Exception as e:
        app.logger.warning(f"DB init skipped or already exists: {e}")

# ------------------ 유틸 ------------------
def get_members():
    cur = db_execute("SELECT name FROM members ORDER BY name;")
    return [r["name"] for r in cur.fetchall()]

def split_even(total, n):
    if n <= 0: return []
    base = total // n
    rem = total % n
    shares = [base] * n
    for i in range(rem): shares[i] += 1
    return shares

def get_balances():
    members = get_members()
    dep_map = {m: 0 for m in members}
    cur = db_execute("SELECT name, SUM(amount) AS s FROM deposits GROUP BY name;")
    for r in cur.fetchall(): dep_map[r["name"]] = r["s"] or 0
    use_map = {m: 0 for m in members}
    cur = db_execute("SELECT name, SUM(total_amount) AS s FROM meal_parts GROUP BY name;")
    for r in cur.fetchall(): use_map[r["name"]] = r["s"] or 0
    return [{"name": m, "deposit": dep_map.get(m,0), "used": use_map.get(m,0),
             "balance": dep_map.get(m,0)-use_map.get(m,0)} for m in members]

def get_balance_of(name):
    dep = (db_execute("SELECT COALESCE(SUM(amount),0) AS s FROM deposits WHERE name=?;", (name,)).fetchone() or {}).get("s",0)
    used = (db_execute("SELECT COALESCE(SUM(total_amount),0) AS s FROM meal_parts WHERE name=?;", (name,)).fetchone() or {}).get("s",0)
    return dep - used

def get_meal_counts_map():
    rows = db_execute("SELECT name, COUNT(*) AS c FROM meal_parts GROUP BY name;").fetchall()
    return {r["name"]: (r["c"] or 0) for r in rows}

def html_escape(s):
    if s is None: return ""
    return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def delete_auto_deposit_for_meal(meal_id:int):
    db_execute("DELETE FROM deposits WHERE note LIKE ?;", (f"%식사 #{meal_id} 선결제 상환%",))
    get_db().commit()
    log_audit("delete", "deposits", None, {"auto_by_meal": meal_id})

def upsert_hogu_loss(name, n=1):
    if not name:
        return
    db_execute("INSERT INTO hogu_stats(name, losses) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET losses=hogu_stats.losses+?;",
               (name, n, n))

# ------------------ 로그인 보호 ------------------
@app.before_request
def require_login():
    if request.path not in ("/login", "/favicon.ico", "/ping"):
        if not session.get("authed"):
            return redirect(url_for("login"))

# ------------------ 템플릿 ------------------
BASE = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>점심 과비 관리</title>
  <link href="https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/cosmo/bootstrap.min.css" rel="stylesheet">
  <style>
    :root { --brand-green: #00854A; }
    body { padding-bottom: 40px; }
    .num { text-align: right; }
    .table-sm td, .table-sm th { padding:.45rem; }
    ul.compact li { margin-bottom: .25rem; }
    .form-text { font-size: .85rem; }

    header.topbar { background: var(--brand-green); color:#fff; }
    header.topbar a, header.topbar .nav-link { color:#fff !important; }
    header.topbar .nav-link:hover { opacity:.9; }
  </style>
</head>
<body class="bg-light">
<header class="topbar mb-3">
  <div class="container py-2 d-flex justify-content-between align-items-center">
    <a class="navbar-brand fw-bold text-white m-0" href="{{ url_for('home') }}">🍱 점심 과비 관리</a>
    <a class="btn btn-sm btn-outline-light" href="{{ url_for('logout') }}">로그아웃</a>
  </div>
  <div class="container pb-2">
    <ul class="nav nav-pills">
      <li class="nav-item"><a class="nav-link" href="{{ url_for('deposit') }}">입금 등록</a></li>
      <li class="nav-item"><a class="nav-link" href="{{ url_for('meal') }}">식사 등록</a></li>
      <li class="nav-item"><a class="nav-link" href="{{ url_for('meals') }}">식사 기록</a></li>
      <li class="nav-item"><a class="nav-link" href="{{ url_for('status') }}">현황/정산</a></li>
      <li class="nav-item"><a class="nav-link" href="{{ url_for('notices') }}">공지사항</a></li>
      <li class="nav-item"><a class="nav-link" href="{{ url_for('settings') }}">팀원설정</a></li>
      <li class="nav-item"><a class="nav-link" href="{{ url_for('games_home') }}">호구게임</a></li>
    </ul>
  </div>
</header>

<div class="container">
  {% with msgs = get_flashed_messages(with_categories=true) %}
    {% if msgs %}
      {% for cat, msg in msgs %}
        <div class="alert alert-{{cat}}">{{ msg|safe }}</div>
      {% endfor %}
    {% endif %}
  {% endwith %}
  {{ body|safe }}
</div>
</body>
</html>
"""

def render(body_html, **ctx):
    return render_template_string(BASE, body=body_html, **ctx)

# ------------------ 로그인/로그아웃/핑 ------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pw = (request.form.get("password") or "").strip()
        if pw == APP_PASSWORD:
            session['authed'] = True
            return redirect(url_for('home'))
        flash("비밀번호가 올바르지 않습니다.", "danger")
    body = """
    <div class="row justify-content-center">
      <div class="col-12 col-md-6 col-lg-4">
        <div class="card shadow-sm">
          <div class="card-body">
            <h5 class="card-title">로그인</h5>
            <form method="post">
              <div class="mb-3">
                <label class="form-label">비밀번호</label>
                <input class="form-control" type="password" name="password" placeholder="비밀번호를 입력하세요">
              </div>
              <button class="btn btn-primary w-100">로그인</button>
            </form>
          </div>
        </div>
      </div>
    </div>
    """
    return render(body)

@app.get("/logout")
def logout():
    session.pop('authed', None)
    flash("로그아웃 되었습니다.", "info")
    return redirect(url_for('login'))

@app.get("/ping")
def ping():
    return "OK", 200

# ------------------ 홈 ------------------
@app.route("/")
def home():
    members = get_members()

    # 마이너스 잔액 공지
    notice_html = ""
    if members:
        negatives = [b for b in get_balances() if b["balance"] < 0]
        if negatives:
            items = "".join([
                f"<li><strong>{b['name']}</strong> : <span class='text-danger'>{b['balance']:,}원</span></li>"
                for b in negatives
            ])
            notice_html = f"""
            <div class="alert alert-warning shadow-sm" role="alert">
              <div class="d-flex align-items-center mb-1">
                <span class="me-2">🔔</span>
                <strong>공지:</strong>&nbsp;잔액이 마이너스인 인원이 있습니다.
              </div>
              <ul class="mb-0">{items}</ul>
            </div>"""

    # 공지 5개
    notices_html = ""
    nrows = db_execute("SELECT dt, content FROM notices ORDER BY id DESC LIMIT 5;").fetchall()
    if nrows:
        lis = "".join([
            f"<li><span class='text-muted me-2'>[{r['dt']}]</span>{html_escape(r['content'])}</li>"
            for r in nrows
        ])
        notices_html = f"""
        <div class="alert alert-info shadow-sm">
          <div class="fw-bold mb-1">📌 공지사항</div>
          <ul class="mb-0">{lis}</ul>
        </div>"""

    balances_map = {b["name"]: b["balance"] for b in get_balances()}
    counts_map = get_meal_counts_map()
    member_items = "".join([
        f"<li class='d-flex justify-content-between'><span>{n}</span>"
        f"<span class='text-white-50'>잔액 {balances_map.get(n,0):,}원 · 식사 {counts_map.get(n,0)}회</span></li>"
        for n in members
    ])
    body = f"""
    {notice_html}
    {notices_html}
    <div class="row g-3">
      <div class="col-12">
        <div class="card shadow-sm bg-dark text-white">
          <div class="card-body">
            <h5 class="card-title">등록된 팀원 (총 {len(members)}명)</h5>
            <ul class="mb-0 compact" style="color:white;">{member_items}</ul>
            <div class="mt-3">
              <a class="btn btn-sm btn-secondary" href="{ url_for('settings') }">팀원설정</a>
            </div>
          </div>
        </div>
      </div>
    </div>
    """
    return render(body)

# ------------------ 공지사항 ------------------
@app.route("/notices", methods=["GET", "POST"])
def notices():
    if request.method == "POST":
        content = (request.form.get("content") or "").strip()
        if content:
            db_execute("INSERT INTO notices(dt, content) VALUES (?,?);",
                       (datetime.now().strftime("%Y-%m-%d %H:%M"), content))
            get_db().commit()
            log_audit("insert", "notices", None, {"content": content})
            flash("공지사항이 등록되었습니다.", "success")
        else:
            flash("내용을 입력하세요.", "warning")
        return redirect(url_for("notices"))
    rows = db_execute("SELECT id, dt, content FROM notices ORDER BY id DESC LIMIT 100;").fetchall()
    items = "".join([
        f"<tr><td>#{r['id']}</td><td>{r['dt']}</td><td>{html_escape(r['content'])}</td>"
        f"<td><form method='post' action='{ url_for('notice_delete') }' onsubmit=\"return confirm('삭제할까요?');\">"
        f"<input type='hidden' name='id' value='{r['id']}'><button class='btn btn-sm btn-outline-danger'>삭제</button></form></td></tr>"
        for r in rows
    ])
    body = f"""
    <div class="row g-3">
      <div class="col-12 col-lg-5">
        <div class="card shadow-sm">
          <div class="card-body">
            <h5 class="card-title">공지 등록</h5>
            <form method="post">
              <textarea class="form-control" name="content" rows="4" placeholder="공지 내용을 입력하세요"></textarea>
              <div class="mt-2 d-flex gap-2">
                <button class="btn btn-primary">등록</button>
                <a class="btn btn-outline-secondary" href="{ url_for('home') }">메인으로</a>
              </div>
            </form>
          </div>
        </div>
      </div>
      <div class="col-12 col-lg-7">
        <div class="card shadow-sm">
          <div class="card-body">
            <h5 class="card-title">공지 목록</h5>
            <div class="table-responsive">
              <table class="table table-sm align-middle">
                <thead><tr><th>ID</th><th>작성시각</th><th>내용</th><th>관리</th></tr></thead>
                <tbody>{items}</tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
    """
    return render(body)

@app.post("/notice/delete")
def notice_delete():
    nid = int(request.form.get("id") or 0)
    if nid:
        row = db_execute("SELECT * FROM notices WHERE id=?;", (nid,)).fetchone()
        db_execute("DELETE FROM notices WHERE id=?;", (nid,))
        get_db().commit()
        log_audit("delete", "notices", nid, row)
        flash("삭제되었습니다.", "info")
    return redirect(url_for("notices"))

# ------------------ 팀원설정 ------------------
@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        new_name = (request.form.get("new_name") or "").strip()
        if new_name:
            cur = db_execute("INSERT INTO members(name) VALUES (?) ON CONFLICT (name) DO NOTHING;", (new_name,))
            get_db().commit()
            if cur.rowcount == 0:
                flash("이미 존재하는 이름입니다.", "warning")
            else:
                flash(f"팀원 <b>{html_escape(new_name)}</b> 추가 완료.", "success")
        return redirect(url_for('settings'))

    members = get_members()
    rows = ""
    for nm in members:
        bal = get_balance_of(nm)
        bal_html = f"{bal:,}"
        badge = f"<span class='badge bg-danger'>잔액 {bal_html}원</span>" if bal != 0 else "<span class='badge bg-success'>잔액 0원</span>"
        rows += f"""
        <tr>
          <td>{nm}</td>
          <td class="num">{bal_html}</td>
          <td>{badge}</td>
          <td>
            <form method="post" action="{ url_for('member_delete') }" onsubmit="return confirmDelete('{nm}', {bal});">
              <input type="hidden" name="name" value="{nm}">
              <button class="btn btn-sm btn-outline-danger">삭제</button>
            </form>
          </td>
        </tr>"""
    body = f"""
    <div class="row g-3">
      <div class="col-12 col-lg-7">
        <div class="card shadow-sm">
          <div class="card-body">
            <h5 class="card-title">팀원설정</h5>
            <p class="text-muted">입력된 사람만 사용됩니다. 인원 수 제한 없음.</p>
            <div class="table-responsive">
              <table class="table table-sm align-middle">
                <thead><tr><th>이름</th><th class='text-end'>잔액</th><th>상태</th><th>관리</th></tr></thead>
                <tbody>{rows}</tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
      <div class="col-12 col-lg-5">
        <div class="card shadow-sm">
          <div class="card-body">
            <h5 class="card-title">팀원추가</h5>
            <form method="post">
              <div class="mb-2"><input class="form-control" name="new_name" placeholder="새 팀원 이름"></div>
              <button class="btn btn-primary">추가</button>
              <a class="btn btn-outline-secondary" href="{ url_for('home') }">뒤로</a>
            </form>
          </div>
        </div>
      </div>
    </div>

    <script>
      function confirmDelete(name, balance) {{
        if (balance !== 0) {{
          return confirm("⚠️ 잔액 " + balance.toLocaleString() + "원이 남아있습니다.\\n삭제하면 관련 입금/사용 기록도 함께 삭제됩니다. 계속할까요?");
        }}
        return confirm("'" + name + "' 팀원을 삭제하시겠습니까?");
      }}
    </script>
    """
    return render(body)

@app.post("/member/delete")
def member_delete():
    nm = (request.form.get("name") or "").strip()
    if not nm: return redirect(url_for('settings'))
    bal = get_balance_of(nm)
    if bal != 0:
        flash(f"잔액이 0원이 아닌 팀원은 삭제할 수 없습니다. (현재: {bal:,}원)", "warning")
        return redirect(url_for('settings'))
    db_execute("DELETE FROM members WHERE name=?;", (nm,))
    get_db().commit()
    log_audit("delete", "members", None, {"name": nm})
    flash(f"<b>{html_escape(nm)}</b> 삭제 완료.", "success")
    return redirect(url_for('settings'))

# ------------------ 입금: 등록/목록/수정/삭제 ------------------
@app.route("/deposit", methods=["GET", "POST"])
def deposit():
    members = get_members()
    if request.method == "POST":
        dt = request.form.get("dt") or str(date.today())
        name = request.form.get("name")
        amount = int(request.form.get("amount") or 0)
        note = (request.form.get("note") or "").strip()
        if name and amount > 0:
            cur = db_execute("INSERT INTO deposits(dt, name, amount, note) VALUES (?,?,?,?) RETURNING id;",
                             (dt, name, amount, note))
            new_id = cur.fetchone()["id"]
            get_db().commit()
            log_audit("insert", "deposits", new_id, {"dt":dt,"name":name,"amount":amount,"note":note})
            flash("입금 등록 완료.", "success")
        else:
            flash("이름과 금액을 확인하세요.", "warning")
        return redirect(url_for("deposit"))

    rows = db_execute("SELECT id, dt, name, amount, note FROM deposits ORDER BY id DESC LIMIT 100;").fetchall()
    hist = "".join([
        f"<tr><td>{r['dt']}</td><td>{r['name']}</td><td class='num'>{r['amount']:,}</td>"
        f"<td>{html_escape(r['note'] or '')}</td>"
        f"<td class='text-end'><a class='btn btn-sm btn-outline-primary' href='{ url_for('deposit_edit', dep_id=r['id']) }'>수정</a> "
        f"<a class='btn btn-sm btn-outline-danger' href='{ url_for('deposit_delete', dep_id=r['id']) }' onclick='return confirm(\"삭제할까요?\");'>삭제</a></td></tr>"
        for r in rows
    ])
    opts = "".join([f"<option value='{n}'>{n}</option>" for n in members])
    body = f"""
    <div class="row g-3">
      <div class="col-12 col-lg-5">
        <div class="card shadow-sm">
          <div class="card-body">
            <h5 class="card-title">입금 등록</h5>
            <form method="post">
              <div class="mb-2">
                <label class="form-label">날짜</label>
                <input class="form-control" type="date" name="dt" value="{str(date.today())}">
              </div>
              <div class="mb-2">
                <label class="form-label">이름</label>
                <select class="form-select" name="name">{opts}</select>
              </div>
              <div class="mb-2">
                <label class="form-label">금액(원)</label>
                <input class="form-control num" name="amount" type="number" min="0" step="1" placeholder="예: 10000">
              </div>
              <div class="mb-2">
                <label class="form-label">메모(선택)</label>
                <input class="form-control" name="note" placeholder="예: 현금 입금, 이체 등">
              </div>
              <button class="btn btn-primary">등록</button>
            </form>
          </div>
        </div>
      </div>
      <div class="col-12 col-lg-7">
        <div class="card shadow-sm">
          <div class="card-body">
            <h5 class="card-title">최근 입금 내역</h5>
            <table class="table table-sm">
              <thead><tr><th>날짜</th><th>이름</th><th class='text-end'>금액</th><th>메모</th><th class='text-end'>관리</th></tr></thead>
              <tbody>{hist}</tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
    """
    return render(body)

@app.get("/deposit/<int:dep_id>/edit")
def deposit_edit(dep_id):
    r = db_execute("SELECT * FROM deposits WHERE id=?;", (dep_id,)).fetchone()
    if not r:
        flash("입금 내역이 없습니다.", "danger"); return redirect(url_for("deposit"))
    members = get_members()
    opts = "".join([f"<option value='{n}'{' selected' if n==r['name'] else ''}>{n}</option>" for n in members])
    body = f"""
    <div class="card shadow-sm">
      <div class="card-body">
        <h5 class="card-title">입금 수정 #{dep_id}</h5>
        <form method="post" action="{ url_for('deposit_update', dep_id=dep_id) }">
          <div class="row g-2">
            <div class="col-12 col-md-3">
              <label class="form-label">날짜</label>
              <input class="form-control" type="date" name="dt" value="{r['dt']}">
            </div>
            <div class="col-12 col-md-3">
              <label class="form-label">이름</label>
              <select class="form-select" name="name">{opts}</select>
            </div>
            <div class="col-12 col-md-3">
              <label class="form-label">금액(원)</label>
              <input class="form-control num" type="number" name="amount" min="0" step="1" value="{r['amount']}">
            </div>
            <div class="col-12 col-md-3">
              <label class="form-label">메모</label>
              <input class="form-control" name="note" value="{html_escape(r['note'] or '')}">
            </div>
          </div>
          <div class="mt-3 d-flex gap-2">
            <button class="btn btn-primary">저장</button>
            <a class="btn btn-outline-secondary" href="{ url_for('deposit') }">취소</a>
          </div>
        </form>
      </div>
    </div>
    """
    return render(body)

@app.post("/deposit/<int:dep_id>/edit")
def deposit_update(dep_id):
    old = db_execute("SELECT * FROM deposits WHERE id=?;", (dep_id,)).fetchone()
    dt = request.form.get("dt") or str(date.today())
    name = request.form.get("name")
    amount = int(request.form.get("amount") or 0)
    note = (request.form.get("note") or "").strip()
    if name and amount >= 0:
        db_execute("UPDATE deposits SET dt=?, name=?, amount=?, note=? WHERE id=?;", (dt, name, amount, note, dep_id))
        get_db().commit()
        log_audit("update", "deposits", dep_id, {"before": old, "after": {"dt":dt,"name":name,"amount":amount,"note":note}})
        flash("수정되었습니다.", "success")
    else:
        flash("입력값을 확인하세요.", "warning")
    return redirect(url_for("deposit"))

@app.get("/deposit/<int:dep_id>/delete")
def deposit_delete(dep_id):
    old = db_execute("SELECT * FROM deposits WHERE id=?;", (dep_id,)).fetchone()
    db_execute("DELETE FROM deposits WHERE id=?;", (dep_id,))
    get_db().commit()
    log_audit("delete", "deposits", dep_id, old)
    flash("삭제되었습니다.", "info")
    return redirect(url_for("deposit"))

# ------------------ 식사 폼 공통 UI ------------------
def _meal_form_html(members, initial=None, edit_target_id=None):
    today_str = str(date.today())
    entry_mode = (initial or {}).get("entry_mode", "total")
    main_mode  = (initial or {}).get("main_mode", "custom")
    side_mode  = (initial or {}).get("side_mode", "none")
    dt_val     = (initial or {}).get("dt", today_str)
    payer_name = (initial or {}).get("payer_name", "") or ""
    grand_total = int((initial or {}).get("grand_total", 0) or 0)
    main_total  = int((initial or {}).get("main_total", 0) or 0)
    side_total  = int((initial or {}).get("side_total", 0) or 0)
    guest_total = int((initial or {}).get("guest_total", 0) or 0)

    parts_map = {(p["name"]): p for p in (initial or {}).get("parts", [])}
    ate_set = set(parts_map.keys())

    rows_totalcustom = ""
    rows_detailed = ""
    for m in members:
        tot_val = parts_map.get(m, {}).get("total_amount", 0)
        m_main = parts_map.get(m, {}).get("main_amount", 0)
        m_side = parts_map.get(m, {}).get("side_amount", 0)
        checked = "checked" if m in ate_set else ""
        rows_totalcustom += f"""
        <tr>
          <td><input class="form-check-input" type="checkbox" name="ate_{m}" {checked}></td>
          <td>{m}</td>
          <td><input class="form-control form-control-sm num total-custom-cell" type="number" name="tot_{m}" min="0" step="1" value="{tot_val}" {'disabled' if entry_mode!='total' else ''}></td>
        </tr>"""
        rows_detailed += f"""
        <tr>
          <td><input class="form-check-input" type="checkbox" name="ate_{m}" {checked}></td>
          <td>{m}</td>
          <td><input class="form-control form-control-sm num main-custom-cell" type="number" name="main_{m}" min="0" step="1" value="{m_main}"></td>
          <td class="side-custom-cell"><input class="form-control form-control-sm num" type="number" name="side_{m}" min="0" step="1" value="{m_side}" {'disabled' if side_mode!='custom' else ''}></td>
        </tr>"""

    payer_options = "<option value=''></option>" + "".join([f"<option value='{n}'{' selected' if n==payer_name else ''}>{n}</option>" for n in members])

    em_total_ck = "checked" if entry_mode=="total" else ""
    em_detail_ck = "checked" if entry_mode=="detailed" else ""
    mm_custom_ck = "checked" if main_mode=="custom" else ""
    mm_equal_ck  = "checked" if main_mode=="equal" else ""
    sm_equal_ck  = "checked" if side_mode=="equal" else ""
    sm_custom_ck = "checked" if side_mode=="custom" else ""
    sm_none_ck   = "checked" if side_mode=="none" else ""

    html = f"""
    <div class="card shadow-sm">
      <div class="card-body">
        <h5 class="card-title">{'식사 등록' if edit_target_id is None else f'식사 수정 #{edit_target_id}'}</h5>
        <form method="post" id="mealForm">
          <div class="row g-3">
            <div class="col-12 col-md-3">
              <label class="form-label">날짜</label>
              <input class="form-control" type="date" name="dt" value="{dt_val}">
            </div>
            <div class="col-12 col-md-4">
              <label class="form-label d-block">입력 방식</label>
              <div class="d-flex gap-3">
                <div class="form-check">
                  <input class="form-check-input" type="radio" name="entry_mode" id="em_total" value="total" {em_total_ck}>
                  <label class="form-check-label" for="em_total">총액 기반</label>
                </div>
                <div class="form-check">
                  <input class="form-check-input" type="radio" name="entry_mode" id="em_detailed" value="detailed" {em_detail_ck}>
                  <label class="form-check-label" for="em_detailed">상세(메인/사이드)</label>
                </div>
              </div>
            </div>
            <div class="col-12 col-md-5">
              <label class="form-label">결제자(선결제자)</label>
              <select class="form-select" name="payer_name">{payer_options}</select>
              <div class="form-text">결제자가 팀원일 때만, 팀원 몫 합계가 자동 입금(정산)으로 반영됩니다.</div>
            </div>

            <div class="col-12" id="totalBox" style="display:{'block' if entry_mode=='total' else 'none'}">
              <div class="row g-2">
                <div class="col-12 col-md-3">
                  <label class="form-label">총 식비(팀원+게스트)</label>
                  <input class="form-control num" type="number" name="grand_total" min="0" step="1" value="{grand_total}">
                </div>
                <div class="col-12 col-md-3">
                  <label class="form-label">게스트 총액</label>
                  <input class="form-control num" type="number" name="guest_total" min="0" step="1" value="{guest_total}">
                </div>
                <div class="col-12 col-md-6">
                  <label class="form-label d-block">분배 방식(총액)</label>
                  <div class="d-flex gap-3 align-items-center flex-wrap">
                    <div class="form-check">
                      <input class="form-check-input" type="radio" name="total_dist_mode" id="td_equal" value="equal" checked>
                      <label class="form-check-label" for="td_equal">균등분할</label>
                    </div>
                    <div class="form-check">
                      <input class="form-check-input" type="radio" name="total_dist_mode" id="td_custom" value="custom">
                      <label class="form-check-label" for="td_custom">강제입력(사람별 총액)</label>
                    </div>
                    <div class="form-text">팀원 총액 = 총 식비 - 게스트 총액</div>
                  </div>
                </div>
              </div>
              <div class="table-responsive mt-2">
                <table class="table table-sm align-middle">
                  <thead><tr><th>식사</th><th>이름</th><th>사람별 총액(강제입력 모드)</th></tr></thead>
                  <tbody>{rows_totalcustom}</tbody>
                </table>
              </div>
            </div>

            <div class="col-12" id="detailedBox" style="display:{'block' if entry_mode=='detailed' else 'none'}">
              <div class="row g-2">
                <div class="col-12 col-md-6">
                  <label class="form-label d-block">메인 분할 방식</label>
                  <div class="d-flex gap-3 align-items-center flex-wrap">
                    <div class="form-check">
                      <input class="form-check-input" type="radio" name="main_mode" id="mm_custom" value="custom" {mm_custom_ck}>
                      <label class="form-check-label" for="mm_custom">강제입력(사람별)</label>
                    </div>
                    <div class="form-check">
                      <input class="form-check-input" type="radio" name="main_mode" id="mm_equal" value="equal" {mm_equal_ck}>
                      <label class="form-check-label" for="mm_equal">균등분할</label>
                    </div>
                    <div class="ms-3" id="mainTotalWrap" style="display:{'inline-block' if (entry_mode=='detailed' and main_mode=='equal') else 'none'}">
                      <label class="form-label mb-0 me-1">메인 총액</label>
                      <input class="form-control form-control-sm num d-inline-block" style="width:140px" type="number" name="main_total" min="0" step="1" value="{main_total}">
                    </div>
                  </div>
                </div>
                <div class="col-12 col-md-6">
                  <label class="form-label d-block">사이드 분할 방식</label>
                  <div class="d-flex gap-3 align-items-center flex-wrap">
                    <div class="form-check">
                      <input class="form-check-input" type="radio" name="side_mode" id="sm_equal" value="equal" {sm_equal_ck}>
                      <label class="form-check-label" for="sm_equal">균등분할</label>
                    </div>
                    <div class="form-check">
                      <input class="form-check-input" type="radio" name="side_mode" id="sm_custom" value="custom" {sm_custom_ck}>
                      <label class="form-check-label" for="sm_custom">강제입력(사람별)</label>
                    </div>
                    <div class="form-check">
                      <input class="form-check-input" type="radio" name="side_mode" id="sm_none" value="none" {sm_none_ck}>
                      <label class="form-check-label" for="sm_none">없음</label>
                    </div>
                    <div class="ms-3" id="sideTotalWrap" style="display:{'inline-block' if (entry_mode=='detailed' and side_mode=='equal') else 'none'}">
                      <label class="form-label mb-0 me-1">공통 사이드 총액</label>
                      <input class="form-control form-control-sm num d-inline-block" style="width:140px" type="number" name="side_total" min="0" step="1" value="{side_total}">
                    </div>
                  </div>
                </div>
                <div class="col-12">
                  <label class="form-label">게스트(명단 외) 총액</label>
                  <input class="form-control num" type="number" name="guest_total" min="0" step="1" value="{guest_total}" placeholder="예: 20000">
                  <div class="form-text">게스트 금액은 정산에서 제외(기록만).</div>
                </div>
              </div>
              <div class="table-responsive mt-3">
                <table class="table table-sm align-middle">
                  <thead><tr><th>식사</th><th>이름</th><th>메인(강제입력)</th><th>사이드(강제입력)</th></tr></thead>
                  <tbody>{rows_detailed}</tbody>
                </table>
              </div>
            </div>
          </div>

          <div class="mt-2 d-flex gap-2 flex-wrap">
            <button class="btn btn-success">저장</button>
            <a class="btn btn-outline-primary" href="{ url_for('meals') }">식사 기록</a>
            <a class="btn btn-outline-secondary" href="{ url_for('home') }">뒤로</a>
          </div>
        </form>
      </div>
    </div>

    <script>
      const emTotal = document.getElementById('em_total');
      const emDetailed = document.getElementById('em_detailed');
      const totalBox = document.getElementById('totalBox');
      const detailedBox = document.getElementById('detailedBox');
      const totalCustomInputs = document.querySelectorAll('.total-custom-cell');
      function refreshEntryMode() {{
        if (emTotal && emTotal.checked) {{ totalBox.style.display='block'; detailedBox.style.display='none'; }}
        else {{ totalBox.style.display='none'; detailedBox.style.display='block'; }}
        refreshTotalMode(); refreshMainMode(); refreshSideMode();
      }}
      if (emTotal && emDetailed) [emTotal, emDetailed].forEach(r=>r.addEventListener('change', refreshEntryMode));

      const tdEqual = document.getElementById('td_equal');
      const tdCustom = document.getElementById('td_custom');
      function refreshTotalMode() {{
        if (!tdCustom) return;
        const on = tdCustom.checked && (emTotal && emTotal.checked);
        totalCustomInputs.forEach(inp => {{ inp.disabled = !on; if(!on) inp.value = inp.value || 0; }});
      }}
      if (tdEqual && tdCustom) [tdEqual, tdCustom].forEach(r=>r.addEventListener('change', refreshTotalMode));

      const mmCustom = document.getElementById('mm_custom');
      const mmEqual  = document.getElementById('mm_equal');
      const mainTotalWrap = document.getElementById('mainTotalWrap');
      const customMainInputs = document.querySelectorAll('.main-custom-cell');
      function refreshMainMode() {{
        const show = (mmEqual && mmEqual.checked) && (emDetailed && emDetailed.checked);
        if (mainTotalWrap) mainTotalWrap.style.display = show ? 'inline-block' : 'none';
        customMainInputs.forEach(inp => {{
          const dis = (mmEqual && mmEqual.checked) && (emDetailed && emDetailed.checked);
          inp.disabled = dis; if(dis) inp.value = inp.value || 0;
        }});
      }}
      if (mmCustom && mmEqual) [mmCustom, mmEqual].forEach(r=>r.addEventListener('change', refreshMainMode));

      const smEqual = document.getElementById('sm_equal');
      const smCustom = document.getElementById('sm_custom');
      const smNone  = document.getElementById('sm_none');
      const sideTotalWrap = document.getElementById('sideTotalWrap');
      const customSideInputs = document.querySelectorAll('.side-custom-cell input');
      function refreshSideMode() {{
        if (!(emDetailed && emDetailed.checked)) {{
          if (sideTotalWrap) sideTotalWrap.style.display = 'none';
          customSideInputs.forEach(inp => {{ inp.disabled = true; }});
          return;
        }}
        if (smEqual && smEqual.checked) {{
          if (sideTotalWrap) sideTotalWrap.style.display = 'inline-block';
          customSideInputs.forEach(inp => {{ inp.disabled = true; }});
        }} else if (smCustom && smCustom.checked) {{
          if (sideTotalWrap) sideTotalWrap.style.display = 'none';
          customSideInputs.forEach(inp => {{ inp.disabled = false; }});
        }} else {{
          if (sideTotalWrap) sideTotalWrap.style.display = 'none';
          customSideInputs.forEach(inp => {{ inp.disabled = true; }});
        }}
      }}
      if (smEqual && smCustom && smNone) [smEqual, smCustom, smNone].forEach(r=>r.addEventListener('change', refreshSideMode));
      refreshEntryMode();
    </script>
    """
    return html

# ------------------ 식사 등록/상세/수정/삭제 ------------------
@app.route("/meal", methods=["GET", "POST"])
def meal():
    members = get_members()
    if request.method == "POST":
        dt = request.form.get("dt") or str(date.today())
        entry_mode = request.form.get("entry_mode") or "total"
        payer_name = request.form.get("payer_name") or None
        guest_total = int(request.form.get("guest_total") or 0)
        if guest_total < 0: guest_total = 0

        ate_flags = {m: (request.form.get(f"ate_{m}") == "on") for m in members}
        diners = [m for m in members if ate_flags.get(m)]
        if not diners:
            flash("식사한 팀원을 최소 1명 선택하세요.", "warning"); return redirect(url_for("meal"))

        member_totals = {m: 0 for m in diners}
        main_mode, side_mode = "custom", "none"
        main_total = side_total = grand_total = 0

        if entry_mode == "total":
            grand_total = int(request.form.get("grand_total") or 0)
            dist_mode = request.form.get("total_dist_mode") or "equal"
            member_sum_target = max(0, grand_total - guest_total)
            if dist_mode == "equal":
                shares = split_even(member_sum_target, len(diners))
                for i, m in enumerate(diners): member_totals[m] = shares[i]
            else:
                for m in diners:
                    member_totals[m] = max(0, int(request.form.get(f"tot_{m}") or 0))
        else:
            main_mode = request.form.get("main_mode") or "custom"
            side_mode = request.form.get("side_mode") or "none"

            main_dict = {m:0 for m in diners}
            if main_mode == "equal":
                main_total = int(request.form.get("main_total") or 0)
                ms = split_even(main_total, len(diners))
                for i,m in enumerate(diners): main_dict[m] = ms[i]
            else:
                for m in diners: main_dict[m] = max(0, int(request.form.get(f"main_{m}") or 0))

            side_dict = {m:0 for m in diners}
            if side_mode == "equal":
                side_total = int(request.form.get("side_total") or 0)
                ss = split_even(side_total, len(diners))
                for i,m in enumerate(diners): side_dict[m] = ss[i]
            elif side_mode == "custom":
                for m in diners: side_dict[m] = max(0, int(request.form.get(f"side_{m}") or 0))
                side_total = sum(side_dict.values())
            else:
                side_total = 0

            for m in diners: member_totals[m] = main_dict[m] + side_dict[m]

        cur = db_execute("""
          INSERT INTO meals(dt, entry_mode, main_mode, side_mode, main_total, side_total, grand_total, payer_name, guest_total)
          VALUES (?,?,?,?,?,?,?,?,?) RETURNING id;
        """, (dt, entry_mode, main_mode, side_mode, int(main_total), int(side_total), int(grand_total),
              payer_name, int(guest_total)))
        meal_id = cur.fetchone()["id"]

        member_sum = 0
        for m in diners:
            total = int(member_totals[m]); member_sum += total
            if entry_mode == "detailed":
                if main_mode == "equal":
                    m_main = split_even(int(main_total), len(diners))[diners.index(m)]
                else:
                    m_main = int(request.form.get(f"main_{m}") or 0)
                if side_mode == "equal":
                    m_side = split_even(int(side_total), len(diners))[diners.index(m)]
                elif side_mode == "custom":
                    m_side = int(request.form.get(f"side_{m}") or 0)
                else:
                    m_side = 0
            else:
                m_main, m_side = total, 0
            db_execute("INSERT INTO meal_parts(meal_id, name, main_amount, side_amount, total_amount) VALUES (?,?,?,?,?);",
                       (meal_id, m, int(m_main), int(m_side), int(total)))

        if payer_name and (payer_name in members) and member_sum > 0:
            cur2 = db_execute("INSERT INTO deposits(dt, name, amount, note) VALUES (?,?,?,?) RETURNING id;",
                              (dt, payer_name, int(member_sum), f"[자동정산] 식사 #{meal_id} 선결제 상환(게스트 제외)"))
            dep_id = cur2.fetchone()["id"]
            log_audit("insert", "deposits", dep_id, {"auto_for_meal": meal_id, "amount": member_sum, "payer": payer_name})

        get_db().commit()
        log_audit("insert", "meals", meal_id, {"dt":dt,"entry_mode":entry_mode,"main_mode":main_mode,"side_mode":side_mode,"grand_total":grand_total,"payer_name":payer_name,"guest_total":guest_total,"diners":diners})
        flash(f"식사 #{meal_id} 등록 완료.", "success")
        return redirect(url_for("meal_detail", meal_id=meal_id))

    body = _meal_form_html(members)
    return render(body)

@app.get("/meal/<int:meal_id>")
def meal_detail(meal_id):
    meal = db_execute("SELECT * FROM meals WHERE id=?;", (meal_id,)).fetchone()
    parts = db_execute("SELECT name, main_amount, side_amount, total_amount FROM meal_parts WHERE meal_id=? ORDER BY name;", (meal_id,)).fetchall()
    if not meal:
        flash("해당 식사 기록이 없습니다.", "danger"); return redirect(url_for("home"))
    rows = "".join([f"<tr><td>{p['name']}</td><td class='num'>{p['main_amount']:,}</td><td class='num'>{p['side_amount']:,}</td><td class='num'>{p['total_amount']:,}</td></tr>" for p in parts])
    member_sum = sum([p['total_amount'] for p in parts])
    payer_text = meal['payer_name'] if meal['payer_name'] else "(없음)"
    body = f"""
    <div class="card shadow-sm">
      <div class="card-body">
        <h5 class="card-title">식사 상세 #{meal_id}</h5>
        <p class="text-muted mb-2">
          날짜: {meal['dt']} |
          입력 방식: {meal['entry_mode']} |
          메인 모드: {meal['main_mode']} |
          사이드 모드: {meal['side_mode']} |
          메인 총액: {meal['main_total']:,}원 |
          사이드 총액: {meal['side_total']:,}원 |
          총 식비(팀원+게스트): {meal['grand_total']:,}원 |
          게스트 총액: {meal['guest_total']:,}원 |
          결제자: {payer_text}
        </p>
        <table class="table table-sm">
          <thead><tr><th>이름</th><th class='text-end'>메인</th><th class='text-end'>사이드</th><th class='text-end'>총 차감</th></tr></thead>
          <tbody>{rows}</tbody>
          <tfoot><tr><th colspan="3" class="text-end">팀원 차감 합계</th><th class="num">{member_sum:,}</th></tr></tfoot>
        </table>
        <div class="d-flex gap-2">
          <a class="btn btn-outline-secondary" href="{ url_for('meal') }">다른 식사 등록</a>
          <a class="btn btn-outline-primary" href="{ url_for('meal_edit', meal_id=meal_id) }">수정</a>
          <a class="btn btn-outline-dark" href="{ url_for('status') }">잔액 보기</a>
          <a class="btn btn-outline-danger" href="{ url_for('meal_delete', meal_id=meal_id) }" onclick="return confirm('삭제할까요? 자동정산 입금도 함께 제거됩니다.');">삭제</a>
        </div>
      </div>
    </div>
    """
    return render(body)

@app.route("/meal/<int:meal_id>/edit", methods=["GET","POST"])
def meal_edit(meal_id):
    members = get_members()
    meal = db_execute("SELECT * FROM meals WHERE id=?;", (meal_id,)).fetchone()
    if not meal:
        flash("해당 식사 기록이 없습니다.", "danger"); return redirect(url_for("meal"))

    if request.method == "POST":
        old_meal = dict(meal)
        dt = request.form.get("dt") or str(date.today())
        entry_mode = request.form.get("entry_mode") or "total"
        payer_name = request.form.get("payer_name") or None
        guest_total = int(request.form.get("guest_total") or 0)
        if guest_total < 0: guest_total = 0

        ate_flags = {m: (request.form.get(f"ate_{m}") == "on") for m in members}
        diners = [m for m in members if ate_flags.get(m)]
        if not diners:
            flash("식사한 팀원을 최소 1명 선택하세요.", "warning"); return redirect(url_for("meal_edit", meal_id=meal_id))

        member_totals = {m: 0 for m in diners}
        main_mode, side_mode = "custom", "none"
        main_total = side_total = grand_total = 0

        if entry_mode == "total":
            grand_total = int(request.form.get("grand_total") or 0)
            dist_mode = request.form.get("total_dist_mode") or "equal"
            member_sum_target = max(0, grand_total - guest_total)
            if dist_mode == "equal":
                shares = split_even(member_sum_target, len(diners))
                for i, m in enumerate(diners): member_totals[m] = shares[i]
            else:
                for m in diners: member_totals[m] = max(0, int(request.form.get(f"tot_{m}") or 0))
        else:
            main_mode = request.form.get("main_mode") or "custom"
            side_mode = request.form.get("side_mode") or "none"
            main_dict = {m:0 for m in diners}
            if main_mode == "equal":
                main_total = int(request.form.get("main_total") or 0)
                ms = split_even(main_total, len(diners))
                for i,m in enumerate(diners): main_dict[m] = ms[i]
            else:
                for m in diners: main_dict[m] = max(0, int(request.form.get(f"main_{m}") or 0))
            side_dict = {m:0 for m in diners}
            if side_mode == "equal":
                side_total = int(request.form.get("side_total") or 0)
                ss = split_even(side_total, len(diners))
                for i,m in enumerate(diners): side_dict[m] = ss[i]
            elif side_mode == "custom":
                for m in diners: side_dict[m] = max(0, int(request.form.get(f"side_{m}") or 0))
                side_total = sum(side_dict.values())
            else:
                side_total = 0
            for m in diners: member_totals[m] = main_dict[m] + side_dict[m]

        db_execute("""UPDATE meals SET dt=?, entry_mode=?, main_mode=?, side_mode=?, 
                      main_total=?, side_total=?, grand_total=?, payer_name=?, guest_total=? WHERE id=?;""",
                   (dt, entry_mode, main_mode, side_mode, int(main_total), int(side_total),
                    int(grand_total), payer_name, int(guest_total), meal_id))

        db_execute("DELETE FROM meal_parts WHERE meal_id=?;", (meal_id,))
        member_sum = 0
        for m in diners:
            total = int(member_totals[m]); member_sum += total
            if entry_mode == "detailed":
                if main_mode == "equal":
                    m_main = split_even(int(main_total), len(diners))[diners.index(m)]
                else:
                    m_main = int(request.form.get(f"main_{m}") or 0)
                if side_mode == "equal":
                    m_side = split_even(int(side_total), len(diners))[diners.index(m)]
                elif side_mode == "custom":
                    m_side = int(request.form.get(f"side_{m}") or 0)
                else:
                    m_side = 0
            else:
                m_main, m_side = total, 0
            db_execute("INSERT INTO meal_parts(meal_id, name, main_amount, side_amount, total_amount) VALUES (?,?,?,?,?);",
                       (meal_id, m, int(m_main), int(m_side), int(total)))

        delete_auto_deposit_for_meal(meal_id)
        if payer_name and (payer_name in members) and member_sum > 0:
            cur_dep = db_execute("INSERT INTO deposits(dt, name, amount, note) VALUES (?,?,?,?) RETURNING id;",
                       (dt, payer_name, int(member_sum), f"[자동정산] 식사 #{meal_id} 선결제 상환(게스트 제외)"))
            dep_id = cur_dep.fetchone()["id"]
            log_audit("insert", "deposits", dep_id, {"auto_for_meal": meal_id, "amount": member_sum, "payer": payer_name})

        get_db().commit()
        log_audit("update", "meals", meal_id, {"before": old_meal, "after": {"dt":dt,"entry_mode":entry_mode,"main_mode":main_mode,"side_mode":side_mode,"grand_total":grand_total,"payer_name":payer_name,"guest_total":guest_total,"diners":diners}})
        flash("수정되었습니다.", "success")
        return redirect(url_for("meal_detail", meal_id=meal_id))

    parts = db_execute("SELECT name, main_amount, side_amount, total_amount FROM meal_parts WHERE meal_id=? ORDER BY name;", (meal_id,)).fetchall()
    init = dict(meal)
    init["parts"] = parts
    body = _meal_form_html(members, initial=init, edit_target_id=meal_id)
    return render(body)

@app.get("/meal/<int:meal_id>/delete")
def meal_delete(meal_id):
    old_meal = db_execute("SELECT * FROM meals WHERE id=?;", (meal_id,)).fetchone()
    old_parts = db_execute("SELECT * FROM meal_parts WHERE meal_id=?;", (meal_id,)).fetchall()
    delete_auto_deposit_for_meal(meal_id)
    db_execute("DELETE FROM meal_parts WHERE meal_id=?;", (meal_id,))
    db_execute("DELETE FROM meals WHERE id=?;", (meal_id,))
    get_db().commit()
    log_audit("delete", "meals", meal_id, {"meal": old_meal, "parts": old_parts})
    flash("삭제되었습니다.", "info")
    return redirect(url_for("meal"))

# ------------------ 식사 기록 리스트 ------------------
@app.get("/meals")
def meals():
    rows = db_execute("""
        SELECT
          m.id,
          m.dt,
          m.payer_name,
          COALESCE(SUM(p.total_amount), 0) AS team_total,
          COALESCE(COUNT(p.id), 0) AS diners,
          m.guest_total
        FROM meals m
        LEFT JOIN meal_parts p ON p.meal_id = m.id
        GROUP BY m.id
        ORDER BY m.id DESC
        LIMIT 200;
    """).fetchall()

    items = "".join([
        f"<tr>"
        f"<td>#{r['id']}</td>"
        f"<td>{r['dt']}</td>"
        f"<td>{html_escape(r['payer_name'] or '(없음)')}</td>"
        f"<td class='num'>{r['diners']}</td>"
        f"<td class='num'>{r['team_total']:,}</td>"
        f"<td class='num'>{r['guest_total']:,}</td>"
        f"<td class='text-end'>"
        f"<a class='btn btn-sm btn-outline-secondary' href='{ url_for('meal_detail', meal_id=r['id']) }'>보기</a> "
        f"<a class='btn btn-sm btn-outline-primary' href='{ url_for('meal_edit', meal_id=r['id']) }'>수정</a> "
        f"<a class='btn btn-sm btn-outline-danger' href='{ url_for('meal_delete', meal_id=r['id']) }' onclick='return confirm(\"삭제할까요? 자동정산 입금도 함께 제거됩니다.\");'>삭제</a>"
        f"</td>"
        f"</tr>"
        for r in rows
    ])

    body = f"""
    <div class="card shadow-sm">
      <div class="card-body">
        <div class="d-flex justify-content-between align-items-center mb-2">
          <h5 class="card-title mb-0">식사 기록</h5>
          <div class="d-flex gap-2">
            <a class="btn btn-success btn-sm" href="{ url_for('meal') }">식사 등록</a>
            <a class="btn btn-outline-secondary btn-sm" href="{ url_for('home') }">메인으로</a>
          </div>
        </div>
        <div class="table-responsive">
          <table class="table table-sm align-middle">
            <thead>
              <tr>
                <th>ID</th>
                <th>날짜</th>
                <th>결제자</th>
                <th class="text-end">인원</th>
                <th class="text-end">팀원합계</th>
                <th class="text-end">게스트합계</th>
                <th class="text-end">관리</th>
              </tr>
            </thead>
            <tbody>{items}</tbody>
          </table>
        </div>
      </div>
    </div>
    """
    return render(body)

# ------------------ 현황/정산 + 엑셀 버튼 ------------------
@app.route("/status")
def status():
    balances = get_balances()
    total_deposit = sum(b["deposit"] for b in balances)
    total_used    = sum(b["used"]    for b in balances)
    total_balance = sum(b["balance"] for b in balances)

    rows = ""
    for b in balances:
        cls = "text-danger" if b["balance"] < 0 else ""
        rows += (
            f"<tr>"
            f"<td>{b['name']}</td>"
            f"<td class='num'>{b['deposit']:,}</td>"
            f"<td class='num'>{b['used']:,}</td>"
            f"<td class='num {cls}'>{b['balance']:,}</td>"
            f"</tr>"
        )

    body = f"""
    <div class="card shadow-sm">
      <div class="card-body">
        <div class="d-flex justify-content-between align-items-center mb-2">
          <h5 class="card-title mb-0">현황 / 정산</h5>
          <div class="d-flex gap-2">
            <a class="btn btn-sm btn-outline-success" href="{ url_for('export_excel') }">엑셀 내보내기</a>
          </div>
        </div>

        <div class="mb-2">
          <span class="badge bg-secondary me-1">입금 합계: {total_deposit:,}원</span>
          <span class="badge bg-secondary me-1">차감 합계: {total_used:,}원</span>
          <span class="badge bg-dark">잔액 합계: {total_balance:,}원</span>
        </div>

        <div class="table-responsive">
          <table class="table table-sm align-middle">
            <thead>
              <tr>
                <th>이름</th>
                <th class='text-end'>입금합계</th>
                <th class='text-end'>차감합계</th>
                <th class='text-end'>잔액</th>
              </tr>
            </thead>
            <tbody>{rows}</tbody>
            <tfoot>
              <tr class="fw-bold">
                <td class='text-end'>합계</td>
                <td class='num'>{total_deposit:,}</td>
                <td class='num'>{total_used:,}</td>
                <td class='num'>{total_balance:,}</td>
              </tr>
            </tfoot>
          </table>
        </div>
        <div class="text-muted small">
          * 잔액 합계는 실제 통장 잔액과 비교용입니다.
        </div>
      </div>
    </div>
    """
    return render(body)

# ------------------ 엑셀 내보내기 ------------------
@app.get("/export_excel")
def export_excel():
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "members"
    ws1.append(["name"])
    for r in db_execute("SELECT name FROM members ORDER BY name;").fetchall():
        ws1.append([r["name"]])

    def add_sheet(name, sql, cols):
        ws = wb.create_sheet(title=name)
        ws.append(cols)
        for row in db_execute(sql).fetchall():
            ws.append([row.get(c) for c in cols])

    add_sheet("deposits",
              "SELECT id,dt,name,amount,note FROM deposits ORDER BY id;",
              ["id","dt","name","amount","note"])
    add_sheet("meals",
              "SELECT id,dt,entry_mode,main_mode,side_mode,main_total,side_total,grand_total,payer_name,guest_total FROM meals ORDER BY id;",
              ["id","dt","entry_mode","main_mode","side_mode","main_total","side_total","grand_total","payer_name","guest_total"])
    add_sheet("meal_parts",
              "SELECT id,meal_id,name,main_amount,side_amount,total_amount FROM meal_parts ORDER BY id;",
              ["id","meal_id","name","main_amount","side_amount","total_amount"])
    add_sheet("notices",
              "SELECT id,dt,content FROM notices ORDER BY id;",
              ["id","dt","content"])
    add_sheet("audit_logs",
              "SELECT id,dt,action,target_table,target_id,payload FROM audit_logs ORDER BY id;",
              ["id","dt","action","target_table","target_id","payload"])
    add_sheet("games",
              "SELECT id,dt,game_type,rule,participants,winner,loser,extra FROM games ORDER BY id;",
              ["id","dt","game_type","rule","participants","winner","loser","extra"])
    add_sheet("hogu_stats",
              "SELECT name,losses FROM hogu_stats ORDER BY losses DESC, name;",
              ["name","losses"])

    buf = io.BytesIO()
    wb.save(buf); buf.seek(0)
    fname = f"lunch_book_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    return send_file(buf, as_attachment=True, download_name=fname, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ------------------ 호구게임 공통: 참가자 파싱 ------------------
def parse_players():
    members = get_members()
    selected = request.form.getlist("players")  # 멤버 선택
    guest_raw = (request.form.get("guests") or "").strip()
    guests = [x.strip() for x in guest_raw.split(",") if x.strip()] if guest_raw else []
    players = selected + guests
    players = [p for p in players if p]  # dedup 간단히 생략
    return players, members

# ------------------ 호구게임 대시보드 ------------------
@app.get("/games")
def games_home():
    ranks = db_execute("SELECT name, losses FROM hogu_stats ORDER BY losses DESC, name;").fetchall()
    rows = "".join([f"<tr><td>{i+1}</td><td>{html_escape(r['name'])}</td><td class='num'>{r['losses']}</td></tr>" for i,r in enumerate(ranks)])
    members = get_members()
    opts = "".join([f"<option value='{m}'>{m}</option>" for m in members])

    body = f"""
    <div class="card shadow-sm">
      <div class="card-body">
        <h5 class="card-title">호구순위</h5>
        <table class="table table-sm">
          <thead><tr><th>순위</th><th>이름</th><th class='text-end'>걸린 횟수</th></tr></thead>
          <tbody>{rows or "<tr><td colspan='3' class='text-center text-muted'>기록 없음</td></tr>"}</tbody>
        </table>
        <hr>
        <h6 class="mb-2">빠른 시작</h6>
        <div class="d-flex gap-2 flex-wrap">
          <a class="btn btn-outline-primary btn-sm" href="{ url_for('dice_game') }">주사위게임</a>
          <a class="btn btn-outline-success btn-sm" href="{ url_for('ladder_game') }">사다리게임</a>
          <a class="btn btn-outline-dark btn-sm" href="{ url_for('oddcard_game') }">외톨이 카드</a>
        </div>
      </div>
    </div>
    """
    return render(body)

# ------------------ 주사위 게임 ------------------
# 룰은 모두 "한 번씩 굴린 결과(사람별 1~3개 주사위 합/첫 눈)"만으로 판정 가능하게 구성
DICE_RULES = [
    # 1) 합 최대가 호구
    "합이 가장 큰 사람이 호구",
    # 2) 합 최소가 호구, 단 첫 눈이 1이면 면책(그 다음 최소가 호구)
    "최솟값이 호구 (1면책 규칙: 첫 눈이 1이면 면책하고 다음 최솟값이 호구)",
    # 3) 합이 10과 가장 먼 사람이 호구
    "합이 10과 가장 먼 사람이 호구",
    # 4) 인원이 3명이면 첫 눈의 '4에 가장 가까운 사람'이 호구, 아니면 합 최대
    "3명이면 첫 눈 4에 가장 가까운 사람이 호구 (아니면 합 최대)",
    # 5) 한 사람의 '가장 큰 눈' 기준 최대가 호구
    "가장 큰 눈 하나 기준 최대가 호구",
]

def _compute_loser_by_rule(rule_text: str, rolls_per_player, players):
    """
    rolls_per_player: [[d1,d2,...], [d1,d2,...], ...]
    players: [name, ...]
    return: (loser_index, extra_text)
    """
    n = len(players)
    sums = [sum(rs) for rs in rolls_per_player]
    firsts = [rs[0] if rs else 0 for rs in rolls_per_player]
    max_die = [max(rs) if rs else 0 for rs in rolls_per_player]

    loser_index = None
    extra = ""

    if "합이 가장 큰" in rule_text and "가장 먼" not in rule_text:
        # 1) 합 최대
        m = max(sums)
        loser_index = sums.index(m)
        extra = f"(합:{sums})"

    elif "최솟값이 호구" in rule_text:
        # 2) 최솟값 + 1면책
        if 1 in firsts:
            tmp = [(999 if x == 1 else x) for x in firsts]
            loser_index = tmp.index(min(tmp))
            extra = f"(첫 눈:{firsts}, 1면책)"
        else:
            loser_index = firsts.index(min(firsts))
            extra = f"(첫 눈:{firsts})"

    elif "합이 10과 가장 먼" in rule_text:
        # 3) |합-10| 최대
        scores = [abs(s-10) for s in sums]
        loser_index = scores.index(max(scores))
        extra = f"(합:{sums}, 점수:{scores})"

    elif "3명이면 첫 눈 4에 가장 가까운" in rule_text:
        # 4) 3명 -> 첫 눈이 4에 가장 가까운 사람 / 아니면 합 최대
        if n == 3:
            scores = [abs(x-4) for x in firsts]
            loser_index = scores.index(min(scores))
            extra = f"(첫 눈:{firsts})"
        else:
            m = max(sums)
            loser_index = sums.index(m)
            extra = f"(합:{sums})"

    elif "가장 큰 눈 하나 기준" in rule_text:
        # 5) 각 사람의 단일 최대 눈 비교
        m = max(max_die)
        loser_index = max_die.index(m)
        extra = f"(개별최대:{max_die})"

    else:
        # 백업: 합 최대
        m = max(sums)
        loser_index = sums.index(m)
        extra = f"(합:{sums})"

    return loser_index, extra


@app.route("/games/dice", methods=["GET","POST"])
def dice_game():
    members = get_members()

    # 최종 저장 단계 (클라이언트가 전체 굴림을 끝내고 POST로 결과를 보냄)
    final_payload = request.form.get("final_payload")
    if final_payload:
        try:
            payload = json.loads(final_payload)
            players = payload["players"]
            rolls_per_player = payload["rolls"]
            rule_text = payload["rule"]
            max_dice = int(payload.get("max_dice", 1))
        except Exception:
            flash("결과 데이터가 올바르지 않습니다.", "danger")
            return redirect(url_for("dice_game"))

        # 판정
        loser_index, extra = _compute_loser_by_rule(rule_text, rolls_per_player, players)
        loser = players[loser_index]

        # DB 기록
        upsert_hogu_loss(loser, 1)
        db_execute(
            "INSERT INTO games(dt, game_type, rule, participants, loser, extra) VALUES (?,?,?,?,?,?);",
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "dice",
                f"{rule_text} {extra}",
                json.dumps(players, ensure_ascii=False),
                loser,
                json.dumps({"rolls": rolls_per_player, "max_dice": max_dice}, ensure_ascii=False),
            ),
        )
        get_db().commit()

        # 결과 화면 렌더 (다시하기/게임 홈 버튼 제공)
        rows = ""
        for i, p in enumerate(players):
            eyes = rolls_per_player[i]
            rows += f"<tr{' class=\"table-danger\"' if i==loser_index else ''}><td>{html_escape(p)}</td><td class='num'>{' + '.join(map(str,eyes))} = <b>{sum(eyes)}</b></td></tr>"

        body = f"""
        <div class="card shadow-sm">
          <div class="card-body">
            <h5 class="card-title">🎉 결과</h5>
            <div class="mb-2 text-muted">룰: {html_escape(rule_text)} {html_escape(extra)}</div>
            <div class="table-responsive">
              <table class="table table-sm align-middle">
                <thead><tr><th>이름</th><th class='text-end'>주사위 합</th></tr></thead>
                <tbody>{rows}</tbody>
              </table>
            </div>
            <div class="alert alert-success"><b>호구:</b> {html_escape(loser)}</div>
            <div class="d-flex gap-2">
              <a class="btn btn-outline-secondary" href="{ url_for('games_home') }">게임 홈</a>
              <a class="btn btn-primary" href="{ url_for('dice_game') }">다시 하기</a>
            </div>
          </div>
        </div>
        """
        return render(body)

    # 게임 시작 폼 (GET)
    if request.method == "GET":
        opts = "".join([f"<option value='{m}'>{m}</option>" for m in members])
        body = f"""
        <div class="card shadow-sm"><div class="card-body">
          <h5 class="card-title">주사위 게임</h5>
          <form method="post">
            <div class="mb-2">
              <label class="form-label">플레이어(팀원 다중선택 가능)</label>
              <select class="form-select" name="players" multiple size="6">{opts}</select>
              <div class="form-text">모바일은 길게 눌러 다중선택. 게스트는 아래 칸에 입력.</div>
            </div>
            <div class="mb-2">
              <label class="form-label">게스트 (쉼표로 구분)</label>
              <input class="form-control" name="guests" placeholder="예: 홍길동, 김게스트">
            </div>
            <div class="mb-2">
              <label class="form-label">주사위 개수 (1~3개)</label>
              <input class="form-control" type="number" name="max_dice" value="3" min="1" max="3">
            </div>
            <button class="btn btn-primary">게임 시작</button>
            <a class="btn btn-outline-secondary" href="{ url_for('games_home') }">뒤로</a>
          </form>
        </div></div>
        """
        return render(body)

    # POST: 게임 세팅 → 턴 방식 화면 렌더
    players, _ = parse_players()
    if len(players) < 2:
        flash("2명 이상 선택하세요.", "warning")
        return redirect(url_for("dice_game"))

    max_dice = int(request.form.get("max_dice") or 1)
    max_dice = 1 if max_dice < 1 else 3 if max_dice > 3 else max_dice

    # 랜덤 룰 선택
    rule_text = random.choice(DICE_RULES)

    # 클라에서 턴별로 굴리고, 끝나면 결과를 서버로 다시 POST(final_payload)하여 저장
    DATA = json.dumps({"players": players, "rule": rule_text, "max_dice": max_dice}, ensure_ascii=False)

    body = f"""
    <div class="card shadow-sm">
      <div class="card-body">
        <h5 class="card-title">🎲 주사위 게임 - 턴 진행</h5>
        <div class="mb-2 text-muted">룰: {html_escape(rule_text)}</div>

        <div class="mb-2"><b>이번 순번:</b> <span id="turnName"></span></div>

        <div id="stage" class="mb-3"></div>

        <div class="d-flex gap-2 mb-3">
          <button id="rollBtn" class="btn btn-success">주사위 굴리기</button>
          <button id="skipBtn" class="btn btn-outline-secondary" type="button">건너뛰기</button>
        </div>

        <div class="card border-0 bg-light">
          <div class="card-body py-2">
            <div class="fw-bold mb-1">진행 결과</div>
            <ul id="resultList" class="mb-0"></ul>
          </div>
        </div>

        <form id="saveForm" method="post" class="d-none">
          <input type="hidden" name="final_payload" id="final_payload">
        </form>

        <style>
          .player-row {{ display:flex; align-items:center; gap:12px; margin-bottom:10px; }}
          .name-badge {{ min-width:88px; padding:.35rem .6rem; border-radius:.5rem; background:#f1f3f5; }}
          .dice-wrap {{ display:flex; gap:8px; flex-wrap:wrap; }}
          .die {{
            width:40px; height:40px; border-radius:10px; border:1px solid #ddd;
            display:inline-flex; align-items:center; justify-content:center;
            font-weight:700; font-size:18px; background:#fff;
            box-shadow: 0 1px 3px rgba(0,0,0,.06);
          }}
          .spin {{ animation: blink .3s linear infinite; }}
          @keyframes blink {{ 50% {{ opacity:.6; }} }}
          .done {{ opacity:.85; }}
        </style>

        <script>
          const DATA = {DATA};
          const stage = document.getElementById('stage');
          const resultList = document.getElementById('resultList');
          const turnName = document.getElementById('turnName');
          const rollBtn = document.getElementById('rollBtn');
          const skipBtn = document.getElementById('skipBtn');

          // 초기 UI - 모든 사람의 자리 만들기(물음표)
          DATA.players.forEach((p) => {{
            const row = document.createElement('div');
            row.className = 'player-row';
            row.innerHTML = `
              <span class="name-badge">${{p}}</span>
              <div class="dice-wrap">
                ${'{'}Array.from({{length: DATA.max_dice}}).map(()=>'<span class="die spin">?</span>').join(''){'}'}
              </div>
            `;
            stage.appendChild(row);
          }});

          const rows = Array.from(stage.querySelectorAll('.player-row'));
          let turn = 0;
          let results = []; // [[..], ..]
          updateTurn();

          // 애니메이션 속도/시간 (조금 더 천천히)
          const ANIM_INTERVAL = 180;   // 숫자 바뀌는 템포(밀리초) - 80→180으로 느리게
          const ANIM_DURATION = 2000;  // 총 굴리는 시간(ms) - 2초

          rollBtn.addEventListener('click', doRoll);
          skipBtn.addEventListener('click', () => {{
            // 스킵: 전부 0 처리
            results.push(Array(DATA.max_dice).fill(0));
            appendResultLine(DATA.players[turn], Array(DATA.max_dice).fill(0));
            markDoneRow(turn, Array(DATA.max_dice).fill(0));
            nextTurn();
          }});

          function updateTurn() {{
            turnName.textContent = DATA.players[turn];
          }}

          function doRoll() {{
            rollBtn.disabled = true;
            skipBtn.disabled = true;

            const row = rows[turn];
            const diceEls = Array.from(row.querySelectorAll('.die'));

            // 애니메이션(의미 없는 랜덤 숫자)
            const timer = setInterval(() => {{
              diceEls.forEach(el => el.textContent = 1 + Math.floor(Math.random()*6));
            }}, ANIM_INTERVAL);

            setTimeout(() => {{
              clearInterval(timer);

              // 실제 결과 생성
              const eyes = Array.from({{length: DATA.max_dice}}, () => 1 + Math.floor(Math.random()*6));
              diceEls.forEach((el, i) => {{ el.classList.remove('spin'); el.textContent = eyes[i]; }});
              markDoneRow(turn, eyes);
              results.push(eyes);
              appendResultLine(DATA.players[turn], eyes);

              nextTurn();
            }}, ANIM_DURATION);
          }}

          function markDoneRow(idx, eyes) {{
            rows[idx].classList.add('done');
          }}

          function appendResultLine(name, eyes) {{
            const li = document.createElement('li');
            li.innerHTML = `${'{'}name{'}'} : ${'{'}eyes.join(' + '){'}'} = <b>${'{'}eyes.reduce((a,b)=>a+b,0){'}'}</b>`;
            resultList.appendChild(li);
          }}

          function nextTurn() {{
            turn++;
            if (turn >= DATA.players.length) {{
              finishGame();
            }} else {{
              rollBtn.disabled = false;
              skipBtn.disabled = false;
              updateTurn();
            }}
          }}

          function finishGame() {{
            // 서버로 저장(최종 렌더는 서버가 해줌)
            const payload = {{
              players: DATA.players,
              rolls: results,
              rule: DATA.rule,
              max_dice: DATA.max_dice
            }};
            document.getElementById('final_payload').value = JSON.stringify(payload);
            document.getElementById('saveForm').submit();
          }}
        </script>
      </div>
    </div>
    """
    return render(body)

# ------------------ 사다리 게임 (애니메이션/SVG) ------------------
@app.route("/games/ladder", methods=["GET","POST"])
def ladder_game():
    members = get_members()

    # 최종 저장 단계
    final_payload = request.form.get("final_payload")
    if final_payload:
        try:
            payload = json.loads(final_payload)
            players = payload["players"]
            levels = int(payload["levels"])
            rungs = payload["rungs"]  # [{level:int, col:int}]  (col ~ col+1 연결)
            end_positions = payload["end_positions"]  # 각 시작열이 최종 어디로 도착했는지
        except Exception:
            flash("결과 데이터가 올바르지 않습니다.", "danger")
            return redirect(url_for("ladder_game"))

        # 맨 오른쪽(가장 큰 인덱스) 도착자가 호구
        loser_index = end_positions.index(max(end_positions))
        loser = players[loser_index]

        rule_text = "사다리 랜덤 매칭 (가장 오른쪽 도착자가 호구)"
        upsert_hogu_loss(loser, 1)
        db_execute(
            "INSERT INTO games(dt, game_type, rule, participants, loser, extra) VALUES (?,?,?,?,?,?)",
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "ladder",
                rule_text,
                json.dumps(players, ensure_ascii=False),
                loser,
                json.dumps({"levels": levels, "rungs": rungs, "end_positions": end_positions}, ensure_ascii=False),
            ),
        )
        get_db().commit()

        # 표 렌더
        rows = ""
        for i, p in enumerate(players):
            rows += f"<tr{' class=\"table-danger\"' if i==loser_index else ''}><td>{html_escape(p)}</td><td class='num'>{end_positions[i]+1}번 줄</td></tr>"

        body = f"""
        <div class="card shadow-sm">
          <div class="card-body">
            <h5 class="card-title">🎉 결과</h5>
            <div class="mb-2 text-muted">{html_escape(rule_text)}</div>
            <div class="table-responsive">
              <table class="table table-sm align-middle">
                <thead><tr><th>이름</th><th class='text-end'>도착 위치</th></tr></thead>
                <tbody>{rows}</tbody>
              </table>
            </div>
            <div class="alert alert-success"><b>호구:</b> {html_escape(loser)}</div>
            <div class="d-flex gap-2">
              <a class="btn btn-outline-secondary" href="{ url_for('games_home') }">게임 홈</a>
              <a class="btn btn-primary" href="{ url_for('ladder_game') }">다시 하기</a>
            </div>
          </div>
        </div>
        """
        return render(body)

    # GET: 선택 폼
    if request.method == "GET":
        opts = "".join([f"<option value='{m}'>{m}</option>" for m in members])
        body = f"""
        <div class="card shadow-sm"><div class="card-body">
          <h5 class="card-title">사다리 게임</h5>
          <form method="post">
            <div class="mb-2">
              <label class="form-label">플레이어(다중선택)</label>
              <select class="form-select" name="players" multiple size="6">{opts}</select>
            </div>
            <button class="btn btn-primary">게임 시작</button>
            <a class="btn btn-outline-secondary" href="{ url_for('games_home') }">뒤로</a>
          </form>
        </div></div>
        """
        return render(body)

    # POST: 세팅 → 사다리 생성 화면
    players, _ = parse_players()
    if len(players) < 2:
        flash("2명 이상 선택하세요.", "warning")
        return redirect(url_for("ladder_game"))

    n = len(players)            # 세로줄 개수
    levels = max(8, n * 3)      # 가로줄 레벨 수
    prob = 0.28                 # 한 레벨에서 가로줄이 생길 확률

    # 가로줄 생성 (인접/겹침 방지)
    rungs = []  # [{level, col}]  (col <-> col+1 연결)
    last_at_level = {lv: -10 for lv in range(levels)}
    for lv in range(levels):
        for col in range(n - 1):
            # 같은 레벨에서 서로 바로 붙은 가로줄 방지
            if last_at_level[lv] == col - 1:
                continue
            if random.random() < prob:
                rungs.append({"level": lv, "col": col})
                last_at_level[lv] = col

    # 도착 위치 계산(서버 쪽에서도 검증)
    def simulate_end_positions(n, levels, rungs):
        # 빠른 조회를 위해 set/dict
        rung_map = {(r["level"], r["col"]) for r in rungs}
        ends = []
        for start in range(n):
            pos = start
            for lv in range(levels):
                if (lv, pos) in rung_map:
                    pos += 1
                elif (lv, pos - 1) in rung_map:
                    pos -= 1
            ends.append(pos)
        return ends

    end_positions = simulate_end_positions(n, levels, rungs)

    DATA = json.dumps(
        {"players": players, "levels": levels, "rungs": rungs, "end_positions": end_positions},
        ensure_ascii=False,
    )

    # 렌더: SVG 사다리 + 애니메이션
    body = f"""
    <div class="card shadow-sm">
      <div class="card-body">
        <h5 class="card-title">🪜 사다리 진행</h5>
        <div class="mb-2"><b>이번 순번:</b> <span id="turnName"></span></div>

        <div class="mb-3" style="overflow:auto">
          <svg id="ladder" width="100%" height="560"></svg>
        </div>

        <div class="d-flex gap-2 mb-3">
          <button id="startBtn" class="btn btn-success">애니메이션 시작</button>
        </div>

        <div class="card border-0 bg-light">
          <div class="card-body py-2">
            <div class="fw-bold mb-1">진행 결과</div>
            <ul id="resultList" class="mb-0"></ul>
          </div>
        </div>

        <form id="saveForm" method="post" class="d-none">
          <input type="hidden" name="final_payload" id="final_payload">
        </form>

        <style>
          .name-badge {{ font-size:.9rem; fill:#666; }}
          .token {{ transition: transform .12s linear; }}
          .done {{ opacity:.85; }}
          .loser {{ fill:#dc3545; }}
        </style>

        <script>
          const DATA = {DATA};
          const svg = document.getElementById('ladder');
          const resultList = document.getElementById('resultList');
          const turnName = document.getElementById('turnName');
          const startBtn = document.getElementById('startBtn');

          const N = DATA.players.length;
          const LEVELS = DATA.levels;
          const WIDTH = Math.max(640, 90 * N);
          const HEIGHT = 540;
          svg.setAttribute('viewBox', `0 0 ${WIDTH} ${HEIGHT}`);

          // 레이아웃
          const PADX = 60;            // 좌우 여백
          const PADY = 40;            // 상하 여백
          const COL_W = (WIDTH - PADX*2) / (N - 1);
          const STEP_H = (HEIGHT - PADY*2) / (LEVELS + 1);

          // 배경 그리기
          const g = document.createElementNS("http://www.w3.org/2000/svg","g");
          svg.appendChild(g);

          // 세로줄
          for (let i=0;i<N;i++) {{
            const x = PADX + i*COL_W;
            const line = document.createElementNS(svg.namespaceURI,'line');
            line.setAttribute('x1', x); line.setAttribute('y1', PADY);
            line.setAttribute('x2', x); line.setAttribute('y2', HEIGHT-PADY);
            line.setAttribute('stroke', '#999'); line.setAttribute('stroke-width', 2);
            g.appendChild(line);

            // 시작 라벨
            const label = document.createElementNS(svg.namespaceURI,'text');
            label.setAttribute('x', x); label.setAttribute('y', PADY-12);
            label.setAttribute('text-anchor','middle');
            label.setAttribute('class','name-badge');
            label.textContent = DATA.players[i];
            g.appendChild(label);
          }}

          // 가로줄
          DATA.rungs.forEach((r) => {{
            const y = PADY + (r.level+1)*STEP_H;
            const x1 = PADX + r.col*COL_W;
            const x2 = PADX + (r.col+1)*COL_W;
            const rung = document.createElementNS(svg.namespaceURI,'line');
            rung.setAttribute('x1', x1); rung.setAttribute('y1', y);
            rung.setAttribute('x2', x2); rung.setAttribute('y2', y);
            rung.setAttribute('stroke', '#666'); rung.setAttribute('stroke-width', 3);
            g.appendChild(rung);
          }});

          // 토큰 만들기 (원)
          const tokens = [];
          for (let i=0;i<N;i++) {{
            const x = PADX + i*COL_W;
            const y = PADY - 8;
            const c = document.createElementNS(svg.namespaceURI,'circle');
            c.setAttribute('r', 8);
            c.setAttribute('cx', 0); c.setAttribute('cy', 0);
            c.setAttribute('class', 'token');
            c.setAttribute('transform', `translate(${x}, ${y})`);
            c.setAttribute('fill', '#0d6efd');
            svg.appendChild(c);
            tokens.push(c);
          }}

          // 빠른 조회용 셋
          const rungSet = new Set(DATA.rungs.map(r => `${{r.level}}:${{r.col}}`));

          // 애니메이션 파라미터 (조금 천천히)
          const V_STEP = 120;   // 수직 이동 소요(ms)
          const H_STEP = 120;   // 수평 이동 소요(ms)

          function sleep(ms) {{ return new Promise(res => setTimeout(res, ms)); }}

          async function animateOne(idx) {{
            let col = idx;
            let x = PADX + col*COL_W;
            let y = PADY - 8;

            turnName.textContent = DATA.players[idx];

            for (let lv=0; lv<LEVELS; lv++) {{
              // 수직으로 다음 레벨까지 이동
              const ny = PADY + (lv+1)*STEP_H;
              await moveTo(tokens[idx], x, y, x, ny, V_STEP);
              y = ny;

              // 가로줄 확인: 오른쪽으로 연결?
              if (rungSet.has(`${{lv}}:${{col}}`)) {{
                const nx = PADX + (col+1)*COL_W;
                await moveTo(tokens[idx], x, y, nx, y, H_STEP);
                col = col + 1; x = nx;
              }} else if (rungSet.has(`${{lv}}:${{col-1}}`)) {{
                const nx = PADX + (col-1)*COL_W;
                await moveTo(tokens[idx], x, y, nx, y, H_STEP);
                col = col - 1; x = nx;
              }}
            }}

            tokens[idx].classList.add('done');

            // 결과 누적 표시
            const li = document.createElement('li');
            li.innerHTML = `${{DATA.players[idx]}} → <b>${{col+1}}번 줄</b>`;
            resultList.appendChild(li);

            return col; // 도착열
          }}

          function moveTo(el, x1,y1, x2,y2, dur) {{
            return new Promise(res => {{
              const steps = 8;
              let k = 0;
              const dx = (x2 - x1)/steps;
              const dy = (y2 - y1)/steps;
              const t = setInterval(() => {{
                k++;
                const nx = x1 + dx*k;
                const ny = y1 + dy*k;
                el.setAttribute('transform', `translate(${nx}, ${ny})`);
                if (k >= steps) {{ clearInterval(t); res(); }}
              }}, Math.max(16, Math.floor(dur/steps)));
            }});
          }}

          async function runAll() {{
            startBtn.disabled = true;
            const ends = [];
            for (let i=0;i<N;i++) {{
              const dest = await animateOne(i);
              ends.push(dest);
            }}

            // 호구 계산: 맨 오른쪽 도착자
            const maxPos = Math.max(...ends);
            const loserIdx = ends.indexOf(maxPos);
            tokens[loserIdx].classList.add('loser');

            // 서버로 저장
            const payload = {{
              players: DATA.players,
              levels: DATA.levels,
              rungs: DATA.rungs,
              end_positions: ends
            }};
            document.getElementById('final_payload').value = JSON.stringify(payload);
            document.getElementById('saveForm').submit();
          }}

          startBtn.addEventListener('click', runAll);
          turnName.textContent = DATA.players[0];
        </script>
      </div>
    </div>
    """
    return render(body)

# ------------------ 외톨이 카드(페어+1, 조커 5:5) ------------------
@app.route("/games/oddcard", methods=["GET","POST"])
def oddcard_game():
    members = get_members()
    if request.method == "POST":
        players, _ = parse_players()
        if len(players) < 3 or (len(players) % 2 == 0):
            flash("홀수 인원 3명 이상이어야 합니다.", "warning"); return redirect(url_for("oddcard_game"))

        n = len(players)
        # 카드 구성: 페어*[(n-1)/2] + 외톨이 1
        pairs = (n - 1) // 2
        cards = [f"페어{i+1}" for i in range(pairs) for _ in (0,1)] + ["외톨이"]
        random.shuffle(cards)
        assignment = dict(zip(players, cards))

        # 조커 1명 부여(확률 1/n): 조커 효과 50% 좋음(무효/다시), 50% 나쁨(빵 추가)
        joker_person = None
        joker_effect = None
        if random.random() < (1.0 / n):
            joker_person = random.choice(players)
            joker_effect = random.choice(["good","bad"])

        loser = None
        info = {"assignment": assignment, "joker_person": joker_person, "joker_effect": joker_effect}

        if joker_person and joker_effect == "good":
            # 무효 & 재뽑기
            info["note"] = "조커(좋음): 무효 처리, 재뽑기"
            # 재뽑기 간단히 외톨이를 다시 랜덤 선정
            lonely = random.choice(players)
            loser = lonely
        else:
            # 기본: '외톨이'를 뽑은 사람이 호구
            for p, card in assignment.items():
                if card == "외톨이":
                    loser = p
                    break
            if joker_person and joker_effect == "bad":
                info["note"] = "조커(나쁨): 빵까지!"
                # 벌칙을 조금 강화하는 의미로 losses 2회 카운트
                upsert_hogu_loss(loser, 2)
            else:
                upsert_hogu_loss(loser, 1)

        db_execute("INSERT INTO games(dt, game_type, rule, participants, loser, extra) VALUES (?,?,?,?,?,?);",
                   (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "oddcard",
                    "페어+외톨이, 조커 5:5 (좋음:무효/재뽑기, 나쁨:추가벌칙)",
                    json.dumps(players,ensure_ascii=False), loser, json.dumps(info,ensure_ascii=False)))
        get_db().commit()

        msg = f"배정: {', '.join([f'{k}:{v}' for k,v in assignment.items()])}"
        if joker_person:
            msg += f"<br>조커: {joker_person} ({'좋음' if joker_effect=='good' else '나쁨'})"
        msg += f"<br><b>호구: {loser}</b>"
        flash(msg, "success")
        return redirect(url_for("games_home"))

    opts = "".join([f"<option value='{m}'>{m}</option>" for m in members])
    body = f"""
    <div class="card shadow-sm"><div class="card-body">
      <h5 class="card-title">외톨이 카드</h5>
      <p class="text-muted">홀수 인원만 참여 가능. 2-2 페어 + 1 외톨이(호구). 가끔 조커가 등장(좋음/나쁨 5:5).</p>
      <form method="post">
        <div class="mb-2">
          <label class="form-label">플레이어</label>
          <select class="form-select" name="players" multiple size="6">{opts}</select>
        </div>
        <div class="mb-2">
          <label class="form-label">게스트 (쉼표로 구분)</label>
          <input class="form-control" name="guests" placeholder="예: 홍길동, 김게스트">
        </div>
        <button class="btn btn-primary">게임 시작</button>
        <a class="btn btn-outline-secondary" href="{ url_for('games_home') }">뒤로</a>
      </form>
    </div></div>
    """
    return render(body)

# ------------------ 앱 실행 ------------------
if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(host="0.0.0.0", port=8000)
