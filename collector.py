"""
Coletor de dados dos plugs Tapo P110.

Conecta em cada plug configurado via python-kasa (biblioteca mantida
pelo time do python-kasa/Home Assistant, mais robusta que a plugp100
com firmwares recentes), lê potência atual e energia acumulada, e
grava tudo no SQLite local.
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass

from kasa import Discover, Module
from kasa.interfaces.energy import Energy

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("collector")

DB_PATH = os.environ.get("DB_PATH", "/data/tapo.db")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config.json")


@dataclass
class PlugConfig:
    name: str
    host: str


def load_plugs() -> list[PlugConfig]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return [PlugConfig(name=d["name"], host=d["host"]) for d in raw["devices"]]


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_name TEXT NOT NULL,
            ts INTEGER NOT NULL,
            is_on INTEGER,
            current_power_w REAL,
            today_energy_wh REAL,
            month_energy_wh REAL,
            today_runtime_min REAL,
            month_runtime_min REAL
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_readings_device_ts ON readings(device_name, ts)"
    )
    con.commit()
    con.close()


def save_reading(
    device_name: str,
    is_on: bool,
    current_power_w,
    today_energy_wh,
    month_energy_wh,
):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """
        INSERT INTO readings
            (device_name, ts, is_on, current_power_w, today_energy_wh,
             month_energy_wh, today_runtime_min, month_runtime_min)
        VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)
        """,
        (
            device_name,
            int(time.time()),
            1 if is_on else 0,
            current_power_w,
            today_energy_wh,
            month_energy_wh,
        ),
    )
    con.commit()
    con.close()


async def poll_plug(username: str, password: str, plug: PlugConfig):
    device = None
    try:
        device = await Discover.discover_single(
            plug.host, username=username, password=password
        )
        await device.update()

        is_on = device.is_on
        energy = device.modules.get(Module.Energy)

        current_power_w = energy.current_consumption if energy else None
        # python-kasa reporta consumo em kWh; convertemos pra Wh pra manter
        # o mesmo schema/API que o resto do painel já espera
        today_energy_wh = (
            energy.consumption_today * 1000
            if energy and energy.consumption_today is not None
            else None
        )
        month_energy_wh = (
            energy.consumption_this_month * 1000
            if energy and energy.consumption_this_month is not None
            else None
        )

        save_reading(plug.name, is_on, current_power_w, today_energy_wh, month_energy_wh)
        log.info(
            "OK  %-15s on=%s  power=%sW  today=%sWh  month=%sWh",
            plug.name,
            is_on,
            current_power_w,
            today_energy_wh,
            month_energy_wh,
        )
    except Exception as exc:  # noqa: BLE001 - queremos seguir mesmo se um plug falhar
        log.warning("FAIL %-15s host=%s erro=%s", plug.name, plug.host, exc)
    finally:
        if device is not None:
            try:
                await device.disconnect()
            except Exception:  # noqa: BLE001
                pass


async def poll_all():
    username = os.environ["TAPO_EMAIL"]
    password = os.environ["TAPO_PASSWORD"]
    plugs = load_plugs()

    await asyncio.gather(*(poll_plug(username, password, p) for p in plugs))

    healthcheck_url = os.environ.get("HEALTHCHECK_URL")
    if healthcheck_url:
        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                await session.get(healthcheck_url, timeout=aiohttp.ClientTimeout(total=10))
        except Exception as exc:  # noqa: BLE001
            log.warning("Falha ao pingar Healthchecks.io: %s", exc)


def run_poll_cycle():
    """Wrapper síncrono, usado pelo scheduler do Flask app."""
    init_db()
    asyncio.run(poll_all())


if __name__ == "__main__":
    run_poll_cycle()
