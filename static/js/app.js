// UroScan: trend graph + AI chat (used on the patient dashboard and the doctor's patient page)
// Needs PID (patient id) and CSRF defined on the page.

// ---------- trend graph ----------
let chart;
const testSel = document.getElementById('testSel');

// paints the normal range as a light green band behind the line
const normalBand = {
  id: 'normalBand',
  beforeDatasetsDraw(c, args, opts) {
    if (!opts.range) return;
    const { ctx, chartArea: a, scales: { y } } = c;
    const top = Math.max(y.getPixelForValue(opts.range[1] + 0.5), a.top);
    const bottom = Math.min(y.getPixelForValue(opts.range[0] - 0.5), a.bottom);
    ctx.save(); ctx.fillStyle = 'rgba(22,163,74,0.12)'; ctx.fillRect(a.left, top, a.right - a.left, bottom - top); ctx.restore();
  }
};

async function drawGraph() {
  if (!testSel) return;
  const res = await fetch(`/api/graph/${PID}?test=${encodeURIComponent(testSel.value)}`);
  const d = await res.json();
  const colour = s => s === 'Normal' ? '#16a34a' : s === 'Trace' ? '#f59e0b' : '#e11d48';
  if (chart) chart.destroy();
  chart = new Chart(document.getElementById('chart'), {
    type: 'line',
    plugins: [normalBand],
    data: {
      labels: d.points.map(p => p.date),
      datasets: [{
        data: d.points.map(p => p.y), borderColor: '#0f766e', tension: 0.25,
        pointRadius: 7, pointHoverRadius: 9,
        pointBackgroundColor: d.points.map(p => colour(p.status)), pointBorderColor: '#fff'
      }]
    },
    options: {
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false }, normalBand: { range: d.normal },
        tooltip: { callbacks: { label: c => `${d.points[c.dataIndex].label} (${d.points[c.dataIndex].status})` } }
      },
      scales: {
        y: { min: -0.5, max: d.levels.length - 0.5,
             afterBuildTicks: ax => { ax.ticks = d.levels.map((_, i) => ({ value: i })); },
             ticks: { callback: v => d.levels[v] ?? '' }, grid: { color: '#eef2f3' } },
        x: { grid: { display: false } }
      },
      onClick: (e, els) => { if (els.length) location.href = '/report/' + d.points[els[0].index].id; }
    }
  });
}
if (testSel) { testSel.onchange = drawGraph; drawGraph(); }

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
    const res = await fetch('/api/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: question, patient_id: PID, csrf_token: CSRF })
    });
    reply.textContent = (await res.json()).answer;
  } catch (e) { reply.textContent = 'Network problem, please try again.'; }
}
if (chatLog) {
  document.getElementById('chatForm').onsubmit = e => {
    e.preventDefault();
    const input = document.getElementById('chatInput');
    if (input.value.trim()) { ask(input.value.trim()); input.value = ''; }
  };
  document.querySelectorAll('.chips button').forEach(b => b.onclick = () => ask(b.dataset.q));
}
