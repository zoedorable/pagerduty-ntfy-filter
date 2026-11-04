// Icon and badge: number of open incidents for your customers; blinks red while a loud
// alarm (customer + matching service) is open and not acknowledged.
// Adjust the port if WEB_PORT was changed in config.env (also in manifest.json and popup.html).
const STATE_URL = "http://localhost:8790/api/state";
const COLORS = { ok: "#1e8e3e", alarm: "#d93025", idle: "#8b949e" };

let blinkTimer = null;
let blinkOn = false;
let blinkTicks = 0;

function drawIcon(color, ring) {
  const imageData = {};
  for (const size of [16, 32]) {
    const c = new OffscreenCanvas(size, size), g = c.getContext("2d"), r = size / 2;
    g.beginPath();
    g.arc(r, r, r - size * 0.06, 0, Math.PI * 2);
    g.fillStyle = color;
    g.fill();
    if (ring) {
      g.lineWidth = size * 0.12;
      g.strokeStyle = "#ffffff";
      g.stroke();
    }
    // Hint of a bell: white dot in the middle
    g.beginPath();
    g.arc(r, r, size * 0.16, 0, Math.PI * 2);
    g.fillStyle = "#ffffff";
    g.fill();
    imageData[size] = g.getImageData(0, 0, size, size);
  }
  return chrome.action.setIcon({ imageData });
}

function startBlink() {
  if (blinkTimer) return;
  // Every extension API call keeps the service worker alive while blinking
  blinkTimer = setInterval(() => {
    blinkOn = !blinkOn;
    drawIcon(blinkOn ? COLORS.alarm : COLORS.idle, blinkOn);
    chrome.action.setBadgeBackgroundColor({ color: blinkOn ? COLORS.alarm : COLORS.idle });
    // Check more often during an alarm so blinking stops quickly after the ack
    if (++blinkTicks % 10 === 0) update();
  }, 600);
}

function stopBlink() {
  if (!blinkTimer) return;
  clearInterval(blinkTimer);
  blinkTimer = null;
}

async function update() {
  let d = null;
  try {
    const r = await fetch(STATE_URL, { cache: "no-store" });
    d = await r.json();
  } catch {
    stopBlink();
    await drawIcon(COLORS.idle);
    await chrome.action.setBadgeText({ text: "off" });
    await chrome.action.setBadgeBackgroundColor({ color: COLORS.idle });
    await chrome.action.setTitle({ title: "PD-Filter not running" });
    return;
  }
  const mine = (d.incidents || []).filter(i => i.customer || d.all_mine);   // during LAS: all customers
  const alarms = mine.filter(i => i.decision === "alarm");
  let title;
  if (d.paused) {
    title = `PD-Filter: paused (${d.paused})`;
  } else if (d.last_error) {
    title = "PD-Filter: error – " + d.last_error;
  } else {
    title = `PD-Filter: ${mine.length} open for your customers` + (alarms.length ? `, ${alarms.length} loud` : "");
  }
  await chrome.action.setTitle({ title });
  await chrome.action.setBadgeText({ text: d.paused ? "II" : d.last_error ? "!" : mine.length ? String(mine.length) : "" });

  if (alarms.length && !d.paused) {
    startBlink();
    return;
  }
  stopBlink();
  const color = d.paused ? COLORS.idle : d.last_error || mine.length ? COLORS.alarm : COLORS.ok;
  await drawIcon(color);
  await chrome.action.setBadgeBackgroundColor({ color });
}

function schedule() {
  chrome.alarms.create("poll", { periodInMinutes: 0.5 });
  update();
}
chrome.runtime.onInstalled.addListener(schedule);
chrome.runtime.onStartup.addListener(schedule);
chrome.alarms.onAlarm.addListener(update);
update();
