from flask import Flask, request, redirect, url_for, render_template_string, g, session, flash
from datetime import date, datetime
import os
import psycopg2
import psycopg2.extras

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

    get_db().commit()

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
    # 자동정산 입금 제거 (해당 식사 id 표기 포함한 메모 기반)
    db_execute("DELETE FROM deposits WHERE note LIKE ?;", (f"%식사 #{meal_id} 선결제 상환%",))
    get_db().commit()

# ------------------ 로그인 보호 & 초기화 ------------------
@app.before_request
def require_login():
    if request.path not in ("/login", "/favicon.ico", "/ping"):
        if not session.get("authed"):
            return redirect(url_for("login"))

# ------------------ 템플릿 ------------------
# 상단바 컬러: #00854A (R0 G133 B74), 텍스트 흰색
BASE = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>점심 과비 관리</title>
  <link href="https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/cosmo/bootstrap.min.css" rel="stylesheet">
  <style>
    :root {
      --brand-green: #00854A;
    }
    body { padding-bottom: 40px; }
    .num { text-align: right; }
    .table-sm td, .table-sm th { padding:.45rem; }
    ul.compact li { margin-bottom: .25rem; }
    .form-text { font-size: .85rem; }

    /* 2행 상단 바 */
    header.topbar { background: var(--brand-green); color:#fff; }
    header.topbar a, header.topbar .nav-link { color:#fff !important; }
    header.topbar .nav-link:hover { opacity:.9; }

    /* 홈 현황판(블랙 배경, 흰 글씨) */
    .card.dashboard {
      background:#000;
      color:#fff;
    }
    .card.dashboard .table thead th,
    .card.dashboard .table td,
    .card.dashboard .table th { color:#fff; border-color:#444; }
  </style>
</head>
<body class="bg-light">

<header class="topbar mb-3">
  <!-- 1행: 타이틀 + 로그아웃 -->
  <div class="container py-2 d-flex justify-content-between align-items-center">
    <a class="navbar-brand fw-bold text-white m-0" href="{{ url_for('home') }}">🍱 점심 과비 관리</a>
    <a class="btn btn-sm btn-outline-light" href="{{ url_for('logout') }}">로그아웃</a>
  </div>
  <!-- 2행: 메뉴 -->
  <div class="container pb-2">
    <ul class="nav nav-pills">
      <li class="nav-item"><a class="nav-link" href="{{ url_for('deposit') }}">입금 등록</a></li>
      <li class="nav-item"><a class="nav-link" href="{{ url_for('meal') }}">식사 등록</a></li>
      <li class="nav-item"><a class="nav-link" href="{{ url_for('status') }}">현황/정산</a></li>
      <li class="nav-item"><a class="nav-link" href="{{ url_for('notices') }}">공지사항</a></li>
      <li class="nav-item"><a class="nav-link" href="{{ url_for('settings') }}">팀원설정</a></li>
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

    # 관리자 공지 5개
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

    if not members:
        # 초기 세팅 (최대 10칸 제공, 빈 칸 무시)
        input_rows = "".join([
            f"""
            <div class="col-12 col-md-6 col-lg-4">
              <input class="form-control" name="m{i}" placeholder="이름 {i+1}">
            </div>""" for i in range(10)
        ])
        body = f"""
        {notice_html}
        {notices_html}
        <div class="card shadow-sm">
          <div class="card-body">
            <h5 class="card-title">첫 실행: 팀원 등록</h5>
            <form method="post" action="{ url_for('quick_setup') }">
              <div class="row g-2">{input_rows}</div>
              <div class="mt-3 d-flex gap-2">
                <button class="btn btn-primary">저장</button>
              </div>
            </form>
          </div>
        </div>
        """
    else:
        balances_map = {b["name"]: b["balance"] for b in get_balances()}
        counts_map = get_meal_counts_map()
        member_items = "".join([
            f"<li class='d-flex justify-content-between'><span>{n}</span>"
            f"<span class='text-muted'>잔액 {balances_map.get(n,0):,}원 · 식사 {counts_map.get(n,0)}회</span></li>"
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
        db_execute("DELETE FROM notices WHERE id=?;", (nid,))
        get_db().commit()
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
          return confirm("⚠️ 잔액 " + balance.toLocaleString() + "원이 남아있습니다.\n삭제하면 관련 입금/사용 기록도 함께 삭제됩니다. 계속할까요?");
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
    # 외래키 CASCADE 로 연쇄 삭제
    db_execute("DELETE FROM members WHERE name=?;", (nm,))
    get_db().commit()
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
            db_execute("INSERT INTO deposits(dt, name, amount, note) VALUES (?,?,?,?);", (dt, name, amount, note))
            get_db().commit()
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
    dt = request.form.get("dt") or str(date.today())
    name = request.form.get("name")
    amount = int(request.form.get("amount") or 0)
    note = (request.form.get("note") or "").strip()
    if name and amount >= 0:
        db_execute("UPDATE deposits SET dt=?, name=?, amount=?, note=? WHERE id=?;", (dt, name, amount, note, dep_id))
        get_db().commit()
        flash("수정되었습니다.", "success")
    else:
        flash("입력값을 확인하세요.", "warning")
    return redirect(url_for("deposit"))

@app.get("/deposit/<int:dep_id>/delete")
def deposit_delete(dep_id):
    db_execute("DELETE FROM deposits WHERE id=?;", (dep_id,))
    get_db().commit()
    flash("삭제되었습니다.", "info")
    return redirect(url_for("deposit"))

# ------------------ 식사: 등록/수정/상세/삭제 ------------------
def _meal_form_html(members, initial=None, edit_target_id=None):
    """
    members: 팀원 목록
    initial: dict or None - 폼 초기값 (edit 시)
    edit_target_id: 편집 대상 meal_id (edit 시)
    """
    # 초기값 세팅
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

    # 개별 값 (edit 시에만 의미 있음)
    parts_map = {(p["name"]): p for p in (initial or {}).get("parts", [])}
    ate_set = set(parts_map.keys())

    # 사람별 행
    rows_totalcustom = ""
    rows_detailed = ""
    for m in members:
        # total 모드 강제입력 값
        tot_val = parts_map.get(m, {}).get("total_amount", 0)
        # detailed 모드 값
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

    # 라디오 체크 상태
    em_total_ck = "checked" if entry_mode=="total" else ""
    em_detail_ck = "checked" if entry_mode=="detailed" else ""
    mm_custom_ck = "checked" if main_mode=="custom" else ""
    mm_equal_ck  = "checked" if main_mode=="equal" else ""
    sm_equal_ck  = "checked" if side_mode=="equal" else ""
    sm_custom_ck = "checked" if side_mode=="custom" else ""
    sm_none_ck   = "checked" if side_mode=="none" else ""

    action_url = url_for('meal') if edit_target_id is None else url_for('meal_edit', meal_id=edit_target_id)

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

            <!-- 총액 기반 -->
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

            <!-- 상세 -->
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
      // 입력 방식 토글
      const emTotal = document.getElementById('em_total');
      const emDetailed = document.getElementById('em_detailed');
      const totalBox = document.getElementById('totalBox');
      const detailedBox = document.getElementById('detailedBox');
      const totalCustomInputs = document.querySelectorAll('.total-custom-cell');
      function refreshEntryMode() {{
        if (emTotal.checked) {{ totalBox.style.display='block'; detailedBox.style.display='none'; }}
        else {{ totalBox.style.display='none'; detailedBox.style.display='block'; }}
        refreshTotalMode(); refreshMainMode(); refreshSideMode();
      }}
      [emTotal, emDetailed].forEach(r => r.addEventListener('change', refreshEntryMode));

      // 총액 모드 토글
      const tdEqual = document.getElementById('td_equal');
      const tdCustom = document.getElementById('td_custom');
      function refreshTotalMode() {{
        const customOn = tdCustom && tdCustom.checked && emTotal.checked;
        totalCustomInputs.forEach(inp => {{ inp.disabled = !customOn; if(!customOn) inp.value = inp.value || 0; }});
      }}
      if (tdEqual && tdCustom) {{ [tdEqual, tdCustom].forEach(r => r.addEventListener('change', refreshTotalMode)); }}

      // 상세: 메인 토글
      const mmCustom = document.getElementById('mm_custom');
      const mmEqual  = document.getElementById('mm_equal');
      const mainTotalWrap = document.getElementById('mainTotalWrap');
      const customMainInputs = document.querySelectorAll('.main-custom-cell');
      function refreshMainMode() {{
        const show = mmEqual && mmEqual.checked && emDetailed.checked;
        if (mainTotalWrap) {{ mainTotalWrap.style.display = show ? 'inline-block' : 'none'; }}
        customMainInputs.forEach(inp => {{
          const dis = mmEqual && mmEqual.checked && emDetailed.checked;
          inp.disabled = dis; if(dis) inp.value = inp.value || 0;
        }});
      }}
      if (mmCustom && mmEqual) {{ [mmCustom, mmEqual].forEach(r => r.addEventListener('change', refreshMainMode)); }}

      // 상세: 사이드 토글
      const smEqual = document.getElementById('sm_equal');
      const smCustom = document.getElementById('sm_custom');
      const smNone  = document.getElementById('sm_none');
      const sideTotalWrap = document.getElementById('sideTotalWrap');
      const customSideInputs = document.querySelectorAll('.side-custom-cell input');
      function refreshSideMode() {{
        if (!emDetailed.checked) {{
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
      if (smEqual && smCustom && smNone) {{ [smEqual, smCustom, smNone].forEach(r => r.addEventListener('change', refreshSideMode)); }}
      refreshEntryMode();
    </script>
    """
    return html

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

        # 저장: meals (RETURNING id)
        cur = db_execute("""
          INSERT INTO meals(dt, entry_mode, main_mode, side_mode, main_total, side_total, grand_total, payer_name, guest_total)
          VALUES (?,?,?,?,?,?,?,?,?) RETURNING id;
        """, (dt, entry_mode, main_mode, side_mode, int(main_total), int(side_total), int(grand_total),
              payer_name, int(guest_total)))
        meal_id = cur.fetchone()["id"]

        # 저장: meal_parts
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

        # 자동정산 입금(선결제자 상환; 게스트 제외)
        if payer_name and (payer_name in members) and member_sum > 0:
            db_execute("INSERT INTO deposits(dt, name, amount, note) VALUES (?,?,?,?);",
                       (dt, payer_name, int(member_sum), f"[자동정산] 식사 #{meal_id} 선결제 상환(게스트 제외)"))

        get_db().commit()
        flash(f"식사 #{meal_id} 등록 완료.", "success")
        return redirect(url_for("meal_detail", meal_id=meal_id))

    # GET: 입력 폼
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

        # meals 업데이트
        db_execute("""UPDATE meals SET dt=?, entry_mode=?, main_mode=?, side_mode=?, 
                      main_total=?, side_total=?, grand_total=?, payer_name=?, guest_total=? WHERE id=?;""",
                   (dt, entry_mode, main_mode, side_mode, int(main_total), int(side_total),
                    int(grand_total), payer_name, int(guest_total), meal_id))

        # 기존 파츠 삭제 후 재삽입
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

        # 기존 자동정산 입금 제거 후 재삽입
        delete_auto_deposit_for_meal(meal_id)
        if payer_name and (payer_name in members) and member_sum > 0:
            db_execute("INSERT INTO deposits(dt, name, amount, note) VALUES (?,?,?,?);",
                       (dt, payer_name, int(member_sum), f"[자동정산] 식사 #{meal_id} 선결제 상환(게스트 제외)"))

        get_db().commit()
        flash("수정되었습니다.", "success")
        return redirect(url_for("meal_detail", meal_id=meal_id))

    # GET: 등록폼과 동일 UI로, 기존 값 채워서 렌더
    parts = db_execute("SELECT name, main_amount, side_amount, total_amount FROM meal_parts WHERE meal_id=? ORDER BY name;", (meal_id,)).fetchall()
    init = dict(meal)
    init["parts"] = parts
    body = _meal_form_html(members, initial=init, edit_target_id=meal_id)
    return render(body)

@app.get("/meal/<int:meal_id>/delete")
def meal_delete(meal_id):
    # 자동정산 입금 제거
    delete_auto_deposit_for_meal(meal_id)
    db_execute("DELETE FROM meal_parts WHERE meal_id=?;", (meal_id,))
    db_execute("DELETE FROM meals WHERE id=?;", (meal_id,))
    get_db().commit()
    flash("삭제되었습니다.", "info")
    return redirect(url_for("meal"))

# ------------------ 식사 기록 리스트 ------------------
@app.get("/meals")
def meals():
    # 최근 식사 200건: 팀원합계/게스트합계/참여자수 집계
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

# ------------------ 현황/정산 ------------------
@app.route("/status")
def status():
    balances = get_balances()

    # 합계 계산
    total_deposit = sum(b["deposit"] for b in balances)
    total_used    = sum(b["used"]    for b in balances)
    total_balance = sum(b["balance"] for b in balances)  # = total_deposit - total_used

    # 행 렌더링
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

    # 표 푸터에 합계 표시
    body = f"""
    <div class="card shadow-sm">
      <div class="card-body">
        <h5 class="card-title">현황 / 정산</h5>

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

# ------------------ 앱 실행 ------------------
if __name__ == "__main__":
    with app.app_context():
        init_db()
    app.run(host="0.0.0.0", port=8000)
