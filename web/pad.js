"use strict";

const padRoot = typeof document === "undefined" ? null : document.getElementById("app");

const PAD_GAIN_BASE = 1.10;
const PAD_GAIN_MAX = 5.60;
const PAD_GAIN_V0 = 1.20;
const PAD_GAIN_POW = 1.45;
const PAD_DT_MIN_MS = 4;
const PAD_DT_MAX_MS = 64;
const PAD_MAX_STEP = 600;

const PAD_TAP_MAX_MS = 250;
const PAD_TAP_MAX_PX = 10;
const PAD_TAP2_MAX_PX = 12;
const PAD_SECOND_FINGER_MS = 120;

const PAD_DOUBLE_TAP_GAP_MS = 300;
const PAD_DOUBLE_TAP_GAP_PX = 24;
const PAD_DRAG_START_PX = 4;
const PAD_LONG_PRESS_MS = 500;

const PAD_TWO_FINGER_DECIDE_PX = 16;
const PAD_PINCH_RATIO_MIN = 0.15;
const PAD_CENTROID_MIN_PX = 12;

const PAD_SCROLL_GAIN = 1.6;
const PAD_PINCH_NOTCH_LOG2 = 0.12;

const PAD_BATCH_MAX = 32;
const PAD_TEXT_MAX = 500;
const PAD_TOKEN_KEY = "pocket-pad-token";
const PAD_NATURAL_KEY = "pocket-pad-natural";
const PAD_REARM_WINDOW_MS = 60000;
const PAD_MOD_LOCK_MS = 400;
const PAD_BACKOFF_MS = [250, 500, 1000, 2000, 4000];
const PAD_SLOW_RTT_MS = 250;
const PAD_PING_MS = 2000;
const PAD_PAIR_POLL_MS = 1500;
const PAD_REST_STATUS_MS = 1000;
const PAD_REST_TIMEOUT_MS = 8000;
const PAD_REL_RETRY_MAX = 3;
const PAD_WHEEL_NOTCH = 120;
const PAD_VK = {
  ctrl: 0x11,
  alt: 0x12,
  shift: 0x10,
  win: 0x5B,
  enter: 0x0D,
  esc: 0x1B,
  tab: 0x09,
  back: 0x08,
  left: 0x25,
  up: 0x26,
  right: 0x27,
  down: 0x28,
  d: 0x44,
  e: 0x45,
  l: 0x4C,
  r: 0x52,
  s: 0x53,
  c: 0x43,
  v: 0x56,
  f4: 0x73,
  f5: 0x74,
  volumeMute: 0xAD,
  volumeDown: 0xAE,
  volumeUp: 0xAF,
  mediaNext: 0xB0,
  mediaPrev: 0xB1,
  mediaPlayPause: 0xB3,
};
const PAD_MOD_ORDER = ["ctrl", "alt", "shift", "win"];
const PAD_KEY_LEFT = "\u2190";
const PAD_KEY_UP = "\u2191";
const PAD_KEY_RIGHT = "\u2192";
const PAD_KEY_DOWN = "\u2193";

let padActiveCleanup = null;

function padDefaultClock() {
  const root = typeof window !== "undefined" ? window : globalThis;
  return {
    now: () => Date.now(),
    setTimeout: (fn, ms) => root.setTimeout(fn, ms),
    clearTimeout: (id) => root.clearTimeout(id),
    setInterval: (fn, ms) => root.setInterval(fn, ms),
    clearInterval: (id) => root.clearInterval(id),
    requestAnimationFrame: (fn) => (
      root.requestAnimationFrame
        ? root.requestAnimationFrame(fn)
        : root.setTimeout(() => fn(Date.now()), 16)
    ),
    cancelAnimationFrame: (id) => (
      root.cancelAnimationFrame
        ? root.cancelAnimationFrame(id)
        : root.clearTimeout(id)
    ),
  };
}

function padPointerGain(speed) {
  const v = Math.max(0, speed);
  const t = Math.pow(v / PAD_GAIN_V0, PAD_GAIN_POW);
  return PAD_GAIN_BASE + (PAD_GAIN_MAX - PAD_GAIN_BASE) * (1 - Math.exp(-t));
}

function padAccelerate(dx, dy, dtMs) {
  const dt = Math.min(PAD_DT_MAX_MS, Math.max(PAD_DT_MIN_MS, dtMs || PAD_DT_MIN_MS));
  const dist = Math.hypot(dx, dy);
  if (dist === 0) {
    return { dx: 0, dy: 0 };
  }
  const g = padPointerGain(dist / dt);
  const scale = Math.min(g, PAD_MAX_STEP / dist);
  return { dx: dx * scale, dy: dy * scale };
}

function padAccumulate(rest, dx, dy) {
  const ax = rest.x + dx;
  const ay = rest.y + dy;
  const sx = Math.trunc(ax);
  const sy = Math.trunc(ay);
  return { send: { dx: sx, dy: sy }, rest: { x: ax - sx, y: ay - sy } };
}

function padTapDecision(distPx, durationMs, fingers) {
  const count = fingers || 0;
  if (count < 1) {
    return "none";
  }
  const maxPx = count === 1 ? PAD_TAP_MAX_PX : PAD_TAP2_MAX_PX;
  const dist = Math.max(0, distPx || 0);
  const duration = Math.max(0, durationMs || 0);
  if (duration > PAD_TAP_MAX_MS || dist > maxPx) {
    return "move";
  }
  if (count === 1) {
    return "tap";
  }
  if (count === 2) {
    return "tap2";
  }
  return "tap3";
}

function padDragArm(gapMs, gapPx) {
  return gapMs <= PAD_DOUBLE_TAP_GAP_MS && gapPx <= PAD_DOUBLE_TAP_GAP_PX;
}

function padTwoFingerDecision(a0, b0, a1, b1) {
  const startDist = Math.hypot(b0.x - a0.x, b0.y - a0.y);
  const nowDist = Math.hypot(b1.x - a1.x, b1.y - a1.y);
  const pinchDelta = Math.abs(nowDist - startDist);
  const c0x = (a0.x + b0.x) / 2;
  const c0y = (a0.y + b0.y) / 2;
  const c1x = (a1.x + b1.x) / 2;
  const c1y = (a1.y + b1.y) / 2;
  const centroidMove = Math.hypot(c1x - c0x, c1y - c0y);
  const pinchRatio = startDist > 0 ? pinchDelta / startDist : 0;
  const pinchLike = pinchRatio >= PAD_PINCH_RATIO_MIN && pinchDelta >= PAD_TWO_FINGER_DECIDE_PX;
  const scrollLike = centroidMove >= PAD_CENTROID_MIN_PX;
  if (!pinchLike && !scrollLike) {
    return "pending";
  }
  if (pinchLike && (!scrollLike || pinchDelta >= centroidMove)) {
    return "pinch";
  }
  return "scroll";
}

function padScrollDelta(dxCss, dyCss, natural) {
  let dx = Number(dxCss) || 0;
  let dy = Number(dyCss) || 0;
  if (natural) {
    dx = -dx;
    dy = -dy;
  }
  if (Math.abs(dx) < 0.4 * Math.abs(dy)) {
    dx = 0;
  }
  return { wheel: dy * PAD_SCROLL_GAIN, hwheel: dx * PAD_SCROLL_GAIN };
}

function padPinchNotches(accumLog2) {
  const value = Number(accumLog2) || 0;
  if (value === 0) {
    return { notches: 0, rest: 0 };
  }
  const notches = Math.trunc(value / PAD_PINCH_NOTCH_LOG2);
  return { notches, rest: value - notches * PAD_PINCH_NOTCH_LOG2 };
}

function padSplitText(text, maxChars) {
  const limit = maxChars || PAD_TEXT_MAX;
  const units = Array.from(String(text || ""));
  const chunks = [];
  for (let index = 0; index < units.length; index += limit) {
    chunks.push(units.slice(index, index + limit).join(""));
  }
  return chunks;
}

function padApplyCompositionEnd(data) {
  return data ? [{ t: "txt", s: String(data) }] : [];
}

function padApplyInputEvent(event) {
  if (!event) {
    return [];
  }
  if (event.inputType === "insertCompositionText") {
    return [];
  }
  if (event.inputType === "insertText" && event.data) {
    if (event.data === "\n") {
      return [{ t: "combo", vk: PAD_VK.enter, mods: [] }];
    }
    return [{ t: "txt", s: String(event.data) }];
  }
  if (event.inputType === "deleteContentBackward") {
    return [{ t: "combo", vk: PAD_VK.back, mods: [] }];
  }
  if (event.inputType === "insertLineBreak" || event.inputType === "insertParagraph") {
    return [{ t: "combo", vk: PAD_VK.enter, mods: [] }];
  }
  if (event.value) {
    return [{ t: "txt", s: String(event.value) }];
  }
  return [];
}

function padUnknownCopy() {
  return stringFor("statusUnknown");
}

function padShouldSend(cmd, armed, connState) {
  if (!cmd) {
    return false;
  }
  if (cmd.t === "rel" || cmd.t === "p") {
    return true;
  }
  return armed === true && connState === "open";
}

function readPadToken() {
  try {
    return window.localStorage.getItem(PAD_TOKEN_KEY) || "";
  } catch (error) {
    return "";
  }
}

function writePadToken(token) {
  try {
    if (token) {
      window.localStorage.setItem(PAD_TOKEN_KEY, token);
    } else {
      window.localStorage.removeItem(PAD_TOKEN_KEY);
    }
  } catch (error) {
    return;
  }
}

function readNaturalScroll() {
  try {
    const stored = window.localStorage.getItem(PAD_NATURAL_KEY);
    if (stored === "0") {
      return false;
    }
    if (stored === "1") {
      return true;
    }
  } catch (error) {
    return true;
  }
  return true;
}

function writeNaturalScroll(natural) {
  try {
    window.localStorage.setItem(PAD_NATURAL_KEY, natural ? "1" : "0");
  } catch (error) {
    return;
  }
}

function padSocketUrl() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return protocol + "//" + location.host + "/pocket/api/pad/socket";
}

function uniqueMods(mods) {
  const seen = {};
  const out = [];
  PAD_MOD_ORDER.forEach((name) => {
    if (mods.indexOf(name) !== -1 && !seen[name]) {
      seen[name] = true;
      out.push(name);
    }
  });
  return out;
}

function pairErrorText(error) {
  if (error && error.status === 404) {
    return stringFor("padDisabled");
  }
  const reason = error && error.details ? String(error.details.reason || "") : "";
  if (error && error.status === 429) {
    if (/lock/i.test(reason)) {
      return stringFor("padPairLocked");
    }
    return stringFor("padPairBusy");
  }
  return stringFor("padErrorSend");
}

function authFailed(error) {
  return !!(error && (error.status === 401 || error.status === 403));
}

function resetCsrfCache() {
  try {
    csrfSessionPromise = null;
  } catch (error) {
    return;
  }
}

async function postPadJson(url, value, extra) {
  extra = extra || {};
  const send = async () => {
    const headers = await mutationHeaders();
    const padToken = readPadToken();
    if (padToken) {
      headers["X-Pocket-Pad"] = padToken;
    }
    const options = {
      method: "POST",
      headers,
      body: JSON.stringify(value),
    };
    if (extra.signal) {
      options.signal = extra.signal;
    }
    if (extra.keepalive) {
      options.keepalive = true;
    }
    return requestJson(url, options);
  };
  try {
    return await send();
  } catch (error) {
    if (error && error.name === "AbortError") {
      throw error;
    }
    resetCsrfCache();
    return send();
  }
}

async function getPadJson(url) {
  const send = async () => {
    const headers = await mutationHeaders();
    const padToken = readPadToken();
    if (padToken) {
      headers["X-Pocket-Pad"] = padToken;
    }
    return requestJson(url, { headers });
  };
  try {
    return await send();
  } catch (error) {
    resetCsrfCache();
    return send();
  }
}

function createRestQueue(postFn, options) {
  const opts = options || {};
  const time = opts.clock || padDefaultClock();
  const notifyError = typeof opts.onError === "function" ? opts.onError : () => {};
  const sequence = typeof opts.sequence === "function" ? opts.sequence : (cmd) => cmd;
  const releaseSequence = typeof opts.releaseSequence === "function"
    ? opts.releaseSequence
    : (source) => sequence(source && Object.prototype.hasOwnProperty.call(source, "g")
      ? { t: "rel", g: source.g }
      : { t: "rel" });
  const queue = [];
  const held = [];
  let busy = false;
  let relInFlight = false;
  let inFlightRel = false;
  let active = null;

  function makeItem(cmd, resolve, reject, keepalive, relRetry) {
    return {
      cmd: sequence(Object.assign({}, cmd)),
      keepalive: !!keepalive,
      relRetry: relRetry || 0,
      waiters: resolve || reject ? [{ resolve, reject }] : [],
    };
  }

  function settleItem(item, kind, value) {
    const waiters = item && Array.isArray(item.waiters) ? item.waiters.splice(0) : [];
    waiters.forEach((waiter) => {
      if (kind === "reject") {
        if (waiter.reject) {
          waiter.reject(value);
        }
      } else if (waiter.resolve) {
        waiter.resolve(value);
      }
    });
  }

  function settleDropped(list) {
    while (list.length) {
      const item = list.pop();
      settleItem(item, "resolve", null);
    }
  }

  function dropUnsent() {
    settleDropped(queue);
    settleDropped(held);
  }

  function resumeHeld() {
    while (held.length) {
      appendUnsent(queue, held.shift());
    }
  }

  function appendUnsent(list, item) {
    const tail = list[list.length - 1];
    const type = item && item.cmd && item.cmd.t;
    if (
      tail
      && tail.cmd
      && tail.cmd.t === type
      && tail.cmd.g === item.cmd.g
      && (type === "m" || type === "w")
    ) {
      const dx = tail.cmd.dx + item.cmd.dx;
      const dy = tail.cmd.dy + item.cmd.dy;
      const safeMove = type === "m" && Number.isFinite(dx) && Number.isFinite(dy);
      const safeWheel = type === "w"
        && Number.isInteger(dx)
        && Number.isInteger(dy)
        && dx >= -(2 ** 31)
        && dx <= (2 ** 31) - 1
        && dy >= -(2 ** 31)
        && dy <= (2 ** 31) - 1;
      if (safeMove || safeWheel) {
        const envelope = {};
        ["g", "n"].forEach((key) => {
          if (Object.prototype.hasOwnProperty.call(item.cmd, key)) {
            envelope[key] = item.cmd[key];
          }
        });
        tail.cmd = Object.assign({}, tail.cmd, { dx, dy }, envelope);
        tail.waiters.push(...item.waiters);
        return;
      }
    }
    list.push(item);
  }

  function restTimeoutError() {
    const error = new Error("pad REST request timed out");
    error.name = "TimeoutError";
    error.code = "PAD_REST_TIMEOUT";
    return error;
  }

  function retryExhaustedError() {
    const error = new Error("pad REST release retry limit reached");
    error.name = "TimeoutError";
    error.code = "PAD_REL_RETRY_EXHAUSTED";
    return error;
  }

  function enqueueRelease(command, resolve, reject, keepalive, relRetry) {
    dropUnsent();
    relInFlight = true;
    const item = makeItem(command, resolve, reject, keepalive, relRetry || 0);
    if (!item.cmd) {
      settleItem(item, "resolve", null);
      return;
    }
    queue.unshift(item);
    pump();
  }

  function enqueueRecoveryRel(relRetry, sourceCommand) {
    enqueueRelease(releaseSequence(sourceCommand), null, null, true, relRetry || 0);
  }

  function finishRelease(exhausted) {
    if (queue.some((item) => item.cmd.t === "rel")) {
      relInFlight = true;
      return;
    }
    relInFlight = false;
    if (exhausted) {
      settleDropped(held);
    } else {
      resumeHeld();
    }
  }

  function timeoutActive(token) {
    if (active !== token || token.finished) {
      return;
    }
    token.timedOut = true;
    token.finished = true;
    active = null;
    busy = false;
    inFlightRel = false;
    settleItem(token.item, "reject", restTimeoutError());
    if (token.isRel) {
      if (token.item.relRetry < PAD_REL_RETRY_MAX) {
        enqueueRecoveryRel(token.item.relRetry + 1, token.item.cmd);
      } else {
        finishRelease(true);
        try {
          notifyError(retryExhaustedError());
        } catch (error) {
          // The queue must still stop retrying if the notification handler fails.
        }
      }
    } else {
      if (queue.some((item) => item.cmd.t === "rel")) {
        relInFlight = true;
      } else {
        enqueueRecoveryRel(0, token.item.cmd);
      }
    }
    pump();
  }

  function settleActive(token, kind, value) {
    if (token.timedOut) {
      if (!token.isRel) {
        enqueueRecoveryRel(0, token.item.cmd);
      }
      return;
    }
    if (active !== token || token.finished) {
      return;
    }
    token.finished = true;
    if (token.timer !== null) {
      time.clearTimeout(token.timer);
      token.timer = null;
    }
    active = null;
    busy = false;
    inFlightRel = false;
    if (kind === "resolve") {
      settleItem(token.item, "resolve", value);
    } else if (value && value.name === "AbortError") {
      settleItem(token.item, "resolve", null);
    } else {
      settleItem(token.item, "reject", value);
    }
    if (token.isRel) {
      finishRelease();
    }
    pump();
  }

  function pump() {
    if (busy || !queue.length) {
      return;
    }
    if (relInFlight && queue[0].cmd.t !== "rel") {
      return;
    }
    busy = true;
    const item = queue.shift();
    const token = {
      item,
      isRel: item.cmd.t === "rel",
      timedOut: false,
      finished: false,
      timer: null,
    };
    active = token;
    inFlightRel = token.isRel;
    const extra = {
      keepalive: !!item.keepalive || item.cmd.t === "rel",
    };
    Promise.resolve()
      .then(() => {
        token.timer = time.setTimeout(() => timeoutActive(token), PAD_REST_TIMEOUT_MS);
        return postFn(item.cmd, extra);
      })
      .then((result) => {
        settleActive(token, "resolve", result);
      }, (error) => {
        settleActive(token, "reject", error);
      });
  }

  function send(cmd, extra) {
    extra = extra || {};
    if (!cmd) {
      return Promise.resolve(null);
    }
    if (cmd.t === "rel") {
      return new Promise((resolve, reject) => {
        enqueueRelease(cmd, resolve, reject, extra.keepalive, 0);
        // Never abort the in-flight request here. Aborting is client-side only: the
        // server has already received that command and will still apply it, so the
        // server order could become rel -> down and leave a key held. Let the current
        // request settle; the completion handler calls pump(), which sends this rel
        // next because it is at the head of the queue.
        pump();
      });
    }
    return new Promise((resolve, reject) => {
      const item = makeItem(cmd, resolve, reject, extra.keepalive, 0);
      const target = relInFlight ? held : queue;
      appendUnsent(target, item);
      if (target === queue) {
        pump();
      }
    });
  }

  function release(extra, command) {
    return send(command || { t: "rel" }, extra || { keepalive: true });
  }

  return {
    send,
    release,
    dropUnsent,
    pending: () => queue.length + held.length,
    busy: () => busy,
    relInFlight: () => relInFlight,
  };
}

function createPadTransport(options) {
  const opts = options || {};
  const isStale = opts.isStale || (() => false);
  const handlers = opts.handlers || {};
  const post = opts.post || ((cmd, extra) => postPadJson("api/pad/input", cmd, extra));
  const get = opts.get || ((url) => getPadJson(url));
  const readToken = opts.readToken || readPadToken;
  const socketUrl = opts.socketUrl || padSocketUrl;
  const WebSocketImpl = Object.prototype.hasOwnProperty.call(opts, "WebSocketImpl")
    ? opts.WebSocketImpl
    : (typeof WebSocket !== "undefined" ? WebSocket : null);
  const clock = opts.clock || padDefaultClock();
  let currentGeneration = null;
  const nextSequenceByGeneration = new Map();

  function allocateSequence(generation) {
    if (!Number.isSafeInteger(generation) || generation < 1) {
      return null;
    }
    const nextSequence = nextSequenceByGeneration.get(generation) || 1;
    if (!Number.isSafeInteger(nextSequence) || nextSequence < 1) {
      return null;
    }
    nextSequenceByGeneration.set(generation, nextSequence + 1);
    return nextSequence;
  }

  function withSequence(cmd) {
    if (!cmd) {
      return null;
    }
    if (
      Object.prototype.hasOwnProperty.call(cmd, "g")
      && Object.prototype.hasOwnProperty.call(cmd, "n")
    ) {
      return cmd;
    }
    const generation = Object.prototype.hasOwnProperty.call(cmd, "g")
      ? cmd.g
      : currentGeneration;
    if (!Number.isSafeInteger(generation) || generation < 1) {
      return null;
    }
    const stamped = Object.assign({}, cmd);
    if (!Object.prototype.hasOwnProperty.call(stamped, "g")) {
      stamped.g = generation;
    }
    if (!Object.prototype.hasOwnProperty.call(stamped, "n")) {
      stamped.n = allocateSequence(generation);
      if (stamped.n === null) {
        return null;
      }
    }
    return stamped;
  }

  function stampNew(cmd) {
    if (!cmd) {
      return null;
    }
    const clean = Object.assign({}, cmd);
    delete clean.g;
    delete clean.n;
    return withSequence(clean);
  }

  function syncGeneration(generation) {
    if (!Number.isSafeInteger(generation) || generation < 1) {
      return false;
    }
    if (currentGeneration !== generation) {
      currentGeneration = generation;
      if (!nextSequenceByGeneration.has(generation)) {
        nextSequenceByGeneration.set(generation, 1);
      }
    }
    return true;
  }

  function clearGeneration() {
    currentGeneration = null;
  }

  function hasGeneration() {
    return Number.isSafeInteger(currentGeneration) && currentGeneration >= 1;
  }

  function generation() {
    return currentGeneration;
  }

  function freshRelease(sourceCommand) {
    const sourceGeneration = sourceCommand
      && Number.isSafeInteger(sourceCommand.g)
      && sourceCommand.g >= 1
      ? sourceCommand.g
      : currentGeneration;
    if (!Number.isSafeInteger(sourceGeneration) || sourceGeneration < 1) {
      return null;
    }
    return withSequence({ t: "rel", g: sourceGeneration });
  }

  const restQueue = createRestQueue(post, {
    clock,
    onError: emitError,
    sequence: withSequence,
    releaseSequence: freshRelease,
  });

  let ws = null;
  let wsGen = 0;
  let mode = "ws";
  let destroyed = false;
  let backoffIndex = 0;
  let reconnectTimer = null;
  let pingTimer = null;
  let pingId = 1;
  let pingSentAt = 0;
  let restPoll = null;
  let restPollGen = 0;
  let restReady = false;
  let connState = "connecting";

  function emitState(state, rtt) {
    connState = state;
    if (!isStale()) {
      handlers.onState(state, rtt, mode);
    }
  }

  function emitMessage(message) {
    if (!isStale() && message) {
      handlers.onMessage(message);
    }
  }

  function emitError(error) {
    if (!isStale()) {
      handlers.onError(error);
    }
  }

  function stopPing() {
    if (pingTimer !== null) {
      clock.clearInterval(pingTimer);
      pingTimer = null;
    }
  }

  function startPing() {
    stopPing();
    pingTimer = clock.setInterval(() => {
      if (destroyed || isStale()) {
        return;
      }
      pingSentAt = clock.now();
      sendRaw({ t: "p", id: pingId });
      pingId += 1;
    }, PAD_PING_MS);
  }

  function stopRestPoll() {
    if (restPoll !== null) {
      clock.clearInterval(restPoll);
      restPoll = null;
    }
  }

  function startRestPoll() {
    stopRestPoll();
    restReady = false;
    emitState("connecting");
    const tick = () => {
      if (destroyed || isStale() || mode !== "rest") {
        return;
      }
      const gen = ++restPollGen;
      Promise.resolve(get("api/pad/state")).then((state) => {
        if (gen !== restPollGen || destroyed || isStale()) {
          return;
        }
        if (!restReady) {
          restReady = true;
          emitState("open", 0);
        }
        emitMessage({
          t: "hello",
          monitors: state.monitors,
          vd: state.vd,
          armed: state.armed,
          generation: state.generation,
          absolute: state.absolute,
          limits: state.limits,
        });
        emitMessage({
          t: "s",
          mon: state.mon,
          fg: state.fg,
          armed: state.armed,
          generation: state.generation,
        });
      }).catch((error) => {
        if (gen !== restPollGen || destroyed) {
          return;
        }
        restReady = false;
        emitState("closed");
        emitError(error);
      });
    };
    tick();
    restPoll = clock.setInterval(tick, PAD_REST_STATUS_MS);
  }

  function sendRaw(cmd, extra) {
    extra = extra || {};
    const force = extra.force;
    if ((destroyed || isStale()) && !force) {
      return;
    }
    if (!hasGeneration()) {
      return;
    }
    if (cmd && cmd.t === "rel") {
      release(extra);
      return;
    }
    const command = stampNew(cmd);
    if (!command) {
      return;
    }
    if (mode === "ws" && ws && ws.readyState === 1) {
      ws.send(JSON.stringify(command));
      return;
    }
    if (mode === "rest") {
      restQueue.send(command, extra).then((result) => {
        if (result && result.blocked) {
          emitMessage({ t: "blocked" });
        } else if (result && result.dropped) {
          emitMessage({ t: "stale", reason: result.reason });
        } else if (result && result.t) {
          emitMessage(result);
        }
      }).catch((error) => {
        if (error && error.name === "AbortError") {
          return;
        }
        emitError(error);
      });
    }
  }

  function release(extra) {
    extra = extra || { keepalive: true, force: true };
    extra.keepalive = extra.keepalive !== false;
    extra.force = true;
    if (!hasGeneration()) {
      return;
    }
    const command = stampNew({ t: "rel" });
    if (mode === "ws" && ws && ws.readyState === 1) {
      restQueue.dropUnsent();
      try {
        ws.send(JSON.stringify(command));
        return;
      } catch (error) {
        restQueue.release(extra, command).catch(() => {});
        return;
      }
    }
    restQueue.release(extra, command).then((result) => {
      if (result && result.t) {
        emitMessage(result);
      }
    }).catch((error) => {
      if (error && error.name === "AbortError") {
        return;
      }
      emitError(error);
    });
  }

  function handleFrame(raw) {
    let message = raw;
    if (typeof raw === "string") {
      try {
        message = JSON.parse(raw);
      } catch (error) {
        return;
      }
    }
    if (!message || typeof message !== "object") {
      return;
    }
    if (message.t === "po" && pingSentAt) {
      emitState("open", clock.now() - pingSentAt);
    }
    emitMessage(message);
  }

  function useRest() {
    mode = "rest";
    stopPing();
    if (ws) {
      const old = ws;
      ws = null;
      try {
        old.close();
      } catch (error) {
        // Already closed.
      }
    }
    startRestPoll();
  }

  function scheduleReconnect() {
    if (destroyed || isStale() || !readToken()) {
      return;
    }
    if (reconnectTimer !== null) {
      return;
    }
    const wait = PAD_BACKOFF_MS[Math.min(backoffIndex, PAD_BACKOFF_MS.length - 1)];
    backoffIndex += 1;
    emitState("connecting");
    reconnectTimer = clock.setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, wait);
  }

  function connect() {
    if (destroyed || isStale()) {
      return;
    }
    const token = readToken();
    if (!token) {
      return;
    }
    if (!WebSocketImpl) {
      useRest();
      return;
    }
    const gen = ++wsGen;
    emitState("connecting");
    let socket;
    try {
      socket = new WebSocketImpl(socketUrl(), ["pocket-pad", token]);
    } catch (error) {
      useRest();
      return;
    }
    ws = socket;
    let opened = false;
    socket.addEventListener("open", () => {
      if (gen !== wsGen) {
        try {
          socket.close();
        } catch (error) {
          return;
        }
        return;
      }
      opened = true;
      restReady = false;
      stopRestPoll();
      mode = "ws";
      backoffIndex = 0;
      emitState("open", 0);
      startPing();
    });
    socket.addEventListener("message", (event) => {
      if (gen === wsGen) {
        handleFrame(event.data);
      }
    });
    socket.addEventListener("close", () => {
      if (gen !== wsGen) {
        return;
      }
      stopPing();
      if (ws === socket) {
        ws = null;
      }
      if (!opened) {
        useRest();
        return;
      }
      if (!destroyed && !isStale()) {
        emitState("closed");
        scheduleReconnect();
      }
    });
  }

  function reconnectNow() {
    if (destroyed || isStale()) {
      return;
    }
    if (reconnectTimer !== null) {
      clock.clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    backoffIndex = 0;
    if (ws && (ws.readyState === 0 || ws.readyState === 1)) {
      return;
    }
    connect();
  }

  function destroy() {
    destroyed = true;
    stopPing();
    stopRestPoll();
    if (reconnectTimer !== null) {
      clock.clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    release({ keepalive: true, force: true });
    if (ws) {
      try {
        ws.close();
      } catch (error) {
        // Ignore.
      }
      ws = null;
    }
    wsGen += 1;
  }

  return {
    connect,
    reconnectNow,
    send: sendRaw,
    release,
    destroy,
    syncGeneration,
    clearGeneration,
    hasGeneration,
    generation,
    mode: () => mode,
    connState: () => connState,
    restQueue,
  };
}

function padAcceptArmResult(transport, armResult) {
  return !!(
    transport
    && armResult
    && armResult.armed === true
    && transport.syncGeneration(armResult.generation)
  );
}

function padOwnsServerGeneration(transport, serverArmed, serverGeneration) {
  return !!(
    serverArmed
    && transport
    && transport.hasGeneration()
    && transport.generation() === serverGeneration
  );
}

function bindSurface(surface, session, clock) {
  const time = clock || padDefaultClock();
  const pointers = new Map();
  let moveRest = { x: 0, y: 0 };
  let wheelRest = { x: 0, y: 0 };
  let twoMode = "pending";
  let pinchLog = 0;
  let pinchCtrl = false;
  let dragging = false;
  let dragArmed = false;
  let longTimer = null;
  let lastTap = null;
  let pendingDx = 0;
  let pendingDy = 0;
  let pendingDt = 0;
  let pendingKind = "move";
  let rafId = 0;
  let pinchStartDist = 0;
  let lastCentroid = null;
  let maxFingers = 0;
  let lastPinchLog = 0;
  let gestureStartT = 0;
  let gestureMaxDist = 0;
  let chordEligible = true;
  let halted = false;

  function clearLong() {
    if (longTimer !== null) {
      time.clearTimeout(longTimer);
      longTimer = null;
    }
  }

  function cancelRaf() {
    if (rafId) {
      time.cancelAnimationFrame(rafId);
      rafId = 0;
    }
    pendingDx = 0;
    pendingDy = 0;
    pendingDt = 0;
    pendingKind = "move";
  }

  function points() {
    return Array.from(pointers.values());
  }

  function endPinch(sendUp) {
    if (pinchCtrl && sendUp !== false && session && session.send) {
      session.send({ t: "k", vk: PAD_VK.ctrl, d: 0 });
    }
    pinchCtrl = false;
    pinchLog = 0;
    pinchStartDist = 0;
  }

  function endDrag(sendUp) {
    if (dragging && sendUp !== false && session && session.send) {
      session.send({ t: "b", b: "l", d: 0 });
    }
    dragging = false;
    dragArmed = false;
  }

  function clickButton(which) {
    if (session.withOneshot) {
      session.withOneshot(() => {
        session.send({ t: "b", b: which, d: 1 });
        session.send({ t: "b", b: which, d: 0 });
      });
      return;
    }
    session.send({ t: "b", b: which, d: 1 });
    session.send({ t: "b", b: which, d: 0 });
  }

  function flushMove() {
    rafId = 0;
    if (halted) {
      return;
    }
    const dx = pendingDx;
    const dy = pendingDy;
    const dt = pendingDt;
    const kind = pendingKind;
    pendingDx = 0;
    pendingDy = 0;
    pendingDt = 0;
    pendingKind = "move";
    if (kind === "scroll") {
      const delta = padScrollDelta(dx, dy, session.natural ? session.natural() : true);
      const acc = padAccumulate(wheelRest, delta.hwheel, delta.wheel);
      wheelRest = acc.rest;
      if (acc.send.dx || acc.send.dy) {
        session.send({ t: "w", dy: acc.send.dy, dx: acc.send.dx });
      }
      return;
    }
    if (kind === "pinch") {
      const list = points();
      if (list.length >= 2 && pinchStartDist > 0) {
        const dist = Math.hypot(list[1].x - list[0].x, list[1].y - list[0].y);
        const nextLog = Math.log2(Math.max(dist, 1) / pinchStartDist);
        const step = padPinchNotches(pinchLog + (nextLog - lastPinchLog));
        lastPinchLog = nextLog;
        pinchLog = step.rest;
        if (step.notches) {
          if (!pinchCtrl && !(session.modLocked && session.modLocked("ctrl"))) {
            session.send({ t: "k", vk: PAD_VK.ctrl, d: 1 });
            pinchCtrl = true;
          }
          session.send({ t: "w", dy: step.notches * PAD_WHEEL_NOTCH, dx: 0 });
        }
      }
      return;
    }
    const accel = padAccelerate(dx, dy, dt);
    const acc = padAccumulate(moveRest, accel.dx, accel.dy);
    moveRest = acc.rest;
    if (acc.send.dx || acc.send.dy) {
      session.send({ t: "m", dx: acc.send.dx, dy: acc.send.dy });
    }
  }

  function queueDelta(dx, dy, dt, kind) {
    if (halted) {
      return;
    }
    pendingDx += dx;
    pendingDy += dy;
    pendingDt += dt;
    pendingKind = kind;
    if (!rafId) {
      rafId = time.requestAnimationFrame(flushMove);
    }
  }

  function onDown(event) {
    if (halted || (event.button && event.button !== 0)) {
      return;
    }
    event.preventDefault();
    if (surface.setPointerCapture) {
      surface.setPointerCapture(event.pointerId);
    }
    const now = event.timeStamp;
    pointers.set(event.pointerId, {
      id: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      startT: now,
      lastX: event.clientX,
      lastY: event.clientY,
      lastT: now,
      x: event.clientX,
      y: event.clientY,
    });
    if (pointers.size === 1) {
      twoMode = "pending";
      gestureStartT = now;
      gestureMaxDist = 0;
      chordEligible = true;
      maxFingers = 1;
      lastCentroid = { x: event.clientX, y: event.clientY };
      lastPinchLog = 0;
      if (lastTap && padDragArm(now - lastTap.t, Math.hypot(event.clientX - lastTap.x, event.clientY - lastTap.y))) {
        dragArmed = true;
      } else {
        dragArmed = false;
        clearLong();
        const pointerId = event.pointerId;
        longTimer = time.setTimeout(() => {
          longTimer = null;
          if (halted) {
            return;
          }
          const held = pointers.get(pointerId);
          if (!held || pointers.size !== 1 || dragging) {
            return;
          }
          const dist = Math.hypot(held.x - held.startX, held.y - held.startY);
          if (dist <= PAD_TAP_MAX_PX) {
            dragging = true;
            session.send({ t: "b", b: "l", d: 1 });
          }
        }, PAD_LONG_PRESS_MS);
      }
    } else {
      clearLong();
      endDrag();
      maxFingers = Math.max(maxFingers, pointers.size);
      if (now - gestureStartT > PAD_SECOND_FINGER_MS) {
        chordEligible = false;
      }
      const list = points();
      if (list.length >= 2) {
        lastCentroid = {
          x: (list[0].x + list[1].x) / 2,
          y: (list[0].y + list[1].y) / 2,
        };
        pinchStartDist = Math.hypot(list[1].x - list[0].x, list[1].y - list[0].y);
        lastPinchLog = 0;
        pinchLog = 0;
      }
    }
  }

  function onMove(event) {
    const point = pointers.get(event.pointerId);
    if (!point || halted) {
      return;
    }
    event.preventDefault();
    const parts = typeof event.getCoalescedEvents === "function"
      ? event.getCoalescedEvents()
      : null;
    const samples = parts && parts.length ? parts : [event];
    let dx = 0;
    let dy = 0;
    let lastT = point.lastT;
    samples.forEach((sample) => {
      dx += sample.clientX - point.lastX;
      dy += sample.clientY - point.lastY;
      point.lastX = sample.clientX;
      point.lastY = sample.clientY;
      point.x = sample.clientX;
      point.y = sample.clientY;
      lastT = sample.timeStamp;
    });
    const dt = Math.max(0, lastT - point.lastT);
    point.lastT = lastT;
    const fingerDist = Math.hypot(point.x - point.startX, point.y - point.startY);
    gestureMaxDist = Math.max(gestureMaxDist, fingerDist);
    const list = points();
    if (twoMode === "scroll" || twoMode === "pinch") {
      if (list.length < 2) {
        return;
      }
    }
    if (list.length >= 2) {
      clearLong();
      const a0 = { x: list[0].startX, y: list[0].startY };
      const b0 = { x: list[1].startX, y: list[1].startY };
      const a1 = { x: list[0].x, y: list[0].y };
      const b1 = { x: list[1].x, y: list[1].y };
      const centroid = { x: (a1.x + b1.x) / 2, y: (a1.y + b1.y) / 2 };
      if (twoMode === "pending") {
        twoMode = padTwoFingerDecision(a0, b0, a1, b1);
        if (twoMode === "pinch") {
          pinchStartDist = Math.hypot(b1.x - a1.x, b1.y - a1.y) || pinchStartDist;
          lastPinchLog = 0;
        }
      }
      if (twoMode === "scroll") {
        const cdx = lastCentroid ? centroid.x - lastCentroid.x : 0;
        const cdy = lastCentroid ? centroid.y - lastCentroid.y : 0;
        lastCentroid = centroid;
        queueDelta(cdx, cdy, dt, "scroll");
      } else if (twoMode === "pinch") {
        lastCentroid = centroid;
        queueDelta(0, 0, dt, "pinch");
      } else {
        lastCentroid = centroid;
      }
      return;
    }
    if (dragArmed && !dragging && fingerDist >= PAD_DRAG_START_PX) {
      dragging = true;
      session.send({ t: "b", b: "l", d: 1 });
    }
    if (dragging) {
      queueDelta(dx, dy, dt, "move");
    } else if (fingerDist > PAD_TAP_MAX_PX) {
      clearLong();
      queueDelta(dx, dy, dt, "move");
    }
  }

  function finishGesture(event, cancelled) {
    const point = pointers.get(event.pointerId);
    if (!point) {
      return;
    }
    event.preventDefault();
    pointers.delete(event.pointerId);
    if (surface.releasePointerCapture) {
      try {
        surface.releasePointerCapture(event.pointerId);
      } catch (error) {
        // Capture may already be released.
      }
    }
    const remaining = points();
    if (remaining.length >= 1) {
      if (twoMode === "pinch") {
        endPinch(true);
      }
      return;
    }
    clearLong();
    if (cancelled || halted) {
      cancelRaf();
      endDrag(true);
      endPinch(true);
      twoMode = "pending";
      lastCentroid = null;
      maxFingers = 0;
      dragArmed = false;
      lastTap = null;
      return;
    }
    flushMove();
    const duration = event.timeStamp - gestureStartT;
    const dist = gestureMaxDist;
    if (dragging) {
      endDrag(true);
      lastTap = null;
    } else if (twoMode === "scroll" || twoMode === "pinch") {
      endPinch(true);
      lastTap = null;
    } else if (dragArmed) {
      clickButton("l");
      lastTap = { t: event.timeStamp, x: point.x, y: point.y };
    } else {
      const fingers = chordEligible ? Math.max(1, maxFingers) : 1;
      const decision = padTapDecision(dist, duration, fingers);
      if (decision === "tap") {
        clickButton("l");
        lastTap = { t: event.timeStamp, x: point.x, y: point.y };
      } else if (decision === "tap2") {
        clickButton("r");
        lastTap = null;
      } else if (decision === "tap3") {
        clickButton("m");
        lastTap = null;
      } else {
        lastTap = null;
      }
    }
    twoMode = "pending";
    lastCentroid = null;
    maxFingers = 0;
    dragArmed = false;
    endPinch(true);
  }

  function onUp(event) {
    finishGesture(event, false);
  }

  function onCancel(event) {
    finishGesture(event, true);
  }

  function halt() {
    halted = true;
    clearLong();
    cancelRaf();
    pointers.clear();
    dragging = false;
    dragArmed = false;
    pinchCtrl = false;
    twoMode = "pending";
    lastCentroid = null;
    maxFingers = 0;
    lastTap = null;
  }

  function resume() {
    halted = false;
  }

  surface.addEventListener("pointerdown", onDown);
  surface.addEventListener("pointermove", onMove);
  surface.addEventListener("pointerup", onUp);
  surface.addEventListener("pointercancel", onCancel);
  ["contextmenu", "dblclick", "gesturestart"].forEach((type) => {
    surface.addEventListener(type, (event) => event.preventDefault());
  });

  return {
    halt,
    resume,
    reset: halt,
    destroy() {
      halt();
      surface.removeEventListener("pointerdown", onDown);
      surface.removeEventListener("pointermove", onMove);
      surface.removeEventListener("pointerup", onUp);
      surface.removeEventListener("pointercancel", onCancel);
    },
  };
}

function paintPairPanel(content, onApproved, isStale) {
  content.className = "content pad-pair";
  const title = node("h2", "pad-pair-title", stringFor("padPairTitle"));
  const lead = node("p", "pad-pair-lead", stringFor("padPairLead"));
  const caption = node("div", "pad-pair-caption", stringFor("padPairCode"));
  const codeBox = node("div", "pad-pair-code");
  const waiting = node("div", "pad-pair-waiting");
  const status = node("div", "pad-pair-status");
  const start = node("button", "pad-pair-start", stringFor("padPairStart"));
  start.type = "button";
  const retry = node("button", "pad-pair-retry", stringFor("padPairRetry"));
  retry.type = "button";
  retry.hidden = true;
  content.replaceChildren(title, lead, caption, codeBox, waiting, status, start, retry);

  let nonce = "";

  function showError(text) {
    waiting.textContent = "";
    status.textContent = text;
    start.disabled = false;
    retry.hidden = false;
    clearPolling();
  }

  async function pollStatus() {
    if (isStale() || !nonce) {
      return;
    }
    try {
      const result = await fetchJson("api/pad/pair/status?nonce=" + encodeURIComponent(nonce));
      if (isStale()) {
        return;
      }
      if (result.state === "approved" && result.token) {
        writePadToken(result.token);
        clearPolling();
        status.textContent = stringFor("padPairApproved");
        onApproved();
        return;
      }
      if (result.state === "denied") {
        showError(stringFor("padPairDenied"));
        return;
      }
      if (result.state === "expired") {
        showError(stringFor("padPairTimeout"));
        return;
      }
      waiting.textContent = stringFor("padPairWaiting");
    } catch (error) {
      if (!isStale()) {
        showError(pairErrorText(error));
      }
    }
  }

  async function beginPair() {
    if (isStale()) {
      return;
    }
    start.disabled = true;
    retry.hidden = true;
    status.textContent = "";
    codeBox.textContent = "";
    waiting.textContent = stringFor("padPairWaiting");
    try {
      const result = await postJson("api/pad/pair/start", { label: "iPhone" });
      if (isStale()) {
        return;
      }
      nonce = result.nonce;
      codeBox.textContent = result.code || "";
      startPolling(pollStatus, PAD_PAIR_POLL_MS);
      pollStatus();
    } catch (error) {
      if (!isStale()) {
        showError(pairErrorText(error));
      }
    }
  }

  start.addEventListener("click", beginPair);
  retry.addEventListener("click", beginPair);
}

function paintPadChrome(view, content) {
  content.className = "content pad-surface";
  const hint = node("div", "pad-hint", stringFor("padHint"));
  content.replaceChildren(hint);

  const buttons = node("div", "pad-buttons");
  const modifiers = node("div", "pad-modifiers");
  const keys = node("div", "pad-keys");
  const tabs = node("div", "pad-panel-tabs");
  const panel = node("div", "pad-panel");
  const textBar = node("div", "pad-text-bar");
  textBar.hidden = true;
  view.append(buttons, modifiers, keys, tabs, panel, textBar);
  return { buttons, modifiers, keys, tabs, panel, textBar, hint };
}

function fillMouseButtons(row, session) {
  [
    ["l", "padLeft"],
    ["m", "padMiddle"],
    ["r", "padRight"],
  ].forEach(([which, key]) => {
    const button = node("button", "pad-button", stringFor(key));
    button.type = "button";
    button.dataset.button = which;
    button.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      if (button.setPointerCapture) {
        button.setPointerCapture(event.pointerId);
      }
      session.withOneshotDown(which);
    });
    button.addEventListener("pointerup", (event) => {
      event.preventDefault();
      session.withOneshotUp(which);
    });
    button.addEventListener("pointercancel", (event) => {
      event.preventDefault();
      session.withOneshotUp(which);
    });
    row.append(button);
  });
}

function fillModifiers(row, session, clock) {
  const time = clock || padDefaultClock();
  PAD_MOD_ORDER.forEach((name) => {
    const label = name === "ctrl" ? "Ctrl" : name === "alt" ? "Alt" : name === "shift" ? "Shift" : "Win";
    const button = node("button", "pad-mod", label);
    button.type = "button";
    button.dataset.mod = name;
    button.setAttribute("aria-pressed", "false");
    let lockTimer = null;
    function clearLockTimer() {
      if (lockTimer !== null) {
        time.clearTimeout(lockTimer);
        lockTimer = null;
      }
    }
    session.addHaltHook(clearLockTimer);
    button.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      clearLockTimer();
      lockTimer = time.setTimeout(() => {
        lockTimer = null;
        session.lockMod(name);
      }, PAD_MOD_LOCK_MS);
    });
    button.addEventListener("pointerup", (event) => {
      event.preventDefault();
      if (lockTimer !== null) {
        clearLockTimer();
        session.tapMod(name);
      }
    });
    button.addEventListener("pointercancel", () => {
      clearLockTimer();
    });
    row.append(button);
  });
  const release = node("button", "pad-release", stringFor("padReleaseAll"));
  release.type = "button";
  release.addEventListener("click", () => session.releaseAll());
  row.append(release);
}

function fillKeys(row, session) {
  [
    ["Esc", PAD_VK.esc],
    ["Tab", PAD_VK.tab],
    [PAD_KEY_LEFT, PAD_VK.left],
    [PAD_KEY_UP, PAD_VK.up],
    [PAD_KEY_DOWN, PAD_VK.down],
    [PAD_KEY_RIGHT, PAD_VK.right],
    ["BS", PAD_VK.back],
    ["Enter", PAD_VK.enter],
  ].forEach(([label, vk]) => {
    const button = node("button", "pad-key", label);
    button.type = "button";
    button.addEventListener("click", () => session.sendCombo(vk, []));
    row.append(button);
  });
}

function fillTabs(row, onSelect) {
  const items = [
    ["keyboard", "padTabKeyboard"],
    ["media", "padTabMedia"],
    ["shortcut", "padTabShortcut"],
    ["monitor", "padTabMonitor"],
  ];
  items.forEach(([id, key]) => {
    const button = node("button", "pad-tab", stringFor(key));
    button.type = "button";
    button.dataset.tab = id;
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", () => onSelect(id));
    row.append(button);
  });
}

function bindTextField(field, session, view) {
  field.type = "text";
  field.autocomplete = "off";
  field.autocapitalize = "off";
  field.setAttribute("autocorrect", "off");
  field.spellcheck = false;
  field.placeholder = stringFor("padTextPlaceholder");
  field.setAttribute("aria-label", stringFor("padTextPlaceholder"));
  let composing = false;
  field.addEventListener("focus", () => {
    view.classList.add("is-typing");
  });
  field.addEventListener("blur", () => {
    view.classList.remove("is-typing");
  });
  field.addEventListener("compositionstart", () => {
    composing = true;
  });
  field.addEventListener("compositionend", (event) => {
    composing = false;
    padApplyCompositionEnd(event.data).forEach((cmd) => {
      if (cmd.t === "txt") {
        session.sendText(cmd.s);
      } else {
        session.send(cmd);
      }
    });
    field.value = "";
  });
  field.addEventListener("input", (event) => {
    if (composing) {
      return;
    }
    const commands = padApplyInputEvent({
      inputType: event.inputType,
      data: event.data,
      value: field.value,
    });
    commands.forEach((cmd) => {
      if (cmd.t === "txt") {
        session.sendText(cmd.s);
      } else if (cmd.t === "combo") {
        session.sendCombo(cmd.vk, cmd.mods || []);
      } else {
        session.send(cmd);
      }
    });
    field.value = "";
  });
}

function renderMediaPanel(panel, session) {
  const grid = node("div", "pad-media");
  [
    ["padMediaVolumeDown", PAD_VK.volumeDown],
    ["padMediaVolumeMute", PAD_VK.volumeMute],
    ["padMediaVolumeUp", PAD_VK.volumeUp],
    ["padMediaPrev", PAD_VK.mediaPrev],
    ["padMediaPlayPause", PAD_VK.mediaPlayPause],
    ["padMediaNext", PAD_VK.mediaNext],
  ].forEach(([key, vk]) => {
    const button = node("button", "pad-media-key", stringFor(key));
    button.type = "button";
    button.addEventListener("click", () => session.sendCombo(vk, []));
    grid.append(button);
  });
  panel.replaceChildren(grid);
}

function renderShortcutPanel(panel, session) {
  const list = node("div", "pad-shortcut");
  const items = [
    ["padShortcutShowDesktop", PAD_VK.d, ["win"]],
    ["padShortcutTaskView", PAD_VK.tab, ["win"]],
    ["padShortcutSwitchApp", PAD_VK.tab, ["alt"]],
    ["padShortcutDesktopLeft", PAD_VK.left, ["win", "ctrl"]],
    ["padShortcutDesktopRight", PAD_VK.right, ["win", "ctrl"]],
    ["padShortcutSnip", PAD_VK.s, ["win", "shift"]],
    ["padShortcutExplorer", PAD_VK.e, ["win"]],
    ["padShortcutRun", PAD_VK.r, ["win"]],
    ["padShortcutRefresh", PAD_VK.f5, []],
    ["padShortcutCopy", PAD_VK.c, ["ctrl"]],
    ["padShortcutPaste", PAD_VK.v, ["ctrl"]],
    ["padShortcutCloseWindow", PAD_VK.f4, ["alt"]],
    ["padShortcutLock", PAD_VK.l, ["win"], true],
  ];
  items.forEach(([key, vk, mods, confirmLock]) => {
    const button = node("button", "pad-shortcut-key", stringFor(key));
    button.type = "button";
    button.addEventListener("click", () => {
      if (confirmLock && !window.confirm(stringFor("padConfirmLock"))) {
        return;
      }
      session.sendCombo(vk, mods);
    });
    list.append(button);
  });
  panel.replaceChildren(list);
}

function renderMonitorPanel(panel, session) {
  const list = node("div", "pad-monitor");
  const monitors = session.monitors() || [];
  if (!monitors.length) {
    list.append(node("div", "pad-monitor-empty", stringFor("padConnecting")));
    panel.replaceChildren(list);
    return;
  }
  monitors.forEach((monitor) => {
    const row = node("button", "pad-monitor-row");
    row.type = "button";
    const title = node("span", "pad-monitor-id", monitor.id || "");
    const flags = node("span", "pad-monitor-flags");
    if (monitor.primary) {
      flags.append(node("span", "pad-monitor-primary", stringFor("padMonitorPrimary")));
    }
    if (session.currentMon() && monitor.id === session.currentMon()) {
      flags.append(node("span", "pad-monitor-current", stringFor("padMonitorCurrent")));
    }
    row.append(title, flags, node("span", "pad-monitor-jump", stringFor("padMonitorJump")));
    row.addEventListener("click", () => {
      if (monitor.id) {
        session.send({ t: "warp", mon: monitor.id });
      }
    });
    list.append(row);
  });
  panel.replaceChildren(list);
}

function renderKeyboardPanel(panel, session) {
  const wrap = node("div", "pad-keyboard");
  wrap.append(node("div", "pad-hint", stringFor("padHint")));
  const toggle = node("button", "pad-natural", stringFor("padNaturalScroll"));
  toggle.type = "button";
  toggle.setAttribute("aria-pressed", session.natural() ? "true" : "false");
  toggle.classList.toggle("selected", session.natural());
  toggle.addEventListener("click", () => {
    session.setNatural(!session.natural());
    toggle.setAttribute("aria-pressed", session.natural() ? "true" : "false");
    toggle.classList.toggle("selected", session.natural());
  });
  wrap.append(toggle);
  panel.replaceChildren(wrap);
}

async function renderPad() {
  if (typeof padActiveCleanup === "function") {
    padActiveCleanup();
    padActiveCleanup = null;
  }
  clearPolling();
  const token = ++routeToken;
  const { view, content } = screen(stringFor("padTitle"), "#/");
  view.classList.add("pad-screen");
  view.setAttribute("aria-label", stringFor("padTitle"));
  const host = (typeof app !== "undefined" && app) || padRoot;
  if (!host) {
    return;
  }
  host.replaceChildren(view);

  const conn = node("div", "pad-conn");
  const connDot = node("span", "pad-conn-dot is-connecting");
  const connRtt = node("span", "pad-conn-rtt", stringFor("padConnecting"));
  conn.append(connDot, connRtt);
  const bar = view.querySelector(".topbar");
  if (bar) {
    bar.append(conn);
  }

  const status = node("div", "pad-status");
  const monEl = node("span", "pad-mon is-unknown", padUnknownCopy());
  const fgLabel = node("span", "pad-fg-label", stringFor("padForeground"));
  const fgEl = node("span", "pad-fg is-unknown", padUnknownCopy());
  const armBtn = node("button", "pad-arm-state", stringFor("padDisarmed"));
  armBtn.type = "button";
  status.append(monEl, fgLabel, fgEl, armBtn);
  view.insertBefore(status, content);
  const errorBox = node("div", "pad-error");
  view.insertBefore(errorBox, content);

  let destroyed = false;
  const isStale = () => destroyed || token !== routeToken;
  let transport = null;
  let surfaceCtl = null;
  let pending = [];
  let flushRaf = 0;
  let oneshot = [];
  let locked = [];
  let armed = false;
  let lastArmedAt = 0;
  let allowRearm = false;
  let armPending = false;
  let natural = readNaturalScroll();
  let monitors = [];
  let currentMon = "";
  let currentTab = "";
  let chrome = null;
  let connState = "connecting";
  const haltHooks = [];

  function setError(text) {
    errorBox.textContent = text || "";
    errorBox.hidden = !text;
  }

  function setConn(state, rtt, mode) {
    connState = state;
    connDot.classList.remove("is-connecting", "is-connected", "is-disconnected");
    if (state === "open") {
      connDot.classList.add("is-connected");
      if (typeof rtt === "number" && rtt >= PAD_SLOW_RTT_MS) {
        connRtt.textContent = stringFor("padSlow", { n: Math.round(rtt) });
      } else if (typeof rtt === "number" && rtt > 0) {
        connRtt.textContent = Math.round(rtt) + "ms";
      } else {
        connRtt.textContent = "";
      }
    } else if (state === "connecting") {
      connDot.classList.add("is-connecting");
      connRtt.textContent = stringFor("padConnecting");
    } else {
      connDot.classList.add("is-disconnected");
      connRtt.textContent = stringFor("padDisconnected");
    }
    if (mode === "rest") {
      connRtt.textContent = connRtt.textContent
        ? "REST " + connRtt.textContent
        : "REST";
    }
  }

  function paintArm() {
    armBtn.textContent = armed ? stringFor("padArmed") : stringFor("padDisarmed");
    armBtn.classList.toggle("is-armed", armed);
    armBtn.setAttribute("aria-pressed", armed ? "true" : "false");
  }

  function paintUnknown(el, value) {
    if (value) {
      el.textContent = value;
      el.classList.remove("is-unknown");
      return;
    }
    el.textContent = padUnknownCopy();
    el.classList.add("is-unknown");
  }

  function paintStatus(snapshot) {
    const mon = snapshot && snapshot.mon;
    const fg = snapshot && snapshot.fg;
    currentMon = mon || "";
    paintUnknown(monEl, mon);
    paintUnknown(fgEl, fg);
  }

  function paintMods() {
    if (!chrome) {
      return;
    }
    chrome.modifiers.querySelectorAll(".pad-mod").forEach((button) => {
      const name = button.dataset.mod;
      const isLocked = locked.indexOf(name) !== -1;
      const isOnce = oneshot.indexOf(name) !== -1;
      button.classList.toggle("locked", isLocked);
      button.classList.toggle("once", isOnce);
      button.setAttribute("aria-pressed", isLocked ? "true" : "false");
      button.title = isLocked
        ? stringFor("padStickyLock")
        : isOnce
          ? stringFor("padStickyOnce")
          : "";
    });
  }

  function flushPending() {
    flushRaf = 0;
    if (!pending.length || !transport) {
      return;
    }
    const queue = pending;
    pending = [];
    while (queue.length) {
      const chunk = queue.splice(0, PAD_BATCH_MAX);
      if (chunk.length === 1) {
        transport.send(chunk[0]);
      } else {
        transport.send({ t: "batch", c: chunk });
      }
    }
  }

  function enqueue(cmd) {
    if (isStale() || !cmd) {
      return;
    }
    if (!padShouldSend(cmd, armed, connState)) {
      return;
    }
    if (cmd.t === "rel") {
      panicRelease(true);
      return;
    }
    pending.push(cmd);
    if (pending.length >= PAD_BATCH_MAX) {
      flushPending();
      return;
    }
    if (!flushRaf) {
      flushRaf = window.requestAnimationFrame(flushPending);
    }
  }

  function sendText(value) {
    padSplitText(value, PAD_TEXT_MAX).forEach((chunk) => {
      enqueue({ t: "txt", s: chunk });
    });
  }

  function haltProducers() {
    haltHooks.forEach((hook) => hook());
    if (surfaceCtl) {
      surfaceCtl.halt();
    }
    if (flushRaf) {
      window.cancelAnimationFrame(flushRaf);
      flushRaf = 0;
    }
    pending = [];
    oneshot = [];
    locked = [];
    paintMods();
  }

  function panicRelease(keepalive) {
    haltProducers();
    if (transport) {
      transport.release({ keepalive: keepalive !== false, force: true });
    }
  }

  const session = {
    send: enqueue,
    natural: () => natural,
    setNatural(value) {
      natural = !!value;
      writeNaturalScroll(natural);
    },
    monitors: () => monitors,
    currentMon: () => currentMon,
    modLocked: (name) => locked.indexOf(name) !== -1,
    addHaltHook(hook) {
      haltHooks.push(hook);
    },
    sendText,
    sendCombo(vk, extra) {
      const mods = uniqueMods([].concat(oneshot, extra || []));
      enqueue({ t: "combo", vk, mods });
      oneshot = [];
      paintMods();
    },
    tapMod(name) {
      if (locked.indexOf(name) !== -1) {
        locked = locked.filter((item) => item !== name);
        enqueue({ t: "k", vk: PAD_VK[name], d: 0 });
        paintMods();
        return;
      }
      if (oneshot.indexOf(name) !== -1) {
        oneshot = oneshot.filter((item) => item !== name);
      } else {
        oneshot.push(name);
      }
      paintMods();
    },
    lockMod(name) {
      if (locked.indexOf(name) !== -1) {
        return;
      }
      oneshot = oneshot.filter((item) => item !== name);
      locked.push(name);
      enqueue({ t: "k", vk: PAD_VK[name], d: 1 });
      paintMods();
    },
    releaseAll() {
      panicRelease(true);
      if (surfaceCtl) {
        surfaceCtl.resume();
      }
    },
    withOneshotDown(which) {
      oneshot.forEach((name) => enqueue({ t: "k", vk: PAD_VK[name], d: 1 }));
      enqueue({ t: "b", b: which, d: 1 });
    },
    withOneshotUp(which) {
      enqueue({ t: "b", b: which, d: 0 });
      oneshot.forEach((name) => enqueue({ t: "k", vk: PAD_VK[name], d: 0 }));
      oneshot = [];
      paintMods();
    },
    withOneshot(fn) {
      oneshot.forEach((name) => enqueue({ t: "k", vk: PAD_VK[name], d: 1 }));
      fn();
      oneshot.forEach((name) => enqueue({ t: "k", vk: PAD_VK[name], d: 0 }));
      oneshot = [];
      paintMods();
    },
  };

  function showPanel(id) {
    if (!chrome) {
      return;
    }
    if (currentTab === id) {
      currentTab = "";
      chrome.panel.replaceChildren();
      chrome.textBar.hidden = true;
      view.classList.remove("is-typing");
      chrome.tabs.querySelectorAll(".pad-tab").forEach((tab) => {
        tab.classList.remove("selected");
        tab.setAttribute("aria-pressed", "false");
      });
      return;
    }
    currentTab = id;
    chrome.tabs.querySelectorAll(".pad-tab").forEach((tab) => {
      const selected = tab.dataset.tab === id;
      tab.classList.toggle("selected", selected);
      tab.setAttribute("aria-pressed", selected ? "true" : "false");
    });
    chrome.textBar.hidden = id !== "keyboard";
    if (id !== "keyboard") {
      const field = chrome.textBar.querySelector("input");
      if (field) {
        field.blur();
      }
      view.classList.remove("is-typing");
    }
    if (id === "keyboard") {
      renderKeyboardPanel(chrome.panel, session);
    } else if (id === "media") {
      renderMediaPanel(chrome.panel, session);
    } else if (id === "shortcut") {
      renderShortcutPanel(chrome.panel, session);
    } else {
      renderMonitorPanel(chrome.panel, session);
    }
  }

  async function setArmed(next, takeover) {
    if (next && armPending) {
      return;
    }
    if (next) {
      armPending = true;
    }
    try {
      if (next) {
        const armResult = await postPadJson(
          "api/pad/arm",
          takeover === true ? { takeover: true } : {},
        );
        if (!padAcceptArmResult(transport, armResult)) {
          armed = false;
          if (transport) {
            transport.clearGeneration();
          }
          setError(armResult && armResult.blocked
            ? stringFor("padBlocked")
            : stringFor("padErrorSend"));
          return;
        }
        armed = true;
        lastArmedAt = Date.now();
        allowRearm = true;
        if (surfaceCtl) {
          surfaceCtl.resume();
        }
      } else {
        allowRearm = false;
        lastArmedAt = 0;
        panicRelease(true);
        await postPadJson("api/pad/disarm", {});
        armed = false;
        if (transport) {
          transport.clearGeneration();
        }
      }
      setError("");
    } catch (error) {
      if (authFailed(error)) {
        returnToPairing();
        return;
      }
      if (error && error.status === 409) {
        armed = false;
        if (transport) {
          transport.clearGeneration();
        }
        setError("");
      } else if (error && error.status === 404) {
        setError(stringFor("padDisabled"));
      } else {
        setError(stringFor("padErrorSend"));
      }
    }
    finally {
      if (next) {
        armPending = false;
      }
      paintArm();
    }
  }

  armBtn.addEventListener("click", () => setArmed(!armed, true));

  function onTransportState(state, rtt, mode) {
    const wasOpen = connState === "open";
    setConn(state, rtt, mode);
    if (state === "open" && !wasOpen) {
      if (allowRearm && lastArmedAt && Date.now() - lastArmedAt <= PAD_REARM_WINDOW_MS) {
        setArmed(true, false);
      }
    }
    if (state === "closed") {
      armed = false;
      if (transport) {
        transport.clearGeneration();
      }
      haltProducers();
      paintArm();
    }
  }

  function adoptServerArmed(serverArmed, serverGeneration) {
    if (!padOwnsServerGeneration(transport, serverArmed, serverGeneration)) {
      armed = false;
      allowRearm = false;
      lastArmedAt = 0;
      if (transport) {
        transport.clearGeneration();
      }
      haltProducers();
      paintArm();
      return;
    }
    armed = true;
    lastArmedAt = Date.now();
    paintArm();
  }

  function onTransportMessage(message) {
    if (message.t === "hello") {
      monitors = Array.isArray(message.monitors) ? message.monitors : [];
      if (Object.prototype.hasOwnProperty.call(message, "armed")) {
        adoptServerArmed(!!message.armed, message.generation);
      }
      if (currentTab === "monitor") {
        renderMonitorPanel(chrome.panel, session);
      }
      return;
    }
    if (message.t === "s") {
      paintStatus(message);
      if (Object.prototype.hasOwnProperty.call(message, "armed")) {
        adoptServerArmed(!!message.armed, message.generation);
      }
      if (currentTab === "monitor") {
        renderMonitorPanel(chrome.panel, session);
      }
      return;
    }
    if (message.t === "blocked") {
      setError(stringFor("padBlocked"));
      return;
    }
    if (message.t === "stale") {
      armed = false;
      allowRearm = false;
      lastArmedAt = 0;
      transport.clearGeneration();
      haltProducers();
      paintArm();
      return;
    }
    if (message.t === "err") {
      setError(stringFor("padErrorSend"));
    }
  }

  function onTransportError(error) {
    if (authFailed(error)) {
      returnToPairing();
      return;
    }
    if (error && error.status === 404) {
      setError(stringFor("padDisabled"));
      return;
    }
    if (error && error.status === 409) {
      armed = false;
      if (transport) {
        transport.clearGeneration();
      }
      paintArm();
      return;
    }
    setError(stringFor("padErrorSend"));
  }

  function stripChrome() {
    if (!chrome) {
      return;
    }
    [chrome.buttons, chrome.modifiers, chrome.keys, chrome.tabs, chrome.panel, chrome.textBar]
      .forEach((el) => {
        if (el && el.remove) {
          el.remove();
        }
      });
    chrome = null;
  }

  function returnToPairing() {
    writePadToken("");
    allowRearm = false;
    lastArmedAt = 0;
    haltProducers();
    if (transport) {
      transport.destroy();
      transport = null;
    }
    if (surfaceCtl) {
      surfaceCtl.destroy();
      surfaceCtl = null;
    }
    stripChrome();
    conn.hidden = true;
    armBtn.hidden = true;
    status.hidden = true;
    setError("");
    paintPairPanel(content, () => {
      if (isStale()) {
        return;
      }
      conn.hidden = false;
      armBtn.hidden = false;
      status.hidden = false;
      startPad();
    }, isStale);
  }

  function startPad() {
    if (isStale()) {
      return;
    }
    haltHooks.length = 0;
    chrome = paintPadChrome(view, content);
    fillMouseButtons(chrome.buttons, session);
    fillModifiers(chrome.modifiers, session);
    fillKeys(chrome.keys, session);
    fillTabs(chrome.tabs, showPanel);
    const field = node("input", "pad-text-field");
    const hint = node("div", "pad-text-hint", stringFor("padTextHint"));
    chrome.textBar.append(field, hint);
    bindTextField(field, session, view);
    chrome.textBar.hidden = true;
    surfaceCtl = bindSurface(content, session);
    session.addHaltHook(() => {
      if (surfaceCtl) {
        surfaceCtl.halt();
      }
    });
    paintArm();
    paintStatus({});
    transport = createPadTransport({
      isStale,
      handlers: {
        onState: onTransportState,
        onMessage: onTransportMessage,
        onError: onTransportError,
      },
    });
    transport.connect();
  }

  function onVisibility() {
    if (isStale()) {
      return;
    }
    if (document.hidden) {
      panicRelease(true);
      return;
    }
    if (surfaceCtl) {
      surfaceCtl.resume();
    }
    if (transport) {
      transport.reconnectNow();
    }
  }

  function onPageHide() {
    if (isStale()) {
      return;
    }
    panicRelease(true);
  }

  function onOnline() {
    if (!isStale() && transport) {
      resetCsrfCache();
      transport.reconnectNow();
    }
  }

  function destroy() {
    if (destroyed) {
      return;
    }
    destroyed = true;
    document.removeEventListener("visibilitychange", onVisibility);
    window.removeEventListener("pagehide", onPageHide);
    window.removeEventListener("online", onOnline);
    window.removeEventListener("hashchange", onHash);
    clearPolling();
    panicRelease(true);
    if (surfaceCtl) {
      surfaceCtl.destroy();
      surfaceCtl = null;
    }
    if (transport) {
      transport.destroy();
      transport = null;
    }
  }

  function onHash() {
    if ((window.location.hash || "#/") !== "#/pad") {
      destroy();
    }
  }

  document.addEventListener("visibilitychange", onVisibility);
  window.addEventListener("pagehide", onPageHide);
  window.addEventListener("online", onOnline);
  window.addEventListener("hashchange", onHash);
  padActiveCleanup = destroy;

  if (isStale()) {
    destroy();
    return;
  }

  if (!readPadToken()) {
    conn.hidden = true;
    armBtn.hidden = true;
    status.hidden = true;
    paintPairPanel(content, () => {
      if (isStale()) {
        return;
      }
      conn.hidden = false;
      armBtn.hidden = false;
      status.hidden = false;
      startPad();
    }, isStale);
    return;
  }
  startPad();
}

if (typeof window !== "undefined") {
  window.renderPad = renderPad;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    PAD_GAIN_BASE,
    PAD_GAIN_MAX,
    PAD_GAIN_V0,
    PAD_GAIN_POW,
    PAD_DT_MIN_MS,
    PAD_DT_MAX_MS,
    PAD_MAX_STEP,
    PAD_TAP_MAX_MS,
    PAD_TAP_MAX_PX,
    PAD_TAP2_MAX_PX,
    PAD_SECOND_FINGER_MS,
    PAD_DOUBLE_TAP_GAP_MS,
    PAD_DOUBLE_TAP_GAP_PX,
    PAD_DRAG_START_PX,
    PAD_LONG_PRESS_MS,
    PAD_TWO_FINGER_DECIDE_PX,
    PAD_PINCH_RATIO_MIN,
    PAD_CENTROID_MIN_PX,
    PAD_SCROLL_GAIN,
    PAD_PINCH_NOTCH_LOG2,
    PAD_MOD_LOCK_MS,
    PAD_REST_TIMEOUT_MS,
    PAD_REL_RETRY_MAX,
    PAD_TEXT_MAX,
    PAD_VK,
    PAD_KEY_LEFT,
    PAD_KEY_UP,
    PAD_KEY_DOWN,
    PAD_KEY_RIGHT,
    padPointerGain,
    padAccelerate,
    padAccumulate,
    padTapDecision,
    padDragArm,
    padTwoFingerDecision,
    padScrollDelta,
    padPinchNotches,
    padSplitText,
    padApplyCompositionEnd,
    padApplyInputEvent,
    padShouldSend,
    padAcceptArmResult,
    padOwnsServerGeneration,
    createRestQueue,
    createPadTransport,
    bindSurface,
    renderPad,
  };
}
