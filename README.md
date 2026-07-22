# tapo-panel

Painel próprio (sem Grafana, sem Home Assistant) pros plugs Tapo P110.
Fala direto com os dispositivos na LAN via `python-kasa`, guarda
histórico no **InfluxDB** e serve um dashboard dark com Chart.js —
incluindo login com 2FA e uma análise de consumo médio por hora do dia.

## Por que InfluxDB (não SQLite)

O projeto começou com SQLite e migrou pro InfluxDB quando a necessidade
virou análise de série temporal de verdade (consumo médio por hora do dia
ao longo do ano) — coisa que dá pra fazer numa query Flux e seria muito
mais trabalho em SQL puro. Retenção é indefinida por decisão de projeto
(`DOCKER_INFLUXDB_INIT_RETENTION: "0"`).

## Como funciona

- `collector.py`: a cada ciclo, conecta em cada plug do `config.json` via
  `python-kasa`, lê potência atual + energia acumulada, e grava um `Point`
  no InfluxDB (measurement `energy_reading`, tag `device`).
- `app.py`: Flask com `APScheduler` rodando o coletor a cada
  `POLL_INTERVAL_SECONDS` (padrão 60s), mais rotas que consultam o Influx
  via Flux: `/api/latest`, `/api/history/<nome>`, `/api/summary`,
  `/api/table`, e `/api/hourly-pattern` (a análise por hora do dia).
- `templates/` + `static/`: dashboard com hero, stat cards, gráficos
  (consumo total, por dispositivo, e o padrão por hora), tabela de
  histórico, tudo protegido por login + 2FA (TOTP).

## Setup

1. Copie `config.example.json` para `config.json` e liste seus plugs.

2. Gere os segredos obrigatórios:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"    # FLASK_SECRET_KEY
python3 -c "import pyotp; print(pyotp.random_base32())"       # PANEL_2FA_SECRET
python3 -c "import secrets; print(secrets.token_hex(32))"    # INFLUX_TOKEN
```

3. Monte o `.env`:

```bash
TAPO_EMAIL=seu@email.com
TAPO_PASSWORD=suasenha

FLASK_SECRET_KEY=...
PANEL_USER=admin
PANEL_PASS=uma-senha-forte
PANEL_2FA_SECRET=...

INFLUX_USER=admin
INFLUX_PASS=uma-senha-forte-influx
INFLUX_TOKEN=...
INFLUX_ORG=nemik
INFLUX_BUCKET=tapo_energy
```

4. Suba tudo:

```bash
docker compose up -d --build
```

O `influxdb` sobe primeiro (com `DOCKER_INFLUXDB_INIT_MODE=setup`, criando
org/bucket/token automaticamente no primeiro boot), depois o `tapo-panel`
conecta nele via `http://influxdb:8086` (nome do serviço = hostname
interno, não precisa expor porta pública).

Acesse `http://IP-TAILSCALE:5000`, faz login com `PANEL_USER`/`PANEL_PASS`,
depois o código TOTP (escaneia `PANEL_2FA_SECRET` num app autenticador).

## Deploy como stack no Portainer

Mesmo fluxo de sempre: publica a imagem no ghcr.io via GitHub Actions,
cola o `docker-compose.yml` como Web editor stack no Portainer, e define
todas as env vars (`TAPO_EMAIL`, `PANEL_*`, `INFLUX_*`) no campo
Environment variables da stack — não em `.env` em disco, já que o
Portainer não lê arquivos locais quando a stack é colada via editor web.

## Debug do InfluxDB

Pra acessar a UI do Influx diretamente (útil pra rodar queries Flux ad-hoc
ou conferir os dados brutos), descomente o bloco `ports:` do serviço
`influxdb` no compose, amarrado ao IP Tailscale, e acesse
`http://IP-TAILSCALE:8086`.

## Medidor de casa toda (Tuya, ex: EKAZA T3180WB)

Além dos plugs Tapo, o painel suporta opcionalmente um medidor de quadro
Tuya (protocolo local, via `tinytuya`) — mostra numa seção separada
"Casa Toda", com breakdown de quanto do consumo já está identificado
pelos plugs Tapo vs. não identificado (geladeira, ar-condicionado, etc.).

**Importante:** o protocolo local do Tuya normalmente só expõe potência/
tensão/corrente instantâneas e um contador acumulado de energia — não o
"hoje/mês/ano" que aparece no app Smart Life (isso é calculado na nuvem
da Tuya). O painel calcula esses períodos sozinho, comparando o contador
acumulado atual com o valor gravado no início do dia/mês/ano no InfluxDB.

### Extraindo device_id e local_key

1. Pareia o dispositivo no app Tuya/Smart Life normalmente.
2. Com o `tinytuya` instalado (`pip install tinytuya`), roda o wizard:
   ```bash
   python3 -m tinytuya wizard
   ```
   Ele pede login da conta Tuya/Smart Life (mesma do pareamento) e lista
   os dispositivos, com `device_id`, `local_key` e IP de cada um.
3. Confirma o IP do medidor na sua rede (pode variar do que o wizard
   mostrou, se for DHCP sem reserva) — vale fixar via DHCP reservation.
4. Roda um teste rápido de conexão, e principalmente **confira os campos
   DPS retornados** (variam por modelo/firmware):
   ```bash
   python3 -c "
   import tinytuya
   d = tinytuya.OutletDevice(
       dev_id='SEU_DEVICE_ID',
       address='SEU_IP',
       local_key='SEU_LOCAL_KEY',
       version=3.4,
   )
   print(d.status())
   "
   ```
   O retorno é algo como `{'dps': {'1': True, '18': 780, '19': 1200, '20': 2286, ...}}`.
   Por convenção (não universal — confirme no seu caso), `18` costuma ser
   corrente (mA), `19` potência (0.1W), `20` tensão (0.1V), e o contador de
   energia acumulada varia bastante por modelo (procure um valor que só
   cresce, nunca some).
5. Preenche `config.json`:
   ```json
   "house_meter": {
     "enabled": true,
     "device_id": "...",
     "local_key": "...",
     "ip": "192.168.0.150",
     "version": 3.4,
     "dps_power": 19,
     "dps_voltage": 20,
     "dps_current": 18,
     "dps_energy": 17
   }
   ```
   Os campos `dps_*` são opcionais — só sobrescreva se os índices padrão
   (17/18/19/20) não baterem com o que o `status()` mostrou pro seu
   modelo específico.
6. Reinicia o coletor. A seção "Casa Toda" aparece automaticamente no
   painel assim que a primeira leitura for gravada — antes disso, fica
   escondida (sem quebrar nada se `enabled: false` ou os campos
   estiverem incompletos).

## Notas

- Só 1 worker gunicorn: o scheduler roda dentro do processo, então
  múltiplos workers duplicariam a coleta.
- `/api/hourly-pattern?days=N` aceita um parâmetro de período (padrão
  365 dias) — soma a potência de todos os dispositivos por hora, depois
  tira a média de cada hora-do-dia ao longo do período.
- Se algum plug não tiver monitoramento de energia (P100 puro, sem o
  "M" ou sem medição), os campos de energia vêm `None`/0 e o card mostra
  "—" — o painel não quebra, só não tem os números de consumo.
