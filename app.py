import json
import logging
import os
import sqlite3
import time

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template

from collector import CONFIG_PATH, DB_PATH, init_db, load_plugs, run_poll_cycle

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("app")

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))

app = Flask(__name__)


def query(sql, params=()):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(sql, params).fetchall()
    con.close()
    return [dict(r) for r in rows]


@app.route("/")
def index():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        devices = [d["name"] for d in json.load(f)["devices"]]
    return render_template("index.html", devices=devices)


@app.route("/api/latest")
def api_latest():
    """Última leitura de cada plug."""
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
def api_history(device_name):
    """Histórico de potência das últimas 24h (padrão) pra montar o gráfico."""
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
def api_summary():
    """Totais agregados de todos os plugs: potência somada agora, energia
    somada hoje/mês, e histórico da soma de potência das últimas 24h."""
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
    # soma por timestamp (arredondado ao ciclo de coleta) pra montar
    # uma série "potência total" ao longo do dia
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
def api_table():
    """Últimas N leituras de todos os dispositivos, mais recentes primeiro,
    pra alimentar a tabela de histórico no rodapé do dashboard."""
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
    return jsonify({"status": "ok", "plugs": [p.name for p in load_plugs()]})


def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_poll_cycle, "interval", seconds=POLL_INTERVAL_SECONDS, next_run_time=None)
    scheduler.start()
    return scheduler


init_db()
run_poll_cycle()  # primeira leitura já no boot, pra não esperar o intervalo inteiro
_scheduler = start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
