"""
Coletor de dados dos plugs Tapo P110.

Conecta em cada plug configurado via python-kasa, lê potência atual e
energia acumulada, e grava tudo no InfluxDB (medição "energy_reading",
tag "device", fields de potência/energia/estado).
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
from kasa import Discover, Module, Credentials

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("collector")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config.json")

INFLUX_URL = os.environ.get("INFLUX_URL", "http://influxdb:8086")
INFLUX_TOKEN = os.environ.get("INFLUX_TOKEN")
INFLUX_ORG = os.environ.get("INFLUX_ORG", "nemik")
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "tapo_energy")

_client = None
_write_api = None


def get_client() -> InfluxDBClient:
    global _client
    if _client is None:
        _client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    return _client


def get_write_api():
    global _write_api
    if _write_api is None:
        _write_api = get_client().write_api(write_options=SYNCHRONOUS)
    return _write_api


@dataclass
class PlugConfig:
    name: str
    host: str


def load_plugs() -> list[PlugConfig]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return [PlugConfig(name=d["name"], host=d["host"]) for d in raw["devices"]]


def load_house_meter_config():
    """Configuração do medidor de casa toda (Tuya, tipo EKAZA), se houver.
    Retorna None se a chave "house_meter" não existir ou "enabled" for
    false — nesse caso a coleta desse medidor é pulada silenciosamente."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    cfg = raw.get("house_meter")
    if not cfg or not cfg.get("enabled"):
        return None
    required = ("device_id", "local_key", "ip")
    if not all(cfg.get(k) for k in required):
        log.warning(
            "house_meter está habilitado mas faltam campos (device_id/local_key/ip) — pulando coleta."
        )
        return None
    return cfg


def init_db():
    """Mantido por compatibilidade de nome — o InfluxDB não precisa de
    criação de schema antecipada (o bucket já é criado no boot do
    container via DOCKER_INFLUXDB_INIT_BUCKET)."""
    get_client()  # só valida que a conexão abre sem erro


def save_reading(
    device_name: str,
    is_on: bool,
    current_power_w,
    today_energy_wh,
    month_energy_wh,
):
    point = (
        Point("energy_reading")
        .tag("device", device_name)
        .field("is_on", 1 if is_on else 0)
        .field("power_w", float(current_power_w) if current_power_w is not None else 0.0)
        .field("today_wh", float(today_energy_wh) if today_energy_wh is not None else 0.0)
        .field("month_wh", float(month_energy_wh) if month_energy_wh is not None else 0.0)
    )
    get_write_api().write(bucket=INFLUX_BUCKET, record=point)


async def poll_plug(username: str, password: str, plug: PlugConfig):
    device = None
    try:
        creds = Credentials(username, password)
        device = await Discover.discover_single(
            plug.host, credentials=creds, timeout=10
        )
        await device.update()

        is_on = device.is_on
        energy = device.modules.get(Module.Energy)

        current_power_w = energy.current_consumption if energy else None
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
    except Exception as exc:  # noqa: BLE001
        log.warning("FAIL %-15s host=%s erro=%s", plug.name, plug.host, exc)
    finally:
        if device is not None:
            try:
                await device.disconnect()
            except Exception:  # noqa: BLE001
                pass


def poll_house_meter():
    """Lê o medidor de casa toda (Tuya local, ex: EKAZA T3180WB) e grava
    no InfluxDB numa medição separada ("house_reading") — não soma junto
    com "energy_reading" dos plugs, pra evitar contagem duplicada (o
    medidor de quadro já inclui o consumo dos plugs monitorados)."""
    house_cfg = load_house_meter_config()
    if not house_cfg:
        return

    try:
        import tinytuya

        device = tinytuya.OutletDevice(
            dev_id=house_cfg["device_id"],
            address=house_cfg["ip"],
            local_key=house_cfg["local_key"],
            version=house_cfg.get("version", 3.4),
        )
        device.set_socketTimeout(5)
        status = device.status()
        dps = status.get("dps", {})

        if not dps:
            log.warning("FAIL Casa Toda (EKAZA)  resposta sem 'dps': %s", status)
            return

        # ATENÇÃO: os índices DPS abaixo são os mais comuns em medidores
        # Tuya desse tipo, mas VARIAM por modelo/firmware. Depois de parear
        # o dispositivo, rode o comando do README pra ver o "status()" bruto
        # e confirme/ajuste os índices via as chaves dps_power/dps_voltage/
        # dps_current/dps_energy no config.json (defaults abaixo).
        power_raw = dps.get(str(house_cfg.get("dps_power", 19)))
        voltage_raw = dps.get(str(house_cfg.get("dps_voltage", 20)))
        current_raw = dps.get(str(house_cfg.get("dps_current", 18)))
        energy_raw = dps.get(str(house_cfg.get("dps_energy", 17)))

        power_w = (power_raw / 10) if power_raw is not None else None
        voltage_v = (voltage_raw / 10) if voltage_raw is not None else None
        current_a = (current_raw / 1000) if current_raw is not None else None
        total_energy_kwh = (energy_raw / 1000) if energy_raw is not None else None

        point = Point("house_reading").tag("source", "ekaza")
        if power_w is not None:
            point = point.field("power_w", float(power_w))
        if voltage_v is not None:
            point = point.field("voltage_v", float(voltage_v))
        if current_a is not None:
            point = point.field("current_a", float(current_a))
        if total_energy_kwh is not None:
            point = point.field("total_energy_kwh", float(total_energy_kwh))

        get_write_api().write(bucket=INFLUX_BUCKET, record=point)
        log.info(
            "OK  Casa Toda (EKAZA)  power=%sW  voltage=%sV  current=%sA  total=%skWh",
            power_w,
            voltage_v,
            current_a,
            total_energy_kwh,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("FAIL Casa Toda (EKAZA)  erro=%s", exc)


async def poll_all():
    username = os.environ["TAPO_EMAIL"]
    password = os.environ["TAPO_PASSWORD"]
    plugs = load_plugs()

    await asyncio.gather(*(poll_plug(username, password, p) for p in plugs))

    # tinytuya é síncrono/bloqueante — chama direto, sem thread separada,
    # já que o intervalo de coleta (60-120s) absorve tranquilo essa latência
    poll_house_meter()

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