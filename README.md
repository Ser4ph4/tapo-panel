# tapo-panel

Painel próprio (sem Grafana, sem Home Assistant) pros plugs Tapo P110.
Fala direto com os dispositivos na LAN via `plugp100` (mesma lib usada
pela integração do HA), guarda histórico em SQLite e serve um
dashboard dark com Chart.js.

## Como funciona

- `collector.py`: a cada ciclo, conecta em cada plug do `config.json`,
  lê potência atual + energia acumulada (hoje/mês) e grava no SQLite.
- `app.py`: Flask com um `APScheduler` em background thread rodando o
  coletor a cada `POLL_INTERVAL_SECONDS` (padrão 60s), mais duas rotas
  JSON (`/api/latest`, `/api/history/<nome>`) que o frontend consome.
- `templates/` + `static/`: dashboard estático, um card por plug, com
  potência atual, energia de hoje/mês e um gráfico das últimas 24h.

## Setup

1. Copie `config.example.json` para `config.json` e liste seus plugs:

```json
{
  "devices": [
    { "name": "servidor", "host": "192.168.0.50" },
    { "name": "roteador", "host": "192.168.0.51" }
  ]
}
```

O IP de cada plug fica em: app Tapo → engrenagem no plug → "Informações
do dispositivo". Vale fixar via DHCP reservation, já que a comunicação
é direta na LAN (sem passar pela nuvem).

2. Configure as credenciais (mesmas do app Tapo) via variáveis de
   ambiente — `TAPO_EMAIL` e `TAPO_PASSWORD`. Não tem e-mail com letra
   maiúscula? A lib pode falhar autenticação nesse caso; usar o e-mail
   como está cadastrado no app.

3. Suba localmente pra testar:

```bash
cp config.example.json config.json   # edite os devices
export TAPO_EMAIL=seu@email.com
export TAPO_PASSWORD=suasenha
docker compose up --build
```

Acesse `http://localhost:5000`.

## Deploy como stack no Portainer

- Publique a imagem (ex: `ghcr.io/seraph4/tapo-panel`) ou aponte
  `build: .` num Git repo stack do Portainer.
- No `docker-compose.yml`, troque o bind de porta pelo IP Tailscale do
  host (mesmo padrão dos outros serviços — nunca expor em `0.0.0.0`).
- Defina `TAPO_EMAIL` / `TAPO_PASSWORD` como variáveis da stack no
  Portainer (não commitar em texto plano).
- `HEALTHCHECK_URL` é opcional: se setado, o coletor pinga essa URL a
  cada ciclo bem-sucedido, seguindo o mesmo padrão do
  `garantia-torrent`/`check-hd-note`.

## Notas

- Só 1 worker gunicorn: o scheduler roda dentro do processo, então
  múltiplos workers duplicariam a coleta. Se precisar de mais
  concorrência HTTP, aumente `--threads` no Dockerfile.
- O gráfico mostra as últimas 24h de potência instantânea. Se quiser
  retenção maior ou agregação por hora, dá pra adicionar um job de
  limpeza/downsample no `collector.py`.
- Se algum plug não tiver monitoramento de energia (P100 puro, sem o
  "M" ou sem medição), `energy_info` vem `None` e o card mostra "—" —
  o painel não quebra, só não tem os números de consumo.
# tapo-painel
<img width="1865" height="1004" alt="Screenshot 2026-07-11 at 13-17-06 Tapo · Painel de Energia" src="https://github.com/user-attachments/assets/5e7934e6-6a42-41e6-b33d-6770a5e113f0" />
