import json
import logging
import os
import sqlite3
import time
from functools import wraps

import pyotp
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template, request, session, redirect, url_for, flash

from collector import CONFIG_PATH, DB_PATH, init_db, load_plugs, run_poll_cycle

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("app")

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))

app = Flask(__name__)
# Chave secreta obrigatória para usar sessões no Flask
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "tapo-chave-secreta-padrao")

# Dados do administrador puxados das variáveis do Docker
ADMIN_USER = os.environ.get("PANEL_USER", "admin")
ADMIN_PASS = os.environ.get("PANEL_PASS", "senha123")
OTP_SECRET = os.environ.get("PANEL_2FA_SECRET", "JBSWY3DPEHPK3PXP")

def query(sql, params=()):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(sql, params).fetchall()
    con.close()
    return [dict(r) for r in rows]

# --- DECORADOR DE SEGURANÇA ---
def login_required(f):
    """Bloqueia o acesso a qualquer rota se não estiver logado."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('user_logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- ROTAS DE AUTENTICAÇÃO ---
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")
        
        if username == ADMIN_USER and password == ADMIN_PASS:
            # Senha correta. Libera o acesso para a tela de 2FA.
            session['pre_auth'] = True
            return redirect(url_for('verify_2fa'))
        else:
            flash("Usuário ou senha inválidos.")
            
    return render_template("login.html")

@app.route("/verify-2fa", methods=["GET", "POST"])
def verify_2fa():
    # Impede acesso direto à tela de 2FA se não tiver passado pelo login
    if not session.get('pre_auth'):
        return redirect(url_for('login'))
        
    if request.method == "POST":
        token = request.form.get("token")
        
        # Verifica se o código de 6 dígitos bate com o segredo
        totp = pyotp.TOTP(OTP_SECRET)
        if totp.verify(token):
            # Tudo certo! Efetiva o login.
            session.pop('pre_auth', None)
            session['user_logged_in'] = True
            return redirect(url_for('index'))
        else:
            flash("Código 2FA inválido.")
            
    return render_template("verify_2fa.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for('login'))


# --- ROTAS DO PAINEL (AGORA PROTEGIDAS E COM TAMANHO DO BD) ---
@app.route("/")
@login_required
def index():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        devices = [d["name"] for d in json.load(f)["devices"]]
        
    # Lógica para ler o tamanho real do banco de dados (tapo.db) em Megabytes
    try:
        db_size_bytes = os.path.getsize(DB_PATH)
        db_size_mb = round(db_size_bytes / (1024 * 1024), 2)
    except OSError:
        db_size_mb = 0.0

    return render_template("index.html", devices=devices, db_size_mb=db_size_mb)


@app.route("/api/latest")
@login_required
def api_latest():
    rows = query(
        """
        SELECT r.* FROM readings r
        INNER JOIN (
            SELECT device_name, MAX(ts) AS max_ts
            FROM readings GROUP BY device_name
        ) latest
        ON r.device_name = latest.device_name AND r.ts = latest.max_ts
        """
    )
    return jsonify(rows)


@app.route("/api/history/<device_name>")
@login_required
def api_history(device_name):
    since = int(time.time()) - 24 * 3600
    rows = query(
        """
        SELECT ts, current_power_w, is_on
        FROM readings
        WHERE device_name = ? AND ts >= ?
        ORDER BY ts ASC
        """,
        (device_name, since),
    )
    return jsonify(rows)


@app.route("/api/summary")
@login_required
def api_summary():
    latest = query(
        """
        SELECT r.* FROM readings r
        INNER JOIN (
            SELECT device_name, MAX(ts) AS max_ts
            FROM readings GROUP BY device_name
        ) l ON r.device_name = l.device_name AND r.ts = l.max_ts
        """
    )
    total_power = sum(r["current_power_w"] or 0 for r in latest)
    total_today_wh = sum(r["today_energy_wh"] or 0 for r in latest)
    total_month_wh = sum(r["month_energy_wh"] or 0 for r in latest)
    devices_on = sum(1 for r in latest if r["is_on"])

    since = int(time.time()) - 24 * 3600
    rows = query(
        """
        SELECT ts, device_name, current_power_w
        FROM readings
        WHERE ts >= ?
        ORDER BY ts ASC
        """,
        (since,),
    )
    by_ts = {}
    for r in rows:
        bucket = r["ts"] - (r["ts"] % POLL_INTERVAL_SECONDS)
        by_ts.setdefault(bucket, 0)
        by_ts[bucket] += r["current_power_w"] or 0
    history = [{"ts": ts, "total_power_w": p} for ts, p in sorted(by_ts.items())]

    return jsonify(
        {
            "total_power_w": total_power,
            "total_today_wh": total_today_wh,
            "total_month_wh": total_month_wh,
            "devices_on": devices_on,
            "devices_total": len(latest),
            "history": history,
        }
    )


@app.route("/api/table")
@login_required
def api_table():
    limit = int(os.environ.get("TABLE_ROWS", "25"))
    rows = query(
        """
        SELECT device_name, ts, is_on, current_power_w, today_energy_wh
        FROM readings
        ORDER BY ts DESC
        LIMIT ?
        """,
        (limit,),
    )
    return jsonify(rows)


@app.route("/api/health")
def api_health():
    # Rota pública para monitoramento local
    return jsonify({"status": "ok", "plugs": [p.name for p in load_plugs()]})


def start_scheduler():
    scheduler = BackgroundScheduler()
    # A correção mantida
    scheduler.add_job(run_poll_cycle, "interval", seconds=POLL_INTERVAL_SECONDS)
    scheduler.start()
    return scheduler


init_db()
run_poll_cycle()
_scheduler = start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))