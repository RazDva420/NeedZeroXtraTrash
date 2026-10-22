"use strict";

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(
  path.join(root, "src", "needzero", "webui", "index.html"), "utf8");
const matches = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
assert.strictEqual(matches.length, 1, "expected exactly one inline application script");

const logs = [];
class FakeElement {
  constructor(id = "") {
    this.id = id;
    this.children = [];
    this.style = {};
    this.dataset = {};
    this.className = "";
    this.value = "";
    this.checked = false;
    this.hidden = false;
    this.innerHTML = "";
    this.textContent = "";
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.width = 384;
    this.height = 384;
    this.classList = {
      add() {}, remove() {}, toggle() {}, contains() { return false; },
    };
  }
  get firstChild() { return this.children[0] || null; }
  appendChild(child) {
    this.children.push(child);
    this.scrollHeight = this.children.length;
    if (this.id === "log-body") logs.push(String(child.innerHTML || child.textContent));
    return child;
  }
  removeChild(child) {
    const index = this.children.indexOf(child);
    if (index >= 0) this.children.splice(index, 1);
    return child;
  }
  replaceChildren(...children) { this.children = [...children]; }
  querySelector() { return new FakeElement(); }
  querySelectorAll() { return []; }
  addEventListener() {}
  setAttribute(name, value) { this[name] = value; }
  getBoundingClientRect() {
    return { left: 0, top: 0, width: this.width, height: this.height };
  }
  setPointerCapture() {}
  pause() { this.paused = true; }
  play() { this.paused = false; return Promise.resolve(); }
}

const elements = new Map();
const element = (id) => {
  if (!elements.has(id)) elements.set(id, new FakeElement(id));
  return elements.get(id);
};
const documentListeners = {};
global.document = {
  hidden: false,
  body: element("body"),
  getElementById: element,
  createElement: () => new FakeElement(),
  createElementNS: () => new FakeElement(),
  createTextNode: (text) => ({ textContent: String(text) }),
  querySelector: () => new FakeElement(),
  querySelectorAll: () => [],
  addEventListener: (name, fn) => { documentListeners[name] = fn; },
};

const windowListeners = {};
global.window = {
  pywebview: { api: null },
  addEventListener: (name, fn) => { windowListeners[name] = fn; },
};
global.requestAnimationFrame = (fn) => { fn(); return 1; };
global.cancelAnimationFrame = () => {};

let timerId = 0;
const timers = [];
global.setTimeout = (fn, delay) => {
  const timer = { id: ++timerId, fn, delay: Number(delay), interval: false };
  timers.push(timer);
  return timer.id;
};
global.clearTimeout = (id) => {
  const index = timers.findIndex((timer) => timer.id === id);
  if (index >= 0) timers.splice(index, 1);
};
global.setInterval = (fn, delay) => {
  const timer = { id: ++timerId, fn, delay: Number(delay), interval: true };
  timers.push(timer);
  return timer.id;
};
global.clearInterval = global.clearTimeout;
global.console = { error() {}, log() {} };

const curve = [[40, 30], [55, 45], [70, 70], [85, 100]];
const groups = {
  rear: { channel: "fan1", source: "max", curve },
  top: { channel: "fan2", source: "max", curve },
  front: { channel: "fan3", source: "max", curve },
};
const config = {
  groups,
  mode: "balanced",
  min_duty: 30,
  poll_interval: 2,
  sensors: { cpu: null, gpu: null, gpu_hotspot: null },
  emergency: {
    enabled: true, gpu_hotspot_threshold: 95, duty: 100,
  },
};
const state = {
  temps: { cpu: 40, gpu: 45, gpu_hotspot: 52 },
  groups: Object.fromEntries(Object.entries(groups).map(([name, value]) => [name, {
    ...value, rpm: 900, duty_reported: 40, control_temp: 45, curve_duty: 40,
  }])),
  mode: "balanced",
  sensor_fault: false,
  fan_fault: false,
  emergency_latched: false,
  testing: false,
  backends: {
    sensors: "fake", sensors_available: true,
    fans: "fake", fans_available: true, channels: ["fan1", "fan2", "fan3"],
  },
};

let configCalls = 0;
let stateCalls = 0;
let autostartCalls = 0;
const api = {
  async get_config() {
    configCalls += 1;
    if (configCalls === 1) throw new Error("transient config failure");
    return config;
  },
  async get_state() {
    stateCalls += 1;
    if (stateCalls === 1) throw new Error("transient state failure");
    return state;
  },
  async apply_settings() { throw new Error("disk full"); },
  async set_autostart() { autostartCalls += 1; return { ok: true }; },
};

function takeTimer(delay) {
  const index = timers.findIndex(
    (timer) => !timer.interval && timer.delay === delay);
  assert.notStrictEqual(index, -1, `missing retry timer ${delay}ms`);
  return timers.splice(index, 1)[0].fn;
}
async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

async function main() {
  // Execute the real application IIFE, not a copied boot implementation.
  new Function(matches[0][1])();
  assert.strictEqual(element("case-art").children.length, 1);
  assert.strictEqual(
    element("case-art").children[0].src,
    "assets/case_video_h264.mp4?v=1",
  );
  assert.strictEqual(element("case-art").children[0].autoplay, true);
  assert.strictEqual(element("case-art").children[0].loop, true);
  assert.strictEqual(element("case-art").children[0].muted, true);
  const art = element("case-art");
  art.width = 2000; art.height = 1200;
  windowListeners.resize();
  const scale = 1200 / 560;
  assert(Math.abs(parseFloat(element("case-temp-cpu").style.left) - (1000 + (287 - 376) * scale)) < 0.01);
  assert(Math.abs(parseFloat(element("case-temp-gpu").style.top) - 351 * scale) < 0.01);
  windowListeners.blur();
  assert.strictEqual(art.firstChild.paused, true);
  windowListeners.focus();
  assert.strictEqual(art.firstChild.paused, false);
  assert.strictEqual(element("fanlist").children.length, 3);
  assert(element("curve").children.length > 0);
  window.pywebview.api = api;
  assert.strictEqual(typeof windowListeners.pywebviewready, "function");
  windowListeners.pywebviewready();
  await settle();

  assert.strictEqual(configCalls, 1);
  assert.strictEqual(stateCalls, 0);
  assert(logs.some((line) => line.includes("ERROR: transient config failure")));
  assert(!logs.some((line) => line.includes("Controller initialized")));

  takeTimer(500)();
  await settle();
  assert.strictEqual(configCalls, 2);
  assert.strictEqual(stateCalls, 1);
  assert(logs.some((line) => line.includes("ERROR: transient state failure")));
  assert(!logs.some((line) => line.includes("Controller initialized")));

  takeTimer(1000)();
  await settle();
  assert.strictEqual(configCalls, 3);
  assert.strictEqual(stateCalls, 2);
  assert.strictEqual(
    logs.filter((line) => line.includes("Controller initialized")).length, 1);
  assert(timers.some((timer) => timer.interval && timer.delay === 2000));
  for (const group of ["rear", "top", "front"]) assert.strictEqual(element("rp-" + group).textContent, "900 RPM");
  assert.strictEqual(element("case-temp-cpu").textContent, "40°C");
  assert.strictEqual(element("case-temp-gpu").textContent, "45°C");

  element("ch-rear").value = "fan1";
  element("ch-top").value = "fan2";
  element("ch-front").value = "fan3";
  element("s-minduty").value = "30";
  element("s-poll").value = "2";
  element("e-enabled").checked = true;
  element("e-hotspot").value = "95";
  element("e-duty").value = "100";
  await window.NZ.saveSettings();
  await settle();
  assert(logs.some((line) => line.includes("ERROR: disk full")));
  assert(!logs.some((line) => line.includes("Settings saved")));
  assert.strictEqual(autostartCalls, 0);
}

main().catch((error) => {
  process.stderr.write(String(error && error.stack || error) + "\n");
  process.exitCode = 1;
});
