// uroscan trend graph + AI chat (patient dashboard and the clinician's patient page)
// The page defines PID (patient id) and CSRF before loading this file.

const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const PASTEL = { Normal: '#a9cbb0', Trace: '#e9cf94', High: '#e3a6a1', Low: '#e3a6a1' };

// ---------- trend line for one test (pastel, with the normal range shaded) ----------
const testSel = document.getElementById('testSel');
const normalBand = {
  id: 'normalBand',
  beforeDatasetsDraw(ch, args, opts) {
    if (!opts.range) return;
    const { ctx, chartArea: a, scales: { y } } = ch;
    const top = Math.max(y.getPixelForValue(opts.range[1] + 0.5), a.top);
    const bottom = Math.min(y.getPixelForValue(opts.range[0] - 0.5), a.bottom);
    ctx.save(); ctx.fillStyle = 'rgba(169,203,176,0.22)';
    ctx.beginPath(); ctx.roundRect(a.left, top, a.right - a.left, bottom - top, 10); ctx.fill(); ctx.restore();
  }
};
let trend, trendChart;
async function drawTrend(fetchNew = true) {
  const el = document.getElementById('chart');
  if (!testSel || !el) return;
  if (fetchNew || !trend) trend = await (await fetch(`/api/graph/${PID}?test=${encodeURIComponent(testSel.value)}`)).json();
  const d = trend, line = css('--line'), ink = css('--muted');
  Chart.defaults.color = ink; Chart.defaults.font.family = 'Inter, system-ui, sans-serif';
  if (trendChart) trendChart.destroy();
  const g = el.getContext('2d').createLinearGradient(0, 0, 0, 260);
  g.addColorStop(0, 'rgba(185,199,224,0.45)'); g.addColorStop(1, 'rgba(185,199,224,0)');
  trendChart = new Chart(el, {
    type: 'line', plugins: [normalBand],
    data: { labels: d.points.map(p => p.date), datasets: [{
      data: d.points.map(p => p.y), borderColor: '#8f917c', borderWidth: 2.5, tension: 0.35, fill: true, backgroundColor: g,
      pointRadius: 7, pointHoverRadius: 9, pointBackgroundColor: d.points.map(p => PASTEL[p.status] || '#b9c7e0'),
      pointBorderColor: css('--card') || '#fff', pointBorderWidth: 2.5 }] },
    options: { maintainAspectRatio: false, animation: { duration: 500 },
      plugins: { legend: { display: false }, normalBand: { range: d.normal },
        tooltip: { backgroundColor: '#1f1f1f', padding: 10, cornerRadius: 10,
          callbacks: { label: x => `${d.points[x.dataIndex].label} (${d.points[x.dataIndex].status})` } } },
      scales: {
        y: { min: -0.5, max: d.levels.length - 0.5, grid: { color: line },
             afterBuildTicks: ax => { ax.ticks = d.levels.map((_, i) => ({ value: i })); },
             ticks: { callback: v => d.levels[v] ?? '' } },
        x: { grid: { display: false } } },
      onClick: (e, els) => { if (els.length) location.href = '/report/' + d.points[els[0].index].id; } }
  });
}
if (typeof Chart !== 'undefined' && testSel) {
  testSel.onchange = () => drawTrend(true);
  drawTrend(true);
  document.addEventListener('themechange', () => drawTrend(false));   // redraw with dark / light colours
}

// ---------- AI chat (remembers the last few messages so follow-up questions work) ----------
const chatLog = document.getElementById('chatLog');
const turns = [];
function addMessage(text, who) {
  const div = document.createElement('div');
  div.className = 'msg ' + who; div.textContent = text;
  chatLog.appendChild(div); chatLog.scrollTop = chatLog.scrollHeight;
  return div;
}
async function ask(question) {
  addMessage(question, 'me');
  const typing = document.createElement('div');
  typing.className = 'msg bot typing'; typing.innerHTML = '<i></i><i></i><i></i>';
  chatLog.appendChild(typing); chatLog.scrollTop = chatLog.scrollHeight;
  const send = document.querySelector('#chatForm button'); send.disabled = true;
  let answer;
  try {
    const res = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: question, patient_id: PID, csrf_token: CSRF, history: turns.slice(-6) }) });
    answer = (await res.json()).answer;
  } catch (e) { answer = 'Network problem, please try again.'; }
  typing.remove(); send.disabled = false;
  addMessage(answer, 'bot');
  turns.push({ role: 'me', text: question }, { role: 'bot', text: answer });
}
if (chatLog) {
  document.getElementById('chatForm').onsubmit = e => {
    e.preventDefault();
    const input = document.getElementById('chatInput');
    if (input.value.trim()) { ask(input.value.trim()); input.value = ''; }
  };
  document.querySelectorAll('.chips button').forEach(b => b.onclick = () => ask(b.dataset.q));
}
