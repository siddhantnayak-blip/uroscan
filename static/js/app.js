// UroScan charts + AI chat (patient dashboard and the clinician's patient page)
// The page defines PID (patient id) and CSRF before loading this file.

const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const C = () => ({
  ink: css('--ink') || '#0f172a', muted: css('--muted') || '#64748b', line: css('--line') || '#e2e8f0',
  brand: css('--brand') || '#0d9488', ok: '#16a34a', warn: '#f59e0b', bad: '#e11d48'
});
const charts = {};
function make(id, config) {
  const el = document.getElementById(id);
  if (!el) return;
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart(el, config);
}
function baseOptions() {
  const c = C();
  Chart.defaults.color = c.muted;
  Chart.defaults.font.family = 'Inter, system-ui, sans-serif';
  return { maintainAspectRatio: false, animation: { duration: 600 } };
}

// ---------- 1. trend line for one test ----------
const testSel = document.getElementById('testSel');
const normalBand = {           // light green band = normal range
  id: 'normalBand',
  beforeDatasetsDraw(ch, args, opts) {
    if (!opts.range) return;
    const { ctx, chartArea: a, scales: { y } } = ch;
    const top = Math.max(y.getPixelForValue(opts.range[1] + 0.5), a.top);
    const bottom = Math.min(y.getPixelForValue(opts.range[0] - 0.5), a.bottom);
    ctx.save(); ctx.fillStyle = 'rgba(22,163,74,0.13)'; ctx.fillRect(a.left, top, a.right - a.left, bottom - top); ctx.restore();
  }
};
let trend;
async function drawTrend(fetchNew = true) {
  if (!testSel) return;
  if (fetchNew || !trend) trend = await (await fetch(`/api/graph/${PID}?test=${encodeURIComponent(testSel.value)}`)).json();
  const d = trend, c = C(), colour = s => s === 'Normal' ? c.ok : s === 'Trace' ? c.warn : c.bad;
  make('chart', {
    type: 'line', plugins: [normalBand],
    data: { labels: d.points.map(p => p.date), datasets: [{
      data: d.points.map(p => p.y), borderColor: c.brand, borderWidth: 3, tension: 0.3, fill: false,
      pointRadius: 7, pointHoverRadius: 9, pointBackgroundColor: d.points.map(p => colour(p.status)),
      pointBorderColor: css('--card') || '#fff', pointBorderWidth: 2 }] },
    options: { ...baseOptions(),
      plugins: { legend: { display: false }, normalBand: { range: d.normal },
        tooltip: { callbacks: { label: x => `${d.points[x.dataIndex].label} (${d.points[x.dataIndex].status})` } } },
      scales: {
        y: { min: -0.5, max: d.levels.length - 0.5, grid: { color: c.line },
             afterBuildTicks: ax => { ax.ticks = d.levels.map((_, i) => ({ value: i })); },
             ticks: { callback: v => d.levels[v] ?? '' } },
        x: { grid: { display: false } } },
      onClick: (e, els) => { if (els.length) location.href = '/report/' + d.points[els[0].index].id; } }
  });
}

// ---------- 2. normal vs flagged per scan (stacked bars) + 3. latest scan doughnut ----------
let summary;
async function drawSummary(fetchNew = true) {
  if (!document.getElementById('bars') && !document.getElementById('donut')) return;
  if (fetchNew || !summary) summary = await (await fetch(`/api/summary/${PID}`)).json();
  const s = summary, c = C();
  make('bars', {
    type: 'bar',
    data: { labels: s.scans.map(x => x.label), datasets: [
      { label: 'Normal', data: s.scans.map(x => x.normal), backgroundColor: c.ok, borderRadius: 6 },
      { label: 'Trace', data: s.scans.map(x => x.trace), backgroundColor: c.warn, borderRadius: 6 },
      { label: 'Flagged', data: s.scans.map(x => x.flagged), backgroundColor: c.bad, borderRadius: 6 }] },
    options: { ...baseOptions(),
      plugins: { legend: { position: 'bottom', labels: { usePointStyle: true, boxWidth: 8 } } },
      scales: { x: { stacked: true, grid: { display: false } },
                y: { stacked: true, max: 10, ticks: { stepSize: 2 }, grid: { color: c.line } } } }
  });
  const L = s.latest, parts = [['Normal', c.ok], ['Trace', c.warn], ['High', c.bad], ['Low', '#8b5cf6']].filter(p => L[p[0]]);
  make('donut', {
    type: 'doughnut',
    data: { labels: parts.map(p => p[0]), datasets: [{ data: parts.map(p => L[p[0]]), backgroundColor: parts.map(p => p[1]),
      borderColor: css('--card') || '#fff', borderWidth: 3 }] },
    options: { ...baseOptions(), cutout: '64%',
      plugins: { legend: { position: 'bottom', labels: { usePointStyle: true, boxWidth: 8 } } } },
    plugins: [{ id: 'centre', afterDraw(ch) {
      const { ctx, chartArea: a } = ch, cx = (a.left + a.right) / 2, cy = (a.top + a.bottom) / 2;
      ctx.save(); ctx.textAlign = 'center'; ctx.fillStyle = C().ink; ctx.font = '800 22px Inter, sans-serif';
      ctx.fillText(`${L.Normal || 0}/10`, cx, cy + 4); ctx.font = '600 11px Inter, sans-serif'; ctx.fillStyle = C().muted;
      ctx.fillText('normal', cx, cy + 20); ctx.restore(); } }]
  });
}

if (typeof Chart !== 'undefined') {
  if (testSel) testSel.onchange = () => drawTrend(true);
  drawTrend(true); drawSummary(true);
  // redraw with the right colours when light / dark mode changes
  document.addEventListener('themechange', () => { drawTrend(false); drawSummary(false); });
}

// ---------- AI chat ----------
const chatLog = document.getElementById('chatLog');
function addMessage(text, who) {
  const div = document.createElement('div');
  div.className = 'msg ' + who; div.textContent = text;
  chatLog.appendChild(div); chatLog.scrollTop = chatLog.scrollHeight;
  return div;
}
async function ask(question) {
  addMessage(question, 'me');
  const reply = addMessage('Thinking…', 'bot');
  try {
    const res = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: question, patient_id: PID, csrf_token: CSRF }) });
    reply.textContent = (await res.json()).answer;
  } catch (e) { reply.textContent = 'Network problem, please try again.'; }
  chatLog.scrollTop = chatLog.scrollHeight;
}
if (chatLog) {
  document.getElementById('chatForm').onsubmit = e => {
    e.preventDefault();
    const input = document.getElementById('chatInput');
    if (input.value.trim()) { ask(input.value.trim()); input.value = ''; }
  };
  document.querySelectorAll('.chips button').forEach(b => b.onclick = () => ask(b.dataset.q));
}
