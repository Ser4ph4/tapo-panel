let chartTotal = null;
let chartDevices = null;

async function authedFetch(url, options = {}) {
  const res = await fetch(url, options);
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("sessão expirada");
  }
  return res;
}

function fmt(n, digits = 0) {
  if (n === null || n === undefined) return "—";
  return Number(n).toLocaleString("pt-BR", { maximumFractionDigits: digits });
}

function timeAgo(ts) {
  const diff = Math.floor(Date.now() / 1000 - ts);
  if (diff < 60) return `${diff}s atrás`;
  if (diff < 3600) return `${Math.floor(diff / 60)}min atrás`;
  return `${Math.floor(diff / 3600)}h atrás`;
}

function hhmm(ts) {
  return new Date(ts * 1000).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
}

function daysInMonth(date) {
  return new Date(date.getFullYear(), date.getMonth() + 1, 0).getDate();
}

function updateClock() {
  const now = new Date();
  document.getElementById("clock-time").textContent = now.toLocaleTimeString("pt-BR");
  document.getElementById("clock-date").textContent = now
    .toLocaleDateString("pt-BR", { weekday: "short", day: "2-digit", month: "short", year: "numeric" })
    .replace(".", "");
}

async function refreshLatest() {
  try {
    const res = await authedFetch("/api/latest");
    const rows = await res.json();
    const byDevice = Object.fromEntries(rows.map((r) => [r.device_name, r]));

    document.querySelectorAll(".device-card").forEach((card) => {
      const name = card.dataset.device;
      const r = byDevice[name];
      if (!r) return;

      const stateEl = card.querySelector('[data-role="state"]');
      stateEl.textContent = r.is_on ? "ligado" : "desligado";
      stateEl.className = "pill " + (r.is_on ? "pill-on" : "pill-off");
      card.querySelector('[data-role="power"]').textContent = fmt(r.current_power_w, 1);
      card.querySelector('[data-role="today"]').textContent = fmt(r.today_energy_wh);
      card.querySelector('[data-role="updated"]').textContent = `atualizado ${timeAgo(r.ts)}`;
    });

    setGlobalStatus(true);
  } catch (err) {
    console.error(err);
    setGlobalStatus(false);
  }
}

async function refreshSummary() {
  try {
    const res = await authedFetch("/api/summary");
    const s = await res.json();

    document.querySelector('[data-role="hero-power"]').textContent = `${fmt(s.total_power_w, 1)} W`;
    document.querySelector('[data-role="hero-sub"]').textContent = `${s.devices_on} de ${s.devices_total} dispositivos ligados`;
    document.querySelector('[data-role="hero-today"]').textContent = `${fmt(s.total_today_wh)} Wh`;
    document.querySelector('[data-role="hero-month"]').textContent = `${fmt(s.total_month_wh / 1000, 1)} kWh`;
    document.querySelector('[data-role="hero-devices"]').textContent = `${s.devices_on}/${s.devices_total}`;

    document.querySelector('[data-role="stat-power"]').textContent = `${fmt(s.total_power_w, 1)} W`;
    document.querySelector('[data-role="stat-power-sub"]').textContent = `${s.devices_on} de ${s.devices_total} dispositivos ligados`;
    const powerPct = s.devices_total ? (s.devices_on / s.devices_total) * 100 : 0;
    document.querySelector('[data-role="stat-power-bar"]').style.width = `${powerPct}%`;

    document.querySelector('[data-role="stat-today"]').textContent = `${fmt(s.total_today_wh)} Wh`;
    const now = new Date();
    const secondsToday = now.getHours() * 3600 + now.getMinutes() * 60 + now.getSeconds();
    const dayPct = (secondsToday / 86400) * 100;
    document.querySelector('[data-role="stat-today-sub"]').textContent =
      `${Math.round(dayPct)}% do dia decorrido`;
    document.querySelector('[data-role="stat-today-bar"]').style.width = `${dayPct}%`;

    document.querySelector('[data-role="stat-month"]').textContent = `${fmt(s.total_month_wh / 1000, 1)} kWh`;
    const dim = daysInMonth(now);
    const monthPct = (now.getDate() / dim) * 100;
    document.querySelector('[data-role="stat-month-sub"]').textContent = `dia ${now.getDate()} de ${dim}`;
    document.querySelector('[data-role="stat-month-bar"]').style.width = `${monthPct}%`;

    renderChartTotal(s.history);
  } catch (err) {
    console.error("erro no summary", err);
  }
}

function renderChartTotal(history) {
  const ctx = document.getElementById("chart-total");
  const labels = history.map((r) => hhmm(r.ts));
  const data = history.map((r) => r.total_power_w);

  if (!chartTotal) {
    const gradient = ctx.getContext("2d").createLinearGradient(0, 0, 0, 160);
    gradient.addColorStop(0, "rgba(79, 140, 255, 0.35)");
    gradient.addColorStop(1, "rgba(79, 140, 255, 0.0)");

    chartTotal = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            data,
            borderColor: "#4f8cff",
            backgroundColor: gradient,
            borderWidth: 2,
            pointRadius: 0,
            tension: 0.35,
            fill: true,
          },
        ],
      },
      options: chartBaseOptions(),
    });
  } else {
    chartTotal.data.labels = labels;
    chartTotal.data.datasets[0].data = data;
    chartTotal.update();
  }
}

function chartBaseOptions() {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    plugins: { legend: { display: false } },
    scales: {
      x: { display: false },
      y: {
        display: true,
        grid: { color: "#151b2b" },
        ticks: { color: "#6b7690", font: { size: 10 } },
      },
    },
  };
}

async function refreshDevicesChart() {
  try {
    const res = await authedFetch("/api/latest");
    const rows = await res.json();
    const labels = rows.map((r) => r.device_name);
    const data = rows.map((r) => r.today_energy_wh || 0);
    const colors = ["#22d3ee", "#4f8cff", "#f5a623", "#34d399", "#f87171"];

    const ctx = document.getElementById("chart-devices");
    if (!chartDevices) {
      chartDevices = new Chart(ctx, {
        type: "bar",
        data: {
          labels,
          datasets: [
            {
              data,
              backgroundColor: labels.map((_, i) => colors[i % colors.length]),
              borderRadius: 6,
              maxBarThickness: 42,
            },
          ],
        },
        options: chartBaseOptions(),
      });
    } else {
      chartDevices.data.labels = labels;
      chartDevices.data.datasets[0].data = data;
      chartDevices.data.datasets[0].backgroundColor = labels.map((_, i) => colors[i % colors.length]);
      chartDevices.update();
    }
  } catch (err) {
    console.error("erro no chart de dispositivos", err);
  }
}

async function refreshTable() {
  try {
    const res = await authedFetch("/api/table");
    const rows = await res.json();
    const tbody = document.getElementById("log-body");
    tbody.innerHTML = rows
      .map(
        (r) => `
        <tr>
          <td>${hhmm(r.ts)}</td>
          <td>${r.device_name}</td>
          <td><span class="chip ${r.is_on ? "chip-on" : "chip-off"}">${r.is_on ? "ligado" : "desligado"}</span></td>
          <td class="num">${fmt(r.current_power_w, 1)} W</td>
          <td class="num">${fmt(r.today_energy_wh)} Wh</td>
        </tr>`
      )
      .join("");
  } catch (err) {
    console.error("erro na tabela", err);
  }
}

function setGlobalStatus(ok) {
  const dot = document.getElementById("global-dot");
  const text = document.getElementById("global-status-text");
  dot.className = "status-dot" + (ok ? "" : " bad");
  text.textContent = ok ? "online" : "sem resposta";
}

function tick() {
  refreshLatest();
  refreshSummary();
  refreshDevicesChart();
  refreshTable();
}

document.getElementById("ping-btn").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  btn.classList.add("spin");
  btn.disabled = true;
  try {
    const res = await authedFetch("/api/ping", { method: "POST" });
    const data = await res.json();
    if (data.status !== "success") {
      console.warn("ping falhou:", data.message);
    }
  } catch (err) {
    console.error("erro no ping manual", err);
  } finally {
    tick();
    btn.disabled = false;
    setTimeout(() => btn.classList.remove("spin"), 600);
  }
});

updateClock();
setInterval(updateClock, 1000);

tick();
setInterval(refreshLatest, 15000);
setInterval(refreshSummary, 15000);
setInterval(refreshDevicesChart, 30000);
setInterval(refreshTable, 20000);
