"""The single-file demo page served at GET /.

Self-contained HTML/CSS/JS (no external requests). Fetches /latest and renders:
a sleep-stage bar, today's calendar timeline with the Restore block highlighted,
and the morning brief. Palette validated with the dataviz skill (light + dark).
"""

DEMO_HTML = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AnAn</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #f5f4ef; --surface: #ffffff; --text: #141310; --muted: #6b6a64;
    --line: #e4e2db; --surface-2: #f0efe9;
    --deep: #2a78d6; --core: #1baf7a; --rem: #4a3aa7; --awake: #bcbab1;
    --restore: #008300;
    --good: #1a7f37; --warn: #b8860b; --bad: #d24a49;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      --bg: #131310; --surface: #1e1e19; --text: #f4f3ea; --muted: #a6a49a;
      --line: #33322c; --surface-2: #26261f;
      --deep: #3987e5; --core: #199e70; --rem: #9085e9; --awake: #55544e;
      --restore: #2fae2f;
      --good: #3fb950; --warn: #d4a017; --bad: #e5706f;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex; justify-content: center; padding: 24px 16px;
  }
  .card {
    background: var(--surface); border: 1px solid var(--line); border-radius: 14px;
    width: 100%; max-width: 620px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,.06);
  }
  header { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
  h1 { font-size: 20px; margin: 0; letter-spacing: -.01em; }
  .updated { color: var(--muted); font-size: 12px; }
  .badge { font-size: 12px; font-weight: 600; padding: 3px 9px; border-radius: 999px; color: #fff; }
  .badge.good { background: var(--good); } .badge.short, .badge.fragmented { background: var(--warn); }
  .badge.poor { background: var(--bad); }
  section { margin-top: 22px; }
  h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); margin: 0 0 10px; }

  /* sleep bar */
  .bar { display: flex; gap: 2px; height: 30px; border-radius: 6px; overflow: hidden; }
  .seg { position: relative; min-width: 2px; }
  .seg span { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    font-size: 11px; font-weight: 600; color: #fff; white-space: nowrap; }
  .legend { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 10px; font-size: 13px; color: var(--muted); }
  .legend b { color: var(--text); }
  .swatch { display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 6px; vertical-align: middle; }
  .metrics { margin-top: 10px; font-size: 13px; color: var(--muted); }

  /* timeline */
  .track { position: relative; height: 62px; background: var(--surface-2); border-radius: 8px; margin-top: 4px; }
  .tick { position: absolute; top: 0; bottom: 0; border-left: 1px solid var(--line); }
  .tick b { position: absolute; bottom: -18px; left: 3px; font-size: 11px; color: var(--muted); font-weight: 500; }
  .evt { position: absolute; top: 8px; height: 30px; background: var(--surface); border: 1px solid var(--line);
    border-radius: 5px; padding: 3px 6px; font-size: 11px; overflow: hidden; white-space: nowrap;
    text-overflow: ellipsis; }
  .evt.restore { background: var(--restore); border-color: var(--restore); color: #fff; font-weight: 600; top: 8px; height: 30px; }
  .evt.restore.proposed { background: transparent; color: var(--restore); border: 1px dashed var(--restore); }
  .axis-pad { height: 20px; }

  /* brief */
  .brief { font-size: 17px; line-height: 1.55; }
  .brief .src { color: var(--muted); font-size: 12px; margin-top: 8px; }
  .empty { color: var(--muted); text-align: center; padding: 40px 0; }
  .chat { display: inline-block; margin-top: 18px; font-size: 14px; font-weight: 600;
    color: var(--restore); text-decoration: none; }
  .chat:hover { text-decoration: underline; }
</style>
</head>
<body>
<div class="card" id="card">
  <div class="empty" id="empty">Loading…</div>
  <div id="content" hidden>
    <header>
      <div><h1>🌿 AnAn</h1><div class="updated" id="updated"></div></div>
      <span class="badge" id="badge"></span>
    </header>

    <section>
      <h2>Last night's sleep</h2>
      <div class="bar" id="bar"></div>
      <div class="legend" id="legend"></div>
      <div class="metrics" id="metrics"></div>
    </section>

    <section>
      <h2>Today</h2>
      <div class="track" id="track"></div>
      <div class="axis-pad"></div>
    </section>

    <section>
      <h2>Morning brief</h2>
      <div class="brief" id="brief"></div>
      <div class="brief src" id="src"></div>
      <a class="chat" id="chatlink" hidden></a>
    </section>
  </div>
</div>

<script>
const BOT_URL = "__BOT_URL__", BOT_NAME = "__BOT_NAME__", PAGE_TOKEN = "__PAGE_TOKEN__";
const WIN_START = 9 * 60, WIN_END = 21 * 60, WIN = WIN_END - WIN_START;
const mins = iso => { const m = /T(\d\d):(\d\d)/.exec(iso); return m ? (+m[1]) * 60 + (+m[2]) : null; };
const pct = m => ((m - WIN_START) / WIN) * 100;
const el = id => document.getElementById(id);

const STAGES = [
  ["Deep", "deep_minutes", "--deep"],
  ["Core", "core_minutes", "--core"],
  ["REM",  "rem_minutes",  "--rem"],
  ["Awake","awake_minutes","--awake"],
];

function render(d) {
  el("empty").hidden = true; el("content").hidden = false;

  if (BOT_URL) {
    const link = el("chatlink");
    link.href = BOT_URL; link.textContent = "💬 chat with " + (BOT_NAME || "the bot");
    link.hidden = false;
  }

  const badge = el("badge");
  badge.textContent = d.sleep.quality_flag;
  badge.className = "badge " + d.sleep.quality_flag;
  el("updated").textContent = "Updated " + new Date(d.generated_at).toLocaleString();

  // sleep stage bar (direct-labeled segments)
  const total = STAGES.reduce((s, [, k]) => s + (d.sleep[k] || 0), 0) || 1;
  el("bar").innerHTML = STAGES.map(([name, k, v]) => {
    const m = d.sleep[k] || 0, w = (m / total) * 100;
    const lbl = w > 12 ? `<span>${name} ${Math.round(m)}m</span>` : "";
    return `<div class="seg" style="width:${w}%;background:var(${v})">${lbl}</div>`;
  }).join("");
  el("legend").innerHTML = STAGES.map(([name, k, v]) =>
    `<span><i class="swatch" style="background:var(${v})"></i>${name} <b>${Math.round(d.sleep[k] || 0)}m</b></span>`
  ).join("");
  const s = d.sleep;
  el("metrics").textContent =
    `${s.total_hours}h asleep · resting HR ${s.resting_hr ?? "—"} bpm · HRV ${s.hrv_ms ?? "—"} ms`;

  // timeline
  const track = el("track");
  let html = "";
  for (let h = 9; h <= 21; h += 3) {
    html += `<div class="tick" style="left:${pct(h * 60)}%"><b>${((h + 11) % 12) + 1}${h < 12 ? "a" : "p"}</b></div>`;
  }
  for (const e of d.events) {
    if (e.is_all_day) continue;
    const a = mins(e.start_iso), b = mins(e.end_iso);
    if (a == null || b <= WIN_START || a >= WIN_END) continue;
    const l = Math.max(pct(a), 0), r = Math.min(pct(b), 100);
    html += `<div class="evt" style="left:${l}%;width:${r - l}%" title="${e.title} ${e.start_label}">${e.title}</div>`;
  }
  if (d.restore) {
    const a = mins(d.restore.start_iso), b = mins(d.restore.end_iso);
    const l = Math.max(pct(a), 0), r = Math.min(pct(b), 100);
    const cls = d.restore.created ? "evt restore" : "evt restore proposed";
    html += `<div class="${cls}" style="left:${l}%;width:${Math.max(r - l, 14)}%"
      title="🌿 ${d.restore.activity} at ${d.restore.start_label}">🌿 ${d.restore.activity}</div>`;
  }
  track.innerHTML = html;

  // brief
  el("brief").textContent = d.brief;
  const note = d.brief_source === "fallback" ? " (fallback — Claude unavailable)" : "";
  const rb = d.restore
    ? (d.restore.created ? ` · wrote Restore block at ${d.restore.start_label}` : ` · proposed Restore at ${d.restore.start_label} (write disabled)`)
    : "";
  el("src").textContent = "source: " + d.brief_source + note + rb;
}

fetch("/latest" + (PAGE_TOKEN ? "?k=" + encodeURIComponent(PAGE_TOKEN) : ""))
  .then(r => r.ok ? r.json() : Promise.reject(r.status))
  .then(render)
  .catch(() => { el("empty").textContent = "No brief yet — run /wake (or scripts/demo_reset.py)."; });
</script>
</body>
</html>'''
