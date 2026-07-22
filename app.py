import json
import logging
import os
import time
from datetime import datetime
from functools import wraps

import pyotp
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template, request, session, redirect, url_for, flash
from influxdb_client import InfluxDBClient

from collector import (
    CONFIG_PATH,
    INFLUX_BUCKET,
    INFLUX_ORG,
    INFLUX_TOKEN,
    INFLUX_URL,
    init_db,
    load_house_meter_config,
    load_plugs,
    run_poll_cycle,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("app")

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))

app = Flask(__name__)


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Variável de ambiente obrigatória '{name}' não definida. "
            "Configure-a na stack antes de subir o painel (sem valor padrão por segurança)."
        )
    return value


app.secret_key = require_env("FLASK_SECRET_KEY")
ADMIN_USER = require_env("PANEL_USER")
ADMIN_PASS = require_env("PANEL_PASS")
ADMIN_2FA_SECRET = require_env("PANEL_2FA_SECRET")

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "true").lower() == "true",
)

_influx_client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
_query_api = _influx_client.query_api()


def flux_query(flux: str):
    """Roda uma query Flux e devolve uma lista de dicts (um por record)."""
    tables = _query_api.query(flux, org=INFLUX_ORG)
    rows = []
    for table in tables:
        for record in table.records:
            rows.append(dict(record.values))
    return rows


# --- DECORADOR DE SEGURANÇA ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("user_logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "not_authenticated"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated_function


# --- ROTAS DE AUTENTICAÇÃO ---
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        if username == ADMIN_USER and password == ADMIN_PASS:
            session["pre_auth"] = True
            return redirect(url_for("verify_2fa"))
        else:
            flash("Usuário ou senha inválidos.")

    return render_template("login.html")


@app.route("/verify-2fa", methods=["GET", "POST"])
def verify_2fa():
    if not session.get("pre_auth"):
        return redirect(url_for("login"))

    if request.method == "POST":
        token = request.form.get("token")
        totp = pyotp.TOTP(ADMIN_2FA_SECRET)
        if totp.verify(token):
            session.pop("pre_auth", None)
            session["user_logged_in"] = True
            return redirect(url_for("index"))
        else:
            flash("Código 2FA inválido.")

    return render_template("verify_2fa.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- ROTAS DO PAINEL ---
@app.route("/")
@login_required
def index():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        devices = [d["name"] for d in json.load(f)["devices"]]

    try:
        rows = flux_query(
            f'''
            from(bucket: "{INFLUX_BUCKET}")
              |> range(start: time(v: 0))
              |> filter(fn: (r) => r._measurement == "energy_reading" and r._field == "power_w")
              |> count()
              |> group()
              |> sum()
            '''
        )
        total_readings = int(rows[0]["_value"]) if rows else 0
    except Exception as exc:  # noqa: BLE001
        log.warning("Falha ao contar leituras no InfluxDB: %s", exc)
        total_readings = 0

    return render_template("index.html", devices=devices, total_readings=total_readings)


@app.route("/api/ping", methods=["POST"])
@login_required
def api_ping():
    try:
        log.info("Coleta manual solicitada via botão Ping.")
        run_poll_cycle()
        return jsonify({"status": "success", "message": "Ping executado com sucesso!"})
    except Exception as e:  # noqa: BLE001
        log.error(f"Erro na coleta manual: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500


def _latest_rows():
    """Última leitura (todos os fields pivotados) de cada dispositivo."""
    rows = flux_query(
        f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -30d)
          |> filter(fn: (r) => r._measurement == "energy_reading")
          |> filter(fn: (r) => r._field == "power_w" or r._field == "today_wh" or r._field == "month_wh" or r._field == "is_on")
          |> toFloat()
          |> group(columns: ["device", "_field"])
          |> last()
          |> group(columns: ["device"])
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
    )
    out = []
    for r in rows:
        out.append(
            {
                "device_name": r.get("device"),
                "ts": int(r["_time"].timestamp()) if r.get("_time") else None,
                "is_on": int(r.get("is_on") or 0),
                "current_power_w": r.get("power_w"),
                "today_energy_wh": r.get("today_wh"),
                "month_energy_wh": r.get("month_wh"),
            }
        )
    return out


@app.route("/api/latest")
@login_required
def api_latest():
    return jsonify(_latest_rows())


@app.route("/api/history/<device_name>")
@login_required
def api_history(device_name):
    safe_name = device_name.replace('"', "")
    rows = flux_query(
        f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -24h)
          |> filter(fn: (r) => r._measurement == "energy_reading" and r.device == "{safe_name}")
          |> filter(fn: (r) => r._field == "power_w" or r._field == "is_on")
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
          |> group()
          |> sort(columns: ["_time"])
        '''
    )
    out = [
        {
            "ts": int(r["_time"].timestamp()),
            "current_power_w": r.get("power_w"),
            "is_on": int(r.get("is_on") or 0),
        }
        for r in rows
        if r.get("_time")
    ]
    return jsonify(out)


@app.route("/api/summary")
@login_required
def api_summary():
    latest = _latest_rows()
    total_power = sum(r["current_power_w"] or 0 for r in latest)
    total_today_wh = sum(r["today_energy_wh"] or 0 for r in latest)
    total_month_wh = sum(r["month_energy_wh"] or 0 for r in latest)
    devices_on = sum(1 for r in latest if r["is_on"])

    history_rows = flux_query(
        f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -24h)
          |> filter(fn: (r) => r._measurement == "energy_reading" and r._field == "power_w")
          |> aggregateWindow(every: {POLL_INTERVAL_SECONDS}s, fn: last, createEmpty: false)
          |> group(columns: ["_time"])
          |> sum()
          |> sort(columns: ["_time"])
        '''
    )
    history = [
        {"ts": int(r["_time"].timestamp()), "total_power_w": r.get("_value") or 0}
        for r in history_rows
        if r.get("_time")
    ]

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
    rows = flux_query(
        f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -30d)
          |> filter(fn: (r) => r._measurement == "energy_reading")
          |> filter(fn: (r) => r._field == "power_w" or r._field == "today_wh" or r._field == "is_on")
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
          |> group()
          |> sort(columns: ["_time"], desc: true)
          |> limit(n: {limit})
        '''
    )
    out = [
        {
            "device_name": r.get("device"),
            "ts": int(r["_time"].timestamp()),
            "is_on": int(r.get("is_on") or 0),
            "current_power_w": r.get("power_w"),
            "today_energy_wh": r.get("today_wh"),
        }
        for r in rows
        if r.get("_time")
    ]
    return jsonify(out)


@app.route("/api/hourly-pattern")
@login_required
def api_hourly_pattern():
    days = int(request.args.get("days", "365"))
    rows = flux_query(
        f'''
        import "date"
        import "timezone"
        option location = timezone.location(name: "America/Sao_Paulo")

        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -{days}d)
          |> filter(fn: (r) => r._measurement == "house_reading" and r._field == "power_w")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
          |> map(fn: (r) => ({{ r with hour: date.hour(t: r._time) }}))
          |> group(columns: ["hour"])
          |> mean(column: "_value")
          |> sort(columns: ["hour"])
        '''
    )
    out = [{"hour": int(r.get("hour", 0)), "avg_power_w": round(r.get("_value") or 0, 1)} for r in rows]
    return jsonify(out)


def _house_energy_kwh_since(dt: datetime):
    """Calcula energia consumida (kWh) desde o instante dt, integrando as
    leituras de potência (W) ao longo do tempo — em vez de depender do
    contador acumulado do próprio medidor (dps 111), que se mostrou
    estático por horas mesmo com consumo real acontecendo, e portanto
    não confiável pra esse cálculo. O Flux integral() faz a soma
    trapezoidal de potência × tempo, convertida aqui de W·s pra kWh."""
    ts = dt.isoformat()
    rows = flux_query(
        f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: {ts})
          |> filter(fn: (r) => r._measurement == "house_reading" and r._field == "power_w")
          |> integral(unit: 1s)
        '''
    )
    if not rows or rows[0].get("_value") is None:
        return None
    watt_seconds = rows[0]["_value"]
    return watt_seconds / 3_600_000  # W·s -> kWh


@app.route("/api/house-summary")
@login_required
def api_house_summary():
    """Resumo do medidor de casa toda (Tuya/EKAZA), separado dos plugs
    Tapo — não soma no total dos plugs, pra evitar contagem duplicada."""
    house_cfg = load_house_meter_config()
    if not house_cfg:
        return jsonify({"enabled": False})

    latest_rows = flux_query(
        f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -30d)
          |> filter(fn: (r) => r._measurement == "house_reading")
          |> filter(fn: (r) => r._field == "power_w" or r._field == "voltage_v" or r._field == "current_a" or r._field == "total_energy_kwh")
          |> toFloat()
          |> group(columns: ["_field"])
          |> last()
          |> group()
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''
    )
    if not latest_rows:
        return jsonify({"enabled": True, "has_data": False})

    latest = latest_rows[0]

    today_kwh = month_kwh = year_kwh = None
    now = datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    today_val = _house_energy_kwh_since(midnight)
    month_val = _house_energy_kwh_since(month_start)
    year_val = _house_energy_kwh_since(year_start)

    today_kwh = max(0, round(today_val, 2)) if today_val is not None else None
    month_kwh = max(0, round(month_val, 2)) if month_val is not None else None
    year_kwh = max(0, round(year_val, 2)) if year_val is not None else None

    plugs = _latest_rows()
    monitored_power_w = sum(r["current_power_w"] or 0 for r in plugs)
    house_power_w = latest.get("power_w") or 0
    unidentified_power_w = max(0, round(house_power_w - monitored_power_w, 1))

    return jsonify(
        {
            "enabled": True,
            "has_data": True,
            "power_w": house_power_w,
            "voltage_v": latest.get("voltage_v"),
            "current_a": latest.get("current_a"),
            "monitored_power_w": round(monitored_power_w, 1),
            "unidentified_power_w": unidentified_power_w,
            "today_kwh": today_kwh,
            "month_kwh": month_kwh,
            "year_kwh": year_kwh,
        }
    )


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "plugs": [p.name for p in load_plugs()]})


def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_poll_cycle, "interval", seconds=POLL_INTERVAL_SECONDS)
    scheduler.start()
    return scheduler


init_db()
run_poll_cycle()
_scheduler = start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
