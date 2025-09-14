from flask import Flask, request, redirect, url_for, render_template_string, g, session
from datetime import date, datetime
import os

# Postgres 연결
import psycopg2
import psycopg2.extras

DB_URL = os.environ.get("DATABASE_URL")  # Render 환경변수에 설정한 값 사용

def get_db():
    conn = getattr(g, "_db_conn", None)
    if conn is None:
        if not DB_URL:
            raise RuntimeError("DATABASE_URL not set")
        conn = g._db_conn = psycopg2.connect(DB_URL, sslmode="require")
    return conn

def db_execute(sql: str, params=()):
    sql = sql.replace("?", "%s")
    cur = get_db().cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql, params)
    return cur

@app.teardown_appcontext
def close_db(_exc):
    conn = getattr(g, "_db_conn", None)
    if conn is not None:
        conn.close()

# ===== 설정 =====
APP_PASSWORD = os.environ.get("APP_PASSWORD", "7467")  # Replit Secrets 권장
DB = "lunchfund.db"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me")   # Replit Secrets 권장

# ------------------ DB ------------------
def get_db():
    db = getattr(g, "_db", None)
    if db is None:
        db = g._db = sqlite3.connect(DB)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON;")
    return db

@app.teardown_appcontext
def close_db(_exc):
    db = getattr(g, "_db", None)
    if db is not None:
        db.close()

def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""CREATE TABLE IF NOT EXISTS members(
        name TEXT PRIMARY KEY
    );""")

    cur.execute("""CREATE TABLE IF NOT EXISTS deposits(
        id SERIAL PRIMARY KEY,
        dt TEXT NOT NULL,
        name TEXT NOT NULL,
        amount INTEGER NOT NULL,
        note TEXT DEFAULT '',
        CONSTRAINT fk_dep_member FOREIGN KEY(name) REFERENCES members(name) ON DELETE CASCADE
    );""")

    cur.execute("""CREATE TABLE IF NOT EXISTS meals(
        id SERIAL PRIMARY KEY,
        dt TEXT NOT NULL,
        entry_mode TEXT NOT NULL DEFAULT 'total',
        main_mode TEXT NOT NULL DEFAULT 'custom',
        side_mode TEXT NOT NULL DEFAULT 'none',
        main_total INTEGER NOT NULL DEFAULT 0,
        side_total INTEGER NOT NULL DEFAULT 0,
        grand_total INTEGER NOT NULL DEFAULT 0,
        payer_name TEXT,
        guest_total INTEGER NOT NULL DEFAULT 0,
        CONSTRAINT fk_meal_payer FOREIGN KEY(payer_name) REFERENCES members(name) ON DELETE SET NULL
    );""")

    cur.execute("""CREATE TABLE IF NOT EXISTS meal_parts(
        id SERIAL PRIMARY KEY,
        meal_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        main_amount INTEGER NOT NULL DEFAULT 0,
        side_amount INTEGER NOT NULL DEFAULT 0,
        total_amount INTEGER NOT NULL DEFAULT 0,
        CONSTRAINT fk_mp_meal FOREIGN KEY(meal_id) REFERENCES meals(id) ON DELETE CASCADE,
        CONSTRAINT fk_mp_member FOREIGN KEY(name) REFERENCES members(name) ON DELETE CASCADE
    );""")

    cur.execute("""CREATE TABLE IF NOT EXISTS notices(
        id SERIAL PRIMARY KEY,
        dt TEXT NOT NULL,
        content TEXT NOT NULL
    );""")

    conn.commit()

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
    for r in cur.fetchall():
        dep_map[r["name"]] = r["s"] or 0

    use_map = {m: 0 for m in members}
    cur = db_execute("SELECT name, SUM(total_amount) AS s FROM meal_parts GROUP BY name;")
    for r in cur.fetchall():
        use_map[r["name"]] = r["s"] or 0

    return [{
        "name": m,
        "deposit": dep_map.get(m, 0),
        "used": use_map.get(m, 0),
        "balance": dep_map.get(m, 0) - use_map.get(m, 0)
    } for m in members]
    
def get_balance_of(name):
    cur = db_execute("SELECT COALESCE(SUM(amount),0) AS s FROM deposits WHERE name=?;", (name,))
    dep = (cur.fetchone() or {}).get("s", 0)

    cur = db_execute("SELECT COALESCE(SUM(total_amount),0) AS s FROM meal_parts WHERE name=?;", (name,))
    used = (cur.fetchone() or {}).get("s", 0)

    return dep - used

def get_meal_counts_map():
    cur = db_execute("SELECT name, COUNT(*) AS c FROM meal_parts GROUP BY name;")
    return {r["name"]: (r["c"] or 0) for r in cur.fetchall()}

def html_escape(s):
    if s is None: return ""
    return str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def delete_auto_deposit_for_meal(meal_id):
    db = get_db()
    db.execute("DELETE FROM deposits WHERE note LIKE ?", (("%식사 #%d 선결제 상환%%" % meal_id),))
    db.commit()

# ------------------ 로그인 보호 ------------------
@app.before_request
def require_login():
    # DB 준비
    if not hasattr(g, "_db"):
        init_db()
    # 로그인 면제 경로
    allowed = ('/login', '/favicon.ico', '/ping')
    if request.path not in allowed and not session.get('authed'):
        return redirect(url_for('login'))

# ------------------ TEMPLATE ------------------
BASE = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>점심 과비 관리</title>
  <link href="https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/cosmo/bootstrap.min.css" rel="stylesheet">
  <style>
    body { padding-bottom: 40px; }
    .num { text-align: right; }
    .table-sm td, .table-sm th { padding:.45rem; }
    ul.compact li { margin-bottom: .25rem; }
    .form-text { font-size: .85rem; }
  </style>
</head>
<body class="bg-light">
<nav class="navbar navbar-expand-lg bg-primary mb-3" data-bs-theme="dark">
  <div class="container-fluid">
    <a class="navbar-brand fw-bold" href="{{ url_for('home') }}">🍱 점심 과비 관리</a>
    <ul class="navbar-nav me-auto">
      <li class="nav-item"><a class="nav-link" href="{{ url_for('deposit') }}">입금 등록</a></li>
      <li class="nav-item"><a class="nav-link" href="{{ url_for('meal') }}">식사 등록</a></li>
      <li class="nav-item"><a class="nav-link" href="{{ url_for('status') }}">현황/정산</a></li>
      <li class="nav-item"><a class="nav-link" href="{{ url_for('notices') }}">공지사항</a></li>
      <li class="nav-item"><a class="nav-link" href="{{ url_for('settings') }}">팀원설정</a></li>
    </ul>
    <div class="d-flex">
      <a class="btn btn-sm btn-outline-light" href="{{ url_for('logout') }}">로그아웃</a>
    </div>
  </div>
</nav>

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

# ------------------ 로그인 ------------------
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

# ------------------ 핑 (깨우기용) ------------------
@app.get("/ping")
def ping():
    return "OK", 200

# ------------------ HOME ------------------
@app.route("/")
def home():
    members = get_members()

    # 잔액 마이너스 공지
    notice_html = ""
    if members:
        negatives = [b for b in get_balances() if b["balance"] < 0]
        if negatives:
            items = "".join(["<li><strong>%s</strong> : <span class='text-danger'>%s원</span></li>" % (b['name'], "{:,}".format(b['balance'])) for b in negatives])
            notice_html = """
            <div class="alert alert-warning shadow-sm" role="alert">
              <div class="d-flex align-items-center mb-1">
                <span class="me-2">🔔</span>
                <strong>공지:</strong>&nbsp;잔액이 마이너스인 인원이 있습니다.
              </div>
              <ul class="mb-0">%s</ul>
            </div>""" % items

    # 관리자 공지(최신 5개)
    notices_html = ""
    db = get_db()
    nrows = db.execute("SELECT dt, content FROM notices ORDER BY id DESC LIMIT 5;").fetchall()
    if nrows:
        lis = "".join(["<li><span class='text-muted me-2'>[%s]</span>%s</li>" % (r['dt'], html_escape(r['content'])) for r in nrows])
        notices_html = """
        <div class="alert alert-info shadow-sm">
          <div class="fw-bold mb-1">📌 공지사항</div>
          <ul class="mb-0">%s</ul>
        </div>""" % lis

    if not members:
        # 초기 설정
        input_rows = "".join([
            """
            <div class="col-12 col-md-6 col-lg-4">
              <input class="form-control" name="m%d" placeholder="이름 %d">
            </div>
            """ % (i, i+1) for i in range(10)
        ])
        body = """
        %s
        %s
        <div class="card shadow-sm">
          <div class="card-body">
            <h5 class="card-title">첫 실행: 팀원 등록</h5>
            <form method="post" action="%s">
              <div class="row g-2">
                %s
              </div>
              <div class="mt-3 d-flex gap-2">
                <button class="btn btn-primary">저장</button>
              </div>
            </form>
          </div>
        </div>
        """ % (notice_html, notices_html, url_for('quick_setup'), input_rows)
    else:
        balances_map = {b["name"]: b["balance"] for b in get_balances()}
        counts_map = get_meal_counts_map()
        member_items = "".join([
            "<li class='d-flex justify-content-between'><span>%s</span><span class='text-muted'>잔액 %s원 · 식사 %d회</span></li>"
            % (n, "{:,}".format(balances_map.get(n,0)), counts_map.get(n,0))
            for n in members
        ])
        body = """
        %s
        %s
        <div class="row g-3">
          <div class="col-12 col-lg-6">
            <div class="card shadow-sm">
              <div class="card-body">
                <h5 class="card-title mb-2">빠른 작업</h5>
                <div class="d-grid gap-2">
                  <a class="btn btn-outline-primary" href="%s">입금 등록</a>
                  <a class="btn btn-outline-success" href="%s">식사 등록</a>
                  <a class="btn btn-outline-dark" href="%s">현황/정산</a>
                </div>
              </div>
            </div>
          </div>
          <div class="col-12 col-lg-6">
            <div class="card shadow-sm">
              <div class="card-body">
                <h5 class="card-title">등록된 팀원 (총 %d명)</h5>
                <ul class="mb-0 compact">
                  %s
                </ul>
                <div class="mt-3">
                  <a class="btn btn-sm btn-secondary" href="%s">팀원설정</a>
                </div>
              </div>
            </div>
          </div>
        </div>
        """ % (notice_html, notices_html, url_for('deposit'), url_for('meal'), url_for('status'), len(members), member_items, url_for('settings'))
    return render(body)

@app.post("/quick-setup")
def quick_setup():
    names = []
    for i in range(10):
        v = (request.form.get("m%d" % i) or "").strip()
        if v: names.append(v)
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM members;")
    for n in names:
        cur.execute("INSERT OR IGNORE INTO members(name) VALUES (?);", (n,))
    db.commit()
    return redirect(url_for("home"))

# ------------------ 공지사항 ------------------
@app.route("/notices", methods=["GET", "POST"])
def notices():
    db = get_db()
    if request.method == "POST":
        content = (request.form.get("content") or "").strip()
        if content:
            db.execute("INSERT INTO notices(dt, content) VALUES (?, ?);", (datetime.now().strftime("%Y-%m-%d %H:%M"), content))
            db.commit(); flash("공지사항이 등록되었습니다.", "success")
        else:
            flash("내용을 입력하세요.", "warning")
        return redirect(url_for("notices"))
    rows = db.execute("SELECT id, dt, content FROM notices ORDER BY id DESC LIMIT 100;").fetchall()
    items = "".join([
        "<tr><td>#%d</td><td>%s</td><td>%s</td><td><form method='post' action='%s' onsubmit=\"return confirm('삭제할까요?');\"><input type='hidden' name='id' value='%d'><button class='btn btn-sm btn-outline-danger'>삭제</button></form></td></tr>"
        % (r['id'], r['dt'], html_escape(r['content']), url_for('notice_delete'), r['id'])
        for r in rows
    ])
    body = """
    <div class="row g-3">
      <div class="col-12 col-lg-5">
        <div class="card shadow-sm">
          <div class="card-body">
            <h5 class="card-title">공지 등록</h5>
            <form method="post">
              <textarea class="form-control" name="content" rows="4" placeholder="공지 내용을 입력하세요"></textarea>
              <div class="mt-2 d-flex gap-2">
                <button class="btn btn-primary">등록</button>
                <a class="btn btn-outline-secondary" href="%s">메인으로</a>
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
                <tbody>%s</tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </div>
    """ % (url_for('home'), items)
    return render(body)

@app.post("/notice/delete")
def notice_delete():
    db = get_db()
    nid = int(request.form.get("id") or 0)
    if nid:
        db.execute("DELETE FROM notices WHERE id=?;", (nid,))
        db.commit(); flash("삭제되었습니다.", "info")
    return redirect(url_for("notices"))

# ------------------ 팀원설정 ------------------
@app.route("/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        new_name = (request.form.get("new_name") or "").strip()
        if new_name:
            db = get_db()
            try:
                db.execute("INSERT INTO members(name) VALUES (?)", (new_name,))
                db.commit(); flash("팀원 <b>%s</b> 추가 완료." % html_escape(new_name), "success")
            except sqlite3.IntegrityError:
                flash("이미 존재하는 이름입니다.", "warning")
        return redirect(url_for('settings'))

    members = get_members()
    rows = ""
    for nm in members:
        bal = get_balance_of(nm)
        bal_html = "{:,}".format(bal)
        badge = "<span class='badge bg-danger'>잔액 %s원</span>" % bal_html if bal != 0 else "<span class='badge bg-success'>잔액 0원</span>"
        rows += """
        <tr>
          <td>%s</td>
          <td class="num">%s</td>
          <td>%s</td>
          <td>
            <form method="post" action="%s" onsubmit="return confirmDelete('%s', %d);">
              <input type="hidden" name="name" value="%s">
              <button class="btn btn-sm btn-outline-danger">삭제</button>
            </form>
          </td>
        </tr>""" % (nm, bal_html, badge, url_for('member_delete'), nm, bal, nm)
    body = """
    <div class="row g-3">
      <div class="col-12 col-lg-7">
        <div class="card shadow-sm">
          <div class="card-body">
            <h5 class="card-title">팀원설정</h5>
            <p class="text-muted">입력된 사람만 사용됩니다. 인원 수 제한 없음.</p>
            <div class="table-responsive">
              <table class="table table-sm align-middle">
                <thead><tr><th>이름</th><th class='text-end'>잔액</th><th>상태</th><th>관리</th></tr></thead>
                <tbody>%s</tbody>
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
              <div class="mb-2">
                <input class="form-control" name="new_name" placeholder="새 팀원 이름">
              </div>
              <button class="btn btn-primary">추가</button>
              <a class="btn btn-outline-secondary" href="%s">뒤로</a>
            </form>
          </div>
        </div>
      </div>
    </div>

    <script>
      function confirmDelete(name, balance) {
        if (balance !== 0) {
          return confirm("⚠️ 잔액 " + balance.toLocaleString() + "원이 남아있습니다.\\n삭제하면 관련 입금/사용 기록도 함께 삭제됩니다. 계속할까요?");
        }
        return confirm("'" + name + "' 팀원을 삭제하시겠습니까?");
      }
    </script>
    """ % (rows, url_for('home'))
    return render(body)

@app.post("/member/delete")
def member_delete():
    nm = (request.form.get("name") or "").strip()
    if not nm: return redirect(url_for('settings'))
    bal = get_balance_of(nm)
    if bal != 0:
        flash("잔액이 0원이 아닌 팀원은 삭제할 수 없습니다. (현재: %s원)" % "{:,}".format(bal), "warning")
        return redirect(url_for('settings'))
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM meal_parts WHERE name=?;", (nm,))
    cur.execute("DELETE FROM deposits WHERE name=?;", (nm,))
    cur.execute("DELETE FROM members WHERE name=?;", (nm,))
    db.commit()
    flash("<b>%s</b> 삭제 완료." % html_escape(nm), "success")
    return redirect(url_for('settings'))

# ------------------ 입금: 등록/목록/수정/삭제 ------------------
@app.route("/deposit", methods=["GET", "POST"])
def deposit():
    db = get_db()
    members = get_members()
    if request.method == "POST":
        dt = request.form.get("dt") or str(date.today())
        name = request.form.get("name")
        amount = int(request.form.get("amount") or 0)
        note = (request.form.get("note") or "").strip()
        if name and amount > 0:
            db.execute("INSERT INTO deposits(dt, name, amount, note) VALUES (?,?,?,?);", (dt, name, amount, note))
            db.commit(); flash("입금 등록 완료.", "success")
        else:
            flash("이름과 금액을 확인하세요.", "warning")
        return redirect(url_for("deposit"))

    rows = db.execute("SELECT id, dt, name, amount, note FROM deposits ORDER BY id DESC LIMIT 100;").fetchall()
    hist = "".join([
        "<tr><td>%s</td><td>%s</td><td class='num'>%s</td><td>%s</td><td class='text-end'><a class='btn btn-sm btn-outline-primary' href='%s'>수정</a> <a class='btn btn-sm btn-outline-danger' href='%s' onclick='return confirm(\"삭제할까요?\");'>삭제</a></td></tr>"
        % (r['dt'], r['name'], "{:,}".format(r['amount']), html_escape(r['note'] or ''), url_for('deposit_edit', dep_id=r['id']), url_for('deposit_delete', dep_id=r['id']))
        for r in rows
    ])
    opts = "".join(["<option value='%s'>%s</option>" % (n, n) for n in members])
    body = """
    <div class="row g-3">
      <div class="col-12 col-lg-5">
        <div class="card shadow-sm">
          <div class="card-body">
            <h5 class="card-title">입금 등록</h5>
            <form method="post">
              <div class="mb-2">
                <label class="form-label">날짜</label>
                <input class="form-control" type="date" name="dt" value="%s">
              </div>
              <div class="mb-2">
                <label class="form-label">이름</label>
                <select class="form-select" name="name">%s</select>
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
              <tbody>%s</tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
    """ % (str(date.today()), opts, hist)
    return render(body)

@app.get("/deposit/<int:dep_id>/edit")
def deposit_edit(dep_id):
    db = get_db()
    r = db.execute("SELECT * FROM deposits WHERE id=?;", (dep_id,)).fetchone()
    if not r:
        flash("입금 내역이 없습니다.", "danger"); return redirect(url_for("deposit"))
    members = get_members()
    opts = "".join(["<option value='%s'%s>%s</option>" % (n, " selected" if n==r["name"] else "", n) for n in members])
    body = """
    <div class="card shadow-sm">
      <div class="card-body">
        <h5 class="card-title">입금 수정 #%d</h5>
        <form method="post" action="%s">
          <div class="row g-2">
            <div class="col-12 col-md-3">
              <label class="form-label">날짜</label>
              <input class="form-control" type="date" name="dt" value="%s">
            </div>
            <div class="col-12 col-md-3">
              <label class="form-label">이름</label>
              <select class="form-select" name="name">%s</select>
            </div>
            <div class="col-12 col-md-3">
              <label class="form-label">금액(원)</label>
              <input class="form-control num" type="number" name="amount" min="0" step="1" value="%d">
            </div>
            <div class="col-12 col-md-3">
              <label class="form-label">메모</label>
              <input class="form-control" name="note" value="%s">
            </div>
          </div>
          <div class="mt-3 d-flex gap-2">
            <button class="btn btn-primary">저장</button>
            <a class="btn btn-outline-secondary" href="%s">취소</a>
          </div>
        </form>
      </div>
    </div>
    """ % (dep_id, url_for('deposit_update', dep_id=dep_id), r['dt'], opts, r['amount'], html_escape(r['note'] or ''), url_for('deposit'))
    return render(body)

@app.post("/deposit/<int:dep_id>/edit")
def deposit_update(dep_id):
    db = get_db()
    dt = request.form.get("dt") or str(date.today())
    name = request.form.get("name")
    amount = int(request.form.get("amount") or 0)
    note = (request.form.get("note") or "").strip()
    if name and amount >= 0:
        db.execute("UPDATE deposits SET dt=?, name=?, amount=?, note=? WHERE id=?;", (dt, name, amount, note, dep_id))
        db.commit(); flash("수정되었습니다.", "success")
    else:
        flash("입력값을 확인하세요.", "warning")
    return redirect(url_for("deposit"))

@app.get("/deposit/<int:dep_id>/delete")
def deposit_delete(dep_id):
    db = get_db()
    db.execute("DELETE FROM deposits WHERE id=?;", (dep_id,))
    db.commit(); flash("삭제되었습니다.", "info")
    return redirect(url_for("deposit"))

# ------------------ 식사: 등록/상세/삭제 ------------------
@app.route("/meal", methods=["GET", "POST"])
def meal():
    db = get_db()
    members = get_members()

    if request.method == "POST":
        dt = request.form.get("dt") or str(date.today())
        entry_mode = request.form.get("entry_mode") or "total"
        payer_name = request.form.get("payer_name") or ""
        guest_total = int(request.form.get("guest_total") or 0)
        if guest_total < 0: guest_total = 0

        ate_flags = {m: (request.form.get("ate_%s" % m) == "on") for m in members}
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
                    member_totals[m] = max(0, int(request.form.get("tot_%s" % m) or 0))
        else:
            main_mode = request.form.get("main_mode") or "custom"
            side_mode = request.form.get("side_mode") or "none"

            main_dict = {m:0 for m in diners}
            if main_mode == "equal":
                main_total = int(request.form.get("main_total") or 0)
                ms = split_even(main_total, len(diners))
                for i,m in enumerate(diners): main_dict[m] = ms[i]
            else:
                for m in diners: main_dict[m] = max(0, int(request.form.get("main_%s" % m) or 0))

            side_dict = {m:0 for m in diners}
            if side_mode == "equal":
                side_total = int(request.form.get("side_total") or 0)
                ss = split_even(side_total, len(diners))
                for i,m in enumerate(diners): side_dict[m] = ss[i]
            elif side_mode == "custom":
                for m in diners: side_dict[m] = max(0, int(request.form.get("side_%s" % m) or 0))
                side_total = sum(side_dict.values())
            else:
                side_total = 0

            for m in diners: member_totals[m] = main_dict[m] + side_dict[m]

        # 저장: meals
        cur = db.cursor()
        cur.execute("""
            INSERT INTO meals(dt, entry_mode, main_mode, side_mode, main_total, side_total, grand_total, payer_name, guest_total)
            VALUES (?,?,?,?,?,?,?,?,?);""",
            (dt, entry_mode, main_mode, side_mode, int(main_total), int(side_total), int(grand_total),
             payer_name if payer_name else None, int(guest_total)))
        meal_id = cur.lastrowid

        # 저장: meal_parts
        member_sum = 0
        for m in diners:
            total = int(member_totals[m])
            member_sum += total
            if entry_mode == "detailed":
                if main_mode == "equal":
                    m_main = split_even(int(main_total), len(diners))[diners.index(m)]
                else:
                    m_main = int(request.form.get("main_%s" % m) or 0)
                if side_mode == "equal":
                    m_side = split_even(int(side_total), len(diners))[diners.index(m)]
                elif side_mode == "custom":
                    m_side = int(request.form.get("side_%s" % m) or 0)
                else:
                    m_side = 0
            else:
                m_main, m_side = total, 0
            cur.execute("INSERT INTO meal_parts(meal_id, name, main_amount, side_amount, total_amount) VALUES (?,?,?,?,?);",
                        (meal_id, m, int(m_main), int(m_side), int(total)))

        # 자동정산 입금(선결제자 상환)
        if payer_name and (payer_name in members) and member_sum > 0:
            cur.execute("INSERT INTO deposits(dt, name, amount, note) VALUES (?,?,?,?);",
                        (dt, payer_name, int(member_sum), "[자동정산] 식사 #%d 선결제 상환(게스트 제외)" % meal_id))
        db.commit()
        flash("식사 #%d 등록 완료." % meal_id, "success")
        return redirect(url_for("meal_detail", meal_id=meal_id))

    # GET: 입력 폼
    rows_totalcustom = ""
    rows_detailed = ""
    for m in members:
        rows_totalcustom += """
        <tr>
          <td><input class="form-check-input" type="checkbox" name="ate_%s"></td>
          <td>%s</td>
          <td><input class="form-control form-control-sm num total-custom-cell" type="number" name="tot_%s" min="0" step="1" value="0" disabled></td>
        </tr>""" % (m, m, m)
        rows_detailed += """
        <tr>
          <td><input class="form-check-input" type="checkbox" name="ate_%s"></td>
          <td>%s</td>
          <td><input class="form-control form-control-sm num main-custom-cell" type="number" name="main_%s" min="0" step="1" value="0"></td>
          <td class="side-custom-cell"><input class="form-control form-control-sm num" type="number" name="side_%s" min="0" step="1" value="0" disabled></td>
        </tr>""" % (m, m, m, m)
    payer_options = "<option value=''></option>" + "".join(["<option value='%s'>%s</option>" % (n, n) for n in members])

    body = """
    <div class="card shadow-sm">
      <div class="card-body">
        <h5 class="card-title">식사 등록</h5>
        <form method="post" id="mealForm">
          <div class="row g-3">
            <div class="col-12 col-md-3">
              <label class="form-label">날짜</label>
              <input class="form-control" type="date" name="dt" value="%s">
            </div>
            <div class="col-12 col-md-4">
              <label class="form-label d-block">입력 방식</label>
              <div class="d-flex gap-3">
                <div class="form-check">
                  <input class="form-check-input" type="radio" name="entry_mode" id="em_total" value="total" checked>
                  <label class="form-check-label" for="em_total">총액 기반</label>
                </div>
                <div class="form-check">
                  <input class="form-check-input" type="radio" name="entry_mode" id="em_detailed" value="detailed">
                  <label class="form-check-label" for="em_detailed">상세(메인/사이드)</label>
                </div>
              </div>
            </div>
            <div class="col-12 col-md-5">
              <label class="form-label">결제자(선결제자)</label>
              <select class="form-select" name="payer_name">%s</select>
              <div class="form-text">결제자가 팀원일 때만, 팀원 몫 합계가 자동 입금(정산)으로 반영됩니다.</div>
            </div>

            <!-- 총액 기반 -->
            <div class="col-12" id="totalBox">
              <div class="row g-2">
                <div class="col-12 col-md-3">
                  <label class="form-label">총 식비(팀원+게스트)</label>
                  <input class="form-control num" type="number" name="grand_total" min="0" step="1" value="0">
                </div>
                <div class="col-12 col-md-3">
                  <label class="form-label">게스트 총액</label>
                  <input class="form-control num" type="number" name="guest_total" min="0" step="1" value="0">
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
                  <thead><tr><th>식사</th><th>이름</th><th>사람별 총액(강제입력 모드일 때)</th></tr></thead>
                  <tbody>%s</tbody>
                </table>
              </div>
            </div>

            <!-- 상세 -->
            <div class="col-12" id="detailedBox" style="display:none">
              <div class="row g-2">
                <div class="col-12 col-md-6">
                  <label class="form-label d-block">메인 분할 방식</label>
                  <div class="d-flex gap-3 align-items-center flex-wrap">
                    <div class="form-check">
                      <input class="form-check-input" type="radio" name="main_mode" id="mm_custom" value="custom" checked>
                      <label class="form-check-label" for="mm_custom">강제입력(사람별)</label>
                    </div>
                    <div class="form-check">
                      <input class="form-check-input" type="radio" name="main_mode" id="mm_equal" value="equal">
                      <label class="form-check-label" for="mm_equal">균등분할</label>
                    </div>
                    <div class="ms-3" id="mainTotalWrap">
                      <label class="form-label mb-0 me-1">메인 총액</label>
                      <input class="form-control form-control-sm num d-inline-block" style="width:140px" type="number" name="main_total" min="0" step="1" value="0">
                    </div>
                  </div>
                </div>
                <div class="col-12 col-md-6">
                  <label class="form-label d-block">사이드 분할 방식</label>
                  <div class="d-flex gap-3 align-items-center flex-wrap">
                    <div class="form-check">
                      <input class="form-check-input" type="radio" name="side_mode" id="sm_equal" value="equal">
                      <label class="form-check-label" for="sm_equal">균등분할</label>
                    </div>
                    <div class="form-check">
                      <input class="form-check-input" type="radio" name="side_mode" id="sm_custom" value="custom">
                      <label class="form-check-label" for="sm_custom">강제입력(사람별)</label>
                    </div>
                    <div class="form-check">
                      <input class="form-check-input" type="radio" name="side_mode" id="sm_none" value="none" checked>
                      <label class="form-check-label" for="sm_none">없음</label>
                    </div>
                    <div class="ms-3" id="sideTotalWrap">
                      <label class="form-label mb-0 me-1">공통 사이드 총액</label>
                      <input class="form-control form-control-sm num d-inline-block" style="width:140px" type="number" name="side_total" min="0" step="1" value="0">
                    </div>
                  </div>
                </div>
                <div class="col-12">
                  <label class="form-label">게스트(명단 외) 총액</label>
                  <input class="form-control num" type="number" name="guest_total" min="0" step="1" value="0" placeholder="예: 20000">
                  <div class="form-text">게스트 금액은 정산에서 제외(기록만).</div>
                </div>
              </div>
              <div class="table-responsive mt-3">
                <table class="table table-sm align-middle">
                  <thead><tr><th>식사</th><th>이름</th><th>메인(강제입력 모드일 때)</th><th>사이드(강제입력 모드일 때)</th></tr></thead>
                  <tbody>%s</tbody>
                </table>
              </div>
            </div>
          </div>

          <div class="mt-2 d-flex gap-2">
            <button class="btn btn-success">저장</button>
            <a class="btn btn-outline-secondary" href="%s">뒤로</a>
          </div>
        </form>
      </div>
    </div>

    <script>
      // 입력 방식 토글
      const emTotal = document.getElementById('em_total');
      const emDetailed = document.getElementById('em_detailed');
      const totalBox = document.getElementById('totalBox');
      const detailedBox = document.getElementById('detailedBox');
      const totalCustomInputs = document.querySelectorAll('.total-custom-cell');
      function refreshEntryMode() {
        if (emTotal.checked) { totalBox.style.display='block'; detailedBox.style.display='none'; }
        else { totalBox.style.display='none'; detailedBox.style.display='block'; }
        refreshTotalMode(); refreshMainMode(); refreshSideMode();
      }
      [emTotal, emDetailed].forEach(r => r.addEventListener('change', refreshEntryMode));

      // 총액 모드 토글
      const tdEqual = document.getElementById('td_equal');
      const tdCustom = document.getElementById('td_custom');
      function refreshTotalMode() {
        const customOn = tdCustom && tdCustom.checked && emTotal.checked;
        totalCustomInputs.forEach(inp => { inp.disabled = !customOn; if(!customOn) inp.value = 0; });
      }
      if (tdEqual && tdCustom) { [tdEqual, tdCustom].forEach(r => r.addEventListener('change', refreshTotalMode)); }

      // 상세: 메인 토글
      const mmCustom = document.getElementById('mm_custom');
      const mmEqual  = document.getElementById('mm_equal');
      const mainTotalWrap = document.getElementById('mainTotalWrap');
      const customMainInputs = document.querySelectorAll('.main-custom-cell');
      function refreshMainMode() {
        const show = mmEqual && mmEqual.checked && emDetailed.checked;
        if (mainTotalWrap) { mainTotalWrap.style.display = show ? 'inline-block' : 'none'; }
        customMainInputs.forEach(inp => {
          const dis = mmEqual && mmEqual.checked && emDetailed.checked;
          inp.disabled = dis; if(dis) inp.value = 0;
        });
      }
      if (mmCustom && mmEqual) { [mmCustom, mmEqual].forEach(r => r.addEventListener('change', refreshMainMode)); }

      // 상세: 사이드 토글
      const smEqual = document.getElementById('sm_equal');
      const smCustom = document.getElementById('sm_custom');
      const smNone  = document.getElementById('sm_none');
      const sideTotalWrap = document.getElementById('sideTotalWrap');
      const customSideInputs = document.querySelectorAll('.side-custom-cell input');
      function refreshSideMode() {
        if (!emDetailed.checked) {
          if (sideTotalWrap) sideTotalWrap.style.display = 'none';
          customSideInputs.forEach(inp => { inp.disabled = true; inp.value = 0; });
          return;
        }
        if (smEqual && smEqual.checked) {
          if (sideTotalWrap) sideTotalWrap.style.display = 'inline-block';
          customSideInputs.forEach(inp => { inp.disabled = true; inp.value = 0; });
        } else if (smCustom && smCustom.checked) {
          if (sideTotalWrap) sideTotalWrap.style.display = 'none';
          customSideInputs.forEach(inp => { inp.disabled = false; });
        } else {
          if (sideTotalWrap) sideTotalWrap.style.display = 'none';
          customSideInputs.forEach(inp => { inp.disabled = true; inp.value = 0; });
        }
      }
      if (smEqual && smCustom && smNone) { [smEqual, smCustom, smNone].forEach(r => r.addEventListener('change', refreshSideMode)); }

      // 초기 상태
      refreshEntryMode();
    </script>
    """ % (str(date.today()), payer_options, rows_totalcustom, rows_detailed, url_for('home'))
    return render(body)

@app.get("/meal/<int:meal_id>")
def meal_detail(meal_id):
    db = get_db()
    meal = db.execute("SELECT * FROM meals WHERE id=?;", (meal_id,)).fetchone()
    parts = db.execute("SELECT name, main_amount, side_amount, total_amount FROM meal_parts WHERE meal_id=? ORDER BY name;", (meal_id,)).fetchall()
    if not meal:
        flash("해당 식사 기록이 없습니다.", "danger"); return redirect(url_for("home"))
    rows = "".join(["<tr><td>%s</td><td class='num'>%s</td><td class='num'>%s</td><td class='num'>%s</td></tr>" %
                    (p['name'], "{:,}".format(p['main_amount']), "{:,}".format(p['side_amount']), "{:,}".format(p['total_amount'])) for p in parts])
    member_sum = sum([p['total_amount'] for p in parts])
    payer_text = meal['payer_name'] if meal['payer_name'] else "(없음)"
    body = """
    <div class="card shadow-sm">
      <div class="card-body">
        <h5 class="card-title">식사 상세 #%d</h5>
        <p class="text-muted mb-2">
          날짜: %s |
          입력 방식: %s |
          메인 모드: %s |
          사이드 모드: %s |
          메인 총액: %s원 |
          사이드 총액: %s원 |
          총 식비(팀원+게스트): %s원 |
          게스트 총액: %s원 |
          결제자: %s
        </p>
        <table class="table table-sm">
          <thead><tr><th>이름</th><th class='text-end'>메인</th><th class='text-end'>사이드</th><th class='text-end'>총 차감</th></tr></thead>
          <tbody>%s</tbody>
          <tfoot><tr><th colspan="3" class="text-end">팀원 차감 합계</th><th class="num">%s</th></tr></tfoot>
        </table>
        <div class="d-flex gap-2">
          <a class="btn btn-outline-secondary" href="%s">다른 식사 등록</a>
          <a class="btn btn-outline-dark" href="%s">잔액 보기</a>
          <a class="btn btn-outline-danger" href="%s" onclick="return confirm('삭제할까요? 자동정산 입금도 함께 제거됩니다.');">삭제</a>
        </div>
      </div>
    </div>
    """ % (meal_id, meal['dt'], meal['entry_mode'], meal['main_mode'], meal['side_mode'],
           "{:,}".format(meal['main_total']), "{:,}".format(meal['side_total']), "{:,}".format(meal['grand_total']),
           "{:,}".format(meal['guest_total']), payer_text, rows, "{:,}".format(member_sum),
           url_for('meal'), url_for('status'), url_for('meal_delete', meal_id=meal_id))
    return render(body)

@app.get("/meal/<int:meal_id>/delete")
def meal_delete(meal_id):
    db = get_db()
    delete_auto_deposit_for_meal(meal_id)
    db.execute("DELETE FROM meals WHERE id=?;", (meal_id,))
    db.commit()
    flash("식사 기록이 삭제되었습니다.", "info")
    return redirect(url_for("status"))

# ------------------ 현황 ------------------
@app.route("/status")
def status():
    db = get_db()
    members = get_members()

    dep_map = {m: 0 for m in members}
    for r in db.execute("SELECT name, SUM(amount) s FROM deposits GROUP BY name;"): dep_map[r["name"]] = r["s"] or 0
    use_map = {m: 0 for m in members}
    for r in db.execute("SELECT name, SUM(total_amount) s FROM meal_parts GROUP BY name;"): use_map[r["name"]] = r["s"] or 0

    trs = ""
    for m in members:
        dep = dep_map.get(m, 0); use = use_map.get(m, 0); bal = dep - use
        cls = "text-danger" if bal < 0 else "text-primary"
        trs += "<tr><td>%s</td><td class='num'>%s</td><td class='num'>%s</td><td class='num %s'>%s</td></tr>" % (m, "{:,}".format(dep), "{:,}".format(use), cls, "{:,}".format(bal))

    meals = db.execute("""
      SELECT id, dt, entry_mode, side_mode, side_total, main_mode, main_total, grand_total, guest_total, payer_name
      FROM meals ORDER BY id DESC LIMIT 30;""").fetchall()
    meal_rows = "".join([
        "<tr><td><a href='%s'>#%d</a></td><td>%s</td><td>%s/%s/%s</td><td class='num'>%s</td><td class='num'>%s</td><td class='num'>%s</td><td class='num'>%s</td><td>%s</td></tr>"
        % (url_for('meal_detail', meal_id=m['id']), m['id'], m['dt'], m['entry_mode'], m['main_mode'], m['side_mode'],
           "{:,}".format(m['main_total']), "{:,}".format(m['side_total']), "{:,}".format(m['grand_total']), "{:,}".format(m['guest_total']), (m['payer_name'] or ''))
        for m in meals
    ])

    body = """
    <div class="row g-3">
      <div class="col-12 col-lg-6">
        <div class="card shadow-sm">
          <div class="card-body">
            <h5 class="card-title">사람별 정산 현황</h5>
            <table class="table table-sm">
              <thead><tr><th>이름</th><th class='text-end'>입금합계</th><th class='text-end'>차감합계</th><th class='text-end'>잔액</th></tr></thead>
              <tbody>%s</tbody>
            </table>
          </div>
        </div>
      </div>
      <div class="col-12 col-lg-6">
        <div class="card shadow-sm">
          <div class="card-body">
            <h5 class="card-title">최근 식사 기록</h5>
            <table class="table table-sm">
              <thead><tr>
                <th>ID</th><th>날짜</th><th>모드</th>
                <th class='text-end'>메인총액</th><th class='text-end'>사이드총액</th>
                <th class='text-end'>총식비</th><th class='text-end'>게스트</th><th>결제자</th>
              </tr></thead>
              <tbody>%s</tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
    """ % (trs, meal_rows)
    return render(body)

# ------------------ 앱 시작 ------------------
if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(host="0.0.0.0", port=8000)
