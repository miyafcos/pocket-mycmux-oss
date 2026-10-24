"use strict";

const HUB_VIEW_ORIGIN = typeof location === "undefined" ? "https://example.invalid" : location.origin;
const REPORTS_URL = `${HUB_VIEW_ORIGIN}/mobile/`;
const HUB_LEGACY_ORIGINS = Object.freeze([]);
const HUB_URL_RE = /https?:\/\/[^\s"'`<>、。，．)]+/giu;
const WINDOWS_PATH_RE = /[CG]:[\\/](?:[^\r\n"'`<>|?*]*?\.[A-Za-z0-9]{1,12}|[^\s\r\n"'`<>|?*,;:!()\[\]}\u3001\u3002\uff0c\uff0e\uff1a\uff1b\uff01\uff1f\u3009\u300b\u300d\u300f\u3011\uff09]+)(?=$|[\s"'`<>\u3000,.;:!?)\]}\u3001\u3002\uff0c\uff0e\uff1a\uff1b\uff01\uff1f\u3009\u300b\u300d\u300f\u3011\uff09])/giu;
const INTERNAL_FILE_EXTENSIONS = new Set([
  "md",
  "txt",
  "log",
  "json",
  "jsonl",
  "yaml",
  "yml",
  "csv",
  "html",
  "htm",
]);
// Tailscale publishes each machine under its own name and does not fail over
// between them, so the page does it. When this origin stops answering, the
// shell - still served by the service worker - walks over to the other
// machine instead of asking anyone to retype a URL (REQ_mac_support.md R1).
const ENTRY_ORIGINS = Object.freeze([]);
const ENTRY_MACHINE_NAMES = Object.freeze({});
// A static asset, so the probe needs no CSRF token and no CORS header: an
// opaque no-cors response still tells us the machine answered.
const ENTRY_PROBE_PATH = "/pocket/icon-180.png";
const ENTRY_PROBE_TIMEOUT_MS = 4000;
// A machine coming back from sleep drops the first connection, so one
// timeout is not yet an absence.
const ENTRY_PROBE_ATTEMPTS = 2;
const ENTRY_HOP_PARAM = "fo";
const ENTRY_FROM_PARAM = "foFrom";
// One hop only. Two machines that are both unreachable would otherwise send
// the phone back and forth forever.
const ENTRY_HOP_LIMIT = 1;
let entryFailoverRun = null;
let entryBannerNode = null;
const app = typeof document === "undefined" ? null : document.getElementById("app");
const THEME_STORAGE_KEY = "pocket-theme";
const LAST_TALK_STORAGE_KEY = "pocket-last-talk";
const DRAWER_STORAGE_KEY = "pocket-drawer-folds";
let drawerNode = null;
let drawerOpen = false;
let drawerOverview = null;
let drawerMail = null;
let drawerErrors = {};
let drawerRequest = null;
let drawerTimer = null;
const overviewPoller = { snapshot: null, request: null, failure: null, generation: 0, timer: null };
let overviewDetail = null;
const drawerWorkspaces = new Map();
let drawerFolds = null;
let motionRouteSerial = 0;
let incomingMotion = null;
let stopTalkWait = null;
let talkWaitActive = false;
let talkWaitPending = null;
const busySeatMessages = new Map();
let motionHistoryIndex = null;
let motionHistorySerial = 0;
const backScreens = new Map();
const backEntries = [];
let backEntryPosition = -1;
let backGesture = null;
let backSettle = null;
let backCommitted = false;
let cancelBackSwipe = null;
let backSettleCleanup = null;
let lastTalkScrollAt = -Infinity;
let talkFollowUntil = 0;
let talkScrollFrame = 0;
let talkScrollMotion = null;
let lastVisualViewportGeometry = null;

function reducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function motionDuration(name) {
  return parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name)) || 0;
}

let pressedControl = null;
let pressGesture = null;
let pressHoldTimer = null;
const pressReleaseTimers = new WeakMap();
function releasePressedControl() {
  window.clearTimeout(pressGesture?.timer);
  window.clearTimeout(pressHoldTimer);
  pressGesture = null;
  pressHoldTimer = null;
  hidePressedControl();
}

function hidePressedControl() {
  if (!pressedControl) return;
  const control = pressedControl;
  control.classList.add("is-releasing");
  control.classList.remove("is-pressed");
  window.clearTimeout(pressReleaseTimers.get(control));
  pressReleaseTimers.set(control, window.setTimeout(() => {
    control.classList.remove("is-releasing");
    pressReleaseTimers.delete(control);
  }, motionDuration("--motion-release")));
  pressedControl = null;
}

function installPressFeedback() {
  const show = (current) => {
    window.clearTimeout(pressReleaseTimers.get(current.control));
    current.control.classList.remove("is-releasing");
    pressedControl = current.control;
    current.control.classList.add("is-pressed");
    current.shownAt = performance.now();
  };
  const arm = (current) => {
    current.timer = window.setTimeout(() => {
      if (pressGesture === current && current.inside && !current.dragged) show(current);
    }, 60);
  };
  document.addEventListener("pointerdown", (event) => {
    releasePressedControl();
    if (!event.isPrimary || event.button !== 0) return;
    let control = event.target.closest("a[href],button,summary,select,input,textarea,[role=button],[role=link],.tappable,label[for]");
    if (!control || control.matches(":disabled,[aria-disabled=true],.inert") ||
        control.closest("[inert],[aria-disabled=true],.inert")) return;
    if (control.matches(".situation-link")) control = control.closest(".situation-row");
    pressGesture = { control, id: event.pointerId, touch: event.pointerType === "touch",
      x: event.clientX, y: event.clientY, inside: true, dragged: false, shownAt: null, timer: null };
    if (pressGesture.touch) arm(pressGesture);
    else show(pressGesture);
  }, { passive: true });
  document.addEventListener("pointermove", (event) => {
    const current = pressGesture;
    if (!current || current.id !== event.pointerId) return;
    const box = current.control.getBoundingClientRect();
    const inside = event.clientX >= box.left && event.clientX <= box.right &&
      event.clientY >= box.top && event.clientY <= box.bottom;
    if (!inside) {
      current.inside = false;
      window.clearTimeout(current.timer);
      hidePressedControl();
    } else if (!current.inside) {
      current.inside = true; current.dragged = false;
      current.x = event.clientX; current.y = event.clientY;
      if (current.touch) arm(current); else show(current);
    } else if (Math.hypot(event.clientX - current.x, event.clientY - current.y) >= 8) {
      current.dragged = true;
      window.clearTimeout(current.timer);
      hidePressedControl();
    }
  }, { passive: true });
  document.addEventListener("pointerup", (event) => {
    const current = pressGesture;
    if (!current || current.id !== event.pointerId) return;
    window.clearTimeout(current.timer);
    pressGesture = null;
    if (!current.touch || !current.inside || current.dragged) { releasePressedControl(); return; }
    if (pressedControl !== current.control) show(current);
    const remaining = Math.max(0, 80 - (performance.now() - current.shownAt));
    pressHoldTimer = window.setTimeout(releasePressedControl, remaining);
  }, { passive: true });
  document.addEventListener("pointercancel", releasePressedControl, { passive: true });
  // Native link dragging in WebKit can end without a document pointerup.
  document.addEventListener("dragstart", releasePressedControl, true);
  document.addEventListener("dragend", releasePressedControl, true);
  window.addEventListener("mouseup", () => { if (pressHoldTimer === null) releasePressedControl(); }, { passive: true, capture: true });
  document.addEventListener("scroll", releasePressedControl, { passive: true, capture: true });
  window.addEventListener("blur", releasePressedControl);
}

let routeMotionCleanup = null;
let motionSnapshotStyles = null;
function departingScreen(view, back, saved = null) {
  const host = node("div", "motion-outgoing");
  host.inert = true;
  host.setAttribute("aria-hidden", "true");
  const box = app.getBoundingClientRect();
  Object.assign(host.style, { left: `${box.left}px`, top: `${box.top}px`,
    width: `${box.width}px`, height: `${box.height}px` });
  host.style.setProperty("--motion-to", back ? "8%" : "-8%");
  // A closed shadow keeps duplicate snapshot IDs, controls and headings out of
  // live selectors and accessibility, while retaining the shipped stylesheet.
  const shadow = host.attachShadow({ mode: "closed" });
  if (motionSnapshotStyles === null) {
    motionSnapshotStyles = Array.from(document.styleSheets, (sheet) => {
      try { return Array.from(sheet.cssRules, (rule) => rule.cssText).join("\n"); }
      catch (error) { return ""; }
    }).join("\n");
  }
  const style = node("style");
  style.textContent = motionSnapshotStyles + "\n*{animation:none!important;transition:none!important}";
  const frame = node("div");
  frame.id = "app";
  frame.className = saved?.className || app.className;
  const copy = view.cloneNode(true);
  copy.classList.remove("motion-enter");
  copy.style.transform = "none";
  const originals = view.querySelectorAll("input,textarea,select");
  copy.querySelectorAll("input,textarea,select").forEach((el, i) => { el.value = originals[i].value; });
  frame.append(copy);
  shadow.append(style, frame);
  app.before(host);
  frame.scrollTop = saved?.scrollTop ?? app.scrollTop;
  return host;
}

function backDestination() {
  const entry = backEntries[backEntryPosition - 1];
  return entry && backScreens.has(entry.hash) ? entry : null;
}

function rememberBackScreen(view) {
  if (!view || !activeRouteHash) return;
  const copy = view.cloneNode(true);
  const originals = view.querySelectorAll("input,textarea,select");
  copy.querySelectorAll("input,textarea,select").forEach((el, i) => { el.value = originals[i].value; });
  copy.classList.remove("motion-enter");
  copy.style.removeProperty("transform");
  backScreens.delete(activeRouteHash);
  backScreens.set(activeRouteHash, { view: copy, scrollTop: app.scrollTop, className: app.className });
}

function installBackSwipe() {
  let suppressClick = false;
  const clean = (current) => {
    current.animations?.forEach((animation) => animation.cancel());
    current.preview?.remove();
    current.view.style.removeProperty("transform");
    app.classList.remove("back-dragging");
    if (backSettle === current) backSettle = null;
    if (backGesture === current) backGesture = null;
  };
  const start = (event, point, id) => {
    suppressClick = false;
    const destination = backDestination();
    if (drawerOpen || !destination || point.clientX < 0 || point.clientX > 20 || backSettle) return;
    if (!app.contains(event.target)) return;
    backGesture = { x: point.clientX, y: point.clientY, id, destination, hash: activeRouteHash,
      view: app.firstElementChild, width: app.clientWidth, position: 0, axis: null,
      samples: [{ x: point.clientX, time: event.timeStamp }] };
  };
  const move = (event, point) => {
    const current = backGesture;
    if (!current) return;
    const dx = point.clientX - current.x, dy = point.clientY - current.y;
    if (!current.axis) {
      if (Math.abs(dy) >= 10 || dx < -4) { backGesture = null; return; }
      if (dx < 4 || dx <= Math.abs(dy)) return;
      current.axis = "x";
      routeMotionCleanup?.();
      cancelTransientMotion(app);
      releasePressedControl();
      cancelPullRefresh?.();
      if (!reducedMotion()) {
        const saved = backScreens.get(current.destination.hash);
        current.preview = departingScreen(saved.view, true, saved);
        current.preview.classList.add("back-preview");
        app.classList.add("back-dragging");
      }
    }
    if (event.cancelable) event.preventDefault();
    current.position = Math.max(0, Math.min(current.width, dx));
    if (current.preview) {
      const ratio = current.position / current.width;
      current.view.style.transform = `translateX(${current.position}px)`;
      current.preview.style.transform = `translateX(${-0.16 * current.width * (1 - ratio)}px)`;
      current.preview.style.setProperty("--back-shade", String(0.06 * (1 - ratio)));
    }
    current.samples.push({ x: point.clientX, time: event.timeStamp });
    // Keep the point just before the sampling window as well as those inside it.
    while (current.samples.length > 2 && event.timeStamp - current.samples[1].time > 100) current.samples.shift();
  };
  const finish = (event, point, canceled = false) => {
    const current = backGesture;
    if (!current) return;
    backGesture = null;
    if (current.axis !== "x") return;
    suppressClick = true;
    const first = current.samples[0], last = current.samples.at(-1);
    const velocity = event.timeStamp - last.time > 100 ? 0 :
      (point.clientX - first.x) / Math.max(1, event.timeStamp - first.time);
    const commit = !canceled && (current.position >= current.width * 0.35 || velocity >= 0.3);
    const done = () => {
      if ((window.location.hash || "#/") !== current.hash) { clean(current); return; }
      if (commit) {
        // Keep both layers until route() has installed the destination view.
        backSettle = current;
        backCommitted = true;
        const timer = window.setTimeout(() => backSettleCleanup?.(), 1000);
        backSettleCleanup = () => {
          window.clearTimeout(timer);
          backSettleCleanup = null;
          backCommitted = false;
          clean(current);
        };
        history.back();
      } else clean(current);
    };
    if (!current.preview) { done(); return; }
    backSettle = current;
    const ratio = current.position / current.width;
    const duration = motionDuration("--motion-screen") * (commit ? 1 - ratio : ratio);
    const options = { duration, easing: getComputedStyle(document.documentElement).getPropertyValue("--ease-screen").trim(), fill: "forwards" };
    current.animations = [
      current.view.animate([{ transform: current.view.style.transform },
        { transform: `translateX(${commit ? current.width : 0}px)` }], options),
      current.preview.animate([{ transform: current.preview.style.transform },
        { transform: `translateX(${commit ? 0 : -0.16 * current.width}px)` }], options),
      current.preview.animate([{ opacity: 0.06 * (1 - ratio) }, { opacity: commit ? 0 : 0.06 }],
        { ...options, pseudoElement: "::after" }),
    ];
    current.animations[0].finished.then(done, () => clean(current));
  };
  document.addEventListener("touchstart", (event) => {
    if (event.touches.length !== 1) {
      if (backGesture) finish(event, event.touches[0] || { clientX: backGesture.x }, true);
      return;
    }
    start(event, event.touches[0], "touch");
  }, { passive: true, capture: true });
  document.addEventListener("touchmove", (event) => {
    if (backGesture?.id === "touch" && event.touches.length === 1) move(event, event.touches[0]);
  }, { passive: false, capture: true });
  for (const type of ["touchend", "touchcancel"]) document.addEventListener(type, (event) => {
    if (backGesture?.id === "touch") finish(event, event.changedTouches[0] || { clientX: backGesture.x }, type === "touchcancel");
  }, { passive: true });
  document.addEventListener("pointerdown", (event) => {
    if (event.pointerType !== "touch" && event.isPrimary && event.button === 0) start(event, event, event.pointerId);
  });
  document.addEventListener("pointermove", (event) => { if (backGesture?.id === event.pointerId) move(event, event); }, { passive: false });
  for (const type of ["pointerup", "pointercancel"]) document.addEventListener(type, (event) => {
    if (backGesture?.id === event.pointerId) finish(event, event, type === "pointercancel");
  });
  document.addEventListener("click", (event) => {
    if (!suppressClick) return;
    suppressClick = false;
    event.preventDefault(); event.stopImmediatePropagation();
  }, true);
  const cancel = () => {
    backSettleCleanup?.();
    if (backGesture) clean(backGesture);
    if (backSettle) clean(backSettle);
  };
  cancelBackSwipe = cancel;
  window.addEventListener("hashchange", () => { if (!backSettleCleanup) cancel(); });
  window.addEventListener("blur", cancel);
  window.addEventListener("pagehide", cancel);
}

function cancelTransientMotion(root) {
  for (const animation of root.getAnimations({ subtree: true })) {
    if (Number.isFinite(animation.effect.getTiming().iterations)) animation.cancel();
  }
}

function fadeNewGroup(group) {
  if (drawerNode?.classList.contains("is-dragging")) return;
  if (!reducedMotion() && !incomingMotion && group.isConnected) {
    group.animate([{ opacity: 0, transform: "translateY(8px)" }, { opacity: 1, transform: "translateY(0)" }], {
      duration: motionDuration("--motion-list"),
      easing: getComputedStyle(document.documentElement).getPropertyValue("--ease-out").trim(),
    });
  }
}

// Leave children that are already in order attached, including their images,
// focus, pointer feedback and animation state.
function reconcileChildren(parent, children) {
  const wanted = new Set(children);
  for (const child of Array.from(parent.childNodes)) if (!wanted.has(child)) child.remove();
  let cursor = parent.firstChild;
  for (const child of children) {
    if (child === cursor) cursor = cursor.nextSibling;
    else parent.insertBefore(child, cursor);
  }
  while (cursor) {
    const next = cursor.nextSibling;
    cursor.remove();
    cursor = next;
  }
}

const rowPaint = new WeakMap();
const rowContent = new WeakMap();
function cachedPaintRow(cache, key, signature, create) {
  const previous = cache?.get(key);
  if (previous?.signature === signature) return previous.row;
  const row = create();
  cache?.set(key, { signature, row });
  return row;
}
// Group new siblings into one fade; existing rows retain their own paint.
function replaceMotionRows(parent, items, selector, keyFor) {
  const previous = new Map(Array.from(parent.querySelectorAll(selector), (item) => [keyFor(item), item]));
  const next = [];
  const grouped = new Map();
  const freshGroups = [];
  let batch = null;
  let sourceGroup = null;
  let retainedGroup = null;
  for (const item of items) {
    const old = previous.get(keyFor(item));
    // Compare rendering inputs, never a serialized DOM tree. Live clocks and
    // expanded controls keep their existing nodes and update independently.
    const signature = JSON.stringify([rowContent.get(item) ?? item.textContent, item.className,
      rowContent.get(item.querySelector(".progress-lane")), Boolean(item.querySelector(".seat-busy-note"))]);
    const row = old && rowPaint.get(old) === signature ? old : item;
    rowPaint.set(row, signature);
    if (old) {
      batch = null;
      const container = old.parentElement;
      if (container !== parent && container.classList.contains("motion-batch")) {
        if (sourceGroup !== container) {
          retainedGroup = grouped.has(container) ? node("div", "motion-batch") : container;
          grouped.set(retainedGroup, []);
          next.push(retainedGroup);
        }
        sourceGroup = container;
        grouped.get(retainedGroup).push(row);
      } else { next.push(row); sourceGroup = null; }
    } else {
      sourceGroup = null;
      if (!batch) {
        batch = node("div", "motion-batch");
        next.push(batch);
        freshGroups.push(batch);
      }
      batch.append(row);
    }
  }
  for (const [group, rows] of grouped) reconcileChildren(group, rows);
  reconcileChildren(parent, next);
  for (const group of freshGroups) fadeNewGroup(group);
}
const TALK_SORT_STORAGE_KEY = "pocket-talk-sort";
const LAUNCH_WORKSPACE_STORAGE_KEY = "pocket-launch-ws";
const QUICK_TALK_STORAGE_KEY = "pocket-quick-talk";
const QUICK_TERM_STORAGE_KEY = "pocket-quick-term";
const QUICK_COMMAND_LIMIT = 12;
const QUICK_COMMAND_LABEL_LENGTH = 12;
const QUICK_COMMAND_LONG_PRESS_MS = 600;
const DRAFT_TALK_PREFIX = "pocket-draft-talk-";
const DRAFT_TERM_PREFIX = "pocket-draft-term-";
const DRAFT_DEBOUNCE_MS = 500;
const DRAFT_SESSION_LIMIT = 20;
const DRAFT_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000;
const QUICK_BUILTIN_COMMANDS = Object.freeze([
  Object.freeze({
    labelKey: "quickBuiltinLabel",
    bodyKey: "quickBuiltinLabel",
    hintKey: "quickBuiltinHint",
  }),
]);
const IMAGE_ATTACH_MAX_BYTES = 4 * 1024 * 1024;
const IMAGE_ATTACH_MAX_DIMENSION = 2000;
const IMAGE_ATTACH_QUALITY = 0.85;
const IMAGE_ATTACH_DONE_MS = 2000;
// A press on the attach button held this long reads the clipboard instead.
const ATTACH_LONG_PRESS_MS = 500;
const THEME_VALUES = new Set(["light", "dark", "system"]);
const SVG_NS = "http://www.w3.org/2000/svg";
const MAIL_SWIPE_AXIS_THRESHOLD = 12;
const MAIL_SWIPE_OPEN_OFFSET = 152;
const MAIL_SWIPE_SNAP_THRESHOLD = MAIL_SWIPE_OPEN_OFFSET / 2;
const MAIL_SWIPE_RESISTANCE = 0.3;
const MAIL_SWIPE_REMOVE_MS = 200;
const MAIL_HIDE_TOAST_MS = 6000;
const MAIL_SEND_CONFIRM_MS = 5000;
const LAUNCH_TIMEOUT_MS = 60000;
const BOTTOM_SCROLL_THRESHOLD = 4;
const LANE_HISTORY_PREFIX = "pocket-lane-history-";
const LANE_HISTORY_LIMIT = 20;
const LANE_USUAL_MIN_SAMPLES = 3;
const LANE_STALL_MS = 90000;
const LANE_RECORD_LIMIT = 20;
const LANE_SESSION_LIMIT = 20;
const REQUEST_TIMEOUT_MS = 8000;
// Well inside the eight-second abort, so the reader is told while there is
// still a chance the answer arrives on its own.
const SLOW_LOAD_NOTICE_MS = 2500;
// One placeholder row plus the gap that follows it.
const SKELETON_ROW_PITCH = 104;
const POLL_DELAYS_MS = [5000, 8000, 12000, 20000];
let pollFailures = 0;
const pendingScreenRequests = new Map();
let pollCallback = null;
let pollRun = null;
let pollGeneration = 0;
let pollInterval = 5000;
let connectionNode = null;
let connectionHealth = null;
let connectionOffline = false;
let connectionLastSuccess = null;
let connectionStartedAt = Date.now();
let healthRequest = null;
let healthTimer = null;
let lastResumeAt = -Infinity;
let pollTimer = null;
let laneTickTimer = null;
let laneTickResume = null;
let laneRecordSerial = 0;
let routeTimeoutTimer = null;
let routeToken = 0;
// The app may close a drawer it opened. It may not close one the reader
// opened: tapping the control during a slow start had the landing shut it
// again two seconds later and move them somewhere else.
let drawerOpenedByReader = false;
let csrfSessionPromise = null;
let themePreference = "system";
let themeMediaQuery = null;
let staggerRouteKey = null;
let activeRouteHash = null;
const optimisticMessages = new Map();
const sessionDataCache = new Map();
const dataRequestGenerations = new Map();
const observedTalkEvents = new Map();
const claimedTalkEvents = new Map();
const talkSendInFlight = new Map();
let displayedCache = null;
let drawerDisplayedCache = null;
let activeTalkView = null;
// The AskUserQuestion card of the talk screen on show, if any (DAN5A).
let activeAskCard = null;
const ASK_POLL_MS = 3000;
// answer-ask takes one observed step per key; a multiSelect is several.
const ASK_POST_TIMEOUT_MS = 45000;
const SCROLL_PREFIX = "pocket-scroll:";

function savedScroll(key = activeRouteHash) {
  try {
    const raw = window.sessionStorage.getItem(SCROLL_PREFIX + key);
    return raw !== null && Number.isFinite(Number(raw)) ? Number(raw) : null;
  } catch (error) { return null; }
}

function saveScroll(key = activeRouteHash, top = app?.scrollTop) {
  if (!key || !Number.isFinite(top)) return;
  try { window.sessionStorage.setItem(SCROLL_PREFIX + key, String(top)); }
  catch (error) { /* Storage must not block navigation. */ }
}

function restoreScroll(key = activeRouteHash, root = app) {
  const top = savedScroll(key);
  if (top === null || !root) return false;
  root.scrollTo({ top, behavior: "instant" });
  return true;
}

function showCachedData(name, drawer = false) {
  const cached = sessionDataCache.get(name);
  if (!cached) return;
  const state = { name, savedAt: cached.savedAt, token: routeToken };
  if (drawer) drawerDisplayedCache = state;
  else displayedCache = state;
}

// Cold responses have no row metadata. Use static, native row shapes; once
// data is known, the real cached layout is painted immediately instead.
function delayedSkeleton(parent, kind = "talk", count = 3) {
  const token = routeToken;
  const drawer = parent.classList.contains("drawer-lists");
  const placeholder = node("div", "felt-loading", stringFor("loading"));
  placeholder.setAttribute("role", "status");
  placeholder.setAttribute("aria-label", stringFor("loading"));
  parent.replaceChildren(placeholder);
  const timer = window.setTimeout(() => {
    if (!placeholder.isConnected || (!drawer && token !== routeToken)) return;
    placeholder.classList.add("felt-skeleton", `felt-${kind}`);
    placeholder.setAttribute("aria-live", "polite");
    // The status keeps its accessible label without a hidden text line in the layout.
    if (kind === "talk" || kind === "mail") placeholder.textContent = "";
    // A talk has no row count to go on, and three rows left a hole between the
    // top placeholder and the one pinned to the bottom edge. Fill the height
    // instead: both anchors stay where they are and the wait looks like a
    // conversation. A list knows its own count and keeps it.
    let rows = count;
    if (kind === "talk") {
      // Size against the actual placeholder, without the timeline padding.
      // The two anchors leave one flexible gap in place of two normal gaps.
      const gap = parseFloat(getComputedStyle(placeholder).rowGap) || 0;
      const available = placeholder.clientHeight + 2 * gap;
      rows = Math.max(count, Math.min(8, Math.floor(available / SKELETON_ROW_PITCH)));
    }
    for (let index = 0; index < rows; index += 1) {
      const row = node("div", `felt-row felt-row-${index % 2}`);
      row.append(node("span", "felt-line"), node("span", "felt-line"));
      placeholder.append(row);
    }
  }, 150);
  // A request has eight seconds before it is abandoned, and it is retried once,
  // so a slow answer left these grey blocks standing for sixteen seconds with
  // nothing said. Keep waiting - the answer may still come - but stop
  // pretending, and give the reader something to press.
  const slowTimer = window.setTimeout(() => {
    if (!placeholder.isConnected || (!drawer && token !== routeToken)) return;
    if (placeholder.querySelector(".felt-slow")) return;
    const notice = node("div", "felt-slow");
    notice.setAttribute("role", "status");
    notice.append(node("span", "felt-slow-text", stringFor("loadingSlow")));
    const retry = node("button", "felt-slow-retry", stringFor("loadingRetry"));
    retry.type = "button";
    retry.addEventListener("click", () => renderRoute());
    notice.append(retry);
    placeholder.prepend(notice);
  }, SLOW_LOAD_NOTICE_MS);
  return () => {
    window.clearTimeout(timer);
    window.clearTimeout(slowTimer);
    placeholder.remove();
  };
}
const progressLanes = new Map();
const fileMetaCache = new Map();
const expandedEvents = new Set();
const expandedToolGroups = new Set();
let expandedSessionId = null;
let pendingTalkFocusSessionId = null;
let activeQuickCommandCleanup = null;
let activeMailSwipeShell = null;
let mailSwipeRowCounter = 0;
const AVATAR_COLORS = [
  "#365FA8",
  "#6B4FA8",
  "#A85636",
  "#2D7A68",
  "#986B2D",
  "#76527C",
  "#3E6F80",
  "#596474",
];

const ICON_SHAPES = {
  menu: [["path", { d: "M4 6h16M4 12h16M4 18h16" }]],
  close: [["path", { d: "M6 6l12 12M18 6L6 18" }]],
  // Three sliders: the settings row needs a mark like every other row in
  // its card, and a gear at 20 px is a smudge.
  settings: [
    ["path", { d: "M4 7h9M17 7h3M4 17h3M11 17h9" }],
    ["circle", { cx: "15", cy: "7", r: "2.4" }],
    ["circle", { cx: "7", cy: "17", r: "2.4" }],
  ],
  sun: [
    ["circle", { cx: "12", cy: "12", r: "4" }],
    ["path", { d: "M12 2v2M12 20v2M4.93 4.93l1.42 1.42M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.42-1.42M17.66 6.34l1.41-1.41" }],
  ],
  moon: [
    ["path", { d: "M20.5 14.2A8 8 0 0 1 9.8 3.5 8.5 8.5 0 1 0 20.5 14.2z" }],
  ],
  system: [
    ["circle", { cx: "12", cy: "12", r: "3" }],
    ["path", { d: "M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 .6 1.7 1.7 0 0 0-.4 1.1V21h-4v-.09A1.7 1.7 0 0 0 8.6 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-.6-1 1.7 1.7 0 0 0-1.1-.4H3v-4h.09A1.7 1.7 0 0 0 4.6 8.6a1.7 1.7 0 0 0-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-.6 1.7 1.7 0 0 0 .4-1.1V3h4v.09A1.7 1.7 0 0 0 15.4 4.6a1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 9c.13.38.34.72.6 1 .3.25.68.39 1.1.4h.09v4h-.09A1.7 1.7 0 0 0 19.4 15z" }],
  ],
  monitor: [
    ["rect", { x: "3", y: "4", width: "18", height: "13", rx: "2" }],
    ["path", { d: "M8 21h8M12 17v4" }],
  ],
  mail: [
    ["rect", { x: "3", y: "5", width: "18", height: "14", rx: "2" }],
    ["path", { d: "M3 7l9 6 9-6" }],
  ],
  reports: [
    ["path", { d: "M6 3h9l4 4v14H6zM15 3v5h4M9 12h7M9 16h7" }],
  ],
  send: [
    ["path", { d: "M3 11l18-8-8 18-2-8-8-2z" }],
  ],
  image: [
    ["rect", { x: "3", y: "4", width: "18", height: "16", rx: "2" }],
    ["circle", { cx: "8.5", cy: "9", r: "1.5" }],
    ["path", { d: "M4 17l5-5 4 4 2-2 5 5" }],
  ],
  claude: [
    ["path", { d: "M12 3l2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4z" }],
  ],
  codex: [
    ["rect", { x: "3", y: "5", width: "18", height: "14", rx: "2" }],
    ["path", { d: "M7 9l3 3-3 3M12 15h5" }],
  ],
  shell: [
    ["path", { d: "M4 6l6 6-6 6M12 18h8" }],
  ],
  pad: [
    ["rect", { x: "3", y: "5", width: "18", height: "14", rx: "3" }],
    ["circle", { cx: "12", cy: "12", r: "1.5" }],
  ],
  more: [
    ["circle", { cx: "5", cy: "12", r: "1.7", fill: "currentColor", stroke: "none" }],
    ["circle", { cx: "12", cy: "12", r: "1.7", fill: "currentColor", stroke: "none" }],
    ["circle", { cx: "19", cy: "12", r: "1.7", fill: "currentColor", stroke: "none" }],
  ],
};

function stringFor(key, values = {}) {
  let value = Object.prototype.hasOwnProperty.call(window.STRINGS, key)
    ? window.STRINGS[key]
    : key;
  Object.entries(values).forEach(([name, replacement]) => {
    value = value.replaceAll(`{${name}}`, String(replacement));
  });
  return value;
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) {
    element.className = className;
  }
  if (text !== undefined) {
    element.textContent = text;
  }
  const classes = new Set((className || "").split(/\s+/));
  if (["state-message", "session-status", "mail-job-status", "delivery-chip",
    "pad-conn", "pad-error", "pad-pair-status", "pad-pair-waiting",
    "pad-status", "terminal-readonly", "soundness-header-status"].some((name) => classes.has(name))) {
    element.setAttribute("role", "status");
    element.setAttribute("aria-live", "polite");
    element.setAttribute("aria-atomic", "true");
  }
  if (classes.has("pad-mod") && typeof MutationObserver !== "undefined") {
    // pad.js owns modifier behavior; this receiver exposes its three visual states.
    const syncPressed = () => {
      const state = new Set(element.className.split(/\s+/));
      element.setAttribute("aria-pressed",
        state.has("locked") ? "true" : state.has("once") ? "mixed" : "false");
    };
    const observer = new MutationObserver(syncPressed);
    observer.observe(element, { attributes: true, attributeFilter: ["class"] });
  }
  if (classes.has("timeline")) {
    element.setAttribute("role", "log");
    element.setAttribute("aria-live", "polite");
    element.setAttribute("aria-atomic", "false");
    element.setAttribute("aria-relevant", "additions text");
  }
  return element;
}

function inputHitArea(input, extraClass = "") {
  const label = node("label", `soundness-input-hit ${extraClass}`.trim());
  label.append(input);
  return label;
}

function nonemptyText(value, key = "empty") {
  return typeof value === "string" && value.trim() ? value : stringFor(key);
}

function emptyContent(parent, children, key = "empty") {
  parent.replaceChildren(...(children.length ? children : [
    node("div", "state-message compact", stringFor(key)),
  ]));
}

function fetchNotice(parent, error) {
  let notice = parent.querySelector(":scope > .screen-fetch-notice");
  if (!error) {
    if (notice) notice.remove();
    return;
  }
  if (!notice) {
    notice = node("div", "state-message compact error screen-fetch-notice");
    parent.append(notice);
  }
  const text = fetchErrorText(error);
  if (notice.textContent !== text) notice.textContent = text;
}

function formatContextTokens(value) {
  if (value === null || value === undefined) {
    return "";
  }
  const tokens = Number(value);
  if (!Number.isFinite(tokens) || tokens < 0) {
    return "";
  }
  const wholeTokens = Math.floor(tokens);
  return wholeTokens < 1000 ? String(wholeTokens) : `${Math.round(wholeTokens / 1000)}k`;
}

function formatSessionStatus(status) {
  if (!status || typeof status !== "object") {
    return "";
  }
  const parts = [];
  if (typeof status.model === "string" && status.model) {
    const names = typeof window !== "undefined" && window.STRINGS
      ? window.STRINGS.statusModelNames
      : null;
    const label = names && Object.prototype.hasOwnProperty.call(names, status.model)
      ? names[status.model]
      : status.model;
    parts.push(label);
  }
  if (typeof status.effort === "string" && status.effort) {
    parts.push(status.effort);
  }
  const contextTokens = formatContextTokens(status.contextTokens);
  if (contextTokens) {
    parts.push(contextTokens);
  }
  return parts.join(" \u00B7 ");
}

function sessionStatusView(status) {
  const text = formatSessionStatus(status);
  return text ? node("div", "session-status", text) : null;
}

function svgIcon(name, className = "") {
  const svg = document.createElementNS(SVG_NS, "svg");
  if (className) {
    svg.setAttribute("class", className);
  }
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", name === "send" ? "currentColor" : "none");
  svg.setAttribute("stroke", name === "send" ? "none" : "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  (ICON_SHAPES[name] || []).forEach(([tag, attributes]) => {
    const shape = document.createElementNS(SVG_NS, tag);
    Object.entries(attributes).forEach(([key, value]) => shape.setAttribute(key, value));
    svg.append(shape);
  });
  return svg;
}

function readThemePreference() {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    return THEME_VALUES.has(stored) ? stored : "system";
  } catch (error) {
    return "system";
  }
}

function readDataCache(name) {
  return sessionDataCache.get(name)?.data || null;
}

function beginDataRequest(name) {
  const generation = (dataRequestGenerations.get(name) || 0) + 1;
  dataRequestGenerations.set(name, generation);
  return generation;
}

function writeDataCache(name, data, generation = beginDataRequest(name)) {
  if (generation !== dataRequestGenerations.get(name)) return false;
  sessionDataCache.delete(name);
  sessionDataCache.set(name, { data, savedAt: Date.now() });
  while (sessionDataCache.size > 40) sessionDataCache.delete(sessionDataCache.keys().next().value);
  if (drawerDisplayedCache?.name === name) drawerDisplayedCache = null;
  if (displayedCache?.name === name && displayedCache.token === routeToken) {
    displayedCache = null;
  }
  return true;
}

function writeWorkspaceCache(workspaceId, data, generation) {
  if (!writeDataCache(`workspace:${workspaceId}`, data, generation)) return false;
  drawerWorkspaces.set(workspaceId, data);
  return true;
}

function discardDataCache(name) {
  beginDataRequest(name);
  sessionDataCache.delete(name);
}

function syncThemeColor() {
  const meta = document.querySelector('meta[name="theme-color"]');
  if (!meta) {
    return;
  }
  const systemDark = themeMediaQuery ? themeMediaQuery.matches : false;
  const dark = themePreference === "dark" ||
    (themePreference === "system" && systemDark);
  // The ground the screen paints at its bottom edge: on the talk screen that
  // is the composer's surface, which the canvas also wears (style.css, the
  // talk-screen canvas rule), so whatever the shell paints outside the page
  // matches the composer rather than cutting a dark bar under it.
  const talk = Boolean(app && app.querySelector(":scope > .talk-screen"));
  meta.content = dark ? (talk ? "#202833" : "#10141B") : (talk ? "#FFFFFF" : "#E4E9EF");
}

function updateThemeButtons() {
  document.querySelectorAll("[data-theme-choice]").forEach((button) => {
    const selected = button.dataset.themeChoice === themePreference;
    button.classList.toggle("selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
}

function applyThemePreference(value, persist = false) {
  themePreference = THEME_VALUES.has(value) ? value : "system";
  if (themePreference === "system") {
    document.documentElement.removeAttribute("data-theme");
  } else {
    document.documentElement.setAttribute("data-theme", themePreference);
  }
  if (persist) {
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, themePreference);
    } catch (error) {
      // Keep the in-page selection even if storage is unavailable.
    }
  }
  updateThemeButtons();
  syncThemeColor();
}

function themeToggle() {
  const wrapper = node("div", "theme-toggle");
  const track = node("div", "theme-track");
  [
    ["light", "themeLight", "sun"],
    ["dark", "themeDark", "moon"],
    ["system", "themeSystem", "system"],
  ].forEach(([value, labelKey, iconName]) => {
    const label = stringFor(labelKey);
    const button = node("button", "theme-choice");
    button.type = "button";
    button.dataset.themeChoice = value;
    button.title = label;
    button.setAttribute("aria-label", label);
    button.setAttribute("aria-pressed", String(value === themePreference));
    button.classList.toggle("selected", value === themePreference);
    button.append(svgIcon(iconName), node("span", "theme-choice-label", label));
    button.addEventListener("click", () => applyThemePreference(value, true));
    track.append(button);
  });
  wrapper.append(track);
  return wrapper;
}

function routeLink(hash, className) {
  const link = node("a", className);
  link.href = hash;
  return link;
}

function stopLaneTicker() {
  if (laneTickTimer !== null) {
    window.clearInterval(laneTickTimer);
    laneTickTimer = null;
  }
}

function clearLaneTickerState() {
  stopLaneTicker();
  laneTickResume = null;
}

function clearPolling() {
  stopTalkWait?.();
  stopTalkWait = null;
  pollGeneration += 1;
  pollCallback = null;
  pollRun = null;
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }
  if (routeTimeoutTimer !== null) {
    window.clearTimeout(routeTimeoutTimer);
    routeTimeoutTimer = null;
  }
  clearLaneTickerState();
}

function pollingDelay(interval = 5000) {
  return pollFailures ? POLL_DELAYS_MS[pollFailures] : interval;
}

function schedulePolling() {
  if (!pollCallback || talkWaitActive) return;
  if (pollTimer !== null) window.clearTimeout(pollTimer);
  const generation = pollGeneration;
  pollTimer = window.setTimeout(async () => {
    pollTimer = null;
    if (!document.hidden) await refreshPolling();
    if (generation === pollGeneration) schedulePolling();
  }, pollingDelay(pollInterval));
}

function refreshPolling() {
  if (pollRun) return pollRun;
  if (!pollCallback) return Promise.resolve();
  const generation = pollGeneration;
  pollRun = Promise.resolve().then(pollCallback).catch(() => {}).finally(() => {
    if (generation === pollGeneration) pollRun = null;
  });
  return pollRun;
}

function startPolling(callback, interval) {
  clearPolling();
  pollCallback = callback;
  pollInterval = interval;
  schedulePolling();
}

// Keep manual refresh/resume available, but replace its periodic timer while
// the server holds a transcript wait. Old or failing servers resume that timer.
function startTalkWait(sessionId, initialCursor, receive) {
  talkWaitActive = true;
  if (pollTimer !== null) window.clearTimeout(pollTimer);
  pollTimer = null;
  const generation = pollGeneration;
  let stopped = false, supported = true, running = false, pageHidden = false;
  let controller = null, cursor = initialCursor, lifecycleAbort = false;
  const current = () => !stopped && generation === pollGeneration;
  const run = async () => {
    if (!current() || !supported || running || document.hidden || pageHidden) return;
    running = true;
    lifecycleAbort = false;
    controller = new AbortController();
    const signal = controller.signal;
    const watchdog = window.setTimeout(() => controller?.abort(), 30000);
    const started = performance.now();
    const pending = { generation, startedAt: Date.now() };
    talkWaitPending = pending;
    try {
      const query = new URLSearchParams({ timeout: "25" });
      if (cursor !== null) query.set("cursor", String(cursor));
      const response = await fetch(`api/talk/${encodeURIComponent(sessionId)}/wait?${query}`, {
        cache: "no-store", signal,
      });
      if (!response.ok) throw new Error(`talkWait:${response.status}`);
      const data = await response.json();
      if (!Array.isArray(data.events) || typeof data.changed !== "boolean") {
        throw new Error("talkWait:unsupported");
      }
      if (!current() || signal.aborted) return;
      const next = maxTranscriptCursor(data.events);
      if ((data.changed && (next === null || (cursor !== null && next <= cursor))) ||
          (!data.changed && performance.now() - started < 1000)) {
        // An absent transcript or a saturated wait pool answers immediately.
        // Poll normally instead of spinning an empty long-poll loop.
        throw new Error("talkWait:noProgress");
      }
      if (next !== null) cursor = next;
      // A valid primary transcript response is fresh even when no row changed.
      connectionLastSuccess = Date.now();
      renderConnection();
      receive(data);
    } catch (error) {
      if (current() && !lifecycleAbort && !document.hidden && !pageHidden) {
        supported = false;
        talkWaitActive = false;
        schedulePolling();
      }
    } finally {
      if (talkWaitPending === pending) talkWaitPending = null;
      window.clearTimeout(watchdog);
      controller = null;
      running = false;
      if (current() && supported && !document.hidden && !pageHidden) void run();
    }
  };
  const visibility = () => {
    if (document.hidden) { lifecycleAbort = true; controller?.abort(); }
    else void run();
  };
  const hide = () => { pageHidden = true; lifecycleAbort = true; controller?.abort(); };
  const show = () => { pageHidden = false; void run(); };
  document.addEventListener("visibilitychange", visibility);
  window.addEventListener("pagehide", hide);
  window.addEventListener("pageshow", show);
  stopTalkWait = () => {
    stopped = true;
    talkWaitActive = false;
    if (talkWaitPending?.generation === generation) talkWaitPending = null;
    controller?.abort();
    document.removeEventListener("visibilitychange", visibility);
    window.removeEventListener("pagehide", hide);
    window.removeEventListener("pageshow", show);
  };
  void run();
}

function chooseLaunchWorkspace(workspaces, activeWorkspaceId, rememberedWorkspaceId) {
  const ids = (Array.isArray(workspaces) ? workspaces : [])
    .filter((workspace) => workspace && typeof workspace.id === "string" && workspace.id)
    .map((workspace) => workspace.id);
  if (typeof rememberedWorkspaceId === "string" && ids.includes(rememberedWorkspaceId)) {
    return rememberedWorkspaceId;
  }
  if (typeof activeWorkspaceId === "string" && ids.includes(activeWorkspaceId)) {
    return activeWorkspaceId;
  }
  return ids[0] || "";
}

function readLaunchWorkspace() {
  try {
    return window.localStorage.getItem(LAUNCH_WORKSPACE_STORAGE_KEY);
  } catch (error) {
    return null;
  }
}

function rememberLaunchWorkspace(workspaceId) {
  try {
    window.localStorage.setItem(LAUNCH_WORKSPACE_STORAGE_KEY, workspaceId);
  } catch (error) {
    return;
  }
}

function entryMachineName(origin) {
  try {
    const host = new URL(origin).host;
    return ENTRY_MACHINE_NAMES[host] || host;
  } catch (error) {
    return String(origin);
  }
}

function entryPeerOrigin() {
  const current = window.location.origin;
  if (!ENTRY_ORIGINS.includes(current)) {
    return null;
  }
  return ENTRY_ORIGINS.find((origin) => origin !== current) || null;
}

function entryHopCount() {
  const raw = new URLSearchParams(window.location.search).get(ENTRY_HOP_PARAM);
  const value = Number.parseInt(raw || "0", 10);
  return Number.isFinite(value) && value > 0 ? value : 0;
}

async function probeOrigin(origin) {
  // Our own machine answers same-origin, so the status is readable and a
  // gateway error - the server died while the machine stayed up - counts as
  // gone. The other machine has no CORS header, so all we can learn there is
  // whether the connection happened at all.
  const own = origin === window.location.origin;
  for (let attempt = 0; attempt < ENTRY_PROBE_ATTEMPTS; attempt += 1) {
    const controller = new AbortController();
    const timer = window.setTimeout(
      () => controller.abort(),
      ENTRY_PROBE_TIMEOUT_MS,
    );
    try {
      const response = await fetch(origin + ENTRY_PROBE_PATH, {
        mode: own ? "same-origin" : "no-cors",
        cache: "no-store",
        signal: controller.signal,
      });
      if (!own || response.ok) {
        return true;
      }
    } catch (error) {
      // Fall through to the next attempt.
    } finally {
      window.clearTimeout(timer);
    }
  }
  return false;
}

function entryBanner(text, tone) {
  if (entryBannerNode) {
    entryBannerNode.remove();
    entryBannerNode = null;
  }
  const banner = node("div", `entry-banner entry-banner-${tone}`);
  banner.setAttribute("role", "status");
  banner.setAttribute("aria-live", "polite");
  banner.append(node("span", "entry-banner-text", text));
  const close = node("button", "entry-banner-close", "\u00d7");
  close.type = "button";
  close.setAttribute("aria-label", stringFor("entryBannerDismiss"));
  close.addEventListener("click", () => {
    banner.remove();
    entryBannerNode = null;
  });
  banner.append(close);
  document.body.append(banner);
  entryBannerNode = banner;
  return banner;
}

async function runEntryFailover() {
  const peer = entryPeerOrigin();
  if (!peer || entryHopCount() >= ENTRY_HOP_LIMIT) {
    return false;
  }
  if (await probeOrigin(window.location.origin)) {
    return false;
  }
  if (!(await probeOrigin(peer))) {
    entryBanner(stringFor("entryBothDown"), "warn");
    return false;
  }
  const target = new URL(
    window.location.pathname + window.location.search + window.location.hash,
    peer,
  );
  const params = new URLSearchParams(target.search);
  params.set(ENTRY_HOP_PARAM, String(entryHopCount() + 1));
  params.set(ENTRY_FROM_PARAM, new URL(window.location.origin).host);
  target.search = params.toString();
  window.location.replace(target.toString());
  return true;
}

function noteOriginUnreachable() {
  if (entryFailoverRun) {
    return entryFailoverRun;
  }
  entryFailoverRun = runEntryFailover()
    .catch(() => false)
    .then((moved) => {
      if (!moved) {
        entryFailoverRun = null;
      }
      return moved;
    });
  return entryFailoverRun;
}

function announceEntryHop() {
  if (entryHopCount() < 1) {
    return;
  }
  const from = new URLSearchParams(window.location.search).get(
    ENTRY_FROM_PARAM,
  );
  const here = entryMachineName(window.location.origin);
  entryBanner(
    from
      ? stringFor("entrySwitched", {
          from: ENTRY_MACHINE_NAMES[from] || from,
          to: here,
        })
      : stringFor("entryHere", { to: here }),
    "info",
  );
}

function isCurrentScreenData(url) {
  const hash = activeRouteHash || "#/";
  if (hash === "#/" || hash === "#/launch") return url === "api/overview";
  if (hash === "#/mail") return url === "api/mail";
  const match = hash.match(/^#\/(ws|talk|term|mail)\/([^/?#]+)$/);
  return Boolean(match && url.split("?")[0] === `api/${match[1]}/${match[2]}`);
}

async function requestJson(url, options = {}) {
  const requestRoute = routeToken;
  const requestKey = Symbol();
  const method = options.method === undefined ? "GET" : String(options.method).toUpperCase();
  const retryableRead = method === "GET" || method === "HEAD";
  const screenRequest = isCurrentScreenData(url) && (method === "GET");
  const timers = typeof window === "undefined" ? globalThis : window;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    if (screenRequest) pendingScreenRequests.set(requestKey, requestRoute);
    const controller = new AbortController();
    const cancel = () => controller.abort();
    if (options.signal) {
      options.signal.addEventListener("abort", cancel, { once: true });
      if (options.signal.aborted) cancel();
    }
    const timer = timers.setTimeout(cancel, options.timeoutMs || REQUEST_TIMEOUT_MS);
    let response;
    try {
      response = await fetch(url, { cache: "no-store", ...options, method, signal: controller.signal });
      if (!response.ok) {
        const error = new Error(String(response.status));
        error.status = response.status;
        try {
          error.details = await response.json();
        } catch (parseError) {
          error.details = {};
        }
        throw error;
      }
      const value = method === "HEAD" ? null : await response.json();
      pollFailures = 0;
      // Health and auxiliary calls must not make an old list look fresh.
      if (requestRoute === routeToken && isCurrentScreenData(url) &&
          (method === "GET")) {
        connectionLastSuccess = Date.now();
      }
      return value;
    } catch (error) {
      const canceled = options.signal && options.signal.aborted;
      const networkFailure = !error.status && !canceled &&
        (error.name === "AbortError" || error instanceof TypeError);
      if (networkFailure && retryableRead && attempt === 0 && !options.singleAttempt) continue;
      if (!canceled) pollFailures = Math.min(pollFailures + 1, POLL_DELAYS_MS.length - 1);
      if (networkFailure) {
        // Only transport failure triggers the existing two-machine failover.
        error.offline = true;
        noteOriginUnreachable();
      }
      if (!canceled && !String(url).includes("api/fault")) {
        // What the phone could not reach, and what the server said about it.
        // The url is ours and the code is the server's own word for the
        // failure; neither carries anything that was being read or written.
        const code = error.details && error.details.error;
        reportFault("request", `${method} ${String(url).split("?")[0]} -> ` +
          `${error.status || (error.offline ? "offline" : error.name)}` +
          `${code ? " " + code : ""}`);
      }
      throw error;
    } finally {
      pendingScreenRequests.delete(requestKey);
      timers.clearTimeout(timer);
      if (options.signal) options.signal.removeEventListener("abort", cancel);
    }
  }
}

const PATH_NEUTRAL_ERROR_CODES = new Set([
  "bridgeUnavailable", "sessionNotAlive", "termNotReady", "csrfDenied",
]);

// A code whose truth on the way in is not its truth on the way out.
// sessionNotAllowed on a send means the keystroke will not be delivered;
// on a read it means the conversation cannot be opened at all - the tab is
// gone, or two tabs claim the same session id and neither is addressable.
// Falling through to the general wording told the reader to pull a screen
// that would answer 404 to every pull for as long as they kept doing it.
const READ_ERROR_STRINGS = { sessionNotAllowed: "sessionNotOpenable" };

function fetchErrorText(error, mail = false) {
  if (mail) return stringFor("errorFetchMail");
  if (error && error.offline) return stringFor("errorFetchOffline");
  // When the server names the failure, say what it named. The terminal screen
  // was answering a 503 bridgeUnavailable with "could not fetch, pull to
  // refresh" - vague about the cause, and telling the reader to pull a screen
  // that has nothing to pull.
  //
  // Only codes whose wording is true however the request was made. Mapping every
  // code that happens to have a string put "you cannot send to this terminal" on
  // a screen that had failed to *read* one, which is a different thing and not
  // what went wrong. The rest fall through to the general wording, which is
  // vague but never false.
  const code = error && error.details && error.details.error;
  if (code && PATH_NEUTRAL_ERROR_CODES.has(code)) {
    return stringFor(code);
  }
  if (code && READ_ERROR_STRINGS[code]) {
    return stringFor(READ_ERROR_STRINGS[code]);
  }
  const bridge = connectionHealth && connectionHealth.bridge;
  if ((error && error.details && error.details.error === "bridgeBusy") ||
      (bridge && bridge.busy)) return stringFor("errorFetchBusy");
  if (connectionHealth && connectionHealth.mycmux.connected === false) {
    return stringFor("errorFetchNoMycmux");
  }
  return stringFor("errorFetch");
}

function connectionState(now = Date.now()) {
  if (connectionOffline) return "offline";
  if (connectionHealth && connectionHealth.mycmux.connected === false) return "no-mycmux";
  const lastSuccess = connectionLastSuccess ?? connectionStartedAt;
  const waiting = talkWaitPending?.generation === pollGeneration &&
    now - talkWaitPending.startedAt <= 30000 && talkWaitPending.startedAt - lastSuccess <= 12000;
  if (!connectionHealth || !connectionHealth.bridge.ok ||
      (now - lastSuccess > 12000 && !waiting)) return "stale";
  return "ok";
}

function renderConnection() {
  if (!connectionNode) return;
  const state = connectionState();
  const seconds = Math.max(0, Math.floor((Date.now() -
    (connectionLastSuccess ?? connectionStartedAt)) / 1000));
  const keys = {
    ok: "connectionOk", stale: "connectionStale",
    "no-mycmux": "connectionNoMycmux", offline: "connectionOffline",
  };
  // A failure remains visible without moving the header or the scrollport.
  document.body.classList.toggle("connection-is-quiet", state === "ok");
  connectionNode.className =
    `connection-strip connection-${state}${state === "ok" ? " is-quiet" : ""}`;
  connectionNode.textContent = state === "ok" ? "" : stringFor(keys[state], { seconds });
  const feed = connectionHealth && connectionHealth.feed;
  for (const previous of document.querySelectorAll(".feed-degraded")) previous.remove();
  if (!feed) return;
  const messages = feed.connected ? [] : [stringFor("feedDegraded")];
  for (const workspace of (feed && feed.workspaceErrors) || []) {
    messages.push(`${workspace.name}: ${stringFor("workspaceUnavailable")}`);
  }
  if (messages.length) {
    for (const content of document.querySelectorAll(".content, .drawer-panel")) {
      const warning = node("div", "state-message feed-degraded", messages.join(" | "));
      warning.setAttribute("role", "status");
      content.append(warning);
    }
  }
}

function refreshHealth() {
  if (document.hidden) return Promise.resolve();
  if (healthRequest) return healthRequest;
  if (healthTimer !== null) window.clearTimeout(healthTimer);
  healthTimer = null;
  healthRequest = fetchJson("api/health").then((health) => {
    if (!health || health.server !== "ok" || !health.bridge || !health.mycmux ||
        typeof health.bridge.ok !== "boolean" || typeof health.mycmux.connected !== "boolean") {
      throw new Error("invalidHealth");
    }
    connectionHealth = health;
    connectionOffline = false;
  }).catch((error) => {
    connectionHealth = null;
    connectionOffline = Boolean(error.offline);
  }).finally(() => {
    healthRequest = null;
    renderConnection();
    if (!document.hidden) {
      healthTimer = window.setTimeout(() => {
        healthTimer = null;
        void refreshHealth();
      }, pollingDelay());
    }
  });
  return healthRequest;
}

function startConnection() {
  connectionNode = node("div", "connection-strip");
  connectionNode.setAttribute("role", "status");
  connectionNode.setAttribute("aria-live", "polite");
  document.body.insertBefore(connectionNode, app);
  document.body.classList.add("has-connection-strip");
  document.body.style.setProperty("--connection-strip-height", "0px");
  renderConnection();
  void refreshHealth();
  window.setInterval(() => {
    if (document.hidden) return;
    renderConnection();
  }, 5000);
}

// The app told the reader to pull to refresh and nothing was listening. Pulling
// moved the page, because a browser bounces, and then nothing happened - which
// is what "it can be dragged and the movement means nothing" feels like. Either
// the sentence goes or the gesture arrives; the gesture is what a phone list is
// expected to do, so here it is.
const PULL_THRESHOLD = 64;
const PULL_MAX = 96;
let pullState = null;
let cancelPullRefresh = null;

function pullResistance(delta) {
  // Firm at first, then increasingly reluctant, so the end of the travel is felt
  // rather than guessed.
  return Math.min(PULL_MAX, delta * 0.5);
}

function installPullToRefresh(app) {
  if (!app || pullState) return;
  const indicator = node("div", "pull-indicator");
  const label = node("span", "pull-indicator-label", stringFor("pullToRefresh"));
  indicator.append(label);
  indicator.setAttribute("aria-hidden", "true");
  indicator.style.opacity = "0";
  document.body.append(indicator);
  pullState = { active: false, startY: 0, startX: 0, distance: 0, busy: false };

  const paint = (distance, state) => {
    // The content moves, not the whole screen: moving the screen took the
    // header down with it and left a band of nothing above.
    const screen = app.firstElementChild;
    const moving = screen && screen.querySelector(".content");
    if (moving) {
      moving.style.transform = distance ? "translateY(" + distance + "px)" : "";
      moving.style.transition = state === "settle" ? "transform 220ms ease-out" : "";
    }
    indicator.style.opacity = String(Math.min(1, distance / PULL_THRESHOLD));
    indicator.style.transform = "translateY(" + Math.min(distance, PULL_MAX) + "px)";
    indicator.classList.toggle("is-ready", distance >= PULL_THRESHOLD);
    label.textContent = stringFor(
      state === "busy" ? "pullRefreshing"
        : distance >= PULL_THRESHOLD ? "pullRelease" : "pullToRefresh");
  };

  const settle = () => {
    pullState.active = false;
    pullState.distance = 0;
    paint(0, "settle");
    window.setTimeout(() => {
      const screen = app.firstElementChild;
      const moving = screen && screen.querySelector(".content");
      if (moving) moving.style.transition = "";
    }, 240);
  };
  cancelPullRefresh = () => {
    pullState.active = false;
    pullState.distance = 0;
    paint(0, "cancel");
  };
  window.addEventListener("blur", cancelPullRefresh);
  window.addEventListener("pagehide", cancelPullRefresh);

  app.addEventListener("touchstart", (event) => {
    if (pullState.busy || drawerOpen || backGesture || event.touches.length !== 1) return;
    if (app.scrollTop > 0) return;
    pullState.active = true;
    pullState.startY = event.touches[0].clientY;
    pullState.startX = event.touches[0].clientX;
    pullState.distance = 0;
  }, { passive: true });

  app.addEventListener("touchmove", (event) => {
    if (!pullState.active || pullState.busy) return;
    const touch = event.touches[0];
    const dy = touch.clientY - pullState.startY;
    const dx = touch.clientX - pullState.startX;
    // A sideways gesture belongs to whatever is under it - a mail row, a chip
    // row, a path chip - and an upward one is an ordinary scroll.
    if (dy <= 0 || Math.abs(dx) > Math.abs(dy)) {
      if (pullState.distance) settle();
      pullState.active = false;
      return;
    }
    if (app.scrollTop > 0) { pullState.active = false; return; }
    pullState.distance = pullResistance(dy);
    if (event.cancelable) event.preventDefault();
    paint(pullState.distance, "pull");
  }, { passive: false });

  const release = () => {
    if (!pullState.active || pullState.busy) { pullState.active = false; return; }
    const reached = pullState.distance >= PULL_THRESHOLD;
    if (!reached) { settle(); return; }
    pullState.busy = true;
    paint(PULL_THRESHOLD, "busy");
    Promise.resolve()
      .then(() => { pollFailures = 0; return refreshVisiblePage(); })
      .catch(() => {})
      .finally(() => {
        window.setTimeout(() => { pullState.busy = false; settle(); }, 320);
      });
  };
  app.addEventListener("touchend", release, { passive: true });
  app.addEventListener("touchcancel", () => { if (pullState.distance) settle();
    pullState.active = false; }, { passive: true });
}

function refreshVisiblePage() {
  if (document.hidden || Date.now() - lastResumeAt < 250) return;
  lastResumeAt = Date.now();
  renderConnection();
  void refreshHealth();
  const sameRoute = (window.location.hash || "#/") === activeRouteHash;
  if (sameRoute && !pollCallback && [...pendingScreenRequests.values()].includes(routeToken)) return;
  if (!sameRoute || !pollCallback) {
    route();
    return;
  }
  if (pollTimer !== null) window.clearTimeout(pollTimer);
  pollTimer = null;
  const generation = pollGeneration;
  void refreshPolling().finally(() => {
    if (generation === pollGeneration) schedulePolling();
  });
  handleLaneVisibilityChange();
}

function fetchJson(url) {
  return requestJson(url);
}

// Which build this page is, stamped into the shell by the server as the bytes
// left it. Not which build is current: the two are only the same at the moment
// of serving, and the whole difficulty was that a phone can run a shell that
// is days old with nothing on screen or in any log to say so.
const RUNNING_BUILD = (() => {
  // This file is also required by node to test its pure functions, against a
  // document stubbed down to the two or three methods those functions use.
  // There is no meta tag to read there and nothing to read it with.
  if (typeof document === "undefined" || typeof document.querySelector !== "function") {
    return "";
  }
  const meta = document.querySelector('meta[name="pocket-build"]');
  const value = meta ? String(meta.content || "").trim() : "";
  return /^[0-9a-f]{1,16}$/.test(value) ? value : "";
})();
let buildIsStale = false;
const BUILD_RELOAD_KEY = "pocket-build-reloaded";
// How long a stale page waits for the new worker before reloading anyway.
const STALE_RELOAD_FALLBACK_MS = 20000;
let staleReloadFallback = null;

// When a composer field was last typed into; the listener that keeps it is
// installed with the other page listeners at start-up (this file also loads
// under Node for the pure-function tests, where there is no document).
let lastFieldInputAt = -Infinity;

function composerIsBusy() {
  const active = document.activeElement;
  if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA")) {
    return true;
  }
  // Words already in storage come back after the reload, so they do not hold
  // it up - one saved draft anywhere used to keep the phone on an old build
  // indefinitely. What does: text typed a moment ago and not saved yet, and
  // any field that keeps no draft at all.
  if (performance.now() - lastFieldInputAt < DRAFT_DEBOUNCE_MS + 500) {
    return true;
  }
  return Array.from(document.querySelectorAll(".composer-input, .mail-direction"))
    .some((field) => String(field.value || "").trim() && !field.dataset.draftKey);
}

function applyNewBuild() {
  // Nobody's half-written sentence is worth a reload. The draft is saved to
  // storage half a second after it is typed, and half a second of someone's
  // words is exactly what this would throw away.
  if (!buildIsStale || composerIsBusy()) {
    return false;
  }
  // Nor is anybody's reading. A reload throws the whole screen away and draws
  // it again from blank, and on a day with nine builds the phone did that
  // ten times while someone was looking at it (2026-09-14 02:52-06:49, ten
  // stale lines in clients.log). The new shell is taken up when the app is
  // put away - the page is hidden, nobody sees the blank - or at the next
  // launch. The settings row's update button still reloads on the spot.
  if (!document.hidden) {
    return false;
  }
  let attempted = "";
  try { attempted = window.sessionStorage.getItem(BUILD_RELOAD_KEY) || ""; } catch (error) {}
  // Reloading a second time for the same shell would be a loop: if the reload
  // did not bring a newer one, reloading again will not either.
  if (attempted === RUNNING_BUILD) {
    return false;
  }
  try { window.sessionStorage.setItem(BUILD_RELOAD_KEY, RUNNING_BUILD); } catch (error) {}
  buildIsStale = false;
  window.location.reload();
  return true;
}

// What broke, sent back once. Three rounds of "it does not work on the phone"
// had nothing behind them: no console anyone could open, no log anywhere. A
// dozen lines per visit is plenty to say what went wrong; past that the fault
// is repeating and the first one already said it.
const FAULT_LIMIT = 12;
const faultsSeen = new Set();
let faultsSent = 0;

function reportFault(kind, detail) {
  if (typeof window === "undefined" || typeof window.fetch !== "function") {
    return;
  }
  const text = String(detail || "").slice(0, 300);
  const signature = `${kind}:${text}`;
  if (!text || (kind !== "attachment" && (faultsSent >= FAULT_LIMIT || faultsSeen.has(signature)))) {
    return;
  }
  faultsSeen.add(signature);
  faultsSent += 1;
  const body = JSON.stringify({
    kind,
    detail: text,
    route: String(window.location.hash || "#/").slice(0, 80),
    build: RUNNING_BUILD,
  });
  try {
    // keepalive, so a fault on the way out of the page still arrives.
    window.fetch("api/fault", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      keepalive: true,
    }).catch(() => {});
  } catch (error) {
    /* Reporting a fault must never become one. */
  }
}

// What the phone says its screen is, once per load. Four photos of a band the
// page could not paint (2026-09-14, 03:56 to 12:49) had every number in them
// read off pixels; the two that decide the cause - the height the layout is
// given and the height the view actually shows - were never in a log. This
// puts them there. Phones and installed copies only: the suite's Chrome
// counts every fault it sees.
function reportViewportGeometry(tag = "boot") {
  const standalone = navigator.standalone === true ||
    (typeof window.matchMedia === "function" && window.matchMedia("(display-mode: standalone)").matches);
  if (!standalone && !/iPhone|iPad/.test(navigator.userAgent)) return;
  const probe = document.createElement("div");
  probe.setAttribute("aria-hidden", "true");
  probe.style.cssText = "position:fixed;left:0;top:0;width:0;visibility:hidden;pointer-events:none;" +
    "height:env(safe-area-inset-top,0px);margin-bottom:env(safe-area-inset-bottom,0px)";
  document.body.append(probe);
  const rootStyle = window.getComputedStyle(document.documentElement);
  const px = (value) => Math.round(parseFloat(value) || 0);
  const viewport = window.visualViewport;
  // This file's screen() is the page's window.screen; a blank frame still
  // has the real one.
  let display = "-";
  const frame = document.createElement("iframe");
  frame.setAttribute("aria-hidden", "true");
  frame.style.cssText = "position:fixed;width:0;height:0;border:0;visibility:hidden";
  try {
    document.body.append(frame);
    const real = frame.contentWindow.screen;
    display = `${real.height}x${real.width}/${real.availHeight}`;
  } catch (error) {
    display = "-";
  }
  frame.remove();
  const composer = document.querySelector(".composer, .pad-text-bar, .mail-composer");
  const input = composer ? composer.querySelector(".composer-input, textarea, input") : null;
  const rect = (element) => element ? `${Math.round(element.getBoundingClientRect().top)}..${Math.round(element.getBoundingClientRect().bottom)}` : "-";
  const detail = [
    `geo ${tag} st=${standalone ? 1 : 0}`,
    `sh=${display}`,
    `ih=${window.innerHeight} oh=${window.outerHeight} ch=${document.documentElement.clientHeight}`,
    `vv=${viewport ? [viewport.height, viewport.offsetTop, viewport.pageTop, viewport.scale].map((n) => Math.round(n * 100) / 100).join("/") : "-"}`,
    `lb=${Math.round(layoutViewportBottom())} lc=${Number.isFinite(layoutCeiling.bottom) ? Math.round(layoutCeiling.bottom) : "-"}`,
    `sT=${probe.offsetHeight} sB=${px(window.getComputedStyle(probe).marginBottom)}`,
    `sy=${Math.round(window.scrollY)} body=${rect(document.body)} app=${rect(app)}/${app ? Math.round(app.scrollTop) : "-"} comp=${rect(composer)} in=${rect(input)}${input && document.activeElement === input ? "*" : ""}`,
    `kb=${px(rootStyle.getPropertyValue("--visual-keyboard-inset"))} lh=${px(rootStyle.getPropertyValue("--visual-layout-height"))} db=${px(rootStyle.getPropertyValue("--dead-bottom"))}`,
    `dpr=${window.devicePixelRatio}`,
  ].join(" ");
  probe.remove();
  reportFault("boot", detail);
}

function watchForFaults() {
  window.addEventListener("error", (event) => {
    const where = event.filename
      ? ` (${String(event.filename).split("/").pop()}:${event.lineno})`
      : "";
    reportFault("error", `${event.message}${where}`);
  });
  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason;
    reportFault("rejection", reason && reason.message ? reason.message : String(reason));
  });
}

function quotaKindLabel(kind) {
  if (kind === "five_hour") return stringFor("quotaFiveHour");
  if (kind === "weekly_opus") return stringFor("quotaWeeklyOpus");
  return stringFor("quotaWeekly");
}

function formatQuotaReset(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const month = date.getMonth() + 1;
  const day = date.getDate();
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  const now = new Date();
  const sameDay = date.getFullYear() === now.getFullYear()
    && date.getMonth() === now.getMonth()
    && date.getDate() === now.getDate();
  const when = sameDay ? `${hours}:${minutes}` : `${month}/${day} ${hours}:${minutes}`;
  return stringFor("quotaReset", { t: when });
}

function quotaErrorText(account) {
  if (!account || account.ok) return "";
  if (account.error === "no_login") return stringFor("quotaNoLogin");
  if (account.error === "auth") return stringFor("quotaAuth");
  return stringFor("quotaUnavailable");
}

function quotaAccountKey(account) {
  return `${account.product || account.id}:${account.profile_id || account.identity || ""}`;
}

function paintQuotaWindows(row, account) {
  const error = quotaErrorText(account);
  if (error) {
    row.append(node("div", "quota-error", error));
    return;
  }
  (account.windows || []).forEach((window) => {
    const line = node("div", "quota-window");
    const used = window.used_percent == null ? "\u2014" : String(Math.round(window.used_percent));
    line.append(
      node("span", "quota-kind", quotaKindLabel(window.kind)),
      node("span", "quota-used", stringFor("quotaUsed", { n: used })),
      node("span", "quota-reset", formatQuotaReset(window.reset_at)),
    );
    row.append(line);
  });
}

function quotaProductName(account) {
  const key = { cc: "quotaProductClaude", claude: "quotaProductClaude", cx: "quotaProductCodex", codex: "quotaProductCodex", grok: "quotaGrok" }[
    account.product || account.provider || account.id];
  return key ? stringFor(key) : String(account.provider || account.product || account.id || "");
}

function quotaAccountLabel(account) {
  return `${quotaProductName(account)} \u00b7 ${account.identity || stringFor("quotaIdentityUnknown")}`;
}

function paintQuotaAccount(account) {
  const current = Boolean(account.current);
  const row = node("div", `quota-account${current ? " is-current" : " is-switchable"}`);
  row.append(node("span", "quota-radio"));
  const body = node("div", "quota-account-body");
  body.append(node("div", "quota-provider quota-org", quotaProductName(account)));
  const who = node("div", "quota-identity");
  who.append(node("span", "quota-email", account.identity || stringFor("quotaIdentityUnknown")));
  if (current) who.append(node("span", "quota-current", stringFor("quotaCurrent")));
  if (account.plan) who.append(node("span", "quota-plan", String(account.plan)));
  body.append(who);
  if (account.org) body.append(node("div", "quota-org", account.org));
  paintQuotaWindows(body, account);
  if (account.availability_label) body.append(node("div", "quota-limit", account.availability_label));
  const tickets = account.reset_tickets;
  if (tickets && Number.isInteger(tickets.available)) {
    body.append(node("div", "quota-tickets", stringFor("quotaResetCount", { n: tickets.available })));
    if (tickets.available > 0 && account.profile_id && account.email &&
        !account.needs_relogin && quotaCache.payload?.capabilities?.includes("account.use_reset_ticket")) {
      const reset = node("button", "quota-reset-ticket quota-switch-go", stringFor("quotaResetTicket"));
      reset.type = "button"; reset.disabled = quotaActionBusy;
      reset.addEventListener("click", (event) => {
        event.stopPropagation();
        if (window.confirm(stringFor("quotaResetConfirm", { account: quotaAccountLabel(account) }))) void useQuotaResetTicket(account);
      });
      body.append(reset);
    }
  }
  row.append(body);
  if (!current && account.identity && !account.needs_relogin &&
      (!quotaCache.payload?.availability_groups || quotaCache.payload.capabilities?.includes("account.switch.v1"))) {
    const pending = quotaPending === quotaAccountKey(account);
    if (pending) {
      body.append(node("div", "quota-switch-hint",
        `${stringFor("quotaSwitchTargetConfirm", { account: quotaAccountLabel(account) })} ${stringFor("quotaSwitchWarn")}`));
      const actions = node("div", "quota-switch-actions");
      const go = node("button", "quota-switch-go", stringFor("quotaSwitchGo"));
      go.type = "button";
      go.addEventListener("click", (event) => {
        event.stopPropagation();
        switchQuotaNow(account);
      });
      const cancel = node("button", "quota-switch-cancel", stringFor("quotaSwitchCancel"));
      cancel.type = "button";
      cancel.addEventListener("click", (event) => {
        event.stopPropagation();
        quotaPending = null;
        repaintQuota();
      });
      actions.append(go, cancel);
      body.append(actions);
    } else {
      const choose = node("button", "quota-switch-choice quota-switch-go", stringFor("quotaSwitchGo"));
      choose.type = "button"; choose.disabled = quotaActionBusy;
      choose.addEventListener("click", () => {
        quotaPending = quotaAccountKey(account);
        repaintQuota();
      });
      body.append(choose);
    }
  }
  return row;
}

function paintQuotaLogin(product) {
  if (!quotaLogin || quotaLogin.product !== product || quotaLogin.phase === "idle") return null;
  const card = node("div", "quota-login");
  if (quotaLogin.phase === "waiting") {
    card.append(node("div", "quota-login-wait", stringFor("quotaLoginWait")));
    if (quotaLogin.url) {
      const link = node("a", "quota-login-link", stringFor("quotaLoginOpen"));
      link.href = quotaLogin.url;
      link.target = "_blank";
      link.rel = "noopener";
      card.append(link);
    }
    if (quotaLogin.code) {
      card.append(node("div", "quota-login-code", stringFor("quotaLoginCode", { t: quotaLogin.code })));
    }
    return card;
  }
  if (quotaLogin.phase === "done") {
    card.append(node("div", "quota-login-wait", stringFor("quotaLoginDone")));
    return card;
  }
  card.append(node("div", "quota-error", stringFor("quotaSwitchFailed")));
  return card;
}

function paintQuotaPanel(root, payload) {
  const accounts = Array.isArray(payload && payload.accounts) ? payload.accounts : [];
  if (Array.isArray(payload?.availability_groups)) {
    const labels = { active: "\u4f7f\u7528\u4e2d", available: "\u5207\u66ff\u5019\u88dc", near_limit: "\u6b8b\u91cf\u308f\u305a\u304b",
      limited: "\u5236\u9650\u4e2d", unverified: "\u78ba\u8a8d\u304c\u5fc5\u8981" };
    const sections = Object.keys(labels).map((id) => {
      const group = payload.availability_groups.find((item) => item.id === id);
      const block = node("section", `quota-product quota-group-${id}`);
      block.append(node("h3", "quota-name", group?.label || labels[id]));
      const list = accounts.filter((account) => account.availability_group === id)
        .sort((a, b) => (a.availability_order ?? 0) - (b.availability_order ?? 0));
      if (!list.length) block.append(node("div", "quota-note", stringFor("quotaEmptyGroup")));
      list.forEach((account) => block.append(paintQuotaAccount(account)));
      return block;
    });
    const message = node("p", "quota-action-message", quotaActionMessage);
    message.setAttribute("role", "status");
    root.replaceChildren(node("div", "quota-title", stringFor("quotaTitle")), message, ...sections,
      node("div", "quota-note", stringFor("quotaSeatsNote")));
    return;
  }
  const grouped = { cc: [], cx: [], grok: [] };
  accounts.forEach((account) => {
    const id = account.product || account.id;
    if (!grouped[id]) grouped[id] = [];
    grouped[id].push(account);
  });
  const blocks = [];
  ["cc", "cx", "grok"].forEach((id) => {
    const list = grouped[id] || [];
    const block = node("div", "quota-product");
    block.append(node("div", "quota-name", stringFor({
      cc: "quotaCc", cx: "quotaCx", grok: "quotaGrok",
    }[id])));
    if (!list.length) {
      block.append(node("div", "quota-error", stringFor("quotaUnavailable")));
    } else {
      list.forEach((account) => block.append(paintQuotaAccount(account)));
    }
    blocks.push(block);
  });
  const note = node("div", "quota-note", stringFor("quotaSeatsNote"));
  root.replaceChildren(node("div", "quota-title", stringFor("quotaTitle")), ...blocks, note);
}

let quotaCache = { payload: null, at: 0, inflight: null };
let quotaLogin = { phase: "idle", product: null, url: null, code: null };
let quotaPending = null;
let quotaActionBusy = false;
let quotaActionMessage = "";
let quotaLoginTimer = null;
const QUOTA_CLIENT_TTL_MS = 30000;

function repaintQuota() {
  if (!quotaCache.payload) return;
  document.querySelectorAll(".quota-panel").forEach((root) => {
    paintQuotaPanel(root, quotaCache.payload);
  });
}

function quotaActionResultKey(result, expectedProfileId) {
  if (["switched", "already_active"].includes(result.status)) {
    const mismatch = Array.isArray(result.warnings)
      && result.warnings.includes("cli_account.warning.restored_identity_mismatch");
    const verified = typeof expectedProfileId === "string" && expectedProfileId.length > 0
      && result.active_known === true && result.active_profile_id === expectedProfileId && !mismatch;
    return verified ? "quotaSwitched" : "quotaActionUnconfirmed";
  }
  return result.status === "used" ? "quotaResetUsed"
    : result.status === "unconfirmed" ? "quotaActionUnconfirmed" : "quotaActionFailed";
}

function quotaActionResultText(result, expectedProfileId) {
  const warningKeys = {
    active_snapshot_refreshed: "quotaWarningSnapshot",
    unregistered_live_login_saved: "quotaWarningUnregistered",
    restored_identity_mismatch: "quotaWarningIdentity",
    switch_metadata_not_saved: "quotaWarningMetadata",
    live_token_not_saved: "quotaWarningToken",
    live_token_filed_to_owner: "quotaWarningTokenOwner",
    live_token_foreign_unregistered: "quotaWarningForeignToken",
  };
  const warnings = Array.isArray(result.warnings) ? result.warnings : [];
  return [stringFor(quotaActionResultKey(result, expectedProfileId)), ...warnings.map((code) =>
    stringFor(warningKeys[String(code).replace(/^cli_account\.warning\./, "")] || "quotaWarningUnknown"))].join("\n");
}

async function quotaAction(endpoint, body) {
  if (quotaActionBusy) return;
  quotaActionBusy = true; quotaPending = null; quotaActionMessage = "";
  repaintQuota();
  try {
    let result;
    try {
      result = await postJson(endpoint, body);
    } catch (error) {
      quotaActionMessage = stringFor("quotaActionUnconfirmed");
      return;
    }
    quotaActionMessage = quotaActionResultText(result, body.profile_id);
    // A received result and its warnings stay even when only the list re-read fails.
    try {
      quotaCache.payload = await fetchJson("api/quota");
      quotaCache.at = Date.now();
    } catch (error) {
      quotaActionMessage = [quotaActionMessage, stringFor("quotaRefreshFailed")].join("\n");
    }
  } finally {
    quotaActionBusy = false; repaintQuota();
  }
}

function switchQuotaNow(account) {
  return quotaAction("api/quota/switch", {
    product: account.product || account.id, identity: account.identity,
    profile_id: account.profile_id || "",
  });
}

function useQuotaResetTicket(account) {
  return quotaAction("api/quota/reset-ticket", {
    provider: account.provider, profile_id: account.profile_id,
    expected_email: account.email, request_id: crypto.randomUUID(), confirmed: true,
  });
}

function watchQuotaLogin() {
  if (quotaLoginTimer) {
    window.clearInterval(quotaLoginTimer);
    quotaLoginTimer = null;
  }
  if (quotaLogin.phase !== "waiting") return;
  quotaLoginTimer = window.setInterval(() => {
    fetchJson("api/quota/login").then((status) => {
      quotaLogin = status || quotaLogin;
      if (quotaLogin.phase !== "waiting" && quotaLoginTimer) {
        window.clearInterval(quotaLoginTimer);
        quotaLoginTimer = null;
        quotaCache.at = 0;
        document.querySelectorAll(".quota-panel").forEach((root) => refreshQuota(root));
      } else {
        repaintQuota();
      }
    }).catch(() => {});
  }, 2000);
}

function startQuotaLogin(product) {
  postJson("api/quota/login", { product }).then((status) => {
    quotaLogin = status || quotaLogin;
    repaintQuota();
    watchQuotaLogin();
  }).catch(() => {
    repaintQuota();
  });
}

function refreshQuota(root) {
  const now = Date.now();
  if (quotaCache.payload && now - quotaCache.at < QUOTA_CLIENT_TTL_MS) {
    paintQuotaPanel(root, quotaCache.payload);
    return;
  }
  const apply = (payload) => {
    if (!root.isConnected) return;
    if (payload) {
      paintQuotaPanel(root, payload);
      return;
    }
    if (quotaCache.payload) {
      paintQuotaPanel(root, quotaCache.payload);
      return;
    }
    root.replaceChildren(
      node("div", "quota-title", stringFor("quotaTitle")),
      node("div", "quota-error", stringFor("quotaUnavailable")),
    );
  };
  if (quotaCache.inflight) {
    quotaCache.inflight.then((payload) => apply(payload)).catch(() => apply(null));
    return;
  }
  quotaCache.inflight = fetchJson("api/quota").then((payload) => {
    quotaCache.payload = payload;
    quotaCache.at = Date.now();
    quotaCache.inflight = null;
    return payload;
  }).catch((error) => {
    quotaCache.inflight = null;
    throw error;
  });
  quotaCache.inflight.then((payload) => apply(payload)).catch(() => apply(null));
}

function quotaPanel() {
  const root = node("div", "quota-panel");
  if (quotaCache.payload) {
    paintQuotaPanel(root, quotaCache.payload);
  } else {
    root.append(node("div", "quota-loading", stringFor("quotaLoading")));
  }
  refreshQuota(root);
  return root;
}

function buildRow() {
  // Somewhere a person can read the answer to "which one am I running", which
  // until now had no answer anywhere: not on the screen, not in a log.
  const row = node("div", "build-row");
  const label = node("span", "build-label", stringFor("buildLabel"));
  const value = node("code", "build-id", RUNNING_BUILD || stringFor("buildUnknown"));
  row.append(label, value);
  if (buildIsStale) {
    const update = node("button", "build-update", stringFor("buildUpdate"));
    update.type = "button";
    update.addEventListener("click", () => {
      try { window.sessionStorage.removeItem(BUILD_RELOAD_KEY); } catch (error) {}
      window.location.reload();
    });
    row.append(update);
  }
  return row;
}

function noteStaleBuild() {
  if (!("serviceWorker" in navigator)) {
    applyNewBuild();
    return;
  }
  navigator.serviceWorker.getRegistration("/pocket/")
    .then((registration) => {
      if (!registration) {
        applyNewBuild();
        return;
      }
      // The worker's cache is named after the build, so a newer build is a
      // different sw.js and this is what makes the browser notice. The reload
      // waits for that worker to take over: this page's worker answers the
      // shell from its own cache, so reloading before the new one controls the
      // page brought back the same old build - and the once-per-build guard
      // then refused every later attempt. The phone sat on one build all day.
      registration.update().catch(() => {});
      if (staleReloadFallback === null) {
        staleReloadFallback = window.setTimeout(() => {
          staleReloadFallback = null;
          applyNewBuild();
        }, STALE_RELOAD_FALLBACK_MS);
      }
    })
    .catch(() => { applyNewBuild(); });
}

function csrfSession() {
  if (!csrfSessionPromise) {
    // The token call is the one request every start makes, so it carries the
    // build. The server writes down what it heard.
    const query = RUNNING_BUILD ? `?build=${encodeURIComponent(RUNNING_BUILD)}` : "";
    csrfSessionPromise = fetchJson(`api/session${query}`).then((session) => {
      if (session && session.stale) {
        buildIsStale = true;
        noteStaleBuild();
      }
      return session;
    });
  }
  return csrfSessionPromise;
}

function resetCsrfSession() {
  csrfSessionPromise = null;
}

async function mutationHeaders() {
  const session = await csrfSession();
  return {
    "Content-Type": "application/json",
    "X-Pocket-CSRF": session.csrfToken,
    "X-Pocket-Build": RUNNING_BUILD,
  };
}

async function postJson(url, value, options = {}) {
  return requestJson(url, {
    method: "POST",
    headers: await mutationHeaders(),
    body: JSON.stringify(value),
    ...options,
  });
}

async function deleteJson(url) {
  return requestJson(url, {
    method: "DELETE",
    headers: await mutationHeaders(),
    body: "{}",
  });
}

function avatarData(value) {
  const text = String(value || "?").trim() || "?";
  const glyph = Array.from(text)[0];
  let hash = 2166136261;
  Array.from(text).forEach((character) => {
    hash ^= character.codePointAt(0);
    hash = Math.imul(hash, 16777619);
  });
  return { glyph, color: AVATAR_COLORS[(hash >>> 0) % AVATAR_COLORS.length] };
}

function avatar(value, className = "avatar") {
  const data = avatarData(value);
  const mark = node("span", className, data.glyph);
  mark.style.backgroundColor = data.color;
  return mark;
}

function validAgent(value) {
  return ["claude", "codex"].includes(value) ? value : "none";
}

function agentLabel(agent) {
  const normalized = validAgent(agent);
  if (normalized === "claude") {
    return stringFor("launchClaude");
  }
  if (normalized === "codex") {
    return stringFor("launchCodex");
  }
  return "";
}

function agentAvatar(agent, className = "agent-avatar") {
  const normalized = validAgent(agent);
  const mark = node("span", `${className} agent-${normalized}`);
  mark.setAttribute("aria-hidden", "true");
  mark.append(svgIcon(normalized === "none" ? "shell" : normalized));
  return mark;
}

// The mark names the client the row runs, in the letters the usage panel in
// the drawer's settings already uses for it: CC for Claude Code, CX for Codex.
// It was the agent's own icon, and Claude's four-pointed star on a small grey
// circle read as decoration - on a list where every row is Claude it was the
// same star on every row and told nothing apart. Before the icon it was the
// first two letters of the name, "Cl" over "Co", a word cut in half. Shell
// keeps its prompt glyph; a kind without client letters is a word or an
// initial, never a truncation.
const ROW_AGENT_CLIENTS = new Map([
  ["claude", "quotaCc"],
  ["claude-codex", "quotaCc"],
  ["claude-handoff", "quotaCc"],
  ["codex", "quotaCx"],
  ["codex-handoff", "quotaCx"],
]);
const ROW_AGENT_WORDS = new Map([["grok", "G"]]);

// Kinds the machine actually produces, by the name a person would say.
const AGENT_KIND_NAMES = {
  "claude": "launchClaude",
  "codex": "launchCodex",
  "claude-codex": "tabAgentClaudeCodex",
  "grok": "tabAgentGrok",
  "claude-handoff": "tabAgentClaudeHandoff",
  "codex-handoff": "tabAgentCodexHandoff",
};

function agentKindName(kind) {
  // A kind named "constructor" or "toString" would otherwise pull a function
  // off the prototype and hand it to stringFor, which expects a string.
  return Object.prototype.hasOwnProperty.call(AGENT_KIND_NAMES, kind)
    ? stringFor(AGENT_KIND_NAMES[kind])
    : "";
}

function rowAgentAvatar(row) {
  const kind = row.agentKind || (row.kind === "shell" ? "shell" : row.agent);
  const mark = node("span", `agent-avatar agent-row-mark agent-${validAgent(row.agent)}`);
  if (kind === "shell") {
    mark.append(svgIcon("shell"));
  } else {
    const client = ROW_AGENT_CLIENTS.get(kind);
    mark.textContent = client ? stringFor(client) : ROW_AGENT_WORDS.get(kind) || "?";
  }
  // Two letters are not a name to read aloud, and the mark is where the
  // agent's name lives now that the badge beside it is gone. Callers that know
  // a more exact name overwrite both.
  const name = agentKindName(kind) || agentLabel(row.agent);
  if (name) {
    mark.title = name;
    mark.setAttribute("aria-label", name);
  }
  return mark;
}

function displaySeatTitle(row) {
  const canonical = [row?.display_name, row?.displayName].find(
    (value) => typeof value === "string" && value.trim());
  return displayTabTitle(canonical || row?.title || row?.label);
}

function displayTabTitle(title) {
  // Presentation only: preserve names, ids and routes in the source data.
  return String(title || "").replace(/^[\s\u21b3\u2514\u251c\u2500\u2502\u25b6\u25b8\u276f]+/u, "");
}

function decorateAgentHeader(view, title, agent, status, sessionStatus = null) {
  const heading = view.querySelector(".topbar-heading");
  const titleNode = view.querySelector(".topbar-title");
  if (!heading || !titleNode) {
    return;
  }
  titleNode.textContent = displayTabTitle(title) || agentLabel(agent);
  let meta = heading.querySelector(".agent-meta");
  const normalized = validAgent(agent);
  const announcement = heading.querySelector(".soundness-header-status");
  const announcementText = statusText(validStatus(status));
  if (announcement && announcement.textContent !== announcementText) {
    announcement.textContent = announcementText;
  }
  view.classList.remove("agent-claude", "agent-codex", "agent-none");
  view.classList.add(`agent-${normalized}`);
  if (normalized === "none") {
    meta?.remove();
    return;
  }
  if (!meta) {
    meta = node("div", "agent-meta");
    meta.append(node("span", "agent-chip"), node("span", "agent-status-code"),
      node("span", "agent-session-status"));
    heading.append(meta);
  }
  meta.className = `agent-meta agent-${normalized}`;
  const chip = meta.querySelector(".agent-chip");
  if (chip.textContent !== agentLabel(normalized)) chip.textContent = agentLabel(normalized);
  const state = validStatus(status);
  const stateNode = meta.querySelector(".agent-status-code");
  stateNode.className = `agent-status-code ${state}`;
  if (stateNode.textContent !== statusText(state)) stateNode.textContent = statusText(state);
  const modelNode = meta.querySelector(".agent-session-status");
  const modelText = formatSessionStatus(sessionStatus);
  if (modelNode.textContent !== modelText) modelNode.textContent = modelText;
  modelNode.hidden = !modelText;
  meta.title = [agentLabel(normalized), statusText(state), formatSessionStatus(sessionStatus)].filter(Boolean).join(" \u00b7 ");
}

function thinkingIndicator(agent) {
  const normalized = validAgent(agent);
  const item = node("div", `thinking-indicator agent-${normalized}`);
  item.setAttribute("role", "status");
  item.append(agentAvatar(normalized));
  const copy = node("div", "thinking-copy");
  const labelKey = normalized === "codex" ? "thinkingCodex" : "thinkingClaude";
  const dots = node("span", "thinking-dots");
  dots.setAttribute("aria-hidden", "true");
  for (let index = 0; index < 3; index += 1) {
    dots.append(node("span", "thinking-dot"));
  }
  copy.append(node("span", "thinking-label", stringFor(labelKey)), dots);
  item.append(copy);
  return item;
}

function joinWrappedWindowsPaths(text) {
  // A terminal wraps a long C:\\... path onto the next line. The regex does
  // not cross newlines, so the chip became two dead fragments and the tap
  // had nowhere to go.
  const lines = String(text).split("\n");
  const out = [];
  for (let i = 0; i < lines.length; i += 1) {
    let line = lines[i];
    if (/[CG]:[\\/]/.test(line) && !/\.[A-Za-z0-9]{1,12}\s*$/.test(line)) {
      while (i + 1 < lines.length) {
        const next = lines[i + 1];
        if (next == null || /^\s*$/.test(next)) break;
        if (/[CG]:[\\/]/.test(next)) break;
        if (/^\s/.test(next)) break;
        if (
          !/^[\\/]/.test(next)
          && /\s/.test(next)
          && !/\.[A-Za-z0-9]{1,12}\s*$/.test(next)
        ) {
          break;
        }
        line += next.replace(/^\s+/, "");
        i += 1;
        if (/\.[A-Za-z0-9]{1,12}\s*$/.test(line)) break;
      }
    }
    out.push(line);
  }
  return out.join("\n");
}

let hubIndexPromise = null;
function hubIndexByName() {
  if (!hubIndexPromise) {
    hubIndexPromise = fetch(`${HUB_VIEW_ORIGIN}/mobile/search.json`, { cache: "no-store" })
      .then((response) => (response.ok ? response.json() : []))
      .then((entries) => {
        const byName = new Map();
        (Array.isArray(entries) ? entries : []).forEach((entry) => {
          const relative = String((entry && entry.p) || "").replaceAll("\\", "/");
          if (!relative) return;
          const name = relative.split("/").pop();
          if (!name) return;
          const stripped = name.replace(/__[0-9a-f]{12}(\.[A-Za-z0-9]{1,12})$/i, "$1");
          [name, stripped].forEach((key) => {
            if (byName.has(key) && byName.get(key) !== relative) {
              byName.set(key, null);
            } else if (!byName.has(key)) {
              byName.set(key, relative);
            }
          });
        });
        return byName;
      })
      .catch(() => new Map());
  }
  return hubIndexPromise;
}

function hubUrlForFileName(name) {
  if (!name) return Promise.resolve(null);
  return hubIndexByName().then((byName) => {
    const relative = byName.get(name);
    if (!relative) return null;
    const quoted = relative.split("/").map(encodeURIComponent).join("/");
    return `${HUB_VIEW_ORIGIN}/${quoted}`;
  });
}

// The hub's answer, or nothing once the wait has cost more than the answer is
// worth. The index lives on another machine; when that machine is away the
// fetch can hang, and an opened file must not hang with it.
function hubUrlSoon(name, waitMs = 2500) {
  return Promise.race([
    hubUrlForFileName(name),
    new Promise((resolve) => window.setTimeout(() => resolve(null), waitMs)),
  ]);
}

// An HTML file that the hub already shows is read there, the way every other
// hub document is read. This route is where a tap lands when it beat the
// chip's own lookup, so the route takes the same exit. The talk goes back
// into this history slot first: returning from the hub then lands on the
// talk, not on a screen that would only leave again.
function leaveForHub(url, backHash) {
  window.location.replace(backHash);
  window.location.assign(url);
}

function fileBaseName(path) {
  return String(path).replaceAll("\\", "/").split("/").pop() || "";
}

function detectWindowsPaths(text) {
  const matches = [];
  WINDOWS_PATH_RE.lastIndex = 0;
  let match = WINDOWS_PATH_RE.exec(text);
  while (match) {
    matches.push({ path: match[0], index: match.index, end: WINDOWS_PATH_RE.lastIndex });
    match = WINDOWS_PATH_RE.exec(text);
  }
  WINDOWS_PATH_RE.lastIndex = 0;
  return matches;
}

function rewriteHubUrl(value) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch (error) {
    return value;
  }
  const path = parsed.pathname;
  const isHubPath = path === "/mobile" || path.startsWith("/mobile/")
    || path === "/docs" || path.startsWith("/docs/");
  if (!isHubPath) {
    return parsed.href;
  }
  if (parsed.origin === HUB_VIEW_ORIGIN || HUB_LEGACY_ORIGINS.includes(parsed.origin)) {
    return `${HUB_VIEW_ORIGIN}${path}${parsed.search}${parsed.hash}`;
  }
  return parsed.href;
}

function detectHubUrls(text) {
  const matches = [];
  HUB_URL_RE.lastIndex = 0;
  let match = HUB_URL_RE.exec(text);
  while (match) {
    try {
      const parsed = new URL(match[0]);
      if (parsed.origin === HUB_VIEW_ORIGIN && !parsed.username && !parsed.password
          && /^\/(?:docs|mobile)(?:\/|$)/u.test(parsed.pathname)) {
        matches.push({ url: match[0], index: match.index, end: HUB_URL_RE.lastIndex });
      }
    } catch (_) {}
    match = HUB_URL_RE.exec(text);
  }
  HUB_URL_RE.lastIndex = 0;
  return matches;
}

function hubAnchor(url) {
  const link = node("a", "markdown-link", url);
  link.href = rewriteHubUrl(url);
  link.target = "_blank";
  link.rel = "noopener";
  return link;
}

function fileExtension(path) {
  const name = path.replaceAll("\\", "/").split("/").pop() || "";
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
}

function fileApiUrl(path, meta = false) {
  return `api/file${meta ? "/meta" : ""}?path=${encodeURIComponent(path)}`;
}

function formatFileSize(size) {
  if (!Number.isFinite(size) || size < 0) {
    return "";
  }
  if (size < 1024) {
    return `${size} B`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function getFileMeta(path) {
  if (!fileMetaCache.has(path)) {
    fileMetaCache.set(path, fetchJson(fileApiUrl(path, true)));
  }
  return fileMetaCache.get(path);
}

// The age arrives as a float and was printed as it came: "8.8 days have
// passed" is a precision nobody has about an unanswered email, and it reads as
// machine output. Whole days, floored - eight and a bit days is eight days
// gone, not nine - and anything under one is today.
function mailAgeText(ageDays) {
  const days = Number(ageDays);
  if (!Number.isFinite(days) || days < 1) return stringFor("mailAgeToday");
  return stringFor("mailAgeDays", { n: Math.floor(days) });
}

function fileErrorText(error) {
  const status = error && error.status;
  // The code is more precise than the status: a folder used to arrive as 403
  // and read as "you may not have this", which was not true of the folder or
  // of the reader.
  if (error && error.details && error.details.error === "fileIsDirectory") {
    return stringFor("fileIsDirectory");
  }
  if (status === 403) {
    return stringFor("fileDenied");
  }
  if (status === 404) {
    return stringFor("fileNotFound");
  }
  if (status === 413) {
    const size = error.details && Number(error.details.size);
    const megabytes = Number.isFinite(size) ? Math.ceil(size / (1024 * 1024)) : 50;
    return stringFor("fileTooLarge", { n: megabytes });
  }
  if (status === 503) {
    return stringFor("fileSlow");
  }
  return stringFor("errorFetch");
}

// A path is identified by its end, but it wraps from its start. Five lines of
// "C:\Users\example\<a long Japanese company folder>\..." pushed the file name to
// the bottom, and two different files under the same tree looked like the same
// path printed twice. The chip is one line scrolled to its end instead, so the
// name is what you see and the folder is a swipe away. Splitting the path
// across two elements was tried first and rejected: selecting it copied a
// newline into the middle, and a path is copied to be pasted somewhere.
function pathLink(path) {
  const wrapper = node("span", "file-reference");
  const link = node("a", "file-link");
  link.append(node("span", "file-link-text", path));
  const extension = fileExtension(path);
  if (INTERNAL_FILE_EXTENSIONS.has(extension)) {
    link.href = `#/file?path=${encodeURIComponent(path)}`;
    link.addEventListener("click", () => {
      window.sessionStorage.setItem("pocketFileBackHash", window.location.hash || "#/");
    });
  } else {
    link.href = fileApiUrl(path);
    link.target = "_blank";
    link.rel = "noopener";
  }
  link.title = stringFor("fileOpen");
  const meta = node("span", "file-meta");
  if (extension === "html" || extension === "htm") {
    hubUrlForFileName(fileBaseName(path)).then((url) => {
      if (!url) return;
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener";
      if ([stringFor("errorFetch"), stringFor("fileNotFound")].includes(meta.textContent)) meta.textContent = "";
    });
  }
  getFileMeta(path)
    .then((value) => {
      if (!value.exists) {
        meta.textContent = stringFor("fileNotFound");
        return;
      }
      if (value.hubUrl) {
        link.href = value.hubUrl;
        link.target = "_blank";
        link.rel = "noopener";
      }
      meta.textContent = [String(value.kind).toUpperCase(),
        formatFileSize(value.size)].join(stringFor("metaJoin"));
    })
    .catch((error) => {
      if (String(link.href).startsWith(HUB_VIEW_ORIGIN)) {
        return;
      }
      meta.textContent = fileErrorText(error);
      // A folder has nothing to open, so it stops offering. The text stays put
      // and stays selectable - copying a folder path is the point of it.
      if (error && error.details && error.details.error === "fileIsDirectory") {
        link.removeAttribute("href");
        link.removeAttribute("target");
        link.removeAttribute("title");
        link.classList.add("is-directory");
      }
    });
  wrapper.append(link, meta);
  return wrapper;
}

function appendLinkedText(container, text) {
  text = joinWrappedWindowsPaths(text);
  const matches = [
    ...detectWindowsPaths(text).map((match) => ({ ...match, kind: "path" })),
    ...detectHubUrls(text).map((match) => ({ ...match, kind: "hub" })),
  ].sort((a, b) => a.index - b.index);
  let cursor = 0;
  matches.forEach((match) => {
    if (match.index < cursor) {
      return;
    }
    if (match.index > cursor) {
      container.append(document.createTextNode(text.slice(cursor, match.index)));
    }
    if (match.kind === "hub") {
      container.append(hubAnchor(match.url));
    } else {
      container.append(pathLink(match.path));
    }
    cursor = match.end;
  });
  if (cursor < text.length) {
    container.append(document.createTextNode(text.slice(cursor)));
  }
  return container;
}

function header(title, backHash) {
  const bar = node("header", "topbar");
  const toggle = node("button", "drawer-toggle");
  toggle.type = "button";
  toggle.setAttribute("aria-label", stringFor("openDrawer"));
  toggle.setAttribute("aria-controls", "navigation-drawer");
  toggle.setAttribute("aria-expanded", String(drawerOpen));
  toggle.append(svgIcon("menu"));
  toggle.addEventListener("click", () => {
    drawerOpenedByReader = !drawerOpen;
    setDrawerOpen(!drawerOpen);
  });
  const heading = node("div", "topbar-heading");
  heading.append(node("h1", "topbar-title", title),
    node("span", "soundness-header-status"));
  bar.append(toggle, heading, node("div", "topbar-actions"));
  return bar;
}

// The bar above an opened HTML file. The regular bar carries the drawer and a
// two-line title, which is the right furniture for a conversation and the wrong
// furniture for a document: a document wants the screen. This one is as thin
// as a tap target, names the file, and offers the way back to the talk.
function fileHeader(title, backHash) {
  const bar = node("header", "topbar file-topbar");
  const back = node("a", "back-button", stringFor("back"));
  back.href = backHash;
  back.addEventListener("click", (event) => {
    // The talk is one step back in history when the file was opened from it;
    // going back there keeps its scroll position, where a fresh visit would
    // not. A file opened from a pasted link has no such step, and the href
    // still leads home.
    const previous = backEntries[backEntryPosition - 1];
    if (previous && previous.hash === backHash) {
      event.preventDefault();
      history.back();
    }
  });
  const heading = node("div", "topbar-heading");
  heading.append(node("h1", "topbar-title", title));
  bar.append(back, heading, node("div", "topbar-actions"));
  return bar;
}

function screen(title, backHash, options = {}) {
  const view = node("section", "screen screen-enter");
  if (options.className) view.classList.add(options.className);
  if (incomingMotion && !reducedMotion()) {
    view.classList.add("motion-enter");
    view.style.setProperty("--motion-from", incomingMotion.from);
    if (incomingMotion.delay) view.style.animationDelay = `${incomingMotion.delay}ms`;
    if (incomingMotion.duration) view.style.setProperty("--motion-enter-duration", `${incomingMotion.duration}ms`);
  }
  const bar = (options.header || header)(title, backHash);
  view.append(bar);
  if (typeof ResizeObserver !== "undefined") {
    const observer = new ResizeObserver(() => {
      if (!bar.isConnected) { observer.disconnect(); return; }
      document.documentElement.style.setProperty("--drawer-header-height", `${bar.offsetHeight}px`);
    });
    // offsetHeight is a border-box measure. When the connection strip goes
    // quiet the bar trades min-height for top padding at the same content
    // height, and a content-box observer never hears about it - the drawer
    // then opened 8px under the bar at zero inset.
    observer.observe(bar, { box: "border-box" });
  }
  const content = node("div", "content");
  view.append(content);
  return { view, content };
}

function showLoading(content) {
  content.replaceChildren(node("div", "state-message", stringFor("loading")));
}

function showError(content, error) {
  content.replaceChildren(node("div", "state-message error", fetchErrorText(error)));
}

function sectionLabel(key) {
  const localized = {
    sectionChannels: "appTitle", sectionSessions: "groupMycmuxSub",
    sectionLayout: "workspaceHeader", sectionInbox: "mailListTitle",
  };
  return node("div", "section-label", stringFor(localized[key] || key));
}

function launchSectionLabel() {
  const row = node("div", "section-label-row");
  const button = node("button", "launch-open-button", "+");
  button.type = "button";
  button.title = stringFor("launchTitle");
  button.setAttribute("aria-label", stringFor("launchTitle"));
  button.addEventListener("click", () => {
    window.location.hash = "#/launch";
  });
  row.append(sectionLabel("sectionChannels"), button);
  return row;
}

function staggerRows(rows, routeKey) {
  const items = Array.from(rows);
  if (!items.length || staggerRouteKey === routeKey) {
    return;
  }
  staggerRouteKey = routeKey;
  const parent = items[0].parentElement;
  if (parent && !reducedMotion()) parent.classList.add("row-enter");
}

function validStatus(value) {
  return ["working", "attention", "idle", "stopped", "unobserved"].includes(value) ? value : "unknown";
}

function relativeTime(value) {
  if (value === null || value === undefined) {
    return "";
  }
  const timestamp = typeof value === "number" ? value : Date.parse(value);
  if (!Number.isFinite(timestamp)) {
    return "";
  }
  const minutes = Math.max(0, Math.floor((Date.now() - timestamp) / 60000));
  if (minutes < 1) {
    return stringFor("updatedJustNow");
  }
  if (minutes < 60) {
    return stringFor("updatedMinAgo", { n: minutes });
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return stringFor("updatedHourAgo", { n: hours });
  }
  return stringFor("updatedDayAgo", { n: Math.floor(hours / 24) });
}

function calendarDay(value) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) {
    return null;
  }
  return {
    date,
    key: `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`,
  };
}

function dayLabel(value) {
  const parsed = calendarDay(value);
  if (!parsed) {
    return null;
  }
  // The dictionary's relative-day labels are still English. An absolute date
  // is unambiguous until those entries are translated by the dictionary owner.
  return { key: parsed.key, label: parsed.date.toLocaleDateString() };
}

function eventStableKey(event, occurrence = 0) {
  const provided = event && (
    event.cursor || event.eventId || event.event_id || event.uuid || event.id
  );
  if (provided !== null && provided !== undefined && String(provided)) {
    const value = String(provided);
    return `id:${value.length}:${stableTextHash(value)}:${occurrence}`;
  }
  const role = event && event.role === "user" ? "user" : "assistant";
  const timestamp = event && event.ts !== undefined ? String(event.ts) : "";
  const text = event && event.text !== undefined ? String(event.text) : "";
  const tools = event && Number.isFinite(Number(event.toolCount))
    ? String(Math.max(0, Math.floor(Number(event.toolCount))))
    : "0";
  return `event:${role}:${timestamp}:${tools}:${text.length}:${stableTextHash(text)}:${occurrence}`;
}

function stableTextHash(value) {
  let hash = 2166136261;
  for (const character of String(value)) {
    hash ^= character.codePointAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

function stateTupleKey(...values) {
  return JSON.stringify(values.map((value) => String(value)));
}

function eventDayKey(event) {
  const date = new Date(event && event.ts);
  if (!Number.isFinite(date.getTime())) {
    return "";
  }
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

function groupTimelineEvents(events) {
  const grouped = [];
  const occurrences = new Map();
  (Array.isArray(events) ? events : []).forEach((event) => {
    const baseKey = eventStableKey(event);
    const occurrence = occurrences.get(baseKey) || 0;
    occurrences.set(baseKey, occurrence + 1);
    const key = eventStableKey(event, occurrence);
    const text = typeof event.text === "string" ? event.text : "";
    const toolCount = Math.max(0, Math.floor(Number(event.toolCount) || 0));
    const toolOnly = event.role === "assistant" && !text.trim() && toolCount > 0;
    const previous = grouped[grouped.length - 1];
    if (toolOnly && previous && previous.kind === "tool-group" &&
        previous.dayKey === eventDayKey(event)) {
      previous.events.push({ event, key, toolCount });
      previous.toolCount += toolCount;
    } else if (toolOnly) {
      grouped.push({
        kind: "tool-group",
        events: [{ event, key, toolCount }],
        dayKey: eventDayKey(event),
        key: `tools:${key}`,
        toolCount,
      });
    } else {
      grouped.push({ kind: "event", event, key });
    }
  });
  return grouped;
}

function toolGroupRow(group, sessionId) {
  const stateKey = stateTupleKey(sessionId, group.key);
  const item = node("div", "tool-group");
  const toggle = node("button", "tool-group-toggle");
  toggle.type = "button";
  // "x1" is a count that compresses nothing and reads as machine output; the
  // label exists to stand in for several runs, so one run just says so.
  const label = node("span", "tool-group-label", group.toolCount === 1
    ? stringFor("toolGroupOne")
    : stringFor("toolGroup", { n: group.toolCount }));
  const action = node("span", "tool-group-action", stringFor("toolGroupOpen"));
  toggle.append(label, action);
  const details = node("div", "tool-group-details");
  group.events.forEach((entry, index) => {
    details.append(
      node(
        "div",
        "tool-group-detail",
        `${index + 1}. ${stringFor("toolRuns", { n: entry.toolCount })}`
      )
    );
  });
  const setOpen = (open) => {
    details.hidden = !open;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) {
      expandedToolGroups.add(stateKey);
    } else {
      expandedToolGroups.delete(stateKey);
    }
  };
  setOpen(expandedToolGroups.has(stateKey));
  toggle.addEventListener("click", () => setOpen(details.hidden));
  item.append(toggle, details);
  return item;
}

function eventPaintSignature(event, agent) {
  return JSON.stringify([agent, event.role, event.text, event.toolCount, relativeTime(event.ts)]);
}

function timelineBubbles(events, sessionId, agent = "claude") {
  const items = [];
  let previousDay = null;
  groupTimelineEvents(events).forEach((entry) => {
    const event = entry.kind === "tool-group" ? entry.events[0].event : entry.event;
    const day = dayLabel(event.ts);
    if (day && day.key !== previousDay) {
      items.push(node("div", "day-separator", day.label));
      previousDay = day.key;
    }
    const stateKey = stateTupleKey(sessionId, entry.key);
    const item = entry.kind === "tool-group"
      ? toolGroupRow(entry, sessionId)
      : eventBubble(entry.event, stateKey, agent);
    item.dataset.timelineKey = stateKey;
    rowContent.set(item, entry.kind === "tool-group"
      ? JSON.stringify([entry.toolCount, entry.events.map(({ toolCount }) => toolCount)])
      : eventPaintSignature(entry.event, agent));
    items.push(item);
  });
  return items;
}

function captureTimelineAnchors(timeline) {
  return Array.from(timeline.querySelectorAll("[data-timeline-key]"))
    .filter((item) => item.dataset.timelineKey)
    .map((item) => ({ item, rect: item.getBoundingClientRect() }))
    .filter(({ rect }) => rect.bottom > 0 && rect.top < window.innerHeight)
    .map(({ item, rect }) => ({ key: item.dataset.timelineKey, top: rect.top }));
}

function restoreTimelineAnchor(timeline, anchors) {
  for (const anchor of anchors) {
    const target = Array.from(timeline.querySelectorAll("[data-timeline-key]"))
      .find((item) => item.dataset.timelineKey === anchor.key);
    if (target) {
      const delta = target.getBoundingClientRect().top - anchor.top;
      if (delta) {
        if (!drawerOpen) (app || window).scrollBy(0, delta);
      }
      return;
    }
  }
}

function statusText(status) {
  const names = {
    working: "statusWorking",
    idle: "statusIdle",
    attention: "statusAttention",
    stopped: "statusStopped",
    unknown: "statusUnknown",
    unobserved: "statusUnobserved",
  };
  return stringFor(names[status] || "statusUnknown");
}

function groupRow(titleKey, subKey, options = {}) {
  const row = options.href
    ? routeLink(options.href, "group-row tappable")
    : node("a", "group-row tappable");
  if (options.external) {
    row.href = options.external;
  }
  if (options.status) {
    row.classList.add(validStatus(options.status));
  }
  const mark = node("span", `group-mark ${options.mark || ""}`);
  if (options.icon) {
    mark.append(svgIcon(options.icon));
  }
  const copy = node("span", "group-copy");
  copy.append(node("strong", "group-title", stringFor(titleKey)));
  copy.append(node("span", "group-subtitle", stringFor(subKey, options.values)));
  if (options.urgent) {
    row.classList.add("urgent");
  }
  if (options.badge !== undefined) {
    row.append(mark, copy, node("span", "count-badge",
      options.urgent ? stringFor("urgent") : options.badge));
  } else {
    row.append(mark, copy);
  }
  row.append(node("span", "chevron"));
  return row;
}

function layoutColumnCount(workspace) {
  const columns = workspace && workspace.layout && workspace.layout.splitColumns;
  if (Array.isArray(columns) && columns.length) {
    const api = workspace.layout.api;
    return api && (api.major > 1 || (api.major === 1 && api.minor >= 1))
      ? new Set(columns.flat()).size : columns.length;
  }
  const panes = workspace && workspace.panes;
  return Array.isArray(panes) ? panes.length : 0;
}

function layoutPlacementCount(workspace) {
  if (Array.isArray(workspace?.panes)) return workspace.panes.length;
  return Number.isInteger(workspace?.paneCount) ? workspace.paneCount : 0;
}

function layoutPanePosition(pane) {
  if (typeof pane.column !== "number") return "";
  return typeof pane.row === "number" ? `${pane.column}-${pane.row}` : String(pane.column);
}

// One row of the layout view. The row keeps its position no matter what the
// status is: the whole point of this screen is that "third from the top" stays
// third from the top, so nothing here may reorder on state.
function layoutPaneRow(pane) {
  const status = displayStatus(pane);
  const agent = validAgent(pane.agent);
  const linkable = pane.lifecycle !== "declared" && !pane.duplicateId && pane.linkable !== false && typeof pane.claudeSessionId === "string" && pane.claudeSessionId;
  const className = `layout-row ${status} agent-${agent}${linkable ? " tappable" : " inert"}`;
  const row = linkable
    ? routeLink(`#/talk/${encodeURIComponent(pane.claudeSessionId)}`, className)
    : node("div", className);

  const position = node("span", "layout-position");
  if (typeof pane.column === "number") {
    position.textContent = stringFor("layoutColumn", { n: layoutPanePosition(pane) });
  }
  row.dataset.tabCount = String(pane.tabCount || 0);
  row.append(position, rowAgentAvatar(pane));

  const copy = node("span", "layout-copy");
  copy.append(node("strong", "layout-title", displayTabTitle(pane.title)));
  if (!linkable) {
    copy.append(node("span", "layout-note", stringFor("layoutNoSession")));
  }
  row.append(copy);

  row.append(node("span", "pane-count", stringFor("paneCount", { n: pane.tabCount || 0 })));
  appendTabBadges(copy, pane);
  row.append(node("span", `status-pill ${status}`, statusText(status)));
  if (linkable) {
    row.append(node("span", "chevron"));
  }
  return row;
}

function layoutWorkspaceHeader(workspace) {
  const row = routeLink(
    `#/ws/${encodeURIComponent(workspace.id)}`,
    "layout-ws-row tappable"
  );
  const copy = node("span", "layout-ws-copy");
  copy.append(node("strong", "layout-ws-name", workspace.name));
  copy.append(
    node(
      "span",
      "layout-ws-meta",
      stringFor("layoutWorkspaceMeta", { n: layoutPlacementCount(workspace) })
    )
  );
  appendWorkspaceStatus(copy, workspace);
  row.append(copy, node("span", "chevron"));
  return row;
}

function readDrawerStorage(key) {
  try { return window.localStorage.getItem(key); } catch (error) { return null; }
}

function writeDrawerStorage(key, value) {
  try { window.localStorage.setItem(key, value); } catch (error) {}
}

function drawerWorkspaceData(workspace) {
  const loaded = drawerWorkspaces.get(workspace.id);
  if (connectionHealth?.feed && overviewPoller.generation) {
    const panes = workspace.panes || [];
    const fresh = currentOverview(Math.min(5000, overviewPollingCadence()));
    const extras = new Map((loaded?.tabs || []).map((row) => [tabRoute(row), row]));
    const tabs = panes.flatMap((pane) => pane.tabs || []).map((tab) => {
      const extra = tabRoute(tab) ? extras.get(tabRoute(tab)) : null;
      return { ...tab, snippet: extra?.snippet ?? tab.snippet, subtitle: extra?.subtitle ?? tab.subtitle,
        status: fresh ? displayStatus(tab) : "unobserved",
        agentStatusStale: false, screenObserved: true, stale: false };
    });
    return { workspace, panes, tabs, bridgeCounts: workspace.bridgeCounts };
  }
  if (loaded) return loaded;
  const panes = Array.isArray(workspace.panes) ? workspace.panes : [];
  return { workspace, panes, tabs: panes.flatMap((pane) => pane.tabs || []),
    bridgeCounts: workspace.bridgeCounts };
}

function drawerLandingRoute() {
  const rows = (drawerOverview?.workspaces || []).flatMap((workspace) =>
    sortedWorkspaceTabs(drawerWorkspaceData(workspace)));
  const last = readDrawerStorage(LAST_TALK_STORAGE_KEY);
  const remembered = last && (last.startsWith("term:")
    ? `#/term/${encodeURIComponent(last.slice(5))}` : `#/talk/${encodeURIComponent(last)}`);
  if (remembered && rows.some((row) => tabRoute(row) === remembered)) return remembered;
  const rank = { attention: 6, working: 5, stopped: 4, idle: 3, unobserved: 2, unknown: 1 };
  let best = null;
  rows.forEach((row) => {
    if (tabRoute(row) && (!best || rank[displayStatus(row)] > rank[displayStatus(best)])) best = row;
  });
  return best ? tabRoute(best) : null;
}

function paintDrawer() {
  if (!drawerNode) return;
  const data = drawerOverview;
  const mail = drawerMail;
  const errors = drawerErrors;
  if (!data && !errors.overview) {
    const region = drawerNode.querySelector(".drawer-lists");
    if (drawerOpen && !region.querySelector(".felt-loading")) delayedSkeleton(region, "list");
    return;
  }
  const workspacesData = Array.isArray(data?.workspaces) ? data.workspaces : [];
  const mailData = Array.isArray(mail) ? mail : [];
  const groups = node("div", "group-list");
  const workspaces = node("div", "workspace-list drawer-workspaces");
  const current = window.location.hash === "#/" || !window.location.hash
    ? drawerLandingRoute() : window.location.hash;
  workspacesData.forEach((workspace, index) => {
    const section = node("section", "drawer-workspace");
    const heading = node("div", "drawer-workspace-heading");
    const link = layoutWorkspaceHeader(workspace);
    link.querySelectorAll(".status-pill").forEach((badge) => badge.remove());
    const currentWorkspace = sortedWorkspaceTabs(drawerWorkspaceData(workspace))
      .some((tab) => tabRoute(tab) === current) ||
      [ `#/w/${encodeURIComponent(workspace.id)}`, `#/ws/${encodeURIComponent(workspace.id)}` ]
        .includes(current);
    const expanded = Object.prototype.hasOwnProperty.call(drawerFolds, workspace.id)
      ? drawerFolds[workspace.id] : currentWorkspace;
    // A hyphen and a plus sign are what a keyboard has, not what a control
    // looks like. The chevron points down when the list is folded and up when
    // it is open - not right and down, because right is what every row in this
    // panel uses for "goes somewhere" and the heading beside this control has
    // one of those.
    const toggle = node("button", "drawer-fold");
    toggle.append(node("span", "chevron"));
    toggle.classList.toggle("is-expanded", Boolean(expanded));
    toggle.type = "button";
    toggle.setAttribute("aria-label", stringFor("drawerWorkspaceToggle", { name: workspace.name }));
    toggle.setAttribute("aria-expanded", String(expanded));
    toggle.setAttribute("aria-controls", `drawer-tabs-${index}`);
    toggle.addEventListener("click", () => {
      drawerFolds[workspace.id] = !expanded;
      writeDrawerStorage(DRAWER_STORAGE_KEY, JSON.stringify(drawerFolds));
      paintDrawer();
      drawerNode.querySelector(`#drawer-tabs-${index}`).previousElementSibling
        .querySelector(".drawer-fold").focus({ preventScroll: true });
    });
    heading.append(link, toggle);
    const list = workspaceListView(drawerWorkspaceData(workspace), readTalkSort(), () => paintDrawer(), false, true);
    list.id = `drawer-tabs-${index}`;
    list.hidden = !expanded;
    const sortControl = list.querySelector(".soundness-sort-hit");
    if (sortControl) heading.insertBefore(sortControl, toggle);
    const rows = sortedWorkspaceTabs(drawerWorkspaceData(workspace), readTalkSort());
    list.querySelectorAll(".talk-row").forEach((row, rowIndex) => {
      const tab = rows[rowIndex];
      const target = tabRoute(tab);
      if (target) {
        row.dataset.drawerRoute = target;
        // Also close when the selected route is already the current hash.
        row.addEventListener("click", () => setDrawerOpen(false, false));
        row.addEventListener("keydown", (event) => {
          if (event.key === "Enter" && event.target === row) setDrawerOpen(false, false);
        });
      }
      // Compressing the row dropped every badge, including the count of things
      // waiting on this tab. That number is the reason to open the drawer at
      // all, so it comes back while the rest stays trimmed.
      if (tab && tab.attentionCount > 0 && !row.querySelector(".attention-count")) {
        row.append(node("span", "attention-count",
          stringFor("attentionCount", { n: tab.attentionCount })));
      }
      if (target && target === current) {
        row.setAttribute("aria-current", "page");
        // Compressing the rows removed the only visible mark on the row you are
        // on, leaving background colour to carry it alone - which is exactly
        // what this project decided not to rely on. The label is back, small.
        row.append(node("span", "drawer-current", stringFor("currentTalk")));
      }
    });
    if (!rows.length) list.querySelector(".talk-list").append(node("div", "state-message", stringFor("empty")));
    if (drawerErrors[workspace.id]) list.prepend(node("div", "state-message error", fetchErrorText(drawerErrors[workspace.id])));
    section.append(heading, list);
    workspaces.append(section);
  });
  suppressUniformBadges(workspaces.querySelectorAll(".workspace-tab-list:not([hidden]) .talk-row"),
    ROW_BADGE_SELECTOR, ROW_MAJORITY_SELECTOR);
  if (errors.overview) workspaces.prepend(node("div", "state-message error", fetchErrorText(errors.overview)));
  else if (!data) showLoading(workspaces);
  else if (!workspacesData.length) workspaces.append(node("div", "state-message", stringFor("empty")));
  const firstWorkspace = workspacesData[0];
  groups.append(groupRow("groupMycmux", "groupMycmuxSub", {
    href: firstWorkspace ? `#/ws/${encodeURIComponent(firstWorkspace.id)}` : "#/",
    mark: "mycmux", icon: "monitor",
    status: workspacesData.some((workspace) => workspace.workingCount) ? "working" : "idle",
  }));
  // Mail and research reach helpers that only exist on the Windows machine.
  // A server that lacks them says so, and the row is left out rather than
  // shown as something that always fails. A server that predates this field
  // is treated as having everything, which is what it had.
  const features = (data && data.features) || {};
  const has = (name) => features[name] !== false;
  if (has("mail")) {
    groups.append(
      groupRow("groupMail", errors.mail ? "errorFetchMail" :
        (mail === null ? "loading" : "groupMailSub"), {
        href: "#/mail",
        mark: "mail",
        icon: "mail",
        values: { n: mailData.length },
        badge: errors.mail ? undefined : mailData.length,
        urgent: mailData.some((thread) => thread.urgent),
        // Unreplied is a count; urgency is the state that needs attention.
        status: mailData.some((thread) => thread.urgent) ? "attention" : "idle",
      })
    );
  }
  if (data && data.padEnabled) {
    groups.append(groupRow("groupPad", "groupPadSub", {
      href: "#/pad", mark: "pad", icon: "pad", status: "idle",
    }));
  }
  if (has("research")) {
    groups.append(
      groupRow("groupReports", "groupReportsSub", {
        external: REPORTS_URL,
        mark: "reports",
        icon: "reports",
      })
    );
  }
  // Four rows in one card had been three different species: the three above
  // carry a round mark, two lines of copy and a chevron; this one carried a bare
  // icon and one line, and the settings row was a browser-default disclosure
  // triangle. Same card, same row.
  const launch = groupRow("launchTitle", "launchSubtitle",
    { href: "#/launch", mark: "launch", icon: "send" });
  launch.classList.add("drawer-launch");
  groups.append(launch);
  const settings = node("details", "drawer-settings");
  const summary = node("summary", "group-row tappable");
  const settingsMark = node("span", "group-mark settings");
  settingsMark.append(svgIcon("settings"));
  const settingsCopy = node("span", "group-copy");
  settingsCopy.append(node("strong", "group-title", stringFor("drawerSettings")),
    node("span", "group-subtitle", stringFor("drawerSettingsSubtitle")));
  summary.append(settingsMark, settingsCopy, node("span", "chevron"));
  settings.append(summary, quotaPanel(), themeToggle(), buildRow());
  groups.append(settings);
  const region = drawerNode.querySelector(".drawer-lists");
  const panel = drawerNode.querySelector(".drawer-panel");
  const scrollTop = panel.scrollTop;
  const oldRows = new Map(Array.from(region.querySelectorAll("[data-drawer-route]"),
    (row) => [row.dataset.drawerRoute, row]));
  const newGroups = [];
  for (const list of workspaces.querySelectorAll(".talk-list")) {
    let batch = null;
    for (const row of Array.from(list.children)) {
      const old = oldRows.get(row.dataset.drawerRoute);
      if (!old && row.dataset.drawerRoute) {
        if (!batch) {
          batch = node("div", "motion-batch");
          row.before(batch);
          newGroups.push(batch);
        }
        batch.append(row);
      } else batch = null;
    }
  }
  for (const row of workspaces.querySelectorAll("[data-drawer-route]")) {
    const old = oldRows.get(row.dataset.drawerRoute);
    if (old && old.isEqualNode(row)) row.replaceWith(old);
  }
  settings.open = Boolean(region.querySelector(".drawer-settings")?.open);
  region.replaceChildren(workspaces, groups);
  panel.scrollTop = scrollTop;
  scheduleListBadges();
  if (drawerOpen) {
    for (const group of newGroups) {
      if (!group.closest("[hidden]")) fadeNewGroup(group);
    }
  }
}

async function refreshDrawer() {
  if (drawerRequest) return drawerRequest;
  const firstOverview = !drawerOverview;
  const overviewRequest = sharedOverview(5000).then(async (data) => {
    delete drawerErrors.overview;
    paintDrawer();
    if (drawerOpen && firstOverview) restoreScroll("drawer", drawerNode.querySelector(".drawer-panel"));
    await Promise.allSettled((data.workspaces || []).map(async (workspace) => {
      const generation = beginDataRequest(`workspace:${workspace.id}`);
      try {
        const details = await fetchJson(`api/ws/${encodeURIComponent(workspace.id)}`);
        if (Array.isArray(details.tabs) || Array.isArray(details.talks)) {
          if (!writeWorkspaceCache(workspace.id, details, generation)) return;
        }
        delete drawerErrors[workspace.id];
      } catch (error) {
        if (generation === dataRequestGenerations.get(`workspace:${workspace.id}`)) {
          drawerErrors[workspace.id] = error;
        }
      }
    }));
    paintDrawer();
  }).catch((error) => { drawerErrors.overview = error; paintDrawer(); });
  const mailRequest = fetchJson("api/mail").then((data) => {
    drawerMail = data;
    delete drawerErrors.mail;
    paintDrawer();
  }).catch((error) => { drawerErrors.mail = error; paintDrawer(); });
  drawerRequest = Promise.allSettled([overviewRequest, mailRequest]).then(() => {
    if (!drawerErrors.overview && !drawerErrors.mail) {
      writeDataCache("home", { overview: drawerOverview, mail: drawerMail });
    }
  }).finally(() => { drawerRequest = null; });
  return drawerRequest;
}

function scheduleDrawerRefresh() {
  scheduleOverviewPolling();
  if (drawerTimer !== null) window.clearTimeout(drawerTimer);
  drawerTimer = null;
  if (!drawerOpen) return;
  drawerTimer = window.setTimeout(async () => {
    drawerTimer = null;
    if (!document.hidden) await refreshDrawer();
    scheduleDrawerRefresh();
  }, 5000);
}

function setDrawerOpen(open, restoreFocus = true) {
  if (open && !drawerNode) initializeDrawer();
  if (!drawerNode) return;
  const changed = drawerOpen !== open;
  if (!open && drawerOpen) saveScroll("drawer", drawerNode.querySelector(".drawer-panel").scrollTop);
  if (!open) drawerOpenedByReader = false;
  drawerOpen = open;
  if (open && changed) {
    stopTalkFollowing();
    cancelPullRefresh?.();
    cancelBackSwipe?.();
    cancelTransientMotion(app);
    cancelTransientMotion(drawerNode);
  }
  if (open && document.activeElement instanceof HTMLElement) document.activeElement.blur();
  drawerNode.classList.toggle("route-dismiss", !open && !restoreFocus);
  drawerNode.classList.toggle("is-open", open);
  drawerNode.querySelector(".drawer-panel").inert = !open;
  drawerNode.querySelector(".drawer-panel").setAttribute("aria-hidden", String(!open));
  drawerNode.querySelector(".drawer-overlay").tabIndex = open ? 0 : -1;
  app.classList.toggle("drawer-main-locked", open);
  app.querySelectorAll(".content, .composer").forEach((element) => { element.inert = open; });
  app.querySelectorAll(".drawer-toggle").forEach((button) => button.setAttribute("aria-expanded", String(open)));
  if (open && changed) {
    paintDrawer();
    if (drawerOverview) showCachedData("home", true);
    restoreScroll("drawer", drawerNode.querySelector(".drawer-panel"));
    void refreshDrawer();
    drawerNode.querySelector(".drawer-panel").focus({ preventScroll: true });
  } else if (!open && changed && restoreFocus) {
    app.querySelector(".drawer-toggle")?.focus({ preventScroll: true });
  }
  scheduleDrawerRefresh();
}

function initializeDrawer() {
  if (drawerNode) return;
  try {
    const saved = JSON.parse(readDrawerStorage(DRAWER_STORAGE_KEY));
    drawerFolds = Object.fromEntries(Object.entries(saved || {}).filter(([, value]) => typeof value === "boolean"));
  } catch (error) { drawerFolds = {}; }
  drawerNode = node("div", "navigation-drawer");
  drawerNode.id = "navigation-drawer";
  // On the root as well as the drawer: a focus ring is for someone moving by
  // keyboard, and only the root can tell the rest of the app whether anyone is.
  const keyboardFlag = (on) => {
    drawerNode.classList.toggle("keyboard-navigation", on);
    document.documentElement.classList.toggle("keyboard-navigation", on);
  };
  document.addEventListener("keydown", () => keyboardFlag(true), true);
  document.addEventListener("pointerdown", () => keyboardFlag(false), true);
  const edge = node("div", "drawer-edge");
  edge.setAttribute("aria-hidden", "true");
  const overlay = node("button", "drawer-overlay");
  overlay.type = "button";
  overlay.tabIndex = -1;
  overlay.setAttribute("aria-label", stringFor("closeDrawer"));
  overlay.addEventListener("click", () => setDrawerOpen(false));
  const panel = node("nav", "drawer-panel");
  panel.tabIndex = -1;
  panel.inert = true;
  panel.setAttribute("aria-label", stringFor("openDrawer"));
  panel.setAttribute("aria-hidden", "true");
  const machines = node("div", "drawer-machines");
  ENTRY_ORIGINS.forEach((origin, index) => {
    // Manual selection uses the existing entry origins and hop announcement.
    // Session ids are machine-local, so enter the target machine at its root.
    const target = new URL(window.location.pathname + window.location.search, origin);
    target.hash = "#/";
    target.searchParams.set(ENTRY_HOP_PARAM, "1");
    target.searchParams.set(ENTRY_FROM_PARAM, window.location.host);
    const link = node("a", "drawer-machine", stringFor(index === 0 ? "machineWindows" : "machineMac"));
    link.href = target.toString();
    const localWindows = index === 0 && ["127.0.0.1", "localhost"].includes(window.location.hostname)
      && /^Win/i.test(navigator.userAgentData?.platform || navigator.platform);
    if (origin === window.location.origin || localWindows) {
      link.setAttribute("aria-current", "true");
      link.addEventListener("click", (event) => { event.preventDefault(); setDrawerOpen(false); });
    }
    machines.append(link);
  });
  panel.append(node("div", "drawer-lists"), machines);
  panel.addEventListener("click", (event) => {
    if (event.target.closest("a[href]")) setDrawerOpen(false, false);
  });
  drawerNode.append(edge, overlay, panel);
  document.body.append(drawerNode);
  let gesture = null;
  let suppressGestureClick = false;
  panel.addEventListener("dragstart", (event) => event.preventDefault());
  document.addEventListener("click", (event) => {
    if (!suppressGestureClick) return;
    suppressGestureClick = false;
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);
  document.addEventListener("pointerdown", (event) => {
    suppressGestureClick = false;
    if (!event.isPrimary || event.button !== 0) return;
    if (!drawerOpen && backDestination() && event.clientX <= 20) return;
    if (drawerOpen ? !panel.contains(event.target) : event.target !== edge) return;
    const width = panel.getBoundingClientRect().width;
    gesture = { x: event.clientX, y: event.clientY, id: event.pointerId, open: drawerOpen,
      axis: null, width, offset: new DOMMatrixReadOnly(getComputedStyle(panel).transform).m41,
      position: drawerOpen ? width : 0, samples: [{ x: event.clientX, time: event.timeStamp }] };
  });
  document.addEventListener("pointermove", (event) => {
    if (!gesture || event.pointerId !== gesture.id) return;
    const dx = event.clientX - gesture.x;
    const dy = event.clientY - gesture.y;
    if (!gesture.axis && Math.max(Math.abs(dx), Math.abs(dy)) > 10) {
      gesture.axis = Math.abs(dx) > Math.abs(dy) * 1.3 ? "x" : "y";
      if (gesture.axis === "x") {
        cancelTransientMotion(document);
        drawerNode.classList.add("is-dragging");
        drawerNode.classList.remove("route-dismiss");
        app.classList.add("drawer-main-locked");
        (gesture.open ? panel : edge).setPointerCapture(event.pointerId);
      }
    }
    if (gesture.axis !== "x") return;
    event.preventDefault();
    gesture.position = Math.max(0, Math.min(gesture.width, gesture.width + gesture.offset + dx));
    panel.style.transform = `translateX(${gesture.position - gesture.width}px)`;
    overlay.style.opacity = String(gesture.position / gesture.width);
    gesture.samples.push({ x: event.clientX, time: event.timeStamp });
    gesture.samples = gesture.samples.filter((sample) => event.timeStamp - sample.time <= 100);
  }, { passive: false });
  const finishGesture = (event, canceled = false) => {
    if (!gesture || event.pointerId !== gesture.id) return;
    const current = gesture;
    gesture = null;
    if (current.axis !== "x") return;
    suppressGestureClick = true;
    const first = current.samples[0];
    const velocity = (event.clientX - first.x) / Math.max(1, event.timeStamp - first.time);
    const open = canceled ? current.open : Math.abs(velocity) >= 0.5
      ? velocity > 0 : current.position > current.width / 2;
    // Commit the direct-manipulation position before enabling the settle.
    getComputedStyle(panel).transform;
    drawerNode.classList.remove("is-dragging");
    panel.style.removeProperty("transform");
    overlay.style.removeProperty("opacity");
    if (open) drawerOpenedByReader = true;
    setDrawerOpen(open);
  };
  document.addEventListener("pointerup", (event) => finishGesture(event));
  document.addEventListener("pointercancel", (event) => finishGesture(event, true));
  document.addEventListener("keydown", (event) => {
    if (!drawerOpen) return;
    if (event.key === "Escape") { event.preventDefault(); setDrawerOpen(false); }
    if (event.key === "Tab") {
      const controls = [...app.querySelectorAll(".drawer-toggle"), ...panel.querySelectorAll("a[href], button, select, summary")]
        .filter((element) => !element.disabled && element.getClientRects().length);
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && (document.activeElement === first || document.activeElement === panel)) {
        event.preventDefault(); last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault(); first?.focus();
      }
    }
  });
  if (drawerOpen) paintDrawer();
}

// The situation screen's groups, top to bottom: the same buckets as the
// supervisor's report (the Hermes cron that watches mycmux), so the phone
// and the Slack report say the same thing about a seat. The order is the
// order to look.
const SITUATION_GROUPS = ["waiting", "working", "awake", "stalled", "stopped", "terminals"];
const SITUATION_GROUP_TITLES = {
  waiting: "situationWaiting", working: "situationWorking", awake: "situationAwake",
  stalled: "situationStalled", stopped: "situationStopped", terminals: "situationTerminals",
};
// The supervisor calls a working seat "looks stopped" after twenty silent minutes.
const SITUATION_STALL_MS = 20 * 60000;
// Between the two: a seat whose agent is alive but is not working on anything.
// mycmux calls every live Claude Code seat "working" because its only material
// is "the foreground process is not a shell" (monitor.rs:37), so the silence is
// what separates thinking from waiting. Measured 2026-09-20 over 260+ samples:
// a seat that really was working never went quiet for more than 9.7 s (23 s of
// thinking, a 61 s command), while a seat waiting for a person was quiet for
// 29 s at the very least and usually minutes. Sixty seconds is six times the
// longest working silence seen.
const SITUATION_QUIET_MS = 60 * 1000;

function situationTime(tab) {
  const value = typeof tab.lastOutputAt === "number" ? tab.lastOutputAt : Date.parse(tab.lastOutputAt);
  return Number.isFinite(value) ? value : null;
}

// Rows that fold under the terminals heading at the foot: a tab with no
// agent to read or answer, and the imp-* Codex lanes, which the supervisor
// also keeps out of the working list. A seat that is waiting stays in the
// top group whatever it is - that group answers "what is waiting for me".
function situationFolded(tab) {
  if (/^imp-/i.test(displayTabTitle(tab.label || tab.title))) return true;
  if (tab.claudeSessionId) return false;
  const agent = tab.agent || tab.agentKind;
  return (!agent || ["none", "shell", "powershell", "terminal"].includes(agent)) &&
    !["web", "browser"].includes(tab.kind || tab.type);
}

function situationGroup(tab, now) {
  const status = displayStatus(tab);
  if (status === "attention") return "waiting";
  if (situationFolded(tab)) return "terminals";
  // A seat mycmux itself calls idle is awake and unoccupied, not finished:
  // "stopped" is where a closed or dead seat goes, and reading one as the
  // other is the whole complaint.
  if (status === "idle") return "awake";
  if (tab.uiState === "working") return "working";
  if (status !== "working") return "stopped";
  // The list says working; the last output says when. Without a time the
  // seat cannot be shown as silent, so it stays with the working ones.
  const time = situationTime(tab);
  if (time === null) return "working";
  const silent = now - time;
  if (silent >= SITUATION_STALL_MS) return "stalled";
  return silent >= SITUATION_QUIET_MS ? "awake" : "working";
}

// A row's age at the grain the row shows it: the minute under an hour, the
// hour under a day, then the day. Two rows that both read "3 minutes ago"
// are the same age here. Sorting on the exact millisecond was tried on
// 2026-09-12 and reordered 8 to 11 of 14 working rows every five seconds,
// because every output moves the time; a row that moves under the finger
// opens the wrong seat. At this grain a row moves only when the words on it
// change.
function situationAge(tab, now) {
  const time = situationTime(tab);
  if (time === null) return null;
  const minutes = Math.max(0, Math.floor((now - time) / 60000));
  if (minutes < 60) return minutes;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? 60 + hours : 84 + Math.floor(hours / 24);
}

// Newest output first in every group. Rows of the same shown age, and rows
// without a time (which go last), keep the PC's order among themselves
// (Array.prototype.sort is stable).
function situationOrder(now) {
  return (a, b) => {
    const aAge = situationAge(a, now), bAge = situationAge(b, now);
    if (aAge === null) return bAge === null ? 0 : 1;
    if (bAge === null) return -1;
    return aAge - bAge;
  };
}

async function renderHome() {
  clearPolling();
  const token = ++routeToken;
  initializeDrawer();
  const { view, content } = screen(stringFor("situationTitle"), undefined, { className: "situation-screen" });
  app.replaceChildren(view);
  app.scrollTop = 0;
  let hasPainted = false;
  let lines = {};
  let lineTargets = [];
  let linesRequest = null;
  const situation = node("section", "situation");
  const homeRows = new Map();
  const homeGroups = new Map();

  const paintLines = () => {
    for (const { tab, link, group } of lineTargets) {
      const key = tab.claudeSessionId || `term:${tab.terminalSessionId}`;
      const value = lines[key]?.text;
      const preview = typeof value === "string"
        ? Array.from(maskPreviewSecrets(value).replace(/\s+/g, " ").trim()).slice(0, 200).join("").trim()
        : "";
      const text = preview || (group === "waiting"
        ? stringFor(tab.uiState === "done" ? "situationReviewDone" : "situationAskUnreadable") : "");
      const old = link.querySelector(".situation-text");
      if (!text) { old?.remove(); continue; }
      if (old) { if (old.textContent !== text) old.textContent = text; }
      else link.append(node("span", "situation-text", text));
    }
  };
  const paint = (overview) => {
    // A finger is on a row: the list holds still until it lifts. The next
    // poll, five seconds on, paints what this one would have.
    if (hasPainted && pressGesture && situation.contains(pressGesture.control)) return;
    const now = Date.now();
    const order = situationOrder(now);
    const groups = Object.fromEntries(SITUATION_GROUPS.map((group) => [group, []]));
    const keys = new Map();
    for (const workspace of overview.workspaces || []) {
      for (const pane of workspace.panes || []) {
        for (const [index, tab] of (pane.tabs || []).entries()) {
          keys.set(tab, tabRoute(tab) || `${workspace.id}:${pane.paneIndex}:${index}`);
          groups[situationGroup(tab, now)].push(tab);
        }
      }
    }
    const parts = [];
    const seen = new Set();
    const added = [];
    lineTargets = [];
    if (!Object.values(groups).some((rows) => rows.length)) {
      parts.push(situation.querySelector(".situation-empty") || node("p", "situation-empty", stringFor("situationEmpty")));
    } else {
      if (!groups.waiting.length) {
        parts.push(situation.querySelector(".situation-none") || node("p", "situation-none", stringFor("situationNothingWaiting")));
      }
      for (const group of SITUATION_GROUPS) {
        const rows = groups[group];
        // An empty group has no heading: a heading over nothing is a question.
        if (!rows.length) continue;
        if (!homeGroups.has(group)) {
          const list = node("ul", "situation-list");
          if (group === "terminals") {
            // The fold keeps its open state across repaints because the same
            // <details> element is reused, never rebuilt.
            const fold = node("details", "situation-terminals");
            const title = node("summary", "situation-group situation-fold");
            const label = node("span", "situation-fold-label");
            title.append(label, node("span", "chevron"));
            fold.append(title, list);
            homeGroups.set(group, { title: label, list, fold });
          } else {
            homeGroups.set(group, {
              title: node("h2", "situation-group", stringFor(SITUATION_GROUP_TITLES[group])), list, fold: null,
            });
          }
        }
        const { title, list, fold } = homeGroups.get(group);
        if (fold) {
          const heading = `${stringFor(SITUATION_GROUP_TITLES[group])} ${rows.length}`;
          if (title.textContent !== heading) title.textContent = heading;
          parts.push(fold);
        } else parts.push(title, list);
        const nextRows = [];
        rows.sort(order);
        for (const tab of rows) {
          const key = keys.get(tab);
          seen.add(key);
          const route = tabRoute(tab);
          let record = homeRows.get(key);
          if (!record) {
            const row = node("li", "situation-row");
            const link = node(route ? "a" : "span", `situation-link${route ? "" : " is-unlinked"}`);
            if (route) link.href = route;
            const head = node("span", "situation-head");
            const name = node("span", "situation-name");
            const stamp = node("time", "situation-time");
            head.append(rowAgentAvatar(tab), name, stamp);
            link.append(head);
            row.append(link);
            record = { row, link, head, name, stamp, agent: JSON.stringify([tab.agent, tab.agentKind, tab.kind]) };
            homeRows.set(key, record);
            added.push(row);
          }
          const { row, link, head, name, stamp } = record;
          for (const state of Object.keys(groups)) row.classList.toggle(`is-${state}`, state === group);
          const label = displaySeatTitle(tab), age = relativeTime(tab.lastOutputAt);
          if (name.textContent !== label) { name.textContent = label; name.title = label; }
          if (stamp.textContent !== age) stamp.textContent = age;
          const agent = JSON.stringify([tab.agent, tab.agentKind, tab.kind]);
          if (record.agent !== agent) {
            head.firstElementChild.replaceWith(rowAgentAvatar(tab));
            record.agent = agent;
          }
          nextRows.push(row);
          lineTargets.push({ tab, link, group });
        }
        reconcileChildren(list, nextRows);
      }
    }
    paintLines();
    for (const key of homeRows.keys()) if (!seen.has(key)) homeRows.delete(key);
    reconcileChildren(situation, parts);
    reconcileChildren(content, [situation]);
    if (hasPainted && !reducedMotion()) {
      for (const row of added) if (row.querySelector("a[href]")) fadeNewGroup(row);
    }
    renderConnection();
    hasPainted = true;
  };
  const refreshLines = () => {
    if (linesRequest) return;
    // Body text must never hold up the decision about whether anyone is waiting.
    linesRequest = fetchJson("api/lines").then((data) => {
      if (token !== routeToken) return;
      lines = data.lines || {};
      paintLines();
    }).catch(() => {
      if (token !== routeToken) return;
      lines = {};
      paintLines();
    }).finally(() => { linesRequest = null; });
  };
  const cached = readDataCache("home");
  if (cached?.overview) {
    drawerOverview = cached.overview;
    drawerMail = cached.mail;
    paint(cached.overview);
    showCachedData("home");
  } else delayedSkeleton(content, "list");
  const refresh = async () => {
    refreshLines();
    const generation = beginDataRequest("home");
    try {
      // A pull may happen inside the five-second cache window. Reuse an
      // in-flight request, but make a new one if only its old snapshot remains.
      if (hasPainted && !overviewPoller.request) {
        overviewPoller.snapshot = null;
        overviewPoller.failure = null;
      }
      const overview = await sharedOverview(5000);
      if (token !== routeToken) return;
      writeDataCache("home", { overview, mail: drawerMail || [] }, generation);
      paint(overview);
    } catch (error) {
      if (token === routeToken && !hasPainted) showError(content, error);
    }
  };
  startPolling(refresh, 5000);
  await refresh();
}

async function renderLaunch() {
  clearPolling();
  const token = ++routeToken;
  const { view, content } = screen(stringFor("launchTitle"), "#/");
  view.classList.add("launch-screen");
  view.setAttribute("aria-label", stringFor("launchTitle"));
  content.classList.add("launch-content");
  app.replaceChildren(view);
  showLoading(content);

  try {
    const [overview, presetData] = await Promise.all([
      fetchJson("api/overview"),
      fetchJson("api/launch/presets"),
    ]);
    if (token !== routeToken) {
      return;
    }
    const workspaces = overview && Array.isArray(overview.workspaces)
      ? overview.workspaces
      : [];
    const presets = presetData && Array.isArray(presetData.presets)
      ? presetData.presets.filter((value) => typeof value === "string")
      : [];
    let selectedKind = "claude";

    const form = node("form", "launch-form");
    const kindGrid = node("div", "launch-kind-grid");
    kindGrid.setAttribute("role", "group");
    kindGrid.setAttribute("aria-label", stringFor("launchTitle"));
    const kindButtons = new Map();
    [
      ["claude", "launchClaude"],
      ["codex", "launchCodex"],
      ["shell", "launchShell"],
    ].forEach(([kind, labelKey]) => {
      const button = node("button", "launch-kind-card");
      button.type = "button";
      button.append(svgIcon(kind), node("span", "launch-kind-label", stringFor(labelKey)));
      kindButtons.set(kind, button);
      const selectKind = () => {
        selectedKind = kind;
        kindButtons.forEach((candidate, candidateKind) => {
          const selected = candidateKind === selectedKind;
          candidate.classList.toggle("selected", selected);
          candidate.setAttribute("aria-pressed", String(selected));
        });
      };
      button.addEventListener("click", selectKind);
      kindGrid.append(button);
    });
    kindButtons.get(selectedKind).click();

    const workspaceField = node("div", "launch-field");
    const workspaceLabel = node("label", "launch-field-label", stringFor("launchWorkspace"));
    workspaceLabel.htmlFor = "launch-workspace";
    const workspaceSelect = node("select", "launch-control");
    workspaceSelect.id = "launch-workspace";
    workspaces.forEach((workspace) => {
      if (!workspace || typeof workspace.id !== "string") {
        return;
      }
      const option = node("option", "", workspace.name || workspace.id);
      option.value = workspace.id;
      workspaceSelect.append(option);
    });
    workspaceSelect.value = chooseLaunchWorkspace(
      workspaces,
      overview && overview.activeWorkspaceId,
      readLaunchWorkspace()
    );
    workspaceSelect.addEventListener("change", () => {
      rememberLaunchWorkspace(workspaceSelect.value);
    });
    workspaceField.append(workspaceLabel, workspaceSelect);

    const folderField = node("div", "launch-field");
    const folderLabel = node("label", "launch-field-label", stringFor("launchFolder"));
    folderLabel.htmlFor = "launch-cwd";
    const presetList = node("div", "launch-preset-list");
    const cwdInput = node("input", "launch-control launch-control-path");
    cwdInput.id = "launch-cwd";
    cwdInput.type = "text";
    cwdInput.autocomplete = "off";
    cwdInput.placeholder = stringFor("launchFolder");
    cwdInput.value = presets[0] || "";
    const presetButtons = [];
    const updatePresetSelection = () => {
      presetButtons.forEach(({ value, button }) => {
        const selected = cwdInput.value === value;
        button.classList.toggle("selected", selected);
        button.setAttribute("aria-pressed", String(selected));
      });
    };
    presets.forEach((value) => {
      const button = node("button", "launch-preset-chip");
      button.append(node("span", "compact-chip-label", value));
      button.type = "button";
      button.addEventListener("click", () => {
        cwdInput.value = value;
        updatePresetSelection();
      });
      presetButtons.push({ value, button });
      presetList.append(button);
    });
    cwdInput.addEventListener("input", updatePresetSelection);
    updatePresetSelection();
    folderField.append(folderLabel, cwdInput, presetList);

    const labelField = node("div", "launch-field");
    const labelLabel = node("label", "launch-field-label", stringFor("launchLabel"));
    labelLabel.htmlFor = "launch-label";
    const labelInput = node("input", "launch-control");
    labelInput.id = "launch-label";
    labelInput.type = "text";
    labelInput.maxLength = 40;
    labelInput.autocomplete = "off";
    labelField.append(labelLabel, labelInput);

    const status = node("div", "launch-status");
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    const submit = node("button", "launch-start-button", stringFor("launchStart"));
    submit.type = "submit";
    submit.disabled = workspaces.length === 0;

    form.append(kindGrid, workspaceField, folderField, labelField, status, submit);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (submit.disabled) {
        return;
      }
      const workspaceId = workspaceSelect.value;
      const kind = selectedKind;
      rememberLaunchWorkspace(workspaceId);
      submit.disabled = true;
      status.className = "launch-status";
      status.textContent = "";
      const payload = {
        kind,
        workspaceId,
        cwd: cwdInput.value,
      };
      if (labelInput.value) {
        payload.label = labelInput.value;
      }

      const stepKeys = [
        "launchProgressTab",
        ...(kind === "claude" ? ["launchProgressBoot"] : []),
        "launchProgressOpen",
      ];
      const progress = node("div", "launch-progress");
      progress.tabIndex = -1;
      const stepList = node("ol", "launch-progress-list");
      const steps = stepKeys.map((key, index) => {
        const item = node("li", "launch-progress-step");
        const marker = node("span", "launch-progress-marker", String(index + 1));
        marker.setAttribute("aria-hidden", "true");
        item.append(marker, node("span", "launch-progress-copy", stringFor(key)));
        stepList.append(item);
        return { item, marker, key };
      });
      const announcer = node("div", "launch-progress-announcer");
      announcer.setAttribute("role", "status");
      announcer.setAttribute("aria-live", "polite");
      const setStep = (index, complete = false, announce = true) => {
        steps.forEach((step, stepIndex) => {
          const isComplete = stepIndex < index || (stepIndex === index && complete);
          const isCurrent = stepIndex === index && !complete;
          step.item.classList.toggle("is-complete", isComplete);
          step.item.classList.toggle("is-current", isCurrent);
          if (isCurrent) {
            step.item.setAttribute("aria-current", "step");
          } else {
            step.item.removeAttribute("aria-current");
          }
          step.marker.textContent = isComplete ? "\u2713" : String(stepIndex + 1);
        });
        announcer.textContent = complete || !announce ? "" : stringFor(steps[index].key);
      };
      setStep(0, false, false);
      progress.append(stepList, announcer);
      content.replaceChildren(progress);
      progress.focus();

      let launchResult;
      try {
        launchResult = await postJson("api/launch", payload);
      } catch (error) {
        if (token !== routeToken) {
          return;
        }
        const code = error && error.details && error.details.error
          ? String(error.details.error)
          : String(error && error.status ? error.status : "unknown");
        status.classList.add("error");
        status.textContent = `${stringFor("launchFailed")}: ${code}`;
        submit.disabled = false;
        content.replaceChildren(form);
        submit.focus();
        return;
      }
      if (token !== routeToken) {
        return;
      }
      const tabId = launchResult && typeof launchResult.tabId === "string"
        ? launchResult.tabId
        : "";
      if (!tabId) {
        status.classList.add("error");
        status.textContent = stringFor("launchFailed");
        submit.disabled = false;
        content.replaceChildren(form);
        return;
      }
      setStep(0, true);
      setStep(1);

      let checking = false;
      let finished = false;
      const workspaceHash = `#/ws/${encodeURIComponent(workspaceId)}`;
      const navigate = (claudeSessionId = null) => {
        if (finished || token !== routeToken) {
          return;
        }
        finished = true;
        setStep(steps.length - 1, true);
        clearPolling();
        if (claudeSessionId) {
          pendingTalkFocusSessionId = claudeSessionId;
          try {
            window.sessionStorage.setItem("pocketWorkspaceHash", workspaceHash);
          } catch (error) {
            // Private browsing can deny storage while navigation remains safe.
          }
          window.location.replace(`#/talk/${encodeURIComponent(claudeSessionId)}`);
        } else {
          window.location.replace(workspaceHash);
        }
      };
      const showTimeout = () => {
        if (finished || token !== routeToken) {
          return;
        }
        finished = true;
        clearPolling();
        steps.forEach((step) => {
          step.item.classList.remove("is-current");
          step.item.removeAttribute("aria-current");
        });
        const timeout = node("div", "launch-timeout", stringFor("launchTimeout"));
        timeout.setAttribute("role", "status");
        const openWorkspace = node(
          "button",
          "launch-open-workspace",
          stringFor("launchOpenWorkspace")
        );
        openWorkspace.type = "button";
        openWorkspace.addEventListener("click", () => {
          window.location.hash = workspaceHash;
        });
        progress.append(timeout, openWorkspace);
        openWorkspace.focus();
      };
      const checkStatus = async () => {
        if (checking || finished || token !== routeToken) {
          return;
        }
        checking = true;
        try {
          const result = await fetchJson(
            `api/launch/status?workspaceId=${encodeURIComponent(workspaceId)}` +
            `&tabId=${encodeURIComponent(tabId)}`
          );
          if (finished || token !== routeToken || !result || result.exists !== true) {
            return;
          }
          if (kind === "claude") {
            const claudeSessionId = typeof result.claudeSessionId === "string"
              ? result.claudeSessionId
              : "";
            if (!claudeSessionId) {
              return;
            }
            setStep(1, true);
            setStep(2);
            navigate(claudeSessionId);
            return;
          }
          navigate();
        } catch (error) {
          return;
        } finally {
          checking = false;
        }
      };
      startPolling(checkStatus, 2000);
      routeTimeoutTimer = window.setTimeout(showTimeout, LAUNCH_TIMEOUT_MS);
      void checkStatus();
    });
    content.replaceChildren(form);
  } catch (error) {
    if (token === routeToken) {
      showError(content, error);
    }
  }
}

function displayStatus(row) {
  return row && (row.agentStatusStale === true || row.screenObserved === false || row.stale === true)
    ? "unobserved" : validStatus(row && row.status);
}

function detailDisplayStatus(data) {
  if (data.readObservation) {
    const { target, sessionId } = data.readObservation;
    return listedDetailStatus(data, detailOverview(target === "talk" ? 2000 : 5000), target, sessionId);
  }
  return data.readStatus === undefined ? displayStatus(data) : validStatus(data.readStatus);
}

function listedDetailStatus(data, overview, target, sessionId) {
  const rows = (overview?.workspaces || []).flatMap((workspace) =>
    (workspace.panes || []).flatMap((pane) => pane.tabs || []));
  const matches = rows.filter((row) => target === "term"
    ? row.terminalSessionId === sessionId
    : row.claudeSessionId === sessionId || (row.agent === "codex" &&
      [row.terminalSessionId, row.agentSessionId].includes(sessionId)));
  if (matches.length !== 1) return "unobserved";
  const row = matches[0];
  if (row.duplicateId || row.linkable === false || row.lifecycle === "declared" ||
      (data.terminalSessionId && data.terminalSessionId !== row.terminalSessionId) ||
      (data.agent && row.agent !== data.agent) || displayStatus(data) === "unobserved" ||
      (data.agentSessionId && row.agentSessionId && data.agentSessionId !== row.agentSessionId)) {
    return "unobserved";
  }
  return displayStatus(row);
}

function overviewPollingCadence() {
  const detail = overviewDetail?.token === routeToken && overviewDetail.enabled
    ? overviewDetail.cadence : Infinity;
  return Math.min(detail, drawerOpen ? 5000 : Infinity);
}

function currentOverview(maxAge) {
  const snapshot = overviewPoller.snapshot;
  return snapshot && Date.now() - snapshot.startedAt < maxAge ? snapshot.data : null;
}

function detailOverview(cadence) {
  const fresh = currentOverview(cadence);
  if (fresh) return fresh;
  // A refresh in flight does not mean the API has stopped observing the seat.
  // Keep the last confirmed state within the normal read timeout; a failure
  // clears the snapshot immediately. Drawer/list freshness stays unchanged.
  const snapshot = overviewPoller.snapshot;
  return snapshot && !overviewPoller.failure &&
    (overviewPoller.request || overviewPoller.timer !== null) &&
    Date.now() - snapshot.startedAt < 12000 ? snapshot.data : null;
}

function publishOverview() {
  const snapshot = overviewPoller.snapshot;
  if (snapshot) drawerOverview = snapshot.data;
  const detail = overviewDetail;
  if (detail?.token === routeToken && detail.enabled && detail.data) {
    detail.update(listedDetailStatus(detail.data, detailOverview(detail.cadence),
      detail.target, detail.sessionId));
  }
  if (drawerOpen) paintDrawer();
}

function scheduleOverviewPolling() {
  if (overviewPoller.timer !== null) window.clearTimeout(overviewPoller.timer);
  overviewPoller.timer = null;
  const cadence = overviewPollingCadence();
  if (!Number.isFinite(cadence) || typeof document === "undefined") return;
  const startedAt = overviewPoller.request?.startedAt ?? overviewPoller.snapshot?.startedAt ??
    overviewPoller.failure?.startedAt ?? Date.now();
  const delay = document.hidden ? cadence : Math.max(0, startedAt + cadence - Date.now());
  overviewPoller.timer = window.setTimeout(() => {
    overviewPoller.timer = null;
    if (!Number.isFinite(overviewPollingCadence())) return;
    if (document.hidden) { scheduleOverviewPolling(); return; }
    void sharedOverview(overviewPollingCadence()).catch(() => {});
  }, delay);
}

async function sharedOverview(maxAge = 5000) {
  maxAge = Math.min(maxAge, overviewPollingCadence());
  const now = Date.now();
  let request = overviewPoller.request;
  // Age is measured from dispatch, never from a delayed response's arrival.
  if (!request || now - request.startedAt >= maxAge) {
    if (!request && overviewPoller.failure && now - overviewPoller.failure.startedAt < maxAge) {
      scheduleOverviewPolling();
      throw overviewPoller.failure.error;
    }
    const cached = currentOverview(maxAge);
    if (cached && !request) { publishOverview(); scheduleOverviewPolling(); return cached; }
    request = { generation: ++overviewPoller.generation, startedAt: now, promise: null };
    overviewPoller.request = request;
    overviewPoller.failure = null;
    publishOverview();
    request.promise = fetchJson("api/overview").then((data) => {
      if (request.generation !== overviewPoller.generation) return sharedOverview(maxAge);
      overviewPoller.snapshot = { data, startedAt: request.startedAt };
      delete drawerErrors.overview;
      publishOverview();
      return data;
    }).catch((error) => {
      if (request.generation !== overviewPoller.generation) return sharedOverview(maxAge);
      overviewPoller.snapshot = null;
      overviewPoller.failure = { error, startedAt: request.startedAt };
      drawerErrors.overview = error;
      publishOverview();
      throw error;
    }).finally(() => {
      if (request.generation === overviewPoller.generation) {
        overviewPoller.request = null;
        scheduleOverviewPolling();
      }
    });
    // A slow older request cannot hold a faster consumer beyond its cadence.
    scheduleOverviewPolling();
  }
  await request.promise;
  const data = currentOverview(maxAge);
  if (!data) throw new Error("overviewStale");
  return data;
}

function watchDetailOverview(target, sessionId, cadence, update) {
  overviewDetail = { target, sessionId, cadence, update, token: routeToken,
    enabled: false, data: null };
}

async function fetchDetailJson(url, target, sessionId) {
  // The listing endpoint applies status.subscribe; raw detail status does not.
  // Keep every action/identity field intact and attach only a read-facing state.
  const token = routeToken;
  const data = await fetchJson(url);
  if (token !== routeToken) return { ...data, readStatus: "unobserved" };
  // Older health contracts have no status feed to override pane observations.
  if (connectionHealth && !connectionHealth.feed) return data;
  const detail = overviewDetail;
  if (detail?.token === token && token === routeToken && detail.target === target &&
      detail.sessionId === sessionId) {
    detail.data = data;
    detail.enabled = true;
  }
  const overview = await sharedOverview(target === "talk" ? 2000 : 5000).catch(() => null);
  scheduleOverviewPolling();
  return { ...data, readObservation: { target, sessionId },
    readStatus: listedDetailStatus(data, overview, target, sessionId) };
}

function appendWorkspaceStatus(parent, workspace) {
  const status = displayStatus(workspace);
  parent.append(node("span", `status-pill ${status}`, statusText(status)));
  if (workspace.attentionCount > 0) {
    parent.append(node("span", "attention-count", stringFor("attentionCount", { n: workspace.attentionCount })));
  }
}

function tabKind(row) {
  if (row.kind) return row.kind;
  if (row.type && row.type !== "terminal") return "web";
  return validAgent(row.agent) === "none" ? "shell" : "terminal";
}

function appendTabBadges(parent, row) {
  const key = { web: "tabKindWeb", terminal: "tabKindTerminal", shell: "tabKindShell" }[tabKind(row)];
  if (tabKind(row) !== "terminal") {
    parent.append(node("span", "tab-kind", stringFor(key)));
  }
  // Compressing the rows took this with it. How many things are waiting on a
  // tab is the one number worth crossing the room for, so it stays even when
  // everything else is trimmed.
  if (row.attentionCount > 0) {
    parent.append(node("span", "attention-count",
      stringFor("attentionCount", { n: row.attentionCount })));
  }
  if (row.duplicateId) {
    parent.append(node("span", "tab-duplicate-id", stringFor("tabDuplicateId")));
  }
  if (row.lifecycle === "declared") {
    parent.append(node("span", "tab-not-started", (window.STRINGS && window.STRINGS.tabNotStarted) || "tabNotStarted"));
  }
  if (row.agentKind) {
    // Kinds the machine actually produces get their own name. Only a genuinely
    // unfamiliar kind falls through to the unknown wording, and shell already
    // states itself through the kind badge, so it says nothing twice.
    const agentName = agentKindName(row.agentKind);
    if (agentName) {
      parent.append(node("span", "tab-agent-kind", agentName));
    } else if (row.agentKind !== "shell") {
      parent.append(node("span", "tab-agent-kind",
        stringFor("tabAgentUnknown", { kind: row.agentKind })));
    }
  }
}

function tabRoute(row) {
  if (row.lifecycle === "declared" || row.duplicateId || row.linkable === false || tabKind(row) === "web") return null;
  const session = row.claudeSessionId || row.terminalSessionId;
  if (typeof session !== "string" || !/^[A-Za-z0-9_-]+$/.test(session)) return null;
  return `#/${row.claudeSessionId || validAgent(row.agent) === "codex" ? "talk" : "term"}/${encodeURIComponent(session)}`;
}

function terminalListRow(terminal) {
  return workspaceTabRow({ ...terminal, title: terminal.title || terminal.label });
}

function drawerExcerpt(tab) {
  // Only use the output snippet, never the dictated prompt subtitle. A complete
  // sentence or a file path is useful; a clipped fragment/status echo is not.
  const value = String(tab.snippet || "").trim();
  if (!value || value === tab.title || /<\/?[A-Za-z][^>]*>/.test(value)) return "";
  if (/^(?:[A-Za-z]:[\\/]|\/)[^\n]+\.[A-Za-z0-9]{1,8}$/.test(value)) return value;
  if (value.length < 12 || !/[.!?\u3002\uff01\uff1f]$/.test(value)) return "";
  if (/^(?:routine\b|nothing to (?:act|do)\b|waiting\b|standby\b|\u5f85\u6a5f|\u4e86\u89e3|\u627f\u77e5)/i.test(value)) return "";
  if (/(?:\u306e\u3055[\u3001,]|\u3092\u3055[\u3001,]|\u3048\u30fc\u3068|\u3042\u306e\u3055)/u.test(value)) return "";
  return value;
}

function workspaceTabRow(tab, compact = false) {
  const status = displayStatus(tab);
  const agent = validAgent(tab.agent);
  const route = tabRoute(tab);
  const row = node("div", `talk-row ${status} agent-${agent}${route ? " tappable" : " inert"}`);
  row.dataset.paneIndex = String(tab.paneIndex);
  row.dataset.tabRow = "true";
  if (route) {
    row.tabIndex = 0;
    row.setAttribute("role", "link");
    row.addEventListener("click", (event) => {
      if (!event.target.closest(".file-link")) window.location.hash = route;
    });
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && event.target === row) window.location.hash = route;
    });
  }
  const copy = node("span", "talk-copy");
  const titleLine = node("span", "talk-title-line");
  const title = node("strong", "talk-title", displaySeatTitle(tab));
  title.title = title.textContent;
  titleLine.append(title);
  if (compact) {
    row.classList.add("compact-tab");
    const kinds = node("span");
    appendTabBadges(kinds, tab);
    const kindName = kinds.querySelector(".tab-agent-kind, .tab-kind")?.textContent || agentLabel(agent);
    const kind = rowAgentAvatar(tab);
    if (tabKind(tab) === "web") kind.textContent = "Web";
    kind.title = kindName;
    kind.setAttribute("aria-label", kindName);
    titleLine.append(node("span", `status-pill ${status}`, statusText(status)), kind);
    copy.append(titleLine);
    const excerpt = drawerExcerpt(tab);
    if (excerpt) {
      // A preview line is a preview. A path inside it used to be a link 18 px
      // tall that took the tap the row wanted - aim at the conversation, land in
      // a file viewer - and in the width left over it showed a sliver of a path
      // that could not be read anyway. The whole line belongs to the row.
      const snippet = node("span", "talk-snippet",
        // A conversation carries API keys and .env contents as readily as an
        // email does, and this line is readable without opening anything.
        maskPreviewSecrets(excerpt));
      copy.append(snippet);
    }
    if (!route) row.setAttribute("aria-disabled", "true");
    row.append(copy);
    return row;
  }
  titleLine.append(node("time", "talk-time", relativeTime(tab.lastOutputAt)));
  copy.append(titleLine);
  if (tab.subtitle) copy.append(node("span", "talk-subtitle", tab.subtitle));
  if (tab.snippet) {
    const snippet = node("span", "talk-snippet", maskPreviewSecrets(tab.snippet));
    copy.append(snippet);
  }
  const meta = node("span", "talk-meta");
  meta.append(node("span", `status-pill ${status}`, statusText(status)));
  appendTabBadges(meta, tab);
  if (tab.lifecycle === "declared" || tabKind(tab) === "web") {
    meta.append(node("span", "send-unavailable", stringFor("sendUnavailableShell")));
  }
  copy.append(meta);
  const avatar = rowAgentAvatar(tab);
  // The row carried the agent twice: a mark on the left and a text badge in the
  // meta line, which cost a line on every row. The compact row had already
  // solved this by naming the mark; the roomy one kept both. The name moves to
  // the mark, where a pointer and a screen reader can still reach it, and the
  // badge goes.
  const agentBadge = meta.querySelector(".tab-agent-kind");
  const agentName = agentBadge ? agentBadge.textContent : agentLabel(agent);
  if (agentName) {
    avatar.title = agentName;
    avatar.setAttribute("aria-label", agentName);
    avatar.removeAttribute("aria-hidden");
  }
  if (agentBadge) agentBadge.remove();
  row.append(avatar, copy);
  if (route) row.append(node("span", "chevron"));
  return row;
}

function readTalkSort() {
  try {
    return window.localStorage.getItem(TALK_SORT_STORAGE_KEY) === "recent" ? "recent" : "desktop";
  } catch (error) {
    return "desktop";
  }
}

function writeTalkSort(value) {
  const sort = value === "recent" ? "recent" : "desktop";
  try { window.localStorage.setItem(TALK_SORT_STORAGE_KEY, sort); } catch (error) {}
  return sort;
}

function sortedWorkspaceTabs(data, sort = "desktop") {
  const rows = (Array.isArray(data.tabs) ? data.tabs : [
    ...(Array.isArray(data.talks) ? data.talks : []),
    ...(Array.isArray(data.terminals) ? data.terminals : []),
  ]).filter((row) => row && typeof row === "object");
  if (sort === "recent") {
    const time = (value) => typeof value === "number" ? value : Date.parse(value) || 0;
    rows.sort((a, b) => time(b.lastOutputAt) - time(a.lastOutputAt));
  }
  return rows;
}

function homeListCounts(list) {
  const workspaces = Array.from(list.querySelectorAll(".layout-ws-row, .workspace-row"));
  const panes = Array.from(list.querySelectorAll(".layout-row"));
  const flat = workspaces.filter((row) => row.dataset.paneCount !== undefined);
  return {
    w: workspaces.length,
    p: panes.length + flat.reduce((sum, row) => sum + Number(row.dataset.paneCount), 0),
    t: [...panes, ...flat].reduce((sum, row) => sum + Number(row.dataset.tabCount), 0),
  };
}

function countSummary(drawn, expected, stale = false) {
  const match = !stale && expected && ["w", "p", "t"].every((key) =>
    Number.isInteger(expected[key]) && drawn[key] === expected[key]);
  const summary = node("div", `list-counts ${match ? "match" : "mismatch"}`,
    stringFor(match ? "countMatch" : "countMismatch", drawn));
  summary.setAttribute("role", "status");
  return summary;
}

function workspaceListView(data, sort, onSortChange, stale = false, compact = false, state = null) {
  const fragment = state?.view || node("div", "workspace-tab-list");
  if (state) state.view = fragment;
  const select = fragment.querySelector(".talk-sort") || node("select", "talk-sort");
  select.setAttribute("aria-label", stringFor("sortByDesktop"));
  if (!select.children.length) for (const [value, key] of [["desktop", "sortByDesktop"], ["recent", "sortByRecent"]]) {
    const option = node("option", "", stringFor(key));
    option.value = value;
    select.append(option);
  }
  select.value = sort;
  select.onchange = () => onSortChange(writeTalkSort(select.value));
  const list = fragment.querySelector(".talk-list") || node("div", "talk-list");
  const tabs = sortedWorkspaceTabs(data, sort);
  const seen = new Set();
  const children = tabs.map((tab, index) => {
    const key = tabRoute(tab) || `${tab.paneIndex}:${tab.tabIndex ?? index}`;
    seen.add(key);
    return cachedPaintRow(state?.rows, key, JSON.stringify([tab, compact, relativeTime(tab.lastOutputAt)]),
      () => workspaceTabRow(tab, compact));
  });
  if (!compact) {
    suppressUniformBadges(children, ROW_BADGE_SELECTOR,
      ROW_MAJORITY_SELECTOR);
  }
  const paneIndexes = new Set(tabs.map((tab) => tab.paneIndex));
  const panes = Array.isArray(data.panes) ? data.panes : [];
  panes.forEach((pane) => {
    if (!paneIndexes.has(pane.paneIndex) && pane.tabCount === 0) {
      const key = `empty:${pane.paneIndex}`;
      seen.add(key);
      const empty = cachedPaintRow(state?.rows, key, JSON.stringify(pane), () => {
        const row = node("div", "empty-pane", pane.title || stringFor("layoutNoSession"));
        row.dataset.paneIndex = String(pane.paneIndex);
        return row;
      });
      children.push(empty);
      paneIndexes.add(pane.paneIndex);
    }
  });
  if (state) for (const key of state.rows.keys()) if (!seen.has(key)) state.rows.delete(key);
  reconcileChildren(list, children);
  const drawnRows = Array.from(list.children);
  const drawn = {
    w: data.workspace ? 1 : 0,
    p: new Set(drawnRows.map((row) => row.dataset.paneIndex)).size,
    t: drawnRows.filter((row) => row.dataset.tabRow === "true").length,
  };
  const summary = countSummary(drawn, data.bridgeCounts, stale);
  const oldSummary = fragment.querySelector(".list-counts");
  reconcileChildren(fragment, [select.parentElement || inputHitArea(select, "soundness-sort-hit"), list,
    oldSummary?.isEqualNode(summary) ? oldSummary : summary]);
  return fragment;
}

async function renderWorkspace(workspaceId) {
  clearPolling();
  const token = ++routeToken;
  window.sessionStorage.setItem("pocketWorkspaceHash", `#/ws/${encodeURIComponent(workspaceId)}`);
  const { view, content } = screen(stringFor("workspaceHeader"), "#/");
  app.replaceChildren(view);
  let sort = readTalkSort();
  const workspacePaint = { rows: new Map() };
  const paint = (data, stale = false) => {
    const workspaceName = data.workspace && data.workspace.name;
    if (workspaceName) view.querySelector(".topbar-title").textContent = workspaceName;
    const list = workspaceListView(data, sort, (value) => {
      sort = value;
      paint(data, stale);
    }, stale, false, workspacePaint);
    staggerRows(Array.from(list.querySelector(".talk-list").children), `workspace:${workspaceId}`);
    reconcileChildren(content, [list]);
    scheduleListBadges();
    renderConnection();
    if (stale) showCachedData(`workspace:${workspaceId}`);
    if (!hasPainted) restoreScroll();
    hasPainted = true;
  };
  let hasPainted = false;
  const cacheName = `workspace:${workspaceId}`;
  let hasCached = false;
  const cached = readDataCache(cacheName);
  if (cached && Array.isArray(cached.talks)) {
    try {
      paint(cached, true);
      hasCached = true;
    } catch (error) {
      discardDataCache(cacheName);
    }
  }
  if (!hasCached) {
    delayedSkeleton(content, "list", drawerWorkspaces.get(workspaceId)?.tabs?.length || 3);
  }
  let refreshRevision = 0;
  const refresh = async () => {
    const revision = ++refreshRevision;
    const generation = beginDataRequest(cacheName);
    try {
      const data = await fetchJson(`api/ws/${encodeURIComponent(workspaceId)}`);
      if (token !== routeToken || revision !== refreshRevision) {
        return;
      }
      if (!writeWorkspaceCache(workspaceId, data, generation)) return;
      paint(data);
      hasCached = true;
    } catch (error) {
      if (token === routeToken && revision === refreshRevision &&
          !hasCached && !content.querySelector(".talk-list")) {
        showError(content, error);
      }
    }
  };
  await refresh();
  if (token !== routeToken) {
    return;
  }
  startPolling(refresh, 5000);
}

function quickCommandLabel(body) {
  const normalized = String(body)
    .replace(/\r\n|[\r\n\u2028\u2029]/g, " ")
    .trim();
  const characters = Array.from(normalized);
  const label = characters.slice(0, QUICK_COMMAND_LABEL_LENGTH).join("");
  return characters.length > QUICK_COMMAND_LABEL_LENGTH ? `${label}\u2026` : label;
}

function quickInsertValue(current, body, target) {
  if (target === "term") {
    return body;
  }
  return current === "" ? body : `${current}\n${body}`;
}

function addQuickCommand(commands, body, nowMs = Date.now()) {
  const current = Array.isArray(commands) ? commands : [];
  const label = typeof body === "string" ? quickCommandLabel(body) : "";
  if (!label) {
    return { status: "empty", commands: current, command: null };
  }
  if (current.some((command) => command.body === body)) {
    return { status: "duplicate", commands: current, command: null };
  }
  if (current.length >= QUICK_COMMAND_LIMIT) {
    return { status: "limit", commands: current, command: null };
  }
  const timestamp = Number.isFinite(nowMs) ? Math.trunc(nowMs) : Date.now();
  const baseId = `q${timestamp}`;
  const usedIds = new Set(current.map((command) => command.id));
  let id = baseId;
  let suffix = 0;
  while (usedIds.has(id)) {
    suffix += 1;
    id = `${baseId}-${suffix}`;
  }
  const command = { id, label, body };
  return { status: "added", commands: current.concat(command), command };
}

function readQuickCommands(storageKey) {
  try {
    const raw = window.localStorage.getItem(storageKey);
    if (raw === null) {
      return [];
    }
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed) || parsed.some((command) => (
      !command || typeof command !== "object" ||
      typeof command.id !== "string" ||
      typeof command.label !== "string" ||
      typeof command.body !== "string"
    ))) {
      return null;
    }
    return parsed.slice(0, QUICK_COMMAND_LIMIT);
  } catch (error) {
    return null;
  }
}

function writeQuickCommands(storageKey, commands) {
  try {
    window.localStorage.setItem(storageKey, JSON.stringify(commands));
    return true;
  } catch (error) {
    return false;
  }
}

function bindQuickCommandChip(button, onTap, onLongPress) {
  let timer = null;
  let pointerId = null;
  let startX = 0;
  let startY = 0;
  let suppressClick = false;
  let disposed = false;

  const clearPress = () => {
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
    pointerId = null;
    button.classList.remove("is-long-pressing");
  };
  const pointerDown = (event) => {
    if (event.isPrimary === false || (event.button !== undefined && event.button !== 0)) {
      return;
    }
    clearPress();
    suppressClick = false;
    pointerId = event.pointerId;
    startX = event.clientX;
    startY = event.clientY;
    button.classList.add("is-long-pressing");
    timer = window.setTimeout(() => {
      timer = null;
      pointerId = null;
      suppressClick = true;
      button.classList.remove("is-long-pressing");
      if (!disposed && button.isConnected) {
        onLongPress();
      }
    }, QUICK_COMMAND_LONG_PRESS_MS);
  };
  const pointerMove = (event) => {
    if (event.pointerId !== pointerId) {
      return;
    }
    if (event.clientX !== startX || event.clientY !== startY) {
      suppressClick = true;
      clearPress();
    }
  };
  const pointerUp = (event) => {
    if (event.pointerId === pointerId) {
      clearPress();
    }
  };
  const pointerCancel = (event) => {
    if (event.pointerId === pointerId) {
      suppressClick = true;
      clearPress();
    }
  };
  const click = (event) => {
    if (suppressClick && event.detail !== 0) {
      suppressClick = false;
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    suppressClick = false;
    onTap();
  };
  const contextMenu = (event) => event.preventDefault();

  button.addEventListener("pointerdown", pointerDown);
  button.addEventListener("pointermove", pointerMove, { passive: true });
  button.addEventListener("pointerup", pointerUp);
  button.addEventListener("pointercancel", pointerCancel);
  button.addEventListener("pointerleave", pointerCancel);
  button.addEventListener("click", click);
  button.addEventListener("contextmenu", contextMenu);

  return () => {
    disposed = true;
    clearPress();
    button.removeEventListener("pointerdown", pointerDown);
    button.removeEventListener("pointermove", pointerMove);
    button.removeEventListener("pointerup", pointerUp);
    button.removeEventListener("pointercancel", pointerCancel);
    button.removeEventListener("pointerleave", pointerCancel);
    button.removeEventListener("click", click);
    button.removeEventListener("contextmenu", contextMenu);
  };
}

function clearActiveQuickCommands() {
  if (activeQuickCommandCleanup) {
    activeQuickCommandCleanup();
  }
}

function composerDraftKey(target, sessionId) {
  const prefix = target === "term" ? DRAFT_TERM_PREFIX : DRAFT_TALK_PREFIX;
  return `${prefix}${sessionId}`;
}

function removeComposerDraft(storageKey) {
  try {
    window.localStorage.removeItem(storageKey);
  } catch (error) {
    // Draft storage must never block composing or sending.
  }
}

function pruneComposerDrafts() {
  try {
    const drafts = [];
    for (let index = 0; index < window.localStorage.length; index += 1) {
      const key = window.localStorage.key(index);
      if (!key || (!key.startsWith(DRAFT_TALK_PREFIX) && !key.startsWith(DRAFT_TERM_PREFIX))) {
        continue;
      }
      let at = Number.NEGATIVE_INFINITY;
      try {
        const value = JSON.parse(window.localStorage.getItem(key));
        if (value && Number.isFinite(value.at)) {
          at = value.at;
        }
      } catch (error) {
        // Invalid entries are the first to be discarded.
      }
      drafts.push({ key, at });
    }
    drafts.sort((left, right) => left.at - right.at || left.key.localeCompare(right.key));
    drafts.slice(0, Math.max(0, drafts.length - DRAFT_SESSION_LIMIT)).forEach(({ key }) => {
      window.localStorage.removeItem(key);
    });
  } catch (error) {
    // Draft storage must never block composing or sending.
  }
}

function writeComposerDraft(storageKey, text, at = Date.now()) {
  try {
    if (!text) {
      window.localStorage.removeItem(storageKey);
      return;
    }
    window.localStorage.setItem(storageKey, JSON.stringify({ text, at }));
    pruneComposerDrafts();
  } catch (error) {
    // Draft storage must never block composing or sending.
  }
}

function readComposerDraft(storageKey, now = Date.now()) {
  try {
    const raw = window.localStorage.getItem(storageKey);
    if (!raw) {
      return null;
    }
    const draft = JSON.parse(raw);
    if (!draft || typeof draft.text !== "string" || !draft.text ||
        !Number.isFinite(draft.at) || now - draft.at > DRAFT_MAX_AGE_MS) {
      window.localStorage.removeItem(storageKey);
      return null;
    }
    return draft.text;
  } catch (error) {
    return null;
  }
}

function composerDraftController(input, target, sessionId) {
  const storageKey = composerDraftKey(target, sessionId);
  input.dataset.draftKey = storageKey;
  let timer = null;
  let restoreTimer = null;
  let disposed = false;
  const cancel = () => {
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
  };
  const flush = (text = input.value) => {
    cancel();
    writeComposerDraft(storageKey, text);
  };
  const schedule = () => {
    if (disposed) {
      return;
    }
    cancel();
    timer = window.setTimeout(() => {
      timer = null;
      writeComposerDraft(storageKey, input.value);
    }, DRAFT_DEBOUNCE_MS);
  };
  const accepted = () => {
    cancel();
    if (input.value) {
      writeComposerDraft(storageKey, input.value);
    } else {
      removeComposerDraft(storageKey);
    }
  };
  const cleanup = () => {
    disposed = true;
    cancel();
    if (restoreTimer !== null) {
      window.clearTimeout(restoreTimer);
      restoreTimer = null;
    }
  };
  input.addEventListener("input", schedule);
  const restored = readComposerDraft(storageKey);
  if (restored !== null) {
    input.value = restored;
    if (typeof input.setSelectionRange === "function") {
      const end = input.value.length;
      input.setSelectionRange(end, end);
    }
    if (target === "talk") {
      restoreTimer = window.setTimeout(() => {
        restoreTimer = null;
        if (!disposed && input.isConnected) {
          input.dispatchEvent(new Event("input"));
        }
      }, 0);
    } else {
      input.dispatchEvent(new Event("input"));
    }
  }
  return { accepted, cleanup, flush, storageKey };
}

function quickCommandRow(input, storageKey, target) {
  let commands = readQuickCommands(storageKey);
  let storageAvailable = commands !== null;
  commands = storageAvailable ? commands : [];
  const row = node("div", "quick-command-row");
  const status = node("span", "quick-command-status");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  const disposers = [];
  let destroyed = false;

  const disposeChips = () => {
    while (disposers.length) {
      disposers.pop()();
    }
  };
  const destroy = () => {
    if (destroyed) {
      return;
    }
    destroyed = true;
    disposeChips();
    input.removeEventListener("input", updateEmptyHint);
    if (activeQuickCommandCleanup === destroy) {
      activeQuickCommandCleanup = null;
    }
  };
  const storageFailed = () => {
    storageAvailable = false;
    commands = [];
    render();
  };
  const clearStatus = () => {
    status.textContent = "";
  };
  const showStatus = (key, values = {}) => {
    status.textContent = stringFor(key, values);
  };
  const insert = (body) => {
    clearStatus();
    input.value = quickInsertValue(input.value, body, target);
    input.dispatchEvent(new Event("input"));
    if (typeof input.setSelectionRange === "function") {
      const end = input.value.length;
      input.setSelectionRange(end, end);
    }
    input.focus();
  };
  const render = () => {
    disposeChips();
    clearStatus();
    const builtinChildren = QUICK_BUILTIN_COMMANDS.map((command) => {
      const label = stringFor(command.labelKey);
      const hint = stringFor(command.hintKey);
      const chip = node("button", "quick-command-chip quick-command-builtin");
      chip.append(node("span", "compact-chip-label", label));
      chip.type = "button";
      chip.title = hint;
      chip.setAttribute("aria-label", hint);
      const click = () => insert(stringFor(command.bodyKey));
      chip.addEventListener("click", click);
      disposers.push(() => chip.removeEventListener("click", click));
      return chip;
    });
    const children = storageAvailable ? commands.map((command) => {
      const chip = node("button", "quick-command-chip quick-command-user");
      chip.append(node("span", "compact-chip-label", command.label));
      chip.type = "button";
      chip.setAttribute(
        "aria-label",
        stringFor("quickInsertLabel", { label: command.label })
      );
      disposers.push(bindQuickCommandChip(
        chip,
        () => insert(command.body),
        () => showDeleteConfirm(command)
      ));
      return chip;
    }) : [];
    if (!storageAvailable) {
      row.replaceChildren(...builtinChildren, status);
      return;
    }
    const add = node("button", "quick-command-add", "+");
    add.type = "button";
    add.title = stringFor("quickAdd");
    add.setAttribute("aria-label", stringFor("quickAdd"));
    add.addEventListener("click", () => {
      const result = addQuickCommand(commands, input.value);
      if (result.status === "empty") {
        showStatus("quickAddEmpty");
        return;
      }
      if (result.status === "duplicate") {
        clearStatus();
        return;
      }
      if (result.status === "limit") {
        showStatus("quickLimit", { n: QUICK_COMMAND_LIMIT });
        return;
      }
      if (!writeQuickCommands(storageKey, result.commands)) {
        storageFailed();
        return;
      }
      commands = result.commands;
      render();
      input.focus();
    });
    const emptyHint = commands.length === 0
      ? node("span", "quick-command-empty-hint", stringFor("quickEmptyHint"))
      : null;
    row.replaceChildren(
      ...builtinChildren,
      ...children,
      add,
      ...(emptyHint ? [emptyHint] : []),
      status
    );
    updateEmptyHint();
  };
  const updateEmptyHint = () => {
    const hint = row.querySelector(".quick-command-empty-hint");
    if (hint) hint.hidden = commands.length !== 0 || !input.value.trim();
  };
  const showDeleteConfirm = (command) => {
    disposeChips();
    clearStatus();
    const confirm = node("div", "quick-command-confirm");
    const label = node(
      "span",
      "quick-command-confirm-label",
      stringFor("quickDeleteConfirm", { label: command.label })
    );
    const remove = node("button", "quick-command-action quick-command-delete", stringFor("quickDelete"));
    remove.type = "button";
    remove.addEventListener("click", () => {
      const next = commands.filter((item) => item.id !== command.id);
      if (!writeQuickCommands(storageKey, next)) {
        storageFailed();
        return;
      }
      commands = next;
      render();
      input.focus();
    });
    const cancel = node("button", "quick-command-action", stringFor("quickCancel"));
    cancel.type = "button";
    cancel.addEventListener("click", () => {
      render();
      input.focus();
    });
    confirm.append(label, remove, cancel);
    row.replaceChildren(confirm);
  };

  if (activeQuickCommandCleanup) {
    activeQuickCommandCleanup();
  }
  activeQuickCommandCleanup = destroy;
  row.quickCommandsCleanup = destroy;
  input.addEventListener("input", updateEmptyHint);
  render();
  return row;
}

// There is no stop control on the phone. It stood in the title bar beside the
// close button while a seat was working, a round button with a round mark in
// it: it read as record, and appearing and vanishing with every two-second
// poll it blinked. Esc belongs to the PC and to Slack ("stop that"); the key
// API stays on the server for them (docs/REQUIREMENTS.md: no stop button).

function sendNormalizationCount(value) {
  return [...value.matchAll(/\r\n|[\x00-\x09\x0b-\x1f\x7f-\x9f\u2028\u2029]/g)]
    .reduce((count, match) => count + match[0].length, 0);
}

function normalizeSendText(value) {
  return value.replace(/\r\n?/g, "\n").replace(/[\v\f\x85\u2028\u2029]/g, "\n")
    .replace(/\t/g, "    ").replace(/[\x00-\x09\x0b-\x1f\x7f-\x9f]/g, "");
}

function terminalSendFailure(reason) {
  const known = ["termNotReady", "sendUnavailableShell", "bridgeUnavailable", "termRateLimited", "codexRolloutUnavailable", "codexDeliveryUnconfirmed"];
  const key = known.includes(reason) ? reason : "terminalDeliveryUnconfirmed";
  return stringFor(key);
}

// ---- Closing a pane from the phone ------------------------------------
//
// The PC's close button takes only the front tab. The confirmation includes
// that exact tab ID; a front-tab change after confirmation is refused.
// A legacy server cannot safely promise this scope, so it offers no button.

function paneFromTalk(data) {
  // The talk reply carries the pane it sits in - id, name and every tab -
  // when the server can close panes. Anything less than that shape is no
  // pane, and no pane means no control is offered.
  const pane = data && typeof data === "object" ? data.pane : null;
  if (!pane || typeof pane !== "object") return null;
  if (typeof pane.paneId !== "string" || !pane.paneId || !Array.isArray(pane.tabs)) return null;
  const tabs = pane.tabs.filter((tab) => tab && typeof tab === "object");
  if (pane.closeScope !== "activeTab" || tabs.length !== 1 || pane.activeTabId !== tabs[0].tabId) return null;
  return { paneId: pane.paneId, activeTabId: pane.activeTabId,
    title: typeof pane.title === "string" ? pane.title : "", tabs };
}

function closePaneButton(sessionId) {
  const button = node("button", "close-pane-button");
  button.type = "button";
  button.hidden = true;
  button.title = stringFor("closePane");
  button.setAttribute("aria-label", stringFor("closePane"));
  button.append(svgIcon("close"));
  let found = null;
  let retired = false;
  // Only a server that can close panes describes one; a phone served a new
  // shell by an older server keeps the bar it had rather than a control
  // that would answer "not found".
  const update = (data) => {
    found = retired ? null : paneFromTalk(data);
    button.hidden = !found;
  };
  button.addEventListener("click", () => {
    if (!found || button.disabled || document.querySelector(".close-pane-layer")) return;
    void openClosePaneSheet(sessionId, found, button);
  });
  button.update = update;
  button.retire = () => {
    retired = true;
    found = null;
    button.hidden = true;
  };
  return button;
}

const CLOSE_PANE_ERROR_STRINGS = {
  lastTabRefused: "closePaneLastTab",
  paneChanged: "closePaneChanged",
  tabNeedsConfirmation: "closeTabNeedsConfirmation",
  paneNotClosed: "closePaneIncomplete",
  paneCloseUnverified: "closePaneUnverified",
  sessionNotAllowed: "closePaneGone",
  bridgeUnavailable: "bridgeUnavailable",
  bridgeBusy: "errorFetchBusy",
  csrfDenied: "csrfDenied",
  originDenied: "originDenied",
};

function closePaneErrorText(error) {
  if (error && error.offline) return stringFor("errorFetchOffline");
  const code = error && error.details && error.details.error;
  return stringFor(CLOSE_PANE_ERROR_STRINGS[code] || "closePaneFailed");
}

function closePaneSheetView(found) {
  const { tabs } = found;
  const sheet = node("section", "close-pane-sheet");
  sheet.setAttribute("role", "dialog");
  sheet.setAttribute("aria-modal", "true");
  sheet.setAttribute("aria-labelledby", "close-pane-title");
  const title = node("h2", "close-pane-title", stringFor("closePaneTitle"));
  title.id = "close-pane-title";
  const name = node("p", "close-pane-name", displayTabTitle(found.title) || stringFor("roleClaude"));
  const working = tabs.filter((tab) => displayStatus(tab) === "working");
  const notice = node("p", "close-pane-working", stringFor("closePaneWorking", { n: working.length }));
  notice.setAttribute("role", "status");
  notice.hidden = working.length === 0;
  const count = node("p", "close-pane-note", stringFor("closePaneTabs", { n: tabs.length }));
  const list = node("ul", "close-pane-tabs");
  tabs.forEach((tab) => {
    const status = displayStatus(tab);
    const row = node("li", `close-pane-tab is-${status}`);
    row.append(
      node("span", "close-pane-tab-label",
        displaySeatTitle(tab) || stringFor("closePaneUnnamed")),
      node("span", "close-pane-tab-status", statusText(status))
    );
    list.append(row);
  });
  const error = node("p", "state-message compact error close-pane-error");
  error.hidden = true;
  const confirm = node("button", "close-pane-confirm",
    stringFor(working.length ? "closePaneConfirmWorking" : "closePaneConfirm", { n: working.length }));
  confirm.type = "button";
  const cancel = node("button", "close-pane-cancel", stringFor("closePaneCancel"));
  cancel.type = "button";
  const actions = node("div", "close-pane-actions");
  actions.append(cancel, confirm);
  sheet.append(title, name, notice, count, list, error, actions);
  if (working.length) sheet.classList.add("is-working");
  return { sheet, confirm, cancel, error };
}

function forgetOverview() {
  // The list must be read again, not shown from a snapshot that still has
  // the pane - and not from a poll that left before the pane was closed.
  overviewPoller.generation += 1;
  overviewPoller.request = null;
  overviewPoller.snapshot = null;
  overviewPoller.failure = null;
}

function leaveClosedTalk(sessionId) {
  discardDataCache("home");
  discardDataCache(`talk:${sessionId}`);
  forgetOverview();
  const previous = backEntries[backEntryPosition - 1];
  if (previous && previous.hash === "#/") history.back();
  else window.location.hash = "#/";
}

async function openClosePaneSheet(sessionId, found, opener) {
  // The pane is what the talk's last poll said, at most a few seconds old;
  // the server resolves it again on the confirm and refuses if it moved.
  if (document.querySelector(".close-pane-layer")) return;
  const layer = node("div", "close-pane-layer");
  const scrim = node("button", "close-pane-scrim");
  scrim.type = "button";
  scrim.setAttribute("aria-label", stringFor("closePaneCancel"));
  const { sheet, confirm, cancel, error } = closePaneSheetView(found);
  layer.append(scrim, sheet);
  let closing = false;
  const onKey = (event) => {
    if (event.key === "Escape" && !closing) {
      event.preventDefault();
      dismiss();
    }
  };
  const dismiss = () => {
    if (!layer.isConnected) return;
    layer.remove();
    window.removeEventListener("hashchange", dismiss);
    document.removeEventListener("keydown", onKey);
    if (opener && opener.isConnected) opener.focus();
  };
  scrim.addEventListener("click", () => { if (!closing) dismiss(); });
  cancel.addEventListener("click", () => { if (!closing) dismiss(); });
  window.addEventListener("hashchange", dismiss);
  document.addEventListener("keydown", onKey);
  confirm.addEventListener("click", async () => {
    if (closing) return;
    closing = true;
    confirm.disabled = true;
    cancel.disabled = true;
    error.hidden = true;
    const label = confirm.textContent;
    confirm.textContent = stringFor("closePaneClosing");
    try {
      await postJson(`api/talk/${encodeURIComponent(sessionId)}/close`, { paneId: found.paneId, tabId: found.activeTabId });
    } catch (failure) {
      closing = false;
      const code = failure && failure.details && failure.details.error;
      error.textContent = closePaneErrorText(failure);
      error.hidden = false;
      confirm.textContent = label;
      cancel.disabled = false;
      // A refusal, a pane that is already gone, or a close whose result
      // could not be read does not change by trying again; the sheet stays
      // so the reason can be read, then left.
      confirm.disabled = ["lastTabRefused", "sessionNotAllowed", "paneCloseUnverified"].includes(code);
      return;
    }
    dismiss();
    leaveClosedTalk(sessionId);
  });
  document.body.append(layer);
  cancel.focus();
}

function terminalComposer(terminalSessionId, onDelivered, refresh) {
  const form = node("form", "composer terminal-composer agent-codex");
  const row = node("div", "terminal-composer-row");
  const input = node("textarea", "composer-input");
  input.name = "text";
  input.rows = 1;
  input.maxLength = 4000;
  input.autocomplete = "off";
  input.placeholder = stringFor("composerPlaceholderCodex");
  input.setAttribute("aria-label", stringFor("composerPlaceholderCodex"));
  growComposerInput(form, input);
  const attachment = composerAttachment(input, "term");
  const draft = composerDraftController(input, "term", terminalSessionId);
  const send = node("button", "send-button");
  send.type = "submit";
  send.setAttribute("aria-label", stringFor("send"));
  send.title = stringFor("send");
  send.append(svgIcon("send"));
  const delivery = node("span", "terminal-delivery");
  delivery.setAttribute("role", "status");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const normalizedCharacters = sendNormalizationCount(input.value);
    const text = normalizeSendText(input.value);
    if (!text || send.disabled) {
      return;
    }
    draft.flush(text);
    input.value = "";
    input.style.height = "auto";
    send.disabled = true;
    send.setAttribute("aria-busy", "true");
    delivery.className = "terminal-delivery delivery-chip verifying";
    delivery.textContent = stringFor("termVerifying");
    try {
      const result = await postJson(
        `api/term/${encodeURIComponent(terminalSessionId)}/send`,
        { text, ...(normalizedCharacters ? { normalizedCharacters } : {}) },
        { timeoutMs: 30000 }
      );
      if (["delivered", "reserved"].includes(result.result)) {
        draft.accepted();
        delivery.className = "terminal-delivery delivery-chip sent";
        delivery.textContent = stringFor(result.result === "reserved" ? "reservedPending" : "sent");
        if (result.result === "delivered") onDelivered();
        await refresh(true).catch(() => {});
      } else {
        delivery.className = "terminal-delivery delivery-chip sendFailed";
        delivery.textContent = terminalSendFailure(result.reason);
        if (!input.value) {
          input.value = text;
          input.dispatchEvent(new Event("input"));
        }
      }
    } catch (error) {
      delivery.className = "terminal-delivery delivery-chip sendFailed";
      delivery.textContent = terminalSendFailure(error?.details?.error);
      if (!input.value) {
        input.value = text;
        input.dispatchEvent(new Event("input"));
      }
    } finally {
      send.disabled = false;
      send.removeAttribute("aria-busy");
      input.focus();
    }
  });
  const inputTools = node("div", "talk-input-tools");
  const reveal = node("button", "quick-command-reveal");
  reveal.type = "button";
  reveal.setAttribute("aria-label", stringFor("quickCommands"));
  reveal.append(svgIcon("shell"));
  inputTools.append(inputHitArea(input), reveal, attachment.attach);
  row.append(inputTools, send, attachment.fileInput);
  const quickCommands = quickCommandRow(input, QUICK_TERM_STORAGE_KEY, "term");
  const folding = foldTalkCommands(form, input, quickCommands, reveal);
  const stack = node("div", "talk-composer-stack");
  stack.append(folding.fold, row);
  form.append(stack, delivery, attachment.attachStatus);
  const quickCommandsCleanup = quickCommands ? quickCommands.quickCommandsCleanup : () => {};
  const composerCleanup = () => {
    folding.cleanup();
    attachment.cleanup();
    draft.cleanup();
    quickCommandsCleanup();
    if (activeQuickCommandCleanup === composerCleanup) {
      activeQuickCommandCleanup = null;
    }
  };
  form.quickCommandsCleanup = composerCleanup;
  activeQuickCommandCleanup = composerCleanup;
  return form;
}

async function renderTerminal(terminalSessionId) {
  clearPolling();
  const token = ++routeToken;
  const savedBackHash = window.sessionStorage.getItem("pocketWorkspaceHash");
  const backHash = savedBackHash && /^#\/ws\/[^/?#]+$/.test(savedBackHash)
    ? savedBackHash
    : "#/";
  const { view, content } = screen(stringFor("terminalTabs"), backHash);
  view.classList.add("terminal-screen", "is-loading");
  content.classList.add("terminal-chat-content");
  const availability = node("div", "terminal-readonly");
  availability.hidden = true;
  const refreshStatus = node(
    "div",
    "terminal-refreshing",
    stringFor("terminalRefreshing")
  );
  refreshStatus.setAttribute("aria-hidden", "true");
  const panel = node("div", "terminal-panel");
  const output = node("pre", "terminal-output is-loading", stringFor("loading"));
  output.setAttribute("aria-label", stringFor("terminalReadOnly"));
  output.setAttribute("aria-live", "polite");
  output.setAttribute("aria-atomic", "false");
  panel.append(output);
  const timeline = node("div", "timeline");
  const newMessages = node("button", "talk-new-messages", stringFor("newMessages"));
  newMessages.type = "button";
  newMessages.hidden = true;
  newMessages.addEventListener("click", () => scrollTalkToBottom(token, { smooth: true }));
  const reservations = node("div", "reservation-list");
  const thinkingSlot = node("div", "terminal-thinking-slot");
  content.replaceChildren(availability, refreshStatus, panel, thinkingSlot, reservations);
  app.replaceChildren(view);
  let refreshing = false;
  let hasOutput = false;
  let transcriptMode = false;
  let transcriptSessionId = null;
  let transcriptEvents = [];
  let currentAgent = "none";
  let currentStatus = "unknown";
  let previousOutput = "";
  let awaitingReply = false;
  let hasPostDeliverySnapshot = false;
  let workingSuppressed = false;
  let refreshQueued = false;
  let composer = null;
  let thinkingVisible = false;

  const paintThinking = () => {
    const visible = currentAgent === "codex" && !workingSuppressed &&
      (awaitingReply || currentStatus === "working");
    if (visible === thinkingVisible) {
      return;
    }
    thinkingVisible = visible;
    thinkingSlot.replaceChildren(...(visible ? [thinkingIndicator("codex")] : []));
  };

  const setMode = (data) => {
    reservations.replaceChildren(...(data.reservations || [])
      .filter(record => ["pending", "delivered", "failed"].includes(record.status))
      .map(record => reservationRow(terminalSessionId, record, () => refresh(true))));
    currentAgent = validAgent(data.agent);
    currentStatus = detailDisplayStatus(data);
    decorateAgentHeader(view, data.title, currentAgent, currentStatus);
    if (currentAgent === "codex") {
      availability.textContent = "";
      availability.hidden = true;
      if (!composer) {
        composer = terminalComposer(
          terminalSessionId,
          () => {
            awaitingReply = true;
            hasPostDeliverySnapshot = false;
            workingSuppressed = false;
            paintThinking();
          },
          refresh
        );
        view.append(composer);
      }
      if (transcriptMode && !newMessages.isConnected) composer.append(newMessages);
    } else {
      availability.hidden = false;
      availability.textContent = stringFor(
        currentAgent === "claude" ? "terminalReadOnly" : "sendUnavailableShell"
      );
      if (composer) {
        composer.quickCommandsCleanup();
        composer.remove();
        composer = null;
      }
    }
  };

  const refresh = async (queueIfBusy = false) => {
    if (refreshing || token !== routeToken) {
      if (queueIfBusy && token === routeToken) {
        refreshQueued = true;
      }
      return;
    }
    refreshing = true;
    refreshStatus.classList.add("is-active");
    output.setAttribute("aria-busy", "true");
    try {
      const data = await fetchDetailJson(
        `api/term/${encodeURIComponent(terminalSessionId)}`, "term", terminalSessionId
      );
      if (token !== routeToken) {
        return;
      }
      const lines = Array.isArray(data.lines)
        ? data.lines.filter((line) => typeof line === "string")
        : [];
      const isTranscript = data.source === "transcript";
      const incoming = Array.isArray(data.events) ? data.events.filter((event) =>
        event && ["user", "assistant"].includes(event.role) && typeof event.text === "string") : [];
      const nextOutput = isTranscript ? JSON.stringify(incoming) : lines.join("\n");
      const transcriptChanged = isTranscript && transcriptSessionId !== data.agentSessionId;
      const follow = isTranscript && (!transcriptMode || transcriptChanged || isNearPageBottom());
      const anchors = isTranscript && !follow ? captureTimelineAnchors(timeline) : [];
      const nextStatus = detailDisplayStatus(data);
      const outputChanged = hasOutput && (nextOutput !== previousOutput || transcriptChanged);
      const rested = ["idle", "attention", "stopped"];
      const becameRested = !rested.includes(currentStatus) && rested.includes(nextStatus);
      if (
        awaitingReply &&
        hasPostDeliverySnapshot &&
        outputChanged
      ) {
        awaitingReply = false;
        workingSuppressed = nextStatus === "working";
      }
      if (awaitingReply && becameRested) {
        awaitingReply = false;
        workingSuppressed = false;
      }
      if (awaitingReply) {
        if (hasPostDeliverySnapshot && rested.includes(nextStatus)) {
          awaitingReply = false;
        }
      }
      if (awaitingReply) {
        if (!hasPostDeliverySnapshot) {
          window.setTimeout(() => void refresh(true), 800);
        }
        hasPostDeliverySnapshot = true;
      }
      if (
        currentStatus !== "working" &&
        nextStatus === "working" &&
        !(hasPostDeliverySnapshot && outputChanged)
      ) {
        workingSuppressed = false;
      }
      if (becameRested) {
        workingSuppressed = false;
      }
      transcriptMode = isTranscript;
      view.classList.toggle("talk-screen", transcriptMode);
      content.classList.toggle("talk-content", transcriptMode);
      content.classList.toggle("terminal-chat-content", !transcriptMode);
      panel.hidden = transcriptMode;
      if (transcriptMode) {
        if (expandedSessionId !== terminalSessionId || transcriptChanged) {
          expandedEvents.clear();
          expandedToolGroups.clear();
          expandedSessionId = terminalSessionId;
        }
        if (!timeline.isConnected) content.insertBefore(timeline, thinkingSlot);
        transcriptEvents = mergeTranscriptEvents(transcriptChanged ? [] : transcriptEvents, incoming);
        transcriptSessionId = data.agentSessionId;
        if (!hasOutput || outputChanged) {
          const bubbles = timelineBubbles(transcriptEvents, terminalSessionId, "codex");
          replaceMotionRows(timeline, bubbles.length ? bubbles :
            [node("div", "state-message", stringFor("talkEmpty"))],
            "[data-timeline-key], .day-separator, .state-message",
            (item) => item.dataset.timelineKey || item.className + ":" + item.textContent);
          suppressRepeatedSpeaker(timeline);
        }
        if (outputChanged && !follow) newMessages.hidden = false;
      } else {
        timeline.remove();
        transcriptEvents = [];
        transcriptSessionId = null;
        newMessages.hidden = true;
        if (!hasOutput || outputChanged) output.textContent = nonemptyText(nextOutput);
      }
      previousOutput = nextOutput;
      output.classList.remove("error");
      fetchNotice(panel, null);
      fetchNotice(content, null);
      hasOutput = true;
      view.classList.remove("is-loading");
      output.classList.remove("is-loading");
      setMode(data);
      paintThinking();
      if (follow) scrollTalkToBottom(token, { smooth: outputChanged });
      else if (anchors.length) restoreTimelineAnchor(timeline, anchors);
    } catch (error) {
      if (token === routeToken && !hasOutput) {
        view.classList.remove("is-loading");
        output.classList.remove("is-loading");
        output.textContent = fetchErrorText(error);
        output.classList.add("error");
      } else if (token === routeToken) {
        fetchNotice(transcriptMode ? content : panel, error);
      }
    } finally {
      refreshing = false;
      if (token === routeToken) {
        refreshStatus.classList.remove("is-active");
        output.setAttribute("aria-busy", "false");
      }
      if (refreshQueued && token === routeToken) {
        refreshQueued = false;
        void refresh();
      }
    }
  };
  watchDetailOverview("term", terminalSessionId, 5000, (status) => {
    currentStatus = status;
    decorateAgentHeader(view, overviewDetail.data.title, currentAgent, currentStatus);
    paintThinking();
  });
  await refresh();
  if (token !== routeToken) {
    return;
  }
  startPolling(refresh, 5000);
}

// Not every machine message arrives in an envelope. When a conversation runs out
// of room the harness writes a summary of it and files it as something the
// reader said, so the first thing a new install showed was a wall of English
// about the reader in the third person, signed with their own name. They did not
// write it. The opening is fixed boilerplate, and matching it exactly is what
// keeps a real message that happens to discuss the subject out of this branch.
const CONTEXT_SUMMARY_OPENING =
  "This session is being continued from a previous conversation that ran out of";

function machineMessageKind(value) {
  const source = value.trim();
  if (source.startsWith(CONTEXT_SUMMARY_OPENING)) return "context-summary";
  const known = source.match(/^<(task-notification|system-reminder|command-name|command-message|local-command-stdout|user-prompt-submit-hook)(?=[\s>])/);
  // Known envelopes also arrive incomplete while the transcript is streaming.
  if (known) {
    const closing = `</${known[1]}>`;
    const end = source.indexOf(closing);
    if (end < 0 || end + closing.length === source.length) return known[1];
  }
  // Accept sibling/nested XML-like envelopes, but never swallow prose before,
  // between or after them. This is classification, not HTML interpretation.
  const tags = /<\/?([A-Za-z_][\w:.-]*)(?:\s+[^<>]*?)?\s*\/?>/g;
  const stack = [];
  let end = 0, first = "";
  for (const match of source.matchAll(tags)) {
    if (!stack.length && source.slice(end, match.index).trim()) return "";
    const token = match[0], name = match[1];
    first ||= name;
    if (token.startsWith("</")) {
      if (stack.pop() !== name) return "";
    } else if (!token.endsWith("/>")) stack.push(name);
    end = match.index + token.length;
  }
  return first && !stack.length && !source.slice(end).trim() ? first : "";
}

// The row used to read "System message: task-notification" - the raw XML tag,
// in English, on a Japanese screen. The six envelopes the classifier knows by
// name get a name; anything else keeps its tag, because an unnamed envelope is
// exactly the case where the reader needs to know which one it was.
function machineMessageLabel(kind) {
  const heading = stringFor("machineMessage");
  const named = window.STRINGS && window.STRINGS.machineKinds
    ? window.STRINGS.machineKinds[kind] : null;
  if (typeof named === "string" && named) return `${heading}: ${named}`;
  return kind ? `${heading}: ${kind}` : heading;
}

function machineEventBubble(event, eventKey, kind) {
  const item = node("article", "event machine");
  const bubble = node("div", "bubble");
  const stateKey = eventKey || eventStableKey(event);
  const label = machineMessageLabel(kind);
  const toggle = node("button", "expand-button machine-toggle");
  toggle.type = "button";
  const body = node("div", "machine-body");
  // Literal text retains every tag and character of the message in the DOM, even
  // when hidden. Do not parse this as markdown or insert executable markup.
  body.append(node("div", "event-text", event.text));
  if (event.toolCount) body.append(node("div", "tool-count", stringFor("toolRuns", { n: event.toolCount })));
  const when = relativeTime(event.ts);
  if (when) body.append(node("time", "event-time", when));
  const paint = () => {
    const expanded = expandedEvents.has(stateKey);
    body.hidden = !expanded;
    toggle.textContent = `${expanded ? "-" : "+"} ${label}`;
    toggle.setAttribute("aria-expanded", String(expanded));
    toggle.setAttribute("aria-label", `${label}. ${stringFor(expanded ? "showLess" : "showMore")}`);
  };
  toggle.addEventListener("click", () => {
    if (expandedEvents.has(stateKey)) expandedEvents.delete(stateKey);
    else expandedEvents.add(stateKey);
    paint();
  });
  paint();
  bubble.append(toggle, body);
  item.append(bubble);
  return item;
}

// Lifted out of the talk bubble so mail can use it. A message that runs long is
// clamped with a control to open it - no guessing about what the long part is,
// which matters for mail, where the long part is usually a signature and any
// rule for spotting one would sooner or later hide something that was written.
// Returns the control, or null when the body was short enough to leave alone.
//
// Character count alone was the wrong measure once Japanese arrived: 560 Latin
// characters is about eight lines, 560 Japanese characters is closer to thirty,
// and a signature that fills a phone screen can be under 400. The collapsed
// state shows ten lines, so more than ten lines is the other way in - a source
// line can only ever produce one rendered line or more, which makes it safe:
// when this fires there is always something hidden to reveal.
function collapseWhenLong(text, value, stateKey, limit = 560, lineLimit = 10) {
  const body = String(value);
  if (body.length <= limit && body.split("\n").length <= lineLimit) return null;
  const isExpanded = expandedEvents.has(stateKey);
  text.classList.toggle("collapsed", !isExpanded);
  const toggle = node("button", "expand-button", stringFor("showMore"));
  toggle.type = "button";
  toggle.textContent = stringFor(isExpanded ? "showLess" : "showMore");
  toggle.setAttribute("aria-expanded", String(isExpanded));
  toggle.addEventListener("click", () => {
    const beforeTop = toggle.getBoundingClientRect().top;
    const collapsed = text.classList.toggle("collapsed");
    if (collapsed) {
      expandedEvents.delete(stateKey);
    } else {
      expandedEvents.add(stateKey);
    }
    toggle.textContent = stringFor(collapsed ? "showMore" : "showLess");
    toggle.setAttribute("aria-expanded", String(!collapsed));
    window.requestAnimationFrame(() => {
      if (!toggle.isConnected) {
        return;
      }
      const delta = toggle.getBoundingClientRect().top - beforeTop;
      if (delta) {
        if (!drawerOpen) (app || window).scrollBy(0, delta);
      }
    });
  });
  return toggle;
}

function eventBubble(event, eventKey = "", agent = "claude") {
  const machineKind = machineMessageKind(typeof event.text === "string" ? event.text : "");
  if (machineKind) return machineEventBubble(event, eventKey, machineKind);
  const normalizedRole = event.role === "user" ? "user" : "assistant";
  const item = node("article", `event ${normalizedRole}`);
  const role = normalizedRole === "user"
    ? stringFor("roleMe")
    : validAgent(agent) === "codex" ? agentLabel("codex") : stringFor("roleClaude");
  item.append(node("span", "event-role", role));
  const bubble = node("div", "bubble");
  const value = typeof event.text === "string" ? event.text : "";
  if (value.trim()) {
    const text = node("div", "event-text");
    if (normalizedRole === "assistant") {
      text.append(renderMarkdown(value, "chat-markdown"));
    } else {
      appendLinkedText(text, value);
    }
    const toggle = collapseWhenLong(text, value, eventKey || eventStableKey(event));
    if (toggle) {
      bubble.append(text, toggle);
    } else {
      bubble.append(text);
    }
  }
  if (!value.trim() && !event.toolCount) {
    bubble.append(node("div", "state-message compact", stringFor("talkEmpty")));
  }
  if (event.toolCount) {
    bubble.append(node("div", "tool-count", stringFor("toolRuns", { n: event.toolCount })));
  }
  const when = relativeTime(event.ts);
  if (when) {
    bubble.append(node("time", "event-time", when));
  }
  item.append(bubble);
  return item;
}

function messagesFor(sessionId) {
  if (!optimisticMessages.has(sessionId)) {
    optimisticMessages.set(sessionId, []);
  }
  return optimisticMessages.get(sessionId);
}

function removeOptimistic(sessionId, localId) {
  const remaining = messagesFor(sessionId).filter((item) => item.localId !== localId);
  optimisticMessages.set(sessionId, remaining);
}

async function cancelReservation(sessionId, message, refresh) {
  if (!message.reservationId) {
    return false;
  }
  let removed = false;
  try {
    await deleteJson(`api/reservations/${encodeURIComponent(message.reservationId)}`);
    removeOptimistic(sessionId, message.localId);
    removeProgressLane(sessionId, message.localId);
    removed = true;
  } catch (error) {
    message.status = "reservedFailed";
    commitLaneMessageStatus(sessionId, message, Date.now());
  }
  if (typeof refresh === "function") {
    try {
      await refresh();
    } catch (error) {
      syncLaneTicker(sessionId);
    }
  } else {
    syncLaneTicker(sessionId);
  }
  return removed;
}

function emptyLaneState() {
  return {
    stage: 0,
    reachedAt: { 1: null, 2: null, 3: null, 4: null },
    reserved: false,
    failed: false,
    toolCount: 0,
    lastMotionAt: null,
    sendAt: null,
    reservedAt: null,
  };
}

function cloneLaneState(lane) {
  const source = lane || emptyLaneState();
  const reached = source.reachedAt || {};
  return {
    stage: Math.max(0, Math.floor(Number(source.stage) || 0)),
    reachedAt: {
      1: reached[1] == null ? null : reached[1],
      2: reached[2] == null ? null : reached[2],
      3: reached[3] == null ? null : reached[3],
      4: reached[4] == null ? null : reached[4],
    },
    reserved: Boolean(source.reserved),
    failed: Boolean(source.failed),
    toolCount: Math.max(0, Math.floor(Number(source.toolCount) || 0)),
    lastMotionAt: source.lastMotionAt == null ? null : source.lastMotionAt,
    sendAt: source.sendAt == null ? null : source.sendAt,
    reservedAt: source.reservedAt == null ? null : source.reservedAt,
  };
}

function advanceLane(lane, signal = {}) {
  const next = cloneLaneState(lane);
  if (next.stage >= 4) {
    return next;
  }
  const now = Number(signal.now);
  const at = Number.isFinite(now) ? now : 0;
  const mark = (stage) => {
    if (next.stage >= stage) {
      return;
    }
    for (let step = next.stage + 1; step <= stage; step += 1) {
      if (next.reachedAt[step] == null) {
        next.reachedAt[step] = at;
      }
    }
    next.stage = stage;
    next.lastMotionAt = at;
  };

  if (signal.failed) {
    next.failed = true;
  }
  if (signal.submitted || signal.delivered || signal.answered || next.stage >= 1) {
    mark(1);
    if (next.sendAt == null) {
      next.sendAt = next.reachedAt[1];
    }
  }
  if (next.failed) {
    next.reserved = false;
    return next;
  }

  const tools = Math.max(0, Math.floor(Number(signal.toolCount) || 0));
  if (tools > next.toolCount) {
    next.toolCount = tools;
    next.lastMotionAt = at;
  }
  if (signal.transcriptMotion) {
    next.lastMotionAt = at;
  }

  if (signal.reserved && next.stage < 2) {
    if (!next.reserved) {
      next.reservedAt = at;
      next.lastMotionAt = at;
    }
    next.reserved = true;
  }
  if (signal.delivered) {
    next.reserved = false;
    mark(2);
  }
  if (signal.thinking && next.stage >= 2) {
    mark(3);
  }
  if (signal.answered) {
    mark(4);
  }
  return next;
}

function padLaneClockPart(value) {
  return String(value).padStart(2, "0");
}

function formatLaneClock(timestamp) {
  const date = new Date(timestamp);
  if (!Number.isFinite(date.getTime())) {
    return "";
  }
  return [
    padLaneClockPart(date.getHours()),
    padLaneClockPart(date.getMinutes()),
    padLaneClockPart(date.getSeconds()),
  ].join(":");
}

function formatLaneElapsed(ms) {
  const totalSec = Math.max(0, Math.floor(Number(ms) / 1000) || 0);
  const hours = Math.floor(totalSec / 3600);
  const minutes = Math.floor((totalSec % 3600) / 60);
  const seconds = totalSec % 60;
  if (hours > 0) {
    return stringFor("laneElapsedHourMin", { h: hours, m: minutes });
  }
  if (minutes > 0) {
    return stringFor("laneElapsedMinSec", { m: minutes, s: seconds });
  }
  return stringFor("laneElapsedSec", { n: seconds });
}

function formatLaneDelta(ms) {
  const totalSec = Math.max(0, Math.floor(Number(ms) / 1000) || 0);
  return stringFor("laneDelta", { t: `${totalSec}s` });
}

function laneHistoryKey(sessionId) {
  return `${LANE_HISTORY_PREFIX}${sessionId}`;
}

function isLaneDuration(value) {
  return typeof value === "number" && Number.isFinite(value) && value > 0;
}

function readLaneHistory(sessionId) {
  try {
    const raw = window.localStorage.getItem(laneHistoryKey(sessionId));
    if (!raw) {
      return [];
    }
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      return [];
    }
    return parsed.filter(isLaneDuration).slice(-LANE_HISTORY_LIMIT);
  } catch (error) {
    return [];
  }
}

function writeLaneHistory(sessionId, durations) {
  try {
    window.localStorage.setItem(
      laneHistoryKey(sessionId),
      JSON.stringify(durations.slice(-LANE_HISTORY_LIMIT))
    );
  } catch (error) {
    return;
  }
}

function recordLaneDuration(sessionId, durationMs) {
  if (!isLaneDuration(durationMs)) {
    return;
  }
  const history = readLaneHistory(sessionId);
  history.push(durationMs);
  writeLaneHistory(sessionId, history);
}

function laneUsualMedian(durations) {
  const values = (Array.isArray(durations) ? durations : [])
    .filter(isLaneDuration)
    .sort((left, right) => left - right);
  if (values.length < LANE_USUAL_MIN_SAMPLES) {
    return null;
  }
  const mid = Math.floor(values.length / 2);
  if (values.length % 2 === 1) {
    return values[mid];
  }
  return Math.round((values[mid - 1] + values[mid]) / 2);
}

function numericCursor(event) {
  const raw = event && event.cursor;
  if (typeof raw !== "string" || !/^\d+$/.test(raw)) {
    return null;
  }
  const value = Number(raw);
  return Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function validCursorWatermark(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function maxTranscriptCursor(events) {
  let max = null;
  (Array.isArray(events) ? events : []).forEach((event) => {
    const value = numericCursor(event);
    if (value != null && (max === null || value > max)) {
      max = value;
    }
  });
  return max;
}

function eventsAfterCursor(events, watermark) {
  if (!validCursorWatermark(watermark)) {
    return [];
  }
  return (Array.isArray(events) ? events : []).filter((event) => {
    const value = numericCursor(event);
    return value != null && value > watermark;
  });
}

function assistantAnsweredAfter(events, watermark) {
  return eventsAfterCursor(events, watermark).some((event) => (
    event && event.role === "assistant" &&
    typeof event.text === "string" && event.text.trim()
  ));
}

function toolCountAfterCursor(events, watermark) {
  return eventsAfterCursor(events, watermark).reduce((total, event) => {
    return total + Math.max(0, Math.floor(Number(event && event.toolCount) || 0));
  }, 0);
}

function findOwnUserEvent(events, record) {
  if (!record || !record.attributionAllowed) {
    return null;
  }
  const matches = eventsAfterCursor(events, record.cursorWatermark).filter((event) => (
    event && event.role === "user" && event.text === record.text
  ));
  if (matches.length !== 1) {
    return null;
  }
  const candidate = matches[0];
  const candidateCursor = numericCursor(candidate);
  const competing = lanesFor(record.sessionId).some((other) => {
    if (other === record || other.text !== record.text || !other.attributionAllowed || other.lane.failed) {
      return false;
    }
    if (other.originCursor != null) {
      return other.originCursor === candidateCursor;
    }
    return other.cursorWatermark < candidateCursor;
  });
  return competing ? null : candidate;
}

function laneOriginCursor(record, events) {
  if (!record || !record.attributionAllowed) {
    return null;
  }
  if (record.originCursor != null) {
    return record.originCursor;
  }
  const own = findOwnUserEvent(events, record);
  if (own) {
    record.originCursor = numericCursor(own);
    return record.originCursor;
  }
  return null;
}

function pruneProgressLaneSessions() {
  while (progressLanes.size > LANE_SESSION_LIMIT) {
    const oldest = progressLanes.keys().next().value;
    progressLanes.delete(oldest);
  }
}

function progressLaneSessionCount() {
  return progressLanes.size;
}

function lanesFor(sessionId) {
  if (!progressLanes.has(sessionId)) {
    progressLanes.set(sessionId, []);
    pruneProgressLaneSessions();
  }
  return progressLanes.get(sessionId);
}

function findLane(sessionId, localId) {
  const records = progressLanes.get(sessionId) || [];
  return records.find((record) => record.localId === localId) || null;
}

function registerProgressLane(sessionId, message, watermark) {
  const existing = findLane(sessionId, message.localId);
  if (existing) {
    return existing;
  }
  const submittedAt = Number(message.submittedAt) || Date.now();
  const suppliedWatermark = watermark === undefined ? message.cursorWatermark : watermark;
  const attributionAllowed = validCursorWatermark(suppliedWatermark);
  const cursorWatermark = attributionAllowed ? suppliedWatermark : null;
  const record = {
    sessionId,
    localId: message.localId,
    text: message.text,
    submittedAt,
    status: message.status,
    createdOrder: ++laneRecordSerial,
    cursorWatermark,
    seenCursor: cursorWatermark,
    attributionAllowed,
    originCursor: null,
    seenEventKeys: null,
    lane: advanceLane(emptyLaneState(), { now: submittedAt, submitted: true }),
  };
  lanesFor(sessionId).push(record);
  pruneProgressLanes(sessionId);
  return record;
}

function removeProgressLane(sessionId, localId) {
  const records = progressLanes.get(sessionId);
  if (!records) {
    return;
  }
  const remaining = records.filter((record) => record.localId !== localId);
  if (remaining.length) {
    progressLanes.set(sessionId, remaining);
  } else {
    progressLanes.delete(sessionId);
  }
}

function pruneProgressLanes(sessionId) {
  const active = [];
  const terminal = [];
  lanesFor(sessionId).forEach((record) => {
    if (record.lane.stage >= 4 || record.lane.failed) {
      terminal.push(record);
    } else {
      active.push(record);
    }
  });
  const newest = (records) => records
    .slice()
    .sort((left, right) => left.createdOrder - right.createdOrder)
    .slice(-LANE_RECORD_LIMIT);
  progressLanes.set(sessionId, newest(active).concat(newest(terminal)));
}

function laneStatusFlags(status) {
  return {
    delivered: status === "sent" || status === "reservedDelivered",
    reserved: status === "reservedQueued" || status === "reservedPending",
    failed: status === "sendFailed" || status === "reservedFailed",
  };
}

function syncLaneRecord(record, context = {}) {
  if (!record || record.lane.stage >= 4) {
    return record;
  }
  const flags = laneStatusFlags(record.status);
  const events = context.events || [];
  const maxCursor = maxTranscriptCursor(events);
  const eventKeys = Array.from(new Set(events.map((event) => eventStableKey(event))));
  const stableMotion = record.seenEventKeys !== null && eventKeys.some(
    (key) => !record.seenEventKeys.includes(key)
  );
  const origin = laneOriginCursor(record, events);
  const cursorMotion =
    maxCursor != null &&
    record.seenCursor != null &&
    maxCursor > record.seenCursor;
  const motion = !context.olderLoad && (cursorMotion || stableMotion);
  const nextLane = advanceLane(record.lane, {
    now: context.now,
    submitted: true,
    delivered: flags.delivered,
    reserved: flags.reserved,
    failed: flags.failed,
    thinking: Boolean(context.thinkingVisible && flags.delivered),
    answered: origin != null && assistantAnsweredAfter(events, origin),
    toolCount: origin != null ? toolCountAfterCursor(events, origin) : 0,
    transcriptMotion: motion,
  });
  if (record.lane.stage < 4 && nextLane.stage >= 4) {
    const started = nextLane.sendAt != null ? nextLane.sendAt : record.submittedAt;
    recordLaneDuration(record.sessionId, nextLane.reachedAt[4] - started);
  }
  record.lane = nextLane;
  if (maxCursor != null) {
    record.seenCursor = record.seenCursor == null
      ? maxCursor
      : Math.max(record.seenCursor, maxCursor);
  }
  record.seenEventKeys = eventKeys;
  return record;
}

function commitLaneMessageStatus(sessionId, message, now) {
  const record = findLane(sessionId, message.localId);
  if (!record) {
    return null;
  }
  record.status = message.status;
  record.text = message.text;
  return syncLaneRecord(record, { now, events: [], thinkingVisible: false });
}

function syncSessionLanes(sessionId, context) {
  const messages = new Map(
    messagesFor(sessionId).map((message) => [message.localId, message])
  );
  lanesFor(sessionId).forEach((record) => {
    const message = messages.get(record.localId);
    if (message) {
      record.status = message.status;
      record.text = message.text;
    }
    syncLaneRecord(record, context);
  });
  pruneProgressLanes(sessionId);
}

function laneMarkerKind(row, lane) {
  if (row.id === "reserved") {
    return "current";
  }
  if (lane.stage >= 4 || lane.failed) {
    return row.stage <= lane.stage ? "done" : "pending";
  }
  if (lane.reserved && lane.stage < 2) {
    return row.stage <= 1 ? "done" : "pending";
  }
  if (row.stage === lane.stage) {
    return "current";
  }
  return row.stage < lane.stage ? "done" : "pending";
}

function progressLaneView(lane, now, usualMs, laneId) {
  if (!lane || lane.stage < 1) {
    return null;
  }
  const root = node("div", "progress-lane");
  root.setAttribute("role", "status");
  if (laneId) {
    root.dataset.laneId = String(laneId);
  }
  const rows = [{ id: 1, key: "laneSent", stage: 1 }];
  if (lane.reserved && lane.stage < 2 && !lane.failed) {
    rows.push({ id: "reserved", key: "laneReserved", stage: 1 });
  }
  rows.push(
    { id: 2, key: "laneDelivered", stage: 2 },
    { id: 3, key: "laneThinking", stage: 3 },
    { id: 4, key: "laneAnswered", stage: 4 }
  );
  const steps = node("div", "lane-steps");
  rows.forEach((row, index) => {
    const kind = laneMarkerKind(row, lane);
    const step = node("div", `lane-step is-${kind}`);
    if (kind === "current") {
      step.setAttribute("aria-current", "step");
    }
    const rail = node("div", "lane-rail");
    rail.append(node("span", `lane-marker is-${kind}`));
    if (index < rows.length - 1) {
      rail.append(node("span", "lane-connector"));
    }
    const copy = node("div", "lane-copy");
    copy.append(node("span", "lane-label", stringFor(row.key)));
    if (row.id === 3 && lane.toolCount > 0 && lane.stage >= 3) {
      copy.append(node("div", "lane-tools", stringFor("toolGroup", { n: lane.toolCount })));
    }
    const times = node("div", "lane-times");
    let reachedAt = null;
    if (row.id === "reserved") {
      reachedAt = lane.reservedAt;
    } else if (lane.reachedAt[row.stage] != null && kind !== "pending") {
      reachedAt = lane.reachedAt[row.stage];
    }
    if (reachedAt != null) {
      const clock = node("time", "lane-clock", formatLaneClock(reachedAt));
      times.append(clock);
    }
    const live = kind === "current" && lane.stage < 4 && !lane.failed;
    if (live && reachedAt != null) {
      const elapsed = node("span", "lane-elapsed", formatLaneElapsed(now - reachedAt));
      elapsed.dataset.laneLiveElapsed = "1";
      elapsed.dataset.laneLiveFrom = row.id === "reserved" ? "reserved" : String(row.stage);
      times.append(elapsed);
    } else if (kind === "done" && row.stage >= 2 && reachedAt != null) {
      const previous = lane.reachedAt[row.stage - 1];
      if (previous != null) {
        times.append(node("span", "lane-delta", formatLaneDelta(reachedAt - previous)));
      }
    }
    step.append(rail, copy, times);
    steps.append(step);
  });
  root.append(steps);
  const stalledFor = lane.lastMotionAt == null ? 0 : now - lane.lastMotionAt;
  const stalled = node(
    "div",
    "lane-stalled",
    stalledFor >= LANE_STALL_MS ? stringFor("laneStalled", { t: formatLaneElapsed(stalledFor) }) : ""
  );
  stalled.hidden = lane.failed || lane.stage >= 4 || stalledFor < LANE_STALL_MS;
  root.append(stalled);
  const usual = node(
    "div",
    "lane-usual",
    usualMs == null ? "" : stringFor("laneUsual", { t: formatLaneElapsed(usualMs) })
  );
  usual.hidden = usualMs == null;
  root.append(usual);
  rowContent.set(root, JSON.stringify([laneId, lane.stage, lane.reserved, lane.failed,
    lane.toolCount, lane.reachedAt, lane.reservedAt, lane.lastMotionAt]));
  return root;
}

function updateLaneLiveFields(root, lane, now, usualMs) {
  if (!root || !lane) {
    return;
  }
  const live = root.querySelector("[data-lane-live-elapsed]");
  if (live) {
    const from = live.dataset.laneLiveFrom;
    const reachedAt = from === "reserved"
      ? lane.reservedAt
      : lane.reachedAt[Number(from)];
    if (reachedAt != null) {
      live.textContent = formatLaneElapsed(now - reachedAt);
    }
  }
  const stalledFor = lane.lastMotionAt == null ? 0 : now - lane.lastMotionAt;
  const stalled = root.querySelector(".lane-stalled");
  if (stalled) {
    const show = !lane.failed && lane.stage < 4 && stalledFor >= LANE_STALL_MS;
    stalled.hidden = !show;
    stalled.textContent = show
      ? stringFor("laneStalled", { t: formatLaneElapsed(stalledFor) })
      : "";
  }
  const usual = root.querySelector(".lane-usual");
  if (usual) {
    usual.hidden = usualMs == null;
    usual.textContent = usualMs == null
      ? ""
      : stringFor("laneUsual", { t: formatLaneElapsed(usualMs) });
  }
}

function isActiveLaneRecord(record) {
  return Boolean(
    record && record.lane.stage >= 1 && record.lane.stage < 4 && !record.lane.failed
  );
}

function laneHasActiveTicker(sessionId) {
  if (typeof document !== "undefined" && document.querySelectorAll) {
    const talkVisible = typeof document.querySelector === "function" &&
      Boolean(document.querySelector(".talk-screen") || document.querySelector(".timeline"));
    const nodes = document.querySelectorAll(".progress-lane");
    if (talkVisible || nodes.length) {
      let connected = false;
      nodes.forEach((root) => {
        if (!root || root.isConnected === false) {
          return;
        }
        const record = findLane(sessionId, root.dataset && root.dataset.laneId);
        if (isActiveLaneRecord(record)) {
          connected = true;
        }
      });
      return connected;
    }
  }
  return lanesFor(sessionId).some(isActiveLaneRecord);
}

function syncLaneTicker(sessionId, token, tick) {
  if (typeof window === "undefined") {
    return;
  }
  if (token != null && token !== routeToken) {
    return;
  }
  if (token == null && laneTickResume && laneTickResume.sessionId !== sessionId) {
    return;
  }
  const tickFn = tick || (laneTickResume && laneTickResume.tick);
  const tickToken = token == null
    ? (laneTickResume && laneTickResume.token)
    : token;
  if (typeof tickFn === "function") {
    laneTickResume = { sessionId, token: tickToken, tick: tickFn };
  }
  if (typeof document !== "undefined" && document.hidden) {
    stopLaneTicker();
    return;
  }
  if (!laneHasActiveTicker(sessionId)) {
    stopLaneTicker();
    return;
  }
  if (laneTickTimer !== null) {
    return;
  }
  laneTickTimer = window.setInterval(() => {
    if (tickToken !== routeToken) {
      clearLaneTickerState();
      return;
    }
    if (typeof document !== "undefined" && document.hidden) {
      stopLaneTicker();
      return;
    }
    if (typeof tickFn === "function") {
      tickFn();
    }
    if (!laneHasActiveTicker(sessionId)) {
      stopLaneTicker();
    }
  }, 1000);
}

function handleLaneVisibilityChange() {
  if (typeof document === "undefined") {
    return;
  }
  if (document.hidden) {
    stopLaneTicker();
    return;
  }
  if (laneTickResume) {
    syncLaneTicker(laneTickResume.sessionId, laneTickResume.token, laneTickResume.tick);
  }
}

function laneTickerId() {
  return laneTickTimer;
}

function eventTextContent(element) {
  const textNode = element && element.querySelector && element.querySelector(".event-text");
  return textNode ? textNode.textContent : "";
}

function attachProgressLanes(sessionId, bubbles, now) {
  const usual = laneUsualMedian(readLaneHistory(sessionId));
  const optimisticIds = new Set(messagesFor(sessionId).map((message) => message.localId));
  const used = new Set();
  bubbles.forEach((item) => {
    if (item && item.querySelector && item.querySelector(".progress-lane")) {
      used.add(item);
    }
  });
  const slots = [];
  bubbles.forEach((item) => {
    if (!item || !item.className || used.has(item)) {
      return;
    }
    const className = String(item.className);
    if (!className.includes("user") || className.includes("optimistic")) {
      return;
    }
    slots.push(item);
  });
  const records = lanesFor(sessionId).filter((record) => (
    !optimisticIds.has(record.localId) && record.lane.stage >= 1
  ));
  records.slice().reverse().forEach((record) => {
    let target = null;
    for (let index = slots.length - 1; index >= 0; index -= 1) {
      const item = slots[index];
      if (used.has(item) || eventTextContent(item) !== record.text) {
        continue;
      }
      target = item;
      break;
    }
    if (!target) {
      return;
    }
    const view = progressLaneView(record.lane, now, usual, record.localId);
    if (!view) {
      return;
    }
    used.add(target);
    target.classList.add("has-lane");
    target.append(view);
  });
}

function optimisticBubble(sessionId, message, refresh) {
  const item = eventBubble({ role: "user", text: message.text, toolCount: 0 });
  item.dataset.motionKey = message.localId;
  rowContent.set(item, JSON.stringify([message.text, message.status, message.reservationId,
    Boolean(message.ambiguous), Boolean(message.retry)]));
  const reserved = ["reservedQueued", "reservedPending", "reservedFailed"].includes(message.status);
  item.classList.add(reserved ? "local-reservation" : "optimistic");
  item.classList.toggle("local-sending", message.status === "sending");
  item.classList.toggle("local-failed", message.status === "sendFailed");
  const bubble = item.querySelector(".bubble");
  const label = message.status === "sending" ? "sendingLocal"
    : message.status === "sendFailed" ? (message.ambiguous ? "sendUnconfirmed" : "sendFailed")
    : message.status;
  const labelText = label === "sendingLocal" && !window.STRINGS?.sendingLocal
    ? stringFor("sending") : stringFor(label);
  const chip = node("span", `delivery-chip ${message.status}`, labelText);
  bubble.append(chip);
  if (message.status === "sendFailed" && !message.ambiguous && message.retry) {
    const retry = node("button", "local-retry", stringFor("sendFailedRetry"));
    retry.type = "button";
    retry.addEventListener("click", () => void message.retry());
    bubble.append(retry);
  }
  if (message.reservationId && ["reservedQueued", "reservedPending"].includes(message.status)) {
    const cancel = node("button", "cancel-reservation", stringFor("cancelReservation"));
    cancel.type = "button";
    cancel.addEventListener("click", async () => {
      const removed = await cancelReservation(sessionId, message, refresh);
      if (!item.isConnected) {
        return;
      }
      if (removed) {
        item.remove();
      } else {
        item.replaceWith(optimisticBubble(sessionId, message, refresh));
      }
    });
    bubble.append(cancel);
  }
  const record = findLane(sessionId, message.localId) || registerProgressLane(sessionId, message);
  const view = progressLaneView(
    record.lane,
    Date.now(),
    laneUsualMedian(readLaneHistory(sessionId)),
    record.localId
  );
  if (view) {
    item.classList.add("has-lane");
    item.append(view);
  }
  return item;
}

function reservationStatusKey(status) {
  const keys = {
    pending: "reservedPending",
    delivered: "reservedDelivered",
    failed: "reservedFailed",
  };
  return keys[status] || null;
}

function reservationRow(sessionId, record, refresh) {
  const row = node("article", "reservation-row");
  row.append(node("div", "reservation-text", record.text));
  const statusKey = reservationStatusKey(record.status);
  if (statusKey) {
    row.append(node("span", `delivery-chip ${statusKey}`, stringFor(statusKey)));
  }
  if (record.status === "pending") {
    const cancel = node("button", "cancel-reservation", stringFor("cancelReservation"));
    cancel.type = "button";
    cancel.addEventListener("click", async () => {
      try {
        await deleteJson(`api/reservations/${encodeURIComponent(record.id)}`);
      } catch (error) {
        return;
      }
      refresh();
    });
    row.append(cancel);
  }
  return row;
}

function reconcileOptimistic(sessionId, events, reservations) {
  const records = new Map(reservations.map((record) => [record.id, record]));
  if (!claimedTalkEvents.has(sessionId)) claimedTalkEvents.set(sessionId, new Map());
  const claimed = claimedTalkEvents.get(sessionId);
  const reflected = talkEventKeys(events).filter(({ event }) => event.role === "user");
  const retained = [];
  let becameDelivered = false;
  messagesFor(sessionId).forEach((message) => {
    const record = message.reservationId ? records.get(message.reservationId) : null;
    if (record) {
      const key = reservationStatusKey(record.status);
      if (key) {
        if (key === "reservedDelivered" && message.status !== key) {
          becameDelivered = true;
        }
        message.status = key;
      }
      if (record.status === "canceled") {
        removeProgressLane(sessionId, message.localId);
      }
    }
    const lane = findLane(sessionId, message.localId);
    if (lane) {
      lane.status = message.status;
    }
    const match = reflected.find(({ event, key }) => {
      if (message.baselineReady === false) return false;
      if (event.text !== message.text || claimed.has(key)) return false;
      if (message.eventBaseline) {
        if (message.eventBaseline.has(key)) return false;
        const cursor = numericCursor(event);
        return !validCursorWatermark(message.cursorWatermark) ||
          (cursor !== null && cursor > message.cursorWatermark);
      }
      // Legacy lane callers may lack a baseline. Numeric cursors still bound
      // attribution; without either, do not guess from historical text.
      const watermark = validCursorWatermark(message.cursorWatermark)
        ? message.cursorWatermark : lane?.cursorWatermark;
      return validCursorWatermark(watermark) &&
        numericCursor(event) !== null && numericCursor(event) > watermark;
    });
    if (match) {
      claimed.set(match.key, { localId: message.localId, reservationId: message.reservationId });
      message.echoKey = match.key;
      message.onAccepted?.();
      message.status = message.reservationId ? "reservedDelivered" : "sent";
      if (lane?.lane.failed) {
        removeProgressLane(sessionId, message.localId);
        registerProgressLane(sessionId, message);
      } else if (lane) lane.status = message.status;
    } else if (!record || record.status !== "canceled") {
      retained.push(message);
    }
  });
  optimisticMessages.set(sessionId, retained);
  return becameDelivered;
}

function talkEventKeys(events) {
  const counts = new Map();
  return events.map((event) => {
    const id = event.cursor ?? event.eventId ?? event.event_id ?? event.uuid ?? event.id;
    const base = JSON.stringify(id != null ? ["id", String(id)]
      : ["event", event.role, event.ts ?? null, event.text, event.toolCount ?? 0]);
    const occurrence = counts.get(base) || 0;
    counts.set(base, occurrence + 1);
    return { event, key: `${base}:${occurrence}` };
  });
}

function stopTalkFollowing() {
  cancelAnimationFrame(talkScrollFrame);
  talkScrollFrame = 0;
  talkFollowUntil = 0;
  talkScrollMotion = null;
  app?.classList.remove("talk-follow-smooth");
}

function talkScrollWasInterrupted() {
  return talkScrollMotion && (Math.abs(app.scrollTop - talkScrollMotion.top) > 1 ||
    talkFollowUntil === 0 || (talkScrollMotion.smooth && !app.classList.contains("talk-follow-smooth")));
}

function scrollTalkToBottom(expectedToken = routeToken, { smooth = false } = {}) {
  stopTalkFollowing();
  if (!app) return;
  const started = performance.now();
  const from = app.scrollTop;
  const duration = smooth && !reducedMotion() ? motionDuration("--motion-screen") : 0;
  lastTalkScrollAt = started;
  talkFollowUntil = started + duration + 34;
  app.classList.toggle("talk-follow-smooth", duration > 0);
  talkScrollMotion = { top: from, smooth: duration > 0 };
  const notice = app.querySelector(".talk-new-messages");
  if (notice) notice.hidden = true;
  const step = (now) => {
    if (expectedToken !== routeToken || !app.querySelector(".talk-screen") || drawerOpen || talkScrollWasInterrupted()) {
      stopTalkFollowing();
      return;
    }
    const progress = duration ? Math.min(1, (now - started) / duration) : 1;
    const target = Math.max(0, app.scrollHeight - app.clientHeight);
    app.scrollTo({ top: from + (target - from) * (1 - (1 - progress) ** 3), behavior: "instant" });
    talkScrollMotion.top = app.scrollTop;
    if (progress < 1) talkScrollFrame = requestAnimationFrame(step);
    else stopTalkFollowing();
  };
  talkScrollFrame = requestAnimationFrame(step);
}

function isNearPageBottom(threshold = BOTTOM_SCROLL_THRESHOLD) {
  const root = app || document.documentElement;
  if (talkScrollWasInterrupted()) stopTalkFollowing();
  return Boolean(talkScrollMotion) ||
    root.scrollHeight - (app ? app.scrollTop + app.clientHeight : window.scrollY + window.innerHeight) <= threshold;
}

if (app) {
  const stopFollowing = stopTalkFollowing;
  app.addEventListener("scroll", () => {
    if (talkScrollWasInterrupted()) stopTalkFollowing();
    if (app.scrollHeight - app.scrollTop - app.clientHeight <= BOTTOM_SCROLL_THRESHOLD) {
      const notice = app.querySelector(".talk-new-messages");
      if (notice) notice.hidden = true;
    }
  }, { passive: true });
  app.addEventListener("wheel", stopFollowing, { passive: true });
  app.addEventListener("touchstart", stopFollowing, { passive: true });
  app.addEventListener("pointerdown", stopFollowing, { passive: true });
  app.addEventListener("keydown", (event) => {
    if (["PageUp", "PageDown", "Home", "End", "ArrowUp", "ArrowDown"].includes(event.key)) stopFollowing();
  });
}

// iOS reports the keyboard three ways: only the visual viewport shrinks, the
// layout viewport shrinks with it, or the layout shrinks a moment after the
// visualViewport event while window.innerHeight is still stale. Measured from
// innerHeight, the second left the home indicator's space under the input and
// the third floated fixed composers a keyboard above the keyboard (iPhone,
// 2026-09-13 20:22). The bottom position:fixed really uses is measured instead,
// and "keyboard open" is judged against the tallest view seen at this width.
const KEYBOARD_MIN_HEIGHT = 120;
let layoutBottomProbe = null;
let tallestViewport = { width: 0, height: 0 };
// The screen minus the status bar, once more: an installed copy whose status
// bar style was black-translucent when it was added to the home screen (the
// meta tag is read at install time and never again) gets, on iOS 26, a web
// view the size of the whole screen but a layout viewport shorter by the
// status bar, and nothing below the layout viewport is painted. The phone
// (2026-09-14 12:49, iPhone Air 420x912): layout 844, visual 912, the shell
// sized to 912 by the visual viewport, so the composer's last 68px were a
// black band with the input cut through. The shell now ends where painting
// ends, and the home indicator, which lies in the unpainted band, stops
// asking the composer for room.
let deadBottom = 0;
// Set once the boot line has gone; sync lines are sent only after it.
let viewportGeometryLogged = false;
// The tallest layout viewport that is painted, per width, for an installed
// copy: the one the page booted with. The same copy above later reported a
// layout viewport of 912 after the keyboard had been up (2026-09-14 15:03)
// while iOS still painted to 844, and the composer's foot was cut again. An
// installed copy's window never grows; a browser window does, so only the
// installed copy is held to its first height.
let layoutCeiling = { width: 0, bottom: Infinity };

function runningInstalled() {
  return navigator.standalone === true ||
    (typeof window.matchMedia === "function" && window.matchMedia("(display-mode: standalone)").matches);
}

function layoutViewportBottom() {
  if (!layoutBottomProbe || !layoutBottomProbe.isConnected) {
    layoutBottomProbe = document.createElement("div");
    layoutBottomProbe.setAttribute("aria-hidden", "true");
    layoutBottomProbe.style.cssText =
      "position:fixed;left:0;bottom:0;width:0;height:0;visibility:hidden;pointer-events:none";
    document.body.append(layoutBottomProbe);
  }
  return layoutBottomProbe.getBoundingClientRect().top;
}

function syncVisualViewport() {
  const viewport = window.visualViewport;
  if (!viewport) {
    return;
  }
  if (tallestViewport.width !== window.innerWidth) {
    tallestViewport = { width: window.innerWidth, height: 0 };
  }
  tallestViewport.height = Math.max(tallestViewport.height, viewport.height);
  // Nothing here scrolls the window (html and body clip), yet iOS scrolls it
  // to bring a focused field into view before this sync has resized the
  // shell, and the shell then leaves the screen: at 15:00 (2026-09-14) the
  // keyboard came up over a talk and only the canvas colour and iOS's own
  // bar were left. The shell is sized to the visual viewport below, which
  // puts the field in view; the scroll iOS made in the meantime is undone.
  if (window.scrollY || window.scrollX) {
    window.scrollTo(0, 0);
  }
  let layoutBottom = layoutViewportBottom();
  if (runningInstalled()) {
    if (layoutCeiling.width !== window.innerWidth) {
      layoutCeiling = { width: window.innerWidth, bottom: layoutBottom };
    }
    layoutBottom = Math.min(layoutBottom, layoutCeiling.bottom);
  }
  const visibleTop = Math.max(0, viewport.offsetTop);
  const visibleBottom = viewport.height + viewport.offsetTop;
  const geometry = [layoutBottom, window.innerWidth, viewport.height, viewport.offsetTop].join(":");
  if (geometry === lastVisualViewportGeometry) return;
  lastVisualViewportGeometry = geometry;
  const inset = Math.max(0, layoutBottom - visibleBottom);
  deadBottom = Math.max(0, Math.round(visibleBottom - layoutBottom));
  const keyboard = inset > 0 || viewport.height < tallestViewport.height - KEYBOARD_MIN_HEIGHT;
  const follow = app?.querySelector(".talk-screen") && isNearPageBottom();
  stopTalkFollowing();
  const previousTop = app?.scrollTop;
  const composer = app?.querySelector(".talk-composer");
  const last = Array.from(app?.querySelectorAll(".timeline .event") || []).at(-1);
  const composerTop = composer?.getBoundingClientRect().top;
  const keepLast = last && composer && last.getBoundingClientRect().bottom <= composerTop &&
    last.getBoundingClientRect().top >= app.getBoundingClientRect().top;
  const root = document.documentElement.style;
  root.setProperty("--visual-keyboard-inset", `${inset}px`);
  root.setProperty("--visual-layout-height", `${layoutBottom}px`);
  // Where the visual viewport begins, when iOS has moved it down the layout
  // viewport rather than scrolling the window: the shell starts there, so
  // its bar is the first thing on screen and its composer the last.
  root.setProperty("--visual-top", `${Math.round(visibleTop)}px`);
  root.setProperty("--viewport-safe-bottom", keyboard ? "0px" : "var(--safe-bottom)");
  root.setProperty("--dead-bottom", `${deadBottom}px`);
  if (deadBottom > 0) {
    root.setProperty("--safe-bottom", `max(0px, calc(env(safe-area-inset-bottom, 0px) - ${deadBottom}px))`);
  } else {
    root.removeProperty("--safe-bottom");
  }
  // What the phone said this time, for the paper: the keyboard's arrival and
  // departure are the geometries nobody has a number for (2026-09-14 15:00,
  // the talk vanished under the keyboard). The boot line and reportFault's
  // dozen per load bound it; the line is sent after this frame has painted.
  if (typeof reportViewportGeometry === "function" && viewportGeometryLogged) {
    window.setTimeout(() => reportViewportGeometry("sync"), 0);
  }
  if (app) {
    // Resize and anchoring happen in the same event, before the next paint.
    const anchoredTop = keepLast ? previousTop + composerTop - composer.getBoundingClientRect().top : previousTop;
    app.scrollTo({ top: follow ? app.scrollHeight : anchoredTop, behavior: "instant" });
  }
}

function fallbackAttachmentImage(file) {
  return new Promise((resolve, reject) => {
    const objectUrl = URL.createObjectURL(file);
    const image = new Image();
    image.onload = () => {
      resolve({
        source: image,
        width: image.naturalWidth,
        height: image.naturalHeight,
        cleanup: () => URL.revokeObjectURL(objectUrl),
      });
    };
    image.onerror = () => {
      URL.revokeObjectURL(objectUrl);
      reject(new Error("image decode failed"));
    };
    image.src = objectUrl;
  });
}

async function loadAttachmentImage(file) {
  try {
    const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
    return {
      source: bitmap,
      width: bitmap.width,
      height: bitmap.height,
      cleanup: () => bitmap.close(),
    };
  } catch (error) {
    return fallbackAttachmentImage(file);
  }
}

function attachmentBlob(canvas) {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) {
        resolve(blob);
      } else {
        reject(new Error("jpeg conversion failed"));
      }
    }, "image/jpeg", IMAGE_ATTACH_QUALITY);
  });
}

function attachmentBase64(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = typeof reader.result === "string" ? reader.result : "";
      const separator = result.indexOf(",");
      if (separator < 0) {
        reject(new Error("invalid data URL"));
        return;
      }
      resolve(result.slice(separator + 1));
    };
    reader.onerror = () => reject(reader.error || new Error("file read failed"));
    reader.readAsDataURL(blob);
  });
}

async function convertAttachment(file) {
  let decoded;
  let stage = "decode";
  try {
    decoded = await loadAttachmentImage(file);
    stage = "resize";
    if (!(decoded.width > 0 && decoded.height > 0)) {
      throw new Error("invalid image dimensions");
    }
    const scale = Math.min(1, IMAGE_ATTACH_MAX_DIMENSION / Math.max(
      decoded.width,
      decoded.height
    ));
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(decoded.width * scale));
    canvas.height = Math.max(1, Math.round(decoded.height * scale));
    const context = canvas.getContext("2d");
    if (!context) {
      throw new Error("canvas unavailable");
    }
    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = "high";
    context.drawImage(decoded.source, 0, 0, canvas.width, canvas.height);
    stage = "jpeg";
    const blob = await attachmentBlob(canvas);
    if (blob.size > IMAGE_ATTACH_MAX_BYTES) {
      const error = new Error("image too large");
      error.attachKey = "attachTooLarge";
      throw error;
    }
    stage = "read";
    return await attachmentBase64(blob);
  } catch (error) {
    reportFault("attachment", error?.attachKey === "attachTooLarge" ? "tooLarge" : stage);
    throw error;
  } finally {
    decoded?.cleanup();
  }
}

function attachmentErrorKey(error) {
  if (error && error.attachKey === "attachTooLarge") {
    return "attachTooLarge";
  }
  const reason = error && error.details && error.details.error;
  if (reason === "tooLarge") {
    return "attachTooLarge";
  }
  if (reason === "unsupportedType") {
    return "attachUnsupported";
  }
  return "attachFailed";
}

function foldTalkCommands(form, input, commands, reveal) {
  const fold = node("div", "quick-command-fold");
  fold.append(commands);
  commands.hidden = true;
  commands.inert = true;
  commands.id = "talk-quick-commands";
  reveal.setAttribute("aria-controls", commands.id);
  reveal.setAttribute("aria-expanded", "false");
  let open = false, animation = null, frame = null, anchor = null, target = 0, destroyed = false;
  const releaseAnchor = () => { anchor = null; };
  const align = () => {
    if (!anchor || !app || !form.isConnected) return;
    if (talkScrollMotion) { anchor = null; return; }
    app.scrollTo({ top: anchor.follow ? app.scrollHeight : anchor.top, behavior: "instant" });
  };
  const setOpen = (next) => {
    if (destroyed) return;
    if (next) commands.hidden = false;
    const height = next ? commands.getBoundingClientRect().height +
      parseFloat(getComputedStyle(form).gap) : 0;
    if (open === next && target === height) return;
    const from = fold.getBoundingClientRect().height;
    anchor = app && form.isConnected ? { follow: isNearPageBottom(), top: app.scrollTop } : null;
    animation?.cancel();
    if (frame !== null) cancelAnimationFrame(frame);
    open = next;
    target = height;
    commands.inert = !next;
    reveal.setAttribute("aria-expanded", String(next));
    fold.style.height = height + "px";
    const finish = () => {
      animation = null;
      if (frame !== null) cancelAnimationFrame(frame);
      frame = null;
      commands.hidden = !open;
      fold.style.height = open ? "auto" : "0px";
      align();
      anchor = null;
    };
    if (reducedMotion() || from === height) { finish(); return; }
    animation = fold.animate([{ height: from + "px" }, { height: height + "px" }], {
      duration: motionDuration("--motion-list"),
      easing: getComputedStyle(document.documentElement).getPropertyValue("--ease-out").trim(),
    });
    const current = animation;
    animation.onfinish = () => { if (animation === current) finish(); };
    const followFrame = () => {
      align();
      if (animation) frame = requestAnimationFrame(followFrame);
    };
    align();
    frame = requestAnimationFrame(followFrame);
  };
  const focusChanged = () => setOpen(document.activeElement === input ||
    document.activeElement === reveal || commands.contains(document.activeElement));
  const leaving = () => queueMicrotask(focusChanged);
  const focusInput = () => input.focus({ preventScroll: true });
  // Pointer use keeps the caret in the input. Keyboard users can move focus
  // into the commands and confirmation actions without collapsing them.
  const keepInputFocus = (event) => event.preventDefault();
  commands.addEventListener("mousedown", keepInputFocus);
  form.addEventListener("focusin", focusChanged);
  form.addEventListener("focusout", leaving);
  reveal.addEventListener("click", focusInput);
  const observer = new ResizeObserver(() => { if (open) setOpen(true); });
  observer.observe(commands);
  for (const type of ["wheel", "touchmove", "pointerdown"]) app?.addEventListener(type, releaseAnchor, { passive: true });
  return { fold, cleanup: () => {
    destroyed = true;
    animation?.cancel();
    animation = null;
    anchor = null;
    if (frame !== null) cancelAnimationFrame(frame);
    observer.disconnect();
    form.removeEventListener("focusin", focusChanged);
    form.removeEventListener("focusout", leaving);
    commands.removeEventListener("mousedown", keepInputFocus);
    reveal.removeEventListener("click", focusInput);
    for (const type of ["wheel", "touchmove", "pointerdown"]) app?.removeEventListener(type, releaseAnchor);
  } };
}

function composerAttachment(input, seatKind) {
  const attach = node("button", "attach-button");
  attach.type = "button";
  attach.setAttribute("aria-label", stringFor("attachImage"));
  attach.title = `${stringFor("attachImage")} (${stringFor("attachLongPress")})`;
  attach.append(svgIcon("image"));
  const fileInput = node("input", "attach-input");
  fileInput.type = "file";
  fileInput.accept = "image/*";
  fileInput.setAttribute("aria-label", stringFor("attachImage"));
  const attachStatus = node("span", "attach-status");
  attachStatus.setAttribute("role", "status");
  attachStatus.setAttribute("aria-live", "polite");
  let attaching = false;
  let attachDoneTimer = null;
  // One line for every image, chosen or pasted. A paste that lands while an
  // upload is running waits its turn, so paths land in the order given.
  const queue = [];
  // A tap opens the file picker. A press held ATTACH_LONG_PRESS_MS reads the
  // clipboard instead: iOS WebKit never offers Paste in the textarea for an
  // image-only clipboard (see acceptImagePaste), and its own Paste prompt
  // appears on this read. The read is started inside the pointerup handler,
  // the user gesture it needs; the click that follows is swallowed once.
  let pressTimer = null;
  let longPressed = false;
  let swallowClick = false;
  const endPress = () => {
    if (pressTimer !== null) window.clearTimeout(pressTimer);
    pressTimer = null;
  };
  attach.addEventListener("click", () => {
    if (swallowClick) {
      swallowClick = false;
      return;
    }
    if (!attaching) {
      fileInput.click();
    }
  });
  attach.addEventListener("pointerdown", (event) => {
    swallowClick = false;
    longPressed = false;
    endPress();
    if (!event.isPrimary || attaching) return;
    pressTimer = window.setTimeout(() => {
      pressTimer = null;
      longPressed = true;
    }, ATTACH_LONG_PRESS_MS);
  });
  attach.addEventListener("pointerup", () => {
    endPress();
    if (!longPressed) return;
    longPressed = false;
    swallowClick = true;
    void attachFromClipboard();
  });
  for (const type of ["pointercancel", "pointerleave"]) {
    attach.addEventListener(type, () => {
      endPress();
      longPressed = false;
    });
  }
  attach.addEventListener("contextmenu", (event) => event.preventDefault());
  const refuse = (key) => {
    attachStatus.className = "attach-status failed";
    attachStatus.textContent = stringFor(key);
  };
  const attachFromClipboard = async () => {
    if (attaching) return;
    const clipboard = navigator.clipboard;
    if (!clipboard || typeof clipboard.read !== "function") {
      refuse("attachClipboardUnsupported");
      return;
    }
    let items;
    try {
      items = await clipboard.read();
    } catch (error) {
      refuse("attachClipboardDenied");
      return;
    }
    const files = [];
    for (const item of items || []) {
      const type = (item.types || []).find((entry) => entry.startsWith("image/"));
      if (!type) continue;
      try {
        const blob = await item.getType(type);
        files.push(new File([blob], `clipboard.${type.split("/")[1] || "png"}`, { type }));
      } catch (error) {
        // An entry the browser cannot hand over is skipped; the rest still go.
      }
    }
    if (!files.length) {
      refuse("attachClipboardEmpty");
      return;
    }
    void attachFiles(files);
  };
  const attachOne = async (file) => {
    const dataBase64 = await convertAttachment(file);
    const targetKind = typeof seatKind === "function" ? await seatKind() : seatKind;
    const result = await postJson("api/upload", { dataBase64, mime: "image/jpeg", seatKind: targetKind });
    if (!result || typeof result.path !== "string" || !result.path) {
      throw new Error("missing upload path");
    }
    input.value = input.value ? `${input.value}\n${result.path}` : result.path;
    input.dispatchEvent(new Event("input"));
    input.setSelectionRange(input.value.length, input.value.length);
  };
  const attachFiles = async (files) => {
    queue.push(...files);
    if (attaching || !queue.length) {
      return;
    }
    attaching = true;
    attach.disabled = true;
    if (attachDoneTimer !== null) {
      window.clearTimeout(attachDoneTimer);
      attachDoneTimer = null;
    }
    attachStatus.className = "attach-status uploading";
    attachStatus.textContent = stringFor("attachUploading");
    // The first refusal is what the status says; later images still go.
    let failure = null;
    try {
      while (queue.length) {
        try {
          await attachOne(queue.shift());
        } catch (error) {
          failure = failure || error;
        }
      }
      if (failure) {
        attachStatus.className = "attach-status failed";
        attachStatus.textContent = stringFor(attachmentErrorKey(failure));
      } else {
        attachStatus.className = "attach-status done";
        attachStatus.textContent = stringFor("attachDone");
        attachDoneTimer = window.setTimeout(() => {
          attachStatus.textContent = "";
          attachStatus.className = "attach-status";
          attachDoneTimer = null;
        }, IMAGE_ATTACH_DONE_MS);
      }
    } finally {
      attaching = false;
      attach.disabled = false;
      fileInput.value = "";
      input.focus();
    }
  };
  fileInput.addEventListener("change", () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file || attaching) {
      return;
    }
    void attachFiles([file]);
  });
  return { attach, fileInput, attachStatus, attachFiles, attachFromClipboard, cleanup() {
    queue.length = 0;
    endPress();
    if (attachDoneTimer !== null) window.clearTimeout(attachDoneTimer);
  } };
}

function isAttachableImage(file) {
  if (!file) return false;
  if (typeof file.type === "string" && file.type.startsWith("image/")) return true;
  // A file with no type is taken only when its name says HEIC or HEIF.
  return !file.type && /\.(heic|heif)$/i.test(file.name || "");
}

// Image files on a paste, or none. Text, links and other files are left to
// the browser, so an ordinary paste is never taken.
function pastedImageFiles(clipboardData) {
  if (!clipboardData) return [];
  const files = Array.from(clipboardData.files || []).filter(isAttachableImage);
  if (files.length) return files;
  return Array.from(clipboardData.items || [])
    .filter((item) => item.kind === "file")
    .map((item) => item.getAsFile())
    .filter(isAttachableImage);
}

// iOS WebKit enables Paste in a textarea only when the clipboard holds text,
// a URL or WebKit's own custom data (supportedPlainTextPasteboardTypes in
// WKContentViewInteraction.mm). An image-only clipboard, such as a copied
// screenshot, fires no paste there, so this listener cannot see it.
function acceptImagePaste(form, input, attachment) {
  form.addEventListener("paste", (event) => {
    const target = event.target;
    if (target !== input && target?.closest?.("input, textarea, [contenteditable]")) return;
    const images = pastedImageFiles(event.clipboardData);
    if (!images.length) return;
    event.preventDefault();
    void attachment.attachFiles(images);
  });
}

function growComposerInput(form, input) {
  input.addEventListener("input", () => {
    const follow = form.isConnected && isNearPageBottom();
    const previousTop = app?.scrollTop;
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 124)}px`;
    if (follow) window.requestAnimationFrame(() => {
      // WebKit reveals the caret after dispatching input. Anchor after that
      // native adjustment, but before painting the newly enlarged composer.
      if (form.isConnected && app && !talkScrollMotion && app.scrollTop >= previousTop - 1) {
        app.scrollTo({ top: app.scrollHeight, behavior: "instant" });
      }
    });
  });
}

function talkComposer(sessionId, refresh, onDelivered, onOptimistic, getCursorWatermark, getSendSnapshot) {
  const form = node("form", "composer talk-composer");
  const row = node("div", "talk-composer-row");
  const input = node("textarea", "composer-input");
  input.name = "text";
  input.rows = 1;
  input.maxLength = 4000;
  input.placeholder = stringFor("composerPlaceholder");
  input.setAttribute("aria-label", stringFor("composerPlaceholder"));
  const send = node("button", "send-button");
  send.type = "submit";
  send.setAttribute("aria-label", stringFor("send"));
  send.title = stringFor("send");
  send.append(svgIcon("send"));
  const attachment = composerAttachment(input, async () => {
    const snapshot = getSendSnapshot ? await getSendSnapshot() : null;
    if (snapshot?.error) throw snapshot.error;
    return snapshot?.data?.agent === "codex" ? "term" : "talk";
  });
  const { attach, fileInput, attachStatus } = attachment;
  acceptImagePaste(form, input, attachment);
  growComposerInput(form, input);
  const draft = composerDraftController(input, "talk", sessionId);
  const pendingMessages = new Set(messagesFor(sessionId));
  if (talkSendInFlight.has(sessionId)) pendingMessages.add(talkSendInFlight.get(sessionId));
  for (const message of pendingMessages) {
    // A route return must not restore the already-submitted text as a new send.
    if (input.value === message.text) input.value = "";
  }
  const acceptedMessage = (message) => {
    if (message.ambiguous && input.value === message.text) input.value = "";
    draft.accepted();
  };
  for (const message of pendingMessages) message.onAccepted = () => acceptedMessage(message);
  input.addEventListener("focus", () => {
    syncVisualViewport();
  });
  const submitMessage = async (message) => {
    if (message.inFlight || message.echoKey || message.ambiguous || talkSendInFlight.has(sessionId)) return;
    if (message.status === "sendFailed") {
      message.submittedAt = Date.now();
      message.cursorWatermark = getCursorWatermark ? getCursorWatermark() : null;
      message.eventBaseline = new Set(talkEventKeys(observedTalkEvents.get(sessionId) || []).map(({ key }) => key));
      removeProgressLane(sessionId, message.localId);
      registerProgressLane(sessionId, message, message.cursorWatermark);
    }
    message.inFlight = true;
    talkSendInFlight.set(sessionId, message);
    message.status = "sending";
    const text = message.text;
    // Reusing a failed row also consumes its restored draft, not a second row.
    if (input.value === text) {
      draft.flush(text);
      input.value = "";
      input.style.height = "auto";
    }
    send.disabled = true;
    send.setAttribute("aria-busy", "true");
    const currentView = () => activeTalkView?.sessionId === sessionId &&
      activeTalkView.token === routeToken ? activeTalkView : null;
    (currentView()?.paint || onOptimistic)();
    if (form.isConnected) scrollTalkToBottom(routeToken, { smooth: true });
    try {
      if (getSendSnapshot && message.baselineReady === false) {
        const snapshot = await getSendSnapshot();
        if (snapshot.error) {
          const error = new Error("initialTranscriptUnavailable");
          error.notSent = true;
          throw error;
        }
        const events = snapshot.data.events || [];
        message.cursorWatermark = maxTranscriptCursor(events);
        message.eventBaseline = new Set(talkEventKeys(events).map(({ key }) => key));
        message.baselineReady = true;
        removeProgressLane(sessionId, message.localId);
        registerProgressLane(sessionId, message, message.cursorWatermark);
      }
      message.seatAssistantBaseline = [...(observedTalkEvents.get(sessionId) || [])]
        .reverse().find((event) => event.role === "assistant");
      message.seatAssistantBaseline = message.seatAssistantBaseline ? eventStableKey(message.seatAssistantBaseline) : "";
      const snapshot = getSendSnapshot ? await getSendSnapshot() : null;
      if (snapshot?.error || snapshot?.data?.canSend === false) {
        const error = new Error("sendTargetUnavailable");
        error.notSent = true;
        throw error;
      }
      const codex = snapshot?.data?.agent === "codex";
      const sendId = codex ? snapshot.data.terminalSessionId : sessionId;
      if (typeof sendId !== "string" || !/^[A-Za-z0-9_-]+$/.test(sendId)) {
        const error = new Error("sendTargetUnavailable");
        error.notSent = true;
        throw error;
      }
      const result = await postJson(`api/${codex ? "term" : "talk"}/${encodeURIComponent(sendId)}/send`, { text,
        ...(message.normalizedCharacters ? { normalizedCharacters: message.normalizedCharacters } : {}) });
      if (result.seat === "busy" && ["delivered", "reserved"].includes(result.result)) {
        if (!busySeatMessages.has(sessionId)) busySeatMessages.set(sessionId, new Map());
        message.seatObservedWorking = currentView()?.status() === "working";
        busySeatMessages.get(sessionId).set(message.localId, message);
      }
      if (result.result === "delivered") {
        message.onAccepted?.();
        message.status = "sent";
        (currentView()?.delivered || onDelivered)();
      } else if (result.result === "reserved") {
        message.onAccepted?.();
        message.status = "reservedQueued";
        message.reservationId = result.id;
      } else if (!message.echoKey) {
        message.status = "sendFailed";
        if (!input.value) {
          input.value = text;
          input.dispatchEvent(new Event("input"));
        }
      }
    } catch (error) {
      message.status = message.echoKey ? "sent" : "sendFailed";
      message.ambiguous = !message.echoKey && !error.notSent && (!error.status || error.status >= 500);
      if (!message.echoKey && !input.value) {
        input.value = text;
        input.dispatchEvent(new Event("input"));
      }
    } finally {
      message.inFlight = false;
      talkSendInFlight.delete(sessionId);
      commitLaneMessageStatus(sessionId, message, Date.now());
      (currentView()?.paint || onOptimistic)();
      send.disabled = false;
      send.removeAttribute("aria-busy");
      try {
        await (currentView()?.refresh || refresh)(true);
      } catch (refreshError) {
        syncLaneTicker(sessionId);
      }
      if (input.isConnected) input.focus({ preventScroll: true });
    }
  };
  messagesFor(sessionId).forEach((message) => {
    message.retry = () => submitMessage(message);
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const normalizedCharacters = sendNormalizationCount(input.value);
    const text = normalizeSendText(input.value);
    if (!text || send.disabled || talkSendInFlight.has(sessionId)) return;
    const failed = messagesFor(sessionId).find((message) => message.text === text && message.status === "sendFailed");
    if (failed) {
      if (!failed.ambiguous) void submitMessage(failed);
      return;
    }
    input.value = text;
    const submittedAt = Date.now();
    const cursorWatermark = getCursorWatermark ? getCursorWatermark() : null;
    const message = {
      localId: `${submittedAt}-${Math.random()}`,
      text, normalizedCharacters, status: "sending", reservationId: null, submittedAt, cursorWatermark,
      baselineReady: !getSendSnapshot,
      eventBaseline: new Set(talkEventKeys(observedTalkEvents.get(sessionId) || []).map(({ key }) => key)),
    };
    message.retry = () => submitMessage(message);
    message.onAccepted = () => acceptedMessage(message);
    messagesFor(sessionId).push(message);
    registerProgressLane(sessionId, message, cursorWatermark);
    void submitMessage(message);
  });
  const inputTools = node("div", "talk-input-tools");
  const reveal = node("button", "quick-command-reveal");
  reveal.type = "button";
  reveal.setAttribute("aria-label", stringFor("quickCommands"));
  reveal.append(svgIcon("shell"));
  inputTools.append(inputHitArea(input), reveal, attach);
  row.append(inputTools, send, fileInput);
  const quickCommands = quickCommandRow(input, QUICK_TALK_STORAGE_KEY, "talk");
  const folding = foldTalkCommands(form, input, quickCommands, reveal);
  const stack = node("div", "talk-composer-stack");
  stack.append(folding.fold, row);
  form.append(stack, attachStatus);
  const quickCommandsCleanup = quickCommands ? quickCommands.quickCommandsCleanup : () => {};
  const composerCleanup = () => {
    folding.cleanup();
    draft.cleanup();
    attachment.cleanup();
    quickCommandsCleanup();
    if (activeQuickCommandCleanup === composerCleanup) {
      activeQuickCommandCleanup = null;
    }
  };
  if (!quickCommands && activeQuickCommandCleanup) {
    activeQuickCommandCleanup();
  }
  form.quickCommandsCleanup = composerCleanup;
  activeQuickCommandCleanup = composerCleanup;
  return form;
}

function mergeTranscriptEvents(existing, incoming) {
  const merged = new Map();
  [...existing, ...incoming].forEach((event, index) => {
    if (!event || typeof event !== "object") {
      return;
    }
    const key = typeof event.cursor === "string" && /^\d+$/.test(event.cursor)
      ? `cursor:${event.cursor}`
      : `${eventStableKey(event)}:${index}`;
    merged.set(key, event);
  });
  return Array.from(merged.values()).sort((left, right) => {
    const leftCursor = Number(left.cursor);
    const rightCursor = Number(right.cursor);
    if (Number.isSafeInteger(leftCursor) && Number.isSafeInteger(rightCursor)) {
      return leftCursor - rightCursor;
    }
    return 0;
  });
}

function updateThinkingState(state, status, assistantKey, delivered = false) {
  const next = { ...state };
  const normalizedStatus = validStatus(status);
  const assistantArrived = next.initialized && assistantKey &&
    assistantKey !== next.assistantKey;
  if (next.status !== "working" && normalizedStatus === "working") {
    next.suppressed = false;
  }
  const rested = ["idle", "attention", "stopped"];
  const becameRested = next.initialized && !rested.includes(next.status) &&
    rested.includes(normalizedStatus);
  if (becameRested) {
    next.awaiting = false;
    next.suppressed = false;
  }
  if (delivered) {
    next.awaiting = true;
    next.suppressed = false;
  }
  if (assistantArrived) {
    next.awaiting = false;
    next.suppressed = normalizedStatus === "working";
  }
  next.initialized = true;
  next.status = normalizedStatus;
  next.assistantKey = assistantKey || next.assistantKey || "";
  next.visible = !next.suppressed &&
    (next.awaiting || normalizedStatus === "working");
  return next;
}

// --- AskUserQuestion card (DAN5A) ------------------------------------------
// The server reads the seat's screen with the bridge's own parser and answers
// through its answer-ask (digit keys, verified step by step). This card only
// shows what was read and says which row was chosen; it never picks keys.

function askFromPayload(data) {
  if (!data || typeof data !== "object" || data.available !== true) return null;
  const ask = data.ask;
  if (!ask || typeof ask !== "object" || typeof ask.question !== "string" ||
      typeof data.fingerprint !== "string" || !["single", "tabbed", "review"].includes(ask.kind)) {
    return null;
  }
  return ask;
}

// Every row but the multiSelect Submit row, which the confirm button replaces.
function askVisibleOptions(ask) {
  return (Array.isArray(ask.options) ? ask.options : [])
    .filter((option) => option && typeof option === "object" && option.role !== "submit");
}

function askChoosable(option) {
  return option.role === "option" && Number.isInteger(option.index) &&
    option.index >= 1 && option.index <= 9;
}

function askInitialSelection(ask) {
  const selected = new Set();
  if (ask.multi_select) {
    askVisibleOptions(ask).forEach((option) => {
      if (askChoosable(option) && option.checked === true) selected.add(option.index);
    });
  }
  return selected;
}

function askAnswerBody(ask, fingerprint, selected) {
  if (ask.kind === "review") return { fingerprint, submit: true };
  const picks = Array.from(selected).filter(Number.isInteger).sort((left, right) => left - right);
  return { fingerprint, answers: { [ask.question]: ask.multi_select ? picks : picks[0] } };
}

function askCardView(ask, onToggle, onConfirm) {
  const card = node("section", "ask-card");
  card.setAttribute("aria-label", stringFor("askCardLabel"));
  const head = node("div", "ask-head");
  head.append(node("span", "ask-kicker", stringFor("askCardLabel")));
  const tabs = (Array.isArray(ask.tabs) ? ask.tabs : [])
    .filter((tab) => tab && typeof tab.label === "string" && tab.label);
  if (tabs.length > 1) {
    const list = node("ol", "ask-tabs");
    list.setAttribute("aria-label", stringFor("askTabsLabel"));
    tabs.forEach((tab) => {
      const item = node("li", "ask-tab", (tab.answered === true ? "\u2713 " : "") + tab.label);
      if (tab.answered === true) item.classList.add("is-answered");
      if (tab.active === true) {
        item.classList.add("is-active");
        item.setAttribute("aria-current", "step");
      }
      list.append(item);
    });
    head.append(list);
  } else if (typeof ask.header === "string" && ask.header) {
    head.append(node("span", "ask-header", ask.header));
  }
  const review = ask.kind === "review";
  card.append(head, node("p", "ask-question", review ? stringFor("askReviewHeading") : ask.question));
  const buttons = [];
  if (review) {
    const list = node("dl", "ask-review");
    const answers = ask.review_answers && typeof ask.review_answers === "object" ? ask.review_answers : {};
    Object.entries(answers).forEach(([question, value]) => {
      const row = node("div", "ask-review-row");
      row.append(node("dt", "ask-review-question", question), node("dd", "ask-review-answer", String(value)));
      list.append(row);
    });
    card.append(list);
  } else {
    if (ask.multi_select) card.append(node("p", "ask-note", stringFor("askMultiHint")));
    const group = node("div", ask.multi_select ? "ask-options is-multi" : "ask-options");
    group.setAttribute("role", ask.multi_select ? "group" : "radiogroup");
    group.setAttribute("aria-label", ask.question);
    let inert = false;
    askVisibleOptions(ask).forEach((option) => {
      const choosable = askChoosable(option);
      const row = choosable ? node("button", "ask-option") : node("div", "ask-option is-inert");
      const body = node("span", "ask-option-body");
      const number = Number.isInteger(option.index) ? `${option.index}. ` : "";
      body.append(node("span", "ask-option-label", number + String(option.label || "")));
      if (typeof option.description === "string" && option.description) {
        body.append(node("span", "ask-option-description", option.description));
      }
      if (choosable) {
        const mark = node("span", "ask-mark");
        mark.setAttribute("aria-hidden", "true");
        row.type = "button";
        row.setAttribute("role", ask.multi_select ? "checkbox" : "radio");
        row.setAttribute("aria-checked", "false");
        row.dataset.askIndex = String(option.index);
        row.append(mark, body);
        row.addEventListener("click", () => onToggle(option.index));
        buttons.push(row);
      } else {
        // Typing an answer or chatting about it happens at the PC.
        inert = true;
        row.setAttribute("aria-disabled", "true");
        row.append(body);
      }
      group.append(row);
    });
    card.append(group);
    if (inert) card.append(node("p", "ask-note", stringFor("askFreeTextOnPc")));
    // The preview layout keeps each option's drawing beside the list on the
    // PC; the parser reads labels only there, so the card says where it is.
    if (ask.layout === "preview") card.append(node("p", "ask-note", stringFor("askPreviewOnPc")));
  }
  const actions = node("div", "ask-actions");
  const confirm = node("button", "ask-confirm", stringFor(review ? "askSubmitReview" : "askConfirm"));
  confirm.type = "button";
  confirm.addEventListener("click", onConfirm);
  actions.append(confirm);
  const note = node("div", "ask-status");
  note.setAttribute("role", "status");
  note.setAttribute("aria-live", "polite");
  card.append(actions, note);
  return { card, buttons, confirm, note };
}

const ASK_ERROR_STRINGS = {
  askReviewUnsupported: "askReviewUnsupported",
  askNotDelivered: "askNotDelivered",
};

// Reads the question when the talk screen opens and every few seconds while it
// stays open - faster while the seat is waiting - and not at all once the
// screen is left. A background Claude seat never reports "attention" for its
// question (mycmux only notices one on a tab it renders; pocket sees "working"
// or "unobserved", real seat 2026-09-14), so waiting for that state would never
// show the card. One POST at a time; reads that land during it are ignored.
function talkAskCard(sessionId, token, view, composer, onDelivered) {
  const url = `api/talk/${encodeURIComponent(sessionId)}/ask`;
  let status = "unknown";
  let timer = null;
  let fetching = false;
  let queued = false;
  let sending = false;
  let stopped = false;
  let carryNote = null;
  let current = null;
  let started = false;
  const live = () => !stopped && token === routeToken;
  const clearTimer = () => {
    if (timer !== null) window.clearTimeout(timer);
    timer = null;
  };
  const schedule = () => {
    clearTimer();
    if (!live() || sending) return;
    timer = window.setTimeout(() => {
      timer = null;
      void read();
    }, status === "attention" ? ASK_POLL_MS : ASK_POLL_MS * 2);
  };
  const paint = () => {
    if (!current) return;
    current.buttons.forEach((button) => {
      button.setAttribute("aria-checked", String(current.selected.has(Number(button.dataset.askIndex))));
      button.disabled = sending;
    });
    current.confirm.disabled = sending ||
      (current.ask.kind !== "review" && current.selected.size === 0);
    if (sending) current.card.setAttribute("aria-busy", "true");
    else current.card.removeAttribute("aria-busy");
  };
  const say = (key) => {
    if (current) current.note.textContent = key ? stringFor(key) : "";
  };
  const toggle = (index) => {
    if (!current || sending) return;
    const { ask, selected } = current;
    if (ask.multi_select) {
      if (selected.has(index)) selected.delete(index);
      else selected.add(index);
    } else {
      selected.clear();
      selected.add(index);
    }
    carryNote = null;
    say("");
    paint();
  };
  const remove = () => {
    if (current) current.card.remove();
    current = null;
  };
  const apply = (data) => {
    const ask = askFromPayload(data);
    if (!ask) {
      remove();
      return;
    }
    if (current && current.fingerprint === data.fingerprint) return;
    const followBottom = isNearPageBottom();
    const parts = askCardView(ask, toggle, () => void send());
    const next = { ...parts, ask, fingerprint: data.fingerprint, selected: askInitialSelection(ask) };
    if (current && current.card.isConnected) current.card.replaceWith(next.card);
    else if (composer.isConnected && composer.parentNode === view) view.insertBefore(next.card, composer);
    else return;
    current = next;
    paint();
    if (carryNote) say(carryNote);
    carryNote = null;
    if (followBottom) scrollTalkToBottom(token, { smooth: true });
  };
  const read = async () => {
    if (!live() || sending) return;
    if (fetching) {
      queued = true;
      return;
    }
    clearTimer();
    fetching = true;
    started = true;
    try {
      const data = await fetchJson(url);
      if (live() && !sending) apply(data);
    } catch (error) {
      // What is on screen stays; the next read decides.
    } finally {
      fetching = false;
      if (live() && !sending) {
        if (queued) {
          queued = false;
          void read();
        } else {
          schedule();
        }
      }
    }
  };
  const send = async () => {
    if (!current || sending || !live()) return;
    const target = current;
    const body = askAnswerBody(target.ask, target.fingerprint, target.selected);
    if (target.ask.kind !== "review" && !target.selected.size) return;
    sending = true;
    clearTimer();
    carryNote = null;
    paint();
    say("askSending");
    let reread = false;
    let failure = null;
    try {
      const result = await postJson(url, body, { timeoutMs: ASK_POST_TIMEOUT_MS });
      if (result && result.result === "delivered") {
        if (live() && current === target) remove();
      } else if (result && result.result === "advanced") {
        reread = true;
      } else {
        failure = "askFailed";
      }
    } catch (error) {
      const code = error && error.details && error.details.error;
      if (code === "askChanged") {
        reread = true;
        failure = "askChanged";
      } else {
        failure = ASK_ERROR_STRINGS[code] || "askFailed";
      }
    } finally {
      sending = false;
    }
    if (!live()) return;
    paint();
    say(failure && current === target ? failure : "");
    if (!current) {
      onDelivered();
      schedule();
      return;
    }
    if (reread) {
      carryNote = failure;
      queued = false;
      void read();
    } else {
      carryNote = failure;
      schedule();
    }
  };
  return {
    open() {
      if (!started) void read();
    },
    statusChanged(next) {
      const previous = status;
      status = next;
      if (previous === next || !live()) return;
      // Becoming "waiting" starts the reads; leaving it takes one more read,
      // so an answer given at the PC clears the card here too.
      if (next === "attention" || previous === "attention") void read();
    },
    stop() {
      stopped = true;
      clearTimer();
    },
    pending() {
      return timer !== null || fetching || sending;
    },
  };
}

async function renderTalk(sessionId) {
  clearPolling();
  const token = ++routeToken;
  let latestSendSnapshot = null;
  let settleSendSnapshot;
  const firstSendSnapshot = new Promise((resolve) => { settleSendSnapshot = resolve; });
  const shouldFocusComposer = pendingTalkFocusSessionId === sessionId;
  if (shouldFocusComposer) {
    pendingTalkFocusSessionId = null;
  }
  if (expandedSessionId !== sessionId) {
    expandedEvents.clear();
    expandedToolGroups.clear();
    expandedSessionId = sessionId;
  }
  const savedBackHash = window.sessionStorage.getItem("pocketWorkspaceHash");
  const backHash = savedBackHash && /^#\/ws\/[^/?#]+$/.test(savedBackHash) ? savedBackHash : "#/";
  const { view, content } = screen(stringFor("roleClaude"), backHash);
  view.classList.add("talk-screen");
  const knownTab = (drawerOverview?.workspaces || []).flatMap((workspace) =>
    sortedWorkspaceTabs(drawerWorkspaceData(workspace)))
    .find((tab) => tab.claudeSessionId === sessionId || (tab.agent === "codex" &&
      [tab.terminalSessionId, tab.agentSessionId].includes(sessionId)));
  if (knownTab) decorateAgentHeader(view, knownTab.title, validAgent(knownTab.agent), displayStatus(knownTab));
  content.classList.add("talk-content");
  const historyControl = node("div", "history-control");
  const noTranscript = node("div", "talk-no-transcript", stringFor("talkNoTranscript"));
  noTranscript.setAttribute("role", "status");
  noTranscript.hidden = true;
  const fallbackPanel = node("div", "terminal-panel talk-terminal-fallback");
  const fallbackOutput = node("pre", "terminal-output");
  fallbackOutput.setAttribute("aria-label", stringFor("terminalReadOnly"));
  fallbackOutput.setAttribute("aria-live", "polite");
  fallbackOutput.setAttribute("aria-atomic", "false");
  fallbackPanel.append(fallbackOutput);
  fallbackPanel.hidden = true;
  const timeline = node("div", "timeline");
  const cacheName = `talk:${sessionId}`;
  let firstPaint = true;
  const reservationList = node("div", "reservation-list");
  content.replaceChildren(
    historyControl,
    noTranscript,
    fallbackPanel,
    timeline,
    reservationList
  );
  let refreshing = false;
  let loadingOlder = false;
  let timelineRevision = 0;
  let transcriptEvents = [];
  let nextBefore = null;
  let atStart = false;
  let source = "";
  let currentAgent = "claude";
  let currentStatus = "unknown";
  let latestReservations = [];
  let refreshQueued = false;
  let historyLoadFailed = false;
  let fallbackOutputValue = "";
  let fallbackPostDeliverySnapshot = false;
  let anchorRestorePending = false;
  let timelineSignature = "";
  let thinkingState = {
    initialized: false,
    awaiting: false,
    suppressed: false,
    assistantKey: "",
    status: "unknown",
    visible: false,
  };
  let olderLoadPending = false;
  let currentSessionStatus = null;
  let transcriptIdentity = "";

  const paintSessionStatus = (status) => {
    // Unavailable metadata on an empty/failed wait is not a model change.
    // Explicit fields from a newer observation replace older values together.
    if (status && typeof status === "object") {
      const previousAt = Date.parse(currentSessionStatus?.at);
      const nextAt = Date.parse(status.at);
      if (!Number.isFinite(previousAt) || (Number.isFinite(nextAt) && nextAt >= previousAt)) {
        currentSessionStatus = status;
      }
    }
    decorateAgentHeader(view, view.querySelector(".topbar-title").textContent,
      currentAgent, currentStatus, currentSessionStatus);
  };

  const latestAssistantKey = () => {
    const event = [...transcriptEvents].reverse().find((item) => item.role === "assistant");
    return event ? eventStableKey(event) : "";
  };

  const paintHistory = () => {
    if (source !== "transcript") {
      if (historyControl.childElementCount) {
        historyControl.replaceChildren();
      }
      return;
    }
    if (atStart) {
      if (!historyControl.querySelector(".history-start")) {
        historyControl.replaceChildren(
          node("div", "history-start day-separator", stringFor("historyStart"))
        );
      }
      return;
    }
    let button = historyControl.querySelector(".load-older-button");
    if (!button) {
      button = node("button", "load-older-button");
      button.type = "button";
      button.addEventListener("click", loadOlder);
      historyControl.replaceChildren(button);
    }
    button.textContent = stringFor(loadingOlder ? "loadingOlder" : "loadOlder");
    button.disabled = loadingOlder || !nextBefore;
    let error = historyControl.querySelector(".history-load-error");
    if (historyLoadFailed) {
      if (!error) {
        error = node(
          "div",
          "history-load-error",
          stringFor("errorFetch").split(".")[0].split("\u3002")[0]
        );
        error.setAttribute("role", "status");
        historyControl.append(error);
      }
    } else if (error) {
      error.remove();
    }
  };

  const tickLaneClocks = () => {
    if (token !== routeToken) {
      stopLaneTicker();
      return;
    }
    const tickNow = Date.now();
    const usual = laneUsualMedian(readLaneHistory(sessionId));
    document.querySelectorAll(".progress-lane").forEach((root) => {
      const record = findLane(sessionId, root.dataset.laneId);
      if (record) {
        updateLaneLiveFields(root, record.lane, tickNow, usual);
      }
    });
  };

  const paintTimeline = (anchors = null) => {
    if (token !== routeToken) return;
    reconcileOptimistic(sessionId, transcriptEvents, latestReservations);
    const now = Date.now();
    syncSessionLanes(sessionId, {
      now,
      thinkingVisible: thinkingState.visible,
      events: transcriptEvents,
      olderLoad: olderLoadPending,
    });
    const busy = busySeatMessages.get(sessionId);
    if (busy) {
      for (const [id, message] of busy) {
        const canceled = latestReservations.some((record) => record.id === message.reservationId && record.status === "canceled");
        if (canceled || latestAssistantKey() !== message.seatAssistantBaseline ||
            (currentStatus === "working" && !message.seatObservedWorking)) busy.delete(id);
        message.seatObservedWorking = currentStatus === "working";
      }
      if (!busy.size) busySeatMessages.delete(sessionId);
    }
    const signature = JSON.stringify({
      busy: Array.from(busy?.keys() || []),
      source,
      agent: currentAgent,
      thinking: thinkingState.visible,
      events: transcriptEvents.map((event) => [eventStableKey(event), eventPaintSignature(event, currentAgent)]),
      optimistic: messagesFor(sessionId).map((message) => [
        message.localId,
        message.status,
        message.reservationId,
        stableTextHash(message.text),
      ]),
      lanes: lanesFor(sessionId).map((record) => [
        record.localId,
        record.status,
        record.lane.stage,
        record.lane.reserved ? 1 : 0,
        record.lane.failed ? 1 : 0,
        record.lane.toolCount,
      ]),
    });
    if (anchors === null && signature === timelineSignature) {
      return;
    }
    timelineSignature = signature;
    const shouldFollowBottom = !firstPaint && anchors === null && isNearPageBottom();
    const captured = anchors || (shouldFollowBottom ? [] : captureTimelineAnchors(timeline));
    const bubbles = source === "transcript"
      ? timelineBubbles(transcriptEvents, sessionId, currentAgent)
      : [];
    messagesFor(sessionId).forEach((message) => {
      bubbles.push(optimisticBubble(sessionId, message, refresh));
    });
    for (const message of busy?.values() || []) {
      const echo = talkEventKeys(transcriptEvents).find((entry) => entry.key === message.echoKey);
      const entry = echo && groupTimelineEvents(transcriptEvents).find((entry) => entry.event === echo.event);
      const echoKey = entry && stateTupleKey(sessionId, entry.key);
      const row = bubbles.find((item) => item.dataset.motionKey === message.localId ||
        (echoKey && item.dataset.timelineKey === echoKey));
      if (row) {
        const note = node("span", "delivery-chip seat-busy-note", stringFor("sentSeatBusy"));
        note.setAttribute("role", "status");
        row.querySelector(".bubble").append(note);
      }
    }
    attachProgressLanes(sessionId, bubbles, now);
    if (thinkingState.visible && ["claude", "codex"].includes(currentAgent)) {
      bubbles.push(thinkingIndicator(currentAgent));
    }
    const emptyState = source === "terminal"
      ? []
      : [node("div", "state-message", stringFor("talkEmpty"))];
    const oldKeys = new Set(Array.from(timeline.querySelectorAll(".event"),
      (item) => item.dataset.timelineKey || item.dataset.motionKey));
    const arrived = !firstPaint && anchors === null && bubbles.some((item) =>
      item.classList.contains("event") && !oldKeys.has(item.dataset.timelineKey || item.dataset.motionKey));
    replaceMotionRows(timeline, bubbles.length ? bubbles : emptyState,
      "[data-timeline-key], [data-motion-key], .day-separator, .thinking-indicator, .state-message",
      (item) => item.dataset.timelineKey || item.dataset.motionKey || `${item.className}:${item.textContent}`);
    suppressRepeatedSpeaker(timeline);
    syncLaneTicker(sessionId, token, tickLaneClocks);
    if (shouldFollowBottom) {
      scrollTalkToBottom(token, { smooth: true });
    } else if (captured.length) {
      anchorRestorePending = true;
      window.requestAnimationFrame(() => {
        restoreTimelineAnchor(timeline, captured);
        anchorRestorePending = false;
        if (refreshQueued && token === routeToken) {
          refreshQueued = false;
          void refresh();
        }
      });
    }
    if (arrived && !shouldFollowBottom) newMessages.hidden = false;
  };

  const markDelivered = () => {
    fallbackPostDeliverySnapshot = false;
    thinkingState = updateThinkingState(
      thinkingState,
      currentStatus,
      latestAssistantKey(),
      true
    );
    paintTimeline();
  };

  const paintData = (data, reservationData, older = false, anchors = null, cached = false) => {
    const previousSource = source;
    let fallbackOutputChanged = false;
    const incoming = Array.isArray(data.events)
      ? data.events.filter((event) => event && typeof event === "object")
      : [];
    const identity = !data.agentSessionId && data.source === "terminal" &&
      source === "transcript" && validAgent(data.agent) === currentAgent
      ? transcriptIdentity : `${validAgent(data.agent)}:${data.agentSessionId || sessionId}`;
    if (transcriptIdentity && identity !== transcriptIdentity) {
      transcriptEvents = [];
      currentSessionStatus = null;
      nextBefore = null;
      atStart = false;
      expandedEvents.clear();
      expandedToolGroups.clear();
      source = "";
    }
    transcriptIdentity = identity;
    currentAgent = validAgent(data.agent);
    currentStatus = detailDisplayStatus(data);
    decorateAgentHeader(view, data.title, currentAgent, currentStatus, currentSessionStatus);
    if (!older) paintSessionStatus(data.sessionStatus);
    if (currentAgent === "claude") askCard.statusChanged(currentStatus);
    closePane.update(data);
    if (data.source === "terminal" && !transcriptEvents.length) {
      source = "terminal";
      atStart = true;
      nextBefore = null;
      noTranscript.hidden = false;
      fallbackPanel.hidden = false;
      const lines = Array.isArray(data.lines)
        ? data.lines.filter((line) => typeof line === "string")
        : [];
      const nextFallbackOutput = lines.join("\n");
      fallbackOutputChanged = previousSource === "terminal" &&
        nextFallbackOutput !== fallbackOutputValue;
      fallbackOutputValue = nextFallbackOutput;
      fallbackOutput.textContent = nonemptyText(nextFallbackOutput);
    } else {
      source = "transcript";
      noTranscript.hidden = true;
      fallbackPanel.hidden = true;
      transcriptEvents = mergeTranscriptEvents(transcriptEvents, incoming);
      if (older) {
        nextBefore = typeof data.nextBefore === "string" ? data.nextBefore : null;
        atStart = data.atStart === true;
      } else if (previousSource !== "transcript" || (!nextBefore && !atStart)) {
        nextBefore = typeof data.nextBefore === "string" ? data.nextBefore : null;
        atStart = data.atStart === true;
      }
    }
    latestReservations = Array.isArray(reservationData.reservations)
      ? reservationData.reservations
      : latestReservations;
    observedTalkEvents.set(sessionId, transcriptEvents);
    const becameDelivered = reconcileOptimistic(
      sessionId,
      transcriptEvents,
      latestReservations
    );
    if (becameDelivered) {
      fallbackPostDeliverySnapshot = false;
    }
    olderLoadPending = older;
    thinkingState = updateThinkingState(
      thinkingState,
      currentStatus,
      latestAssistantKey(),
      becameDelivered
    );
    if (source === "terminal" && thinkingState.awaiting) {
      if (
        fallbackPostDeliverySnapshot &&
        (fallbackOutputChanged || ["idle", "attention", "stopped"].includes(currentStatus))
      ) {
        thinkingState.awaiting = false;
        thinkingState.suppressed = fallbackOutputChanged && currentStatus === "working";
        thinkingState.visible = false;
      }
      if (!fallbackPostDeliverySnapshot && thinkingState.awaiting) {
        window.setTimeout(() => void refresh(true), 800);
      }
      fallbackPostDeliverySnapshot = true;
    } else if (source !== "terminal") {
      fallbackPostDeliverySnapshot = false;
    }
    paintHistory();
    paintTimeline(anchors);
    olderLoadPending = false;
    const linked = new Set(
      messagesFor(sessionId).map((message) => message.reservationId).filter(Boolean)
    );
    for (const claim of claimedTalkEvents.get(sessionId)?.values() || []) {
      if (claim.reservationId) linked.add(claim.reservationId);
    }
    const rows = latestReservations
      .filter((record) => record.status !== "canceled" && !linked.has(record.id))
      .map((record) => reservationRow(sessionId, record, refresh));
    reservationList.replaceChildren(...rows);
    fetchNotice(reservationList, reservationData.error);
    if (!cached) {
      const snapshot = {
        data: { ...data, events: transcriptEvents, atStart, nextBefore },
        reservations: { reservations: latestReservations },
      };
      if (!older) writeDataCache(cacheName, snapshot);
      else if (sessionDataCache.has(cacheName)) sessionDataCache.get(cacheName).data = snapshot;
    }
    if (firstPaint) {
      firstPaint = false;
      if (!restoreScroll()) scrollTalkToBottom(token);
    }
  };

  const loadOlder = async () => {
    if (loadingOlder || atStart || !nextBefore || source !== "transcript") {
      return;
    }
    loadingOlder = true;
    historyLoadFailed = false;
    const revision = ++timelineRevision;
    paintHistory();
    const anchors = captureTimelineAnchors(timeline);
    try {
      const data = await fetchDetailJson(
        `api/talk/${encodeURIComponent(sessionId)}?limit=50&before=${encodeURIComponent(nextBefore)}`,
        "talk", sessionId
      );
      if (token !== routeToken || revision !== timelineRevision) {
        return;
      }
      paintData(data, { reservations: latestReservations }, true, anchors);
    } catch (error) {
      historyLoadFailed = true;
      return;
    } finally {
      loadingOlder = false;
      if (token === routeToken) {
        paintHistory();
      }
      if (refreshQueued && !anchorRestorePending && token === routeToken) {
        refreshQueued = false;
        void refresh();
      }
    }
  };

  const refresh = async (queueIfBusy = false) => {
    if (refreshing || loadingOlder || anchorRestorePending || token !== routeToken) {
      if (queueIfBusy && token === routeToken) {
        refreshQueued = true;
      }
      return;
    }
    refreshing = true;
    const revision = ++timelineRevision;
    try {
      const [data, reservationData] = await Promise.all([
        fetchDetailJson(`api/talk/${encodeURIComponent(sessionId)}?limit=50`, "talk", sessionId),
        fetchJson(`api/reservations?session=${encodeURIComponent(sessionId)}`)
          .catch((error) => ({ reservations: latestReservations, error })),
      ]);
      latestSendSnapshot = { data };
      settleSendSnapshot(latestSendSnapshot);
      if (token !== routeToken || revision !== timelineRevision) {
        return;
      }
      paintData(data, reservationData);
      fetchNotice(content, null);
    } catch (error) {
      settleSendSnapshot({ error });
      if (token !== routeToken || revision !== timelineRevision) {
        return;
      }
      if (firstPaint) {
        timeline.replaceChildren(node("div", "state-message error", fetchErrorText(error)));
        // A screen that says the conversation cannot be opened must not also
        // offer a box to type into it. The terminal screen already drops its
        // composer when there is nothing to send to; this is the same case
        // arriving from the read side.
        const code = error && error.details && error.details.error;
        if (code === "sessionNotAllowed" && composer && composer.isConnected) {
          const actions = view.querySelector(".topbar-actions");
          if (actions) actions.replaceChildren();
          closePane.retire();
          composer.quickCommandsCleanup();
          composer.remove();
        }
      } else {
        fetchNotice(content, error);
      }
      syncLaneTicker(sessionId, token, tickLaneClocks);
    } finally {
      refreshing = false;
      if (refreshQueued && !loadingOlder && token === routeToken) {
        refreshQueued = false;
        void refresh();
      }
    }
  };
  const composer = talkComposer(
    sessionId,
    refresh,
    markDelivered,
    paintTimeline,
    () => maxTranscriptCursor(transcriptEvents),
    () => latestSendSnapshot || firstSendSnapshot
  );
  // Answered: the card is gone and the conversation is read again.
  const askCard = talkAskCard(sessionId, token, view, composer, () => void refresh(true));
  activeAskCard = askCard;
  const newMessages = node("button", "talk-new-messages", stringFor("newMessages"));
  newMessages.type = "button";
  newMessages.hidden = true;
  newMessages.addEventListener("click", () => scrollTalkToBottom(token, { smooth: true }));
  composer.append(newMessages);
  activeTalkView = { sessionId, token, paint: paintTimeline, delivered: markDelivered, refresh, status: () => currentStatus };
  view.append(composer);
  const closePane = closePaneButton(sessionId);
  view.querySelector(".topbar-actions").append(closePane);
  app.replaceChildren(view);
  if (shouldFocusComposer) {
    window.requestAnimationFrame(() => {
      const input = composer.querySelector(".composer-input");
      if (token === routeToken && input && input.isConnected) {
        input.focus();
      }
    });
  }
  const cached = readDataCache(cacheName);
  if (cached) {
    paintData(cached.data, cached.reservations, false, null, true);
    showCachedData(cacheName);
  } else {
    // Both transcript history states occupy this same 48 px control slot.
    const historyPlaceholder = node("div", "felt-history");
    historyPlaceholder.setAttribute("aria-hidden", "true");
    historyControl.append(historyPlaceholder);
    delayedSkeleton(timeline);
  }
  watchDetailOverview("talk", sessionId, 2000, (status) => {
    currentStatus = status;
    decorateAgentHeader(view, overviewDetail.data.title, currentAgent, currentStatus, currentSessionStatus);
    if (currentAgent === "claude") askCard.statusChanged(currentStatus);
    thinkingState = updateThinkingState(thinkingState, currentStatus, latestAssistantKey());
    paintTimeline();
  });
  await refresh();
  if (token !== routeToken) {
    return;
  }
  if (currentAgent === "claude") askCard.open();
  startPolling(() => refresh(true), 2000);
  if (source === "transcript") {
    startTalkWait(sessionId, maxTranscriptCursor(transcriptEvents), (data) => {
      if (token !== routeToken) return;
      // A wait carries transcript fields only. Preserve live identity/status,
      // merge every appended event, and paint without awaiting metadata I/O.
      if (data.agentSessionId && latestSendSnapshot.data.agentSessionId &&
          data.agentSessionId !== latestSendSnapshot.data.agentSessionId) {
        void refresh(true);
        return;
      }
      const snapshot = { ...latestSendSnapshot.data, events: data.events,
        sessionStatus: data.sessionStatus, status: currentStatus, readStatus: currentStatus };
      paintData(snapshot, { reservations: latestReservations });
      if (data.changed) void refresh(true);
    });
  }
  syncLaneTicker(sessionId, token, tickLaneClocks);
}

function appendMarkdownInline(container, text) {
  const tokenPattern = /(`+)([^`\n]*?)\1|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|\\(\*)|(?<!\*)(\*{1,3})(?=\S)((?:\\.|[^*\\\n])+?)\6(?!\*)/gi;
  let cursor = 0;
  let match = tokenPattern.exec(text);
  while (match) {
    if (match.index > cursor) {
      appendLinkedText(container, text.slice(cursor, match.index));
    }
    if (match[1]) {
      // A path in backticks is the usual way an agent writes one, and it was
      // the one form that stayed dead text while the bare form was tappable.
      const quoted = detectWindowsPaths(match[2]);
      if (quoted.length === 1 && quoted[0].index === 0 && quoted[0].end === match[2].length) {
        container.append(pathLink(match[2]));
      } else {
        container.append(node("code", "markdown-inline-code", match[2]));
      }
    } else if (match[5]) {
      container.append(document.createTextNode(match[5]));
    } else if (match[6]) {
      if (/\s$/.test(match[7])) {
        appendLinkedText(container, match[0]);
      } else {
        const emphasis = node(match[6].length === 1 ? "em" : "strong", "");
        const inner = match[6].length === 3 ? node("em", "") : emphasis;
        appendMarkdownInline(inner, match[7]);
        if (inner !== emphasis) emphasis.append(inner);
        container.append(emphasis);
      }
    } else {
      let parsed = null;
      try {
        parsed = new URL(match[4]);
      } catch (error) {
        parsed = null;
      }
      if (!parsed || !["http:", "https:"].includes(parsed.protocol)) {
        appendLinkedText(container, match[0]);
      } else {
        const link = node("a", "markdown-link", match[3]);
        link.href = rewriteHubUrl(parsed.href);
        link.target = "_blank";
        link.rel = "noopener";
        container.append(link);
      }
    }
    cursor = tokenPattern.lastIndex;
    match = tokenPattern.exec(text);
  }
  if (cursor < text.length) {
    appendLinkedText(container, text.slice(cursor));
  }
}

function markdownTableCells(line) {
  const trimmed = line.trim();
  if (!trimmed.startsWith("|") || !trimmed.endsWith("|")) {
    return null;
  }
  const body = trimmed.replace(/^\|/, "").replace(/\|$/, "");
  return body.split("|").map((cell) => cell.trim());
}

function markdownTableDelimiter(line) {
  const cells = markdownTableCells(line);
  return Boolean(cells && cells.length && cells.every((cell) => /^:?-{3,}:?$/.test(cell)));
}

function markdownTable(header, rows) {
  const wrapper = node("div", "markdown-table-wrap");
  const table = node("table", "markdown-table");
  const head = node("thead", "");
  const headRow = node("tr", "");
  header.forEach((value) => {
    const cell = node("th", "");
    appendMarkdownInline(cell, value);
    headRow.append(cell);
  });
  head.append(headRow);
  const body = node("tbody", "");
  rows.forEach((values) => {
    const row = node("tr", "");
    header.forEach((unused, index) => {
      const cell = node("td", "");
      appendMarkdownInline(cell, values[index] || "");
      row.append(cell);
    });
    body.append(row);
  });
  table.append(head, body);
  wrapper.append(table);
  return wrapper;
}

function renderMarkdown(text, className = "") {
  const output = node("div", `markdown-view ${className}`.trim());
  const lines = joinWrappedWindowsPaths(
    text.replaceAll("\r\n", "\n").replaceAll("\r", "\n"),
  ).split("\n");
  let paragraph = [];
  let list = null;
  let codeLines = null;

  const flushParagraph = () => {
    if (!paragraph.length) {
      return;
    }
    const item = node("p", "markdown-paragraph");
    appendMarkdownInline(item, paragraph.join("\n"));
    output.append(item);
    paragraph = [];
  };
  const flushList = () => {
    list = null;
  };
  const flushCode = () => {
    if (codeLines === null) {
      return;
    }
    const pre = node("pre", "markdown-code");
    pre.append(node("code", "", codeLines.join("\n")));
    output.append(pre);
    codeLines = null;
  };

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (/^\s*```/.test(line)) {
      flushParagraph();
      flushList();
      if (codeLines === null) {
        codeLines = [];
      } else {
        flushCode();
      }
      continue;
    }
    if (codeLines !== null) {
      codeLines.push(line);
      continue;
    }
    const tableHeader = markdownTableCells(line);
    if (tableHeader && index + 1 < lines.length && markdownTableDelimiter(lines[index + 1])) {
      flushParagraph();
      flushList();
      const rows = [];
      index += 2;
      while (index < lines.length) {
        const values = markdownTableCells(lines[index]);
        if (!values) {
          index -= 1;
          break;
        }
        rows.push(values);
        index += 1;
      }
      output.append(markdownTable(tableHeader, rows));
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      flushParagraph();
      flushList();
      const item = node(`h${heading[1].length}`, "markdown-heading");
      appendMarkdownInline(item, heading[2]);
      output.append(item);
      continue;
    }
    const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
    const ordered = line.match(/^\s*\d+\.\s+(.+)$/);
    const listMatch = unordered || ordered;
    if (listMatch) {
      flushParagraph();
      const tag = ordered ? "ol" : "ul";
      if (!list || list.tagName.toLowerCase() !== tag) {
        list = node(tag, "markdown-list");
        output.append(list);
      }
      const item = node("li", "");
      appendMarkdownInline(item, listMatch[1]);
      list.append(item);
      continue;
    }
    if (!line.trim()) {
      flushParagraph();
      flushList();
      continue;
    }
    flushList();
    paragraph.push(line);
  }
  flushParagraph();
  flushCode();
  return output;
}

async function fetchFileText(path) {
  const response = await fetch(fileApiUrl(path), { cache: "no-store" });
  if (!response.ok) {
    const error = new Error(String(response.status));
    error.status = response.status;
    try {
      error.details = await response.json();
    } catch (parseError) {
      error.details = {};
    }
    throw error;
  }
  return response.text();
}

function fileFailure(content, error, retry) {
  const failure = node("div", "file-failure");
  failure.append(node("div", "state-message error", fileErrorText(error)));
  if (error && error.status === 503) {
    const button = node("button", "file-retry", stringFor("fileOpen"));
    button.type = "button";
    button.addEventListener("click", retry);
    failure.append(button);
  }
  content.replaceChildren(failure);
}

async function renderFile(path) {
  const token = ++routeToken;
  clearPolling();
  const savedBackHash = window.sessionStorage.getItem("pocketFileBackHash");
  const backHash = savedBackHash && /^#\/(?:talk|ws)\/[^/?#]+$/.test(savedBackHash)
    ? savedBackHash
    : "#/";
  // An HTML file is a document, and a document is read the way the hub reads
  // one: the body has the screen, and nothing above it says where the file
  // lives or how big it is. The other kinds keep their card - a path and a
  // size are what a .md or a .json opened on a phone is usually for.
  const htmlScreen = ["html", "htm"].includes(fileExtension(path));
  const { view, content } = htmlScreen
    ? screen(fileBaseName(path), backHash, { header: fileHeader, className: "file-screen" })
    : screen(stringFor("fileOpen"), backHash);
  if (!htmlScreen) content.classList.add("file-content");
  app.replaceChildren(view);
  showLoading(content);
  const load = async () => {
    showLoading(content);
    fileMetaCache.delete(path);
    try {
      const metadata = await getFileMeta(path);
      if (token !== routeToken) {
        return;
      }
      if (htmlScreen) {
        // The hub wins whenever it has the file: the chip asks the same two
        // questions, but a tap can arrive before either answer has, and then
        // this route is where the tap landed.
        const hubUrl = metadata.hubUrl || await hubUrlSoon(fileBaseName(path));
        if (token !== routeToken) {
          return;
        }
        if (hubUrl) {
          leaveForHub(hubUrl, backHash);
          return;
        }
      }
      if (!metadata.exists) {
        const error = new Error("404");
        error.status = 404;
        throw error;
      }
      if (metadata.size > 50 * 1024 * 1024) {
        const error = new Error("413");
        error.status = 413;
        error.details = { size: metadata.size };
        throw error;
      }
      const text = await fetchFileText(path);
      if (token !== routeToken) {
        return;
      }
      if (htmlScreen && metadata.kind === "html") {
        if (!text.trim()) {
          content.replaceChildren(node("div", "state-message compact", stringFor("empty")));
          return;
        }
        // The file API sends HTML as an attachment with a deny-all CSP, so a
        // new tab is a blank page on iPhone. Paint it here, without scripts,
        // and give it the rest of the screen.
        const frame = node("iframe", "html-view");
        frame.setAttribute("sandbox", "");
        frame.setAttribute("referrerpolicy", "no-referrer");
        frame.title = fileBaseName(path);
        frame.srcdoc = text;
        content.replaceChildren(frame);
        return;
      }
      const panel = node("div", "file-panel");
      panel.append(node("div", "file-path", path));
      const mtime = Number.isFinite(metadata.mtime)
        ? new Date(metadata.mtime * 1000).toLocaleString()
        : "";
      // The separator between facts on one line is a middle dot everywhere
      // else in the app; this line was the only one still using a hyphen,
      // which in a date reads as part of the date.
      const summary = [
        String(metadata.kind).toUpperCase(),
        formatFileSize(metadata.size),
        ...(mtime ? [mtime] : []),
      ].join(stringFor("metaJoin"));
      panel.append(node("div", "file-summary", summary));
      // Opening a file is choosing to read it, so the body is normally shown as
      // it is. These names are the exception: nobody opens a .env on a phone in
      // order to show it to the room, and the cost of being wrong is a leaked
      // credential against the inconvenience of reading it somewhere else.
      const secretFile = /(^|[\\/])(\.env(\.|$)|credentials?(\.|$)|id_(rsa|ed25519|ecdsa|dsa)\b)|\.(pem|key|p12|pfx|keystore)$/i
        .test(path);
      if (!text.trim()) {
        panel.append(node("div", "state-message compact", stringFor("empty")));
      } else if (secretFile) {
        panel.append(node("div", "state-message compact", stringFor("fileMasked")));
        panel.append(node("pre", "text-view", maskPreviewSecrets(text)));
      } else if (metadata.kind === "markdown") {
        panel.append(renderMarkdown(text));
      } else {
        panel.append(node("pre", "text-view", text));
      }
      content.replaceChildren(panel);
    } catch (error) {
      if (token === routeToken) {
        fileFailure(content, error, load);
      }
    }
  };
  await load();
}

function maskPreviewSecrets(value) {
  // Preview only. Never mutate the thread or reuse this in the opened body.
  // Deliberately redact the rest of a credential-labeled clause: values may
  // contain spaces or punctuation, and overmasking is preferable on a phone.
  return String(value ?? "")
    .replace(/((?:password|passwd|passphrase|pwd|pw|pin|token|secret|bearer|api[\s_-]*(?:key|\u30ad\u30fc)|\u30d1\u30b9\u30ef\u30fc\u30c9|\u6697\u8a3c(?:\u756a\u53f7)?)[\s:=\uff1a\uff1d-]*(?:(?:is\b|\u306f|\u304c)[\s:=\uff1a\uff1d-]*)?)([^\r\n\u3002\uff01\uff1f]+)/giu,
      (_, label) => `${label.trimEnd()} [redacted]`)
    .replace(/https?:\/\/[^\s<>"'\u3002]+/gi, "[redacted]")
    // A dense unlabeled run still reads as a secret, so it is masked - but a
    // path is not one, and masking every path makes the preview useless without
    // making it safer. Anything carrying a directory separator or a file
    // extension is left alone; the rest keeps the original rule.
    .replace(/[\x21-\x7e]{10,}/g, (word) => {
      if (/[\\/]/.test(word) || /\.[A-Za-z][A-Za-z0-9]{1,4}$/.test(word)) return word;
      const kinds = [/[a-z]/i, /[0-9]/, /[^a-z0-9]/i].filter((kind) => kind.test(word)).length;
      return kinds >= 2 || /^[0-9]+$/.test(word) ? "[redacted]" : word;
    });
}

// Status and the agent mark used to be excluded, for a stated reason: every row
// reading "working" is worth saying, because the moment one of them stops the
// difference has to be visible, and it could not be if the badge were hidden
// while they all agreed. Majority suppression answers that objection instead of
// ignoring it - the row that stops becomes the minority, so its badge is exactly
// the one that stays. Measured against the official app on the same phone width,
// this list painted eighty-nine pieces of text to its twenty, and eleven of
// thirteen rows here said the same two words.
//
// State labels remain visible in lists without state section headings.
// Agent marks and urgency counts also retain their meaning on every row.
const ROW_BADGE_SELECTOR =
  ".tab-kind, .tab-agent-kind, .tab-duplicate-id, .tab-not-started";
const BADGE_MAJORITY_MIN_ROWS = 5;
const ROW_MAJORITY_SELECTOR = "";

function suppressUniformBadges(rows, selector, majoritySelector = "") {
  // A badge earns its place by telling rows apart. One carried by more than half
  // the visible rows tells them apart for nobody, so it gives up its ink and
  // keeps its space, and the rows that differ are what is left to see. Apply
  // after filtering/folding, and recompute on every paint. Missing badges count
  // as a value of their own, which is why a row with no status still counts.
  const groups = Array.from(rows, (row) => Array.from(row.querySelectorAll(selector)));
  if (!groups.length) return;
  const signature = (badge) => JSON.stringify([badge.textContent, badge.title]);
  const counts = new Map();
  groups.forEach((badges) => {
    new Set(badges.map(signature)).forEach((value) => {
      counts.set(value, (counts.get(value) || 0) + 1);
    });
  });
  // Two rules, and the older one is the floor. A value on every row still says
  // nothing, at any length - except on a list of one, where hiding the only
  // status leaves nothing at all, which a test caught. The majority rule is for
  // lists long enough that scanning is the problem: on three rows, hiding the
  // two that agree destroys the contrast with the one that does not, and there
  // was never much to scan. Five is where a list starts being a list.
  const everywhere = (count) => count === groups.length && groups.length >= 2;
  const majority = (count) =>
    groups.length >= BADGE_MAJORITY_MIN_ROWS && count > groups.length / 2;
  groups.forEach((badges, index) => {
    const spoken = [];
    badges.forEach((badge) => {
      const count = counts.get(signature(badge));
      const hidden = everywhere(count) ||
        (Boolean(majoritySelector) && badge.matches(majoritySelector) && majority(count));
      badge.classList.toggle("uniform-badge", hidden);
      if (hidden && badge.textContent.trim()) spoken.push(badge.textContent.trim());
    });
    // Hiding a badge takes it out of the accessibility tree with it, and the
    // row's name is built from its text. What the eye can infer from the colour
    // bar, a screen reader cannot, so the row says it instead.
    const row = rows[index] || badges[0]?.closest(".talk-row, .mail-row");
    if (!row) return;
    if (spoken.length) {
      row.dataset.suppressedBadges = spoken.join(" ");
      row.setAttribute("aria-label",
        [row.textContent.trim().replace(/\s+/g, " "), ...spoken].join(" "));
    } else if (row.dataset.suppressedBadges) {
      delete row.dataset.suppressedBadges;
      row.removeAttribute("aria-label");
    }
  });
}

function suppressRepeatedSpeaker(timeline) {
  // A chat names the speaker when the speaker changes. Three answers in a row
  // each carried "Claude" over them - a row of type per message, saying what
  // the row above it had just said - and the bubble's own edge says whose it
  // is either way. A tool run between two answers is the same speaker still
  // working, so it does not break the run; a system notice or a new day does,
  // and the name comes back.
  //
  // The label is hidden the way the header's status is: out of the layout,
  // still in the accessibility tree, because "who said this" is exactly what a
  // reader who cannot see the edge needs.
  // Rows that arrived together are wrapped in a .motion-batch so they can fade
  // in as one, so the timeline's own children are not the reading order.
  const rows = [];
  Array.from(timeline.children).forEach((child) => {
    if (child.classList && child.classList.contains("motion-batch")) {
      rows.push(...Array.from(child.children));
    } else {
      rows.push(child);
    }
  });
  let previous = null;
  rows.forEach((row) => {
    if (!row.classList || !row.classList.contains("event")) {
      if (row.classList && row.classList.contains("day-separator")) previous = null;
      return;
    }
    const label = row.querySelector(".event-role");
    if (!label) {
      previous = null;
      return;
    }
    const speaker = label.textContent;
    label.classList.toggle("repeated-speaker", speaker === previous);
    previous = speaker;
  });
}

function rowInViewport(row) {
  const box = row.getBoundingClientRect();
  if (!box.width || !box.height) return false;
  let top = row.closest(".screen")?.querySelector(".topbar")?.getBoundingClientRect().bottom || 0;
  let bottom = window.innerHeight, left = 0, right = window.innerWidth;
  for (let parent = row.parentElement; parent; parent = parent.parentElement) {
    const style = getComputedStyle(parent), clip = parent.getBoundingClientRect();
    if (/(auto|scroll|hidden|clip)/.test(style.overflowY)) {
      top = Math.max(top, clip.top); bottom = Math.min(bottom, clip.bottom);
    }
    if (/(auto|scroll|hidden|clip)/.test(style.overflowX)) {
      left = Math.max(left, clip.left); right = Math.min(right, clip.right);
    }
  }
  return box.bottom > top && box.top < bottom && box.right > left && box.left < right;
}

let listBadgeFrame = null;
function scheduleListBadges() {
  if (listBadgeFrame !== null) return;
  listBadgeFrame = window.requestAnimationFrame(() => {
    listBadgeFrame = null;
    document.querySelectorAll(".mail-list, .drawer-workspaces, .screen .workspace-tab-list").forEach((list) => {
      const selector = list.matches(".mail-list") ? ".mail-badge:not(.urgent)" : ROW_BADGE_SELECTOR;
      // Mail rows no longer reserve hidden badge height. Classify the complete
      // list so revealing a badge cannot change its own comparison set at the
      // viewport edge and alternate between two row heights on scroll.
      list.querySelectorAll(list.matches(".mail-list") ? ".mail-badge" : selector)
        .forEach(badge => badge.classList.remove("uniform-badge"));
      const allRows = Array.from(list.querySelectorAll(".mail-row, .talk-row"));
      const rows = list.matches(".mail-list") ? allRows : allRows.filter(rowInViewport);
      suppressUniformBadges(rows, selector,
        list.matches(".mail-list") ? "" : ROW_MAJORITY_SELECTOR);
    });
  });
}

function mailBadge(text, className) {
  return node("span", `mail-badge ${className}`, text);
}

function mailDraftingBadge() {
  const badge = node("span", "mail-badge mail-row-drafting");
  const spinner = node("span", "mail-row-spinner");
  spinner.setAttribute("aria-hidden", "true");
  badge.append(spinner, node("span", "", stringFor("mailRowDrafting")));
  return badge;
}

function mailRowContent(row, thread) {
  if (thread.urgent) {
    row.classList.add("urgent", "attention");
  } else {
    row.classList.add("idle");
  }
  const copy = node("span", "mail-copy");
  const head = node("span", "mail-row-head");
  head.append(node("strong", "mail-from", thread.from));
  head.append(node("span", "mail-age", mailAgeText(thread.ageDays)));
  const subject = node("span", "mail-subject", thread.subject);
  const snippet = node("span", "mail-snippet", maskPreviewSecrets(thread.snippet));
  const badges = node("span", "mail-badges");
  badges.append(mailBadge(stringFor("unreplied"), "unreplied"));
  if (thread.urgent) {
    badges.append(mailBadge(stringFor("urgent"), "urgent"));
  }
  if (thread.hasDraft) {
    badges.append(mailBadge(stringFor("draftReady"), "draft-ready"));
  }
  if (["queued", "drafting", "checking"].includes(thread.activeJobStatus)) {
    badges.append(mailDraftingBadge());
  }
  if (thread.sourceWarning) {
    const warning = mailBadge("!", "source-warning");
    warning.title = window.STRINGS?.mailSourceWarning || stringFor("statusAttention");
    badges.append(warning);
  }
  if (thread.threadingDegraded) {
    const degraded = mailBadge("RFC ID?", "threading-degraded");
    degraded.title = stringFor("mailThreadingDegraded");
    badges.append(degraded);
  }
  copy.append(head, subject, snippet, badges);
  row.append(avatar(thread.from, "mail-avatar"), copy);
}

function mailSwipeDecision(dx, dy) {
  const horizontal = Math.abs(dx);
  const vertical = Math.abs(dy);
  if (Math.max(horizontal, vertical) < MAIL_SWIPE_AXIS_THRESHOLD) {
    return "pending";
  }
  return vertical > horizontal ? "vertical" : "horizontal";
}

function mailSwipeDragOffset(value) {
  const direction = Math.sign(value);
  const distance = Math.abs(value);
  if (distance <= MAIL_SWIPE_OPEN_OFFSET) {
    return value;
  }
  return direction * (
    MAIL_SWIPE_OPEN_OFFSET +
    (distance - MAIL_SWIPE_OPEN_OFFSET) * MAIL_SWIPE_RESISTANCE
  );
}

function mailSwipeTransition(state, event) {
  const current = state && typeof state === "object"
    ? state
    : { phase: "closed", origin: "closed", direction: 0, offset: 0 };
  if (!event || typeof event.type !== "string") {
    return { ...current };
  }
  if (event.type === "start") {
    const origin = current.phase === "open" ? "open" : "closed";
    const direction = origin === "open" && current.direction === 1 ? 1 : -1;
    return {
      phase: "pending",
      origin,
      direction: origin === "open" ? direction : 0,
      offset: origin === "open" ? direction * MAIL_SWIPE_OPEN_OFFSET : 0,
      consumeClick: origin === "open",
    };
  }
  if (event.type === "move" && ["pending", "horizontal"].includes(current.phase)) {
    const dx = Number.isFinite(event.dx) ? event.dx : 0;
    const dy = Number.isFinite(event.dy) ? event.dy : 0;
    if (current.phase === "pending") {
      const decision = mailSwipeDecision(dx, dy);
      if (decision === "pending") {
        return { ...current };
      }
      if (decision === "vertical") {
        return {
          phase: "closed",
          origin: "closed",
          direction: 0,
          offset: 0,
          consumeClick: current.origin === "open",
        };
      }
    }
    if (current.origin === "open") {
      const direction = current.direction === 1 ? 1 : -1;
      const distance = Math.max(0, MAIL_SWIPE_OPEN_OFFSET + direction * dx);
      return {
        ...current,
        phase: "horizontal",
        direction,
        offset: direction * Math.abs(mailSwipeDragOffset(distance)),
        consumeClick: true,
      };
    }
    const direction = Math.sign(dx) || current.direction || -1;
    return {
      ...current,
      phase: "horizontal",
      direction,
      offset: mailSwipeDragOffset(dx),
      consumeClick: true,
    };
  }
  if (event.type === "end") {
    if (current.phase === "horizontal") {
      const distance = Math.abs(current.offset);
      const shouldOpen = current.origin === "open"
        ? distance > MAIL_SWIPE_SNAP_THRESHOLD
        : distance >= MAIL_SWIPE_SNAP_THRESHOLD;
      if (shouldOpen) {
        return {
          phase: "open",
          origin: "open",
          direction: -1,
          offset: -MAIL_SWIPE_OPEN_OFFSET,
          consumeClick: true,
        };
      }
    }
    return {
      phase: "closed",
      origin: "closed",
      direction: 0,
      offset: 0,
      consumeClick: current.origin === "open" || current.phase === "horizontal",
    };
  }
  if (event.type === "close") {
    return {
      phase: "closed",
      origin: "closed",
      direction: 0,
      offset: 0,
      consumeClick: ["row-tap", "other-row-tap"].includes(event.reason),
    };
  }
  return { ...current };
}

function closeActiveMailSwipe(reason = "outside-tap") {
  const shell = activeMailSwipeShell;
  if (shell && typeof shell.closeMailActions === "function") {
    shell.closeMailActions(reason);
  }
}

function activateMailSwipe(shell) {
  if (activeMailSwipeShell && activeMailSwipeShell !== shell) {
    closeActiveMailSwipe("other-row");
  }
  activeMailSwipeShell = shell;
}

function attachMailSwipe(shell, row) {
  let startX = 0;
  let startY = 0;
  let pointerId = null;
  let suppressClick = false;
  let gesture = { phase: "closed", origin: "closed", direction: 0, offset: 0 };
  const actionButtons = Array.from(shell.querySelectorAll(".mail-swipe-action"));
  const toggle = shell.querySelector(".mail-swipe-toggle");

  const setActionsEnabled = (enabled) => {
    actionButtons.forEach((button) => {
      button.disabled = !enabled;
      button.tabIndex = enabled ? 0 : -1;
    });
  };

  const closeActions = (reason = "close") => {
    const returnFocus = actionButtons.includes(document.activeElement);
    gesture = mailSwipeTransition(gesture, { type: "close", reason });
    row.classList.remove("is-dragging");
    shell.classList.remove("is-swiping", "is-actions-open", "from-left", "from-right");
    row.style.transform = "";
    setActionsEnabled(false);
    if (toggle) {
      toggle.setAttribute("aria-expanded", "false");
    }
    if (activeMailSwipeShell === shell) {
      activeMailSwipeShell = null;
    }
    if (returnFocus && toggle) {
      toggle.focus();
    }
  };

  const revealActions = (direction) => {
    activateMailSwipe(shell);
    gesture = {
      phase: "open",
      origin: "open",
      direction: direction > 0 ? 1 : -1,
      offset: (direction > 0 ? 1 : -1) * MAIL_SWIPE_OPEN_OFFSET,
      consumeClick: true,
    };
    row.classList.remove("is-dragging");
    row.style.transform = "";
    shell.classList.remove("is-swiping");
    shell.classList.add("is-actions-open");
    shell.classList.toggle("from-left", direction > 0);
    shell.classList.toggle("from-right", direction < 0);
    setActionsEnabled(true);
    if (toggle) {
      toggle.setAttribute("aria-expanded", "true");
    }
  };

  const reset = () => {
    suppressClick = gesture.phase !== "closed";
    closeActions("cancel");
    pointerId = null;
    window.setTimeout(() => {
      suppressClick = false;
    }, 0);
  };
  shell.closeMailActions = closeActions;
  shell.revealMailActions = revealActions;
  setActionsEnabled(false);
  row.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) {
      return;
    }
    if (shell.classList.contains("is-actions-open")) {
      suppressClick = true;
    }
    pointerId = event.pointerId;
    startX = event.clientX;
    startY = event.clientY;
    gesture = mailSwipeTransition(gesture, { type: "start" });
  });
  row.addEventListener("pointermove", (event) => {
    if (event.pointerId !== pointerId) {
      return;
    }
    const deltaX = event.clientX - startX;
    const deltaY = event.clientY - startY;
    const previousPhase = gesture.phase;
    gesture = mailSwipeTransition(gesture, { type: "move", dx: deltaX, dy: deltaY });
    if (gesture.phase === "closed") {
      closeActions("scroll");
      pointerId = null;
      window.setTimeout(() => {
        suppressClick = false;
      }, 0);
      return;
    }
    if (gesture.phase === "horizontal") {
      if (previousPhase !== "horizontal") {
        suppressClick = true;
        activateMailSwipe(shell);
        row.classList.add("is-dragging");
        shell.classList.add("is-swiping");
        setActionsEnabled(false);
        row.setPointerCapture(event.pointerId);
      }
      event.preventDefault();
      const direction = gesture.direction;
      shell.classList.toggle("from-left", direction > 0);
      shell.classList.toggle("from-right", direction < 0);
      row.style.transform = `translate3d(${gesture.offset}px, 0, 0)`;
    }
  });
  const finish = (event) => {
    if (event.pointerId !== pointerId) {
      return;
    }
    const wasHorizontal = gesture.phase === "horizontal";
    gesture = mailSwipeTransition(gesture, { type: "end" });
    suppressClick = suppressClick || Boolean(gesture.consumeClick) || wasHorizontal;
    if (gesture.phase === "open") {
      revealActions(gesture.direction);
    } else {
      closeActions("release");
    }
    pointerId = null;
    window.setTimeout(() => {
      suppressClick = false;
    }, 0);
  };
  row.addEventListener("pointerup", finish);
  row.addEventListener("pointercancel", reset);
  row.addEventListener("click", (event) => {
    if (shell.classList.contains("is-actions-open")) {
      event.preventDefault();
      event.stopPropagation();
      closeActions("row-tap");
      suppressClick = false;
      return;
    }
    if (suppressClick) {
      event.preventDefault();
      event.stopPropagation();
      suppressClick = false;
    }
  }, true);
}

function mailListRow(thread, onExclude) {
  const shell = node("div", "mail-swipe-shell");
  const actions = node("div", "mail-swipe-actions");
  const hideAction = node(
    "button",
    "mail-swipe-action mail-swipe-hide",
    stringFor("mailHide")
  );
  const deleteAction = node(
    "button",
    "mail-swipe-action mail-swipe-delete",
    stringFor("mailDelete")
  );
  const toggle = node("button", "mail-swipe-toggle");
  toggle.type = "button";
  toggle.setAttribute(
    "aria-label",
    `${stringFor("mailHide")} / ${stringFor("mailDelete")}`
  );
  // The face of this control was the two words it opens - one of them a
  // destructive verb - stacked on two lines inside a 44 px box, which reads as
  // a button whose label failed rather than the way into a row's actions. The
  // dots are what a phone list uses; both words are still its name, and both
  // are one tap away where they belong.
  toggle.append(svgIcon("more"));
  const actionsId = `mail-actions-${++mailSwipeRowCounter}`;
  actions.id = actionsId;
  toggle.setAttribute("aria-controls", actionsId);
  toggle.setAttribute("aria-expanded", "false");
  hideAction.type = "button";
  deleteAction.type = "button";
  actions.append(hideAction, deleteAction);
  const row = routeLink(
    `#/mail/${encodeURIComponent(thread.threadId)}`,
    "mail-row mail-row-surface tappable"
  );
  mailRowContent(row, thread);
  shell.append(row, toggle, actions);
  hideAction.addEventListener("click", () => onExclude(false, shell, row));
  deleteAction.addEventListener("click", () => onExclude(true, shell, row));
  toggle.addEventListener("click", () => {
    if (shell.classList.contains("is-actions-open")) {
      shell.closeMailActions("toggle");
    } else {
      shell.revealMailActions(-1);
    }
  });
  attachMailSwipe(shell, row);
  return shell;
}

async function renderMailList() {
  const token = ++routeToken;
  clearPolling();
  const { view, content } = screen(stringFor("mailListTitle"), "#/");
  app.replaceChildren(view);
  delayedSkeleton(content, "list");
  let hasPainted = false;
  let mailToast = null;
  let currentThreads = [];
  let mutationInFlight = false;
  let toastTimer = null;
  const mailRows = new Map();
  const mailList = node("div", "mail-list");

  const interactionActive = () => (
    mutationInFlight || Boolean(activeMailSwipeShell && activeMailSwipeShell.isConnected)
  );

  const unhide = async (threadId) => {
    const undoToast = mailToast && mailToast.threadId === threadId ? mailToast : null;
    try {
      await deleteJson(`api/mail/hide/${encodeURIComponent(threadId)}`);
      if (undoToast) {
        mailToast = null;
        window.clearTimeout(toastTimer);
        const restored = [...currentThreads];
        const index = Math.min(undoToast.index, restored.length);
        restored.splice(index, 0, undoToast.thread);
        paint(restored);
      }
      try {
        await refresh();
      } catch (error) {
        return;
      }
    } catch (error) {
      showError(content, error);
    }
  };

  const exclude = async (thread, permanent, shell, row) => {
    mutationInFlight = true;
    const originalIndex = Math.max(
      currentThreads.findIndex((item) => item.threadId === thread.threadId),
      0
    );
    if (typeof shell.closeMailActions === "function") {
      shell.closeMailActions("action");
    }
    shell.style.height = `${shell.offsetHeight}px`;
    row.classList.add("is-removing");
    const removeDelay = window.matchMedia("(prefers-reduced-motion: reduce)").matches
      ? 0
      : MAIL_SWIPE_REMOVE_MS;
    const animationDone = new Promise((resolve) => {
      window.setTimeout(resolve, removeDelay);
    });
    window.requestAnimationFrame(() => {
      shell.classList.add("is-removing");
    });
    try {
      await Promise.all([
        postJson("api/mail/hide", {
          threadId: thread.threadId,
          permanent,
        }),
        animationDone,
      ]);
      mailToast = {
        threadId: thread.threadId,
        thread,
        index: originalIndex,
        expiresAt: Date.now() + MAIL_HIDE_TOAST_MS,
        messageKey: permanent ? "mailDeleteDone" : "mailHideDone",
      };
      paint(currentThreads.filter((item) => item.threadId !== thread.threadId));
      window.clearTimeout(toastTimer);
      toastTimer = window.setTimeout(() => {
        mailToast = null;
        if (token === routeToken) {
          const toast = content.querySelector(".mail-toast");
          if (toast) {
            toast.remove();
          }
        }
      }, MAIL_HIDE_TOAST_MS);
      mutationInFlight = false;
    } catch (error) {
      shell.classList.remove("is-removing");
      shell.style.height = "";
      row.classList.remove("is-removing");
      row.style.transform = "";
      if (typeof shell.closeMailActions === "function") {
        shell.closeMailActions();
      }
      mutationInFlight = false;
      if (token === routeToken) {
        fetchNotice(content, error);
        void refresh().finally(() => {
          if (token === routeToken) fetchNotice(content, error);
        });
      }
    }
  };

  const paint = (threads, stale = false) => {
    closeActiveMailSwipe("repaint");
    const visibleThreads = Array.isArray(threads) ? threads : [];
    currentThreads = [...visibleThreads];
    const list = visibleThreads.length ? mailList : null;
    const seen = new Set(visibleThreads.map((thread) => thread.threadId));
    for (const key of mailRows.keys()) if (!seen.has(key)) mailRows.delete(key);
    if (visibleThreads.length) {
      reconcileChildren(list, visibleThreads.map((thread) => cachedPaintRow(mailRows, thread.threadId,
        JSON.stringify(thread), () => mailListRow(
          thread,
          (permanent, shell, row) => exclude(thread, permanent, shell, row)
        )
      )));
      suppressUniformBadges(list.querySelectorAll(".mail-row"), ".mail-badge:not(.urgent)");
      staggerRows(list.children, "mail");
    }
    // The screen title already says what this list is, forty pixels above, and
    // it is the only section here - a label under it was the same words twice.
    const parts = [
      list || node("div", "state-message", stringFor("mailNoUnreplied"))
    ];
    if (mailToast && mailToast.expiresAt > Date.now()) {
      const toast = node("div", "mail-toast");
      toast.setAttribute("role", "status");
      toast.setAttribute("aria-live", "polite");
      const undo = node("button", "mail-toast-undo", stringFor("mailUnhide"));
      undo.type = "button";
      const toastThreadId = mailToast.threadId;
      undo.addEventListener("click", () => unhide(toastThreadId));
      toast.append(node("span", "", stringFor(mailToast.messageKey)), undo);
      parts.push(toast);
    }
    reconcileChildren(content, parts);
    scheduleListBadges();
    if (stale) showCachedData("mail");
    if (!hasPainted) restoreScroll();
    hasPainted = true;
  };
  const cached = readDataCache("mail");
  let hasCached = false;
  if (cached && Array.isArray(cached.threads)) {
    try {
      paint(cached.threads, true);
      hasCached = true;
    } catch (error) {
      discardDataCache("mail");
    }
  }
  let refreshRevision = 0;
  const refresh = async () => {
    if (interactionActive()) {
      return;
    }
    const revision = ++refreshRevision;
    try {
      const threads = await fetchJson("api/mail");
      if (token !== routeToken || revision !== refreshRevision || interactionActive()) {
        return;
      }
      writeDataCache("mail", { threads });
      paint(threads);
      hasCached = true;
    } catch (error) {
      if (token === routeToken && revision === refreshRevision &&
          !interactionActive() && !hasCached && !content.querySelector(".section-label")) {
        showError(content, error);
      } else if (token === routeToken && revision === refreshRevision && !interactionActive()) {
        fetchNotice(content, error);
      }
    }
  };
  await refresh();
  if (token !== routeToken) {
    return;
  }
  startPolling(refresh, 5000);
}

// A reply carries the whole thread below it. On the walk, an entire phone
// screen of one thread was quoted signature: two copies of a company block, rows
// of dashes wrapping onto second lines, and a percent-encoded maps URL over
// eight lines. None of it is what you came to read, and none of it is deleted -
// every run of quoted lines folds where it sits, in the order it was written.
function splitQuotedRuns(body) {
  const lines = String(body == null ? "" : body).split("\n");
  const runs = [];
  lines.forEach((line) => {
    const quoted = /^\s*>/.test(line);
    const last = runs[runs.length - 1];
    if (last && last.quoted === quoted) last.lines.push(line);
    else runs.push({ quoted, lines: [line] });
  });
  // A body with nothing quoted stays exactly one plain run.
  return runs.filter((run, index) =>
    run.lines.some((line) => line.trim()) || index === 0 || index === runs.length - 1);
}

function quotedRunBlock(run, stateKey) {
  const text = run.lines.join("\n");
  const block = node("div", "mail-quote");
  const toggle = node("button", "expand-button mail-quote-toggle");
  toggle.type = "button";
  const body = node("div", "event-text mail-quote-text", text);
  const label = stringFor("mailQuotedLines", { n: run.lines.length });
  const setOpen = (open) => {
    body.hidden = !open;
    toggle.textContent = (open ? "- " : "+ ") + label;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) expandedEvents.add(stateKey);
    else expandedEvents.delete(stateKey);
  };
  toggle.addEventListener("click", () => setOpen(body.hidden));
  block.append(toggle, body);
  setOpen(expandedEvents.has(stateKey));
  return block;
}

function mailMessageBubble(message) {
  const item = node(
    "article",
    `event mail-message ${message.direction === "out" ? "user" : "assistant"}`
  );
  item.append(node("div", "event-role",
    [message.who, message.ts].join(stringFor("metaJoin"))));
  const bubble = node("div", "bubble");
  const runs = splitQuotedRuns(message.body);
  runs.forEach((run, index) => {
    if (run.quoted) {
      bubble.append(quotedRunBlock(
        run, stateTupleKey("mail-quote", message.who, message.ts, index)));
    } else {
      const value = run.lines.join("\n");
      const text = node("div", "event-text", value);
      const toggle = collapseWhenLong(
        text, value, stateTupleKey("mail-body", message.who, message.ts, index));
      bubble.append(text);
      if (toggle) bubble.append(toggle);
    }
  });
  item.append(bubble);
  return item;
}

function mailJobChip(job) {
  if (!job) {
    return null;
  }
  if (["queued", "drafting", "checking"].includes(job.status)) {
    return node(
      "div",
      "mail-job-chip drafting",
      stringFor(job.type === "revise" ? "mailRevising" : "mailDrafting")
    );
  }
  if (job.status === "done") {
    const chip = node("div", "mail-job-chip done", stringFor("mailDraftDone"));
    if (job.truncated) {
      chip.append(
        node(
          "div",
          "mail-draft-note",
          stringFor("mailDraftTruncated", { n: job.omittedMessages || 0 })
        )
      );
    }
    return chip;
  }
  if (job.status === "failed") {
    const details = Array.isArray(job.checkFindings) ? job.checkFindings.join("\n") : job.reason || "";
    const chip = node("div", "mail-job-chip failed", stringFor("mailDraftFailed"));
    if (details) {
      chip.append(node("pre", "mail-job-findings", details));
    }
    return chip;
  }
  return null;
}

function mailComposer(threadId, refreshJobs) {
  const wrapper = node("div", "mail-composer");
  const status = node("div", "mail-job-status");
  const form = node("form", "mail-composer-form");
  const input = node("input", "mail-direction");
  input.type = "text";
  input.maxLength = 500;
  input.placeholder = stringFor("mailDirectionPlaceholder");
  input.setAttribute("aria-label", stringFor("mailDirectionPlaceholder"));
  const button = node("button", "mail-draft-button", stringFor("mailDraftButton"));
  button.type = "submit";
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (button.disabled) {
      return;
    }
    button.disabled = true;
    try {
      await postJson(`api/mail/${encodeURIComponent(threadId)}/draft`, {
        direction: input.value,
      });
      await refreshJobs();
    } catch (error) {
      status.replaceChildren(
        node("div", "mail-job-chip failed", stringFor("mailDraftFailed"))
      );
    } finally {
      button.disabled = false;
    }
  });
  form.append(inputHitArea(input), button);
  wrapper.append(status, form, node("div", "mail-draft-note", stringFor("mailDraftNote")));
  wrapper.updateJob = (job) => {
    const chip = mailJobChip(job);
    status.replaceChildren(...(chip ? [chip] : []));
    button.disabled = Boolean(job && ["queued", "drafting", "checking"].includes(job.status));
  };
  return wrapper;
}

function gmailThreadUrl(threadId) {
  return `https://mail.google.com/mail/u/0/#inbox/${encodeURIComponent(threadId)}`;
}

function mailDraftPreview(threadId, routeId, refreshJobs) {
  const panel = node("section", "mail-draft-preview");
  let loadedDraft = null;
  let loadedJobId = null;
  let loadingJobId = null;
  let confirmTimer = null;
  let revisedTimer = null;
  let revisingJobId = null;
  let revisedUntil = 0;
  let editOpen = false;
  let editInstruction = "";
  let currentJob = null;

  const render = (job) => {
    currentJob = job;
    const revising = job.type === "revise" &&
      ["queued", "drafting", "checking"].includes(job.status);
    const failedRevision = job.type === "revise" && job.status === "failed";
    panel.replaceChildren(node("h2", "mail-draft-title", stringFor("mailDraftPreviewTitle")));
    if (revisedUntil > Date.now()) {
      const revised = node("div", "mail-revise-status done", stringFor("mailRevised"));
      revised.setAttribute("role", "status");
      revised.setAttribute("aria-live", "polite");
      panel.append(revised);
    }
    if (failedRevision) {
      const failed = node("div", "mail-revise-status failed", stringFor("mailDraftFailed"));
      failed.setAttribute("role", "status");
      failed.setAttribute("aria-live", "polite");
      panel.append(failed);
    }
    if (!loadedDraft) {
      if (revising) {
        const status = node("div", "mail-revise-status", stringFor("mailRevising"));
        status.setAttribute("role", "status");
        status.setAttribute("aria-live", "polite");
        panel.append(status);
      } else if (job.sentAt) {
        panel.append(node("div", "mail-send-state sent", stringFor("mailSent")));
      }
      return;
    }
    const metadata = node("div", "mail-draft-metadata");
    metadata.append(
      node("div", "", loadedDraft.to),
      node("div", "", loadedDraft.subject)
    );
    const body = node("div", "mail-draft-body", loadedDraft.body);
    body.tabIndex = 0;
    body.setAttribute("aria-label", stringFor("mailDraftPreviewTitle"));
    const actions = node("div", "mail-draft-actions");
    const open = node("a", "mail-open-gmail", stringFor("mailOpenGmail"));
    open.href = gmailThreadUrl(threadId);
    open.target = "_blank";
    open.rel = "noopener";
    if (!job.sentAt) {
      const edit = node("button", "mail-edit-button", stringFor("mailEditDraft"));
      edit.type = "button";
      edit.disabled = revising;
      edit.setAttribute("aria-expanded", editOpen ? "true" : "false");
      edit.setAttribute("aria-controls", "mail-revise-form");
      edit.addEventListener("click", () => {
        editOpen = !editOpen;
        render(job);
        if (!editOpen) {
          window.requestAnimationFrame(() => {
            const nextEdit = panel.querySelector(".mail-edit-button");
            if (nextEdit) {
              nextEdit.focus();
            }
          });
        }
      });
      actions.append(edit);
    }
    actions.append(open);
    if (job.threadingDegraded) {
      panel.append(
        node("div", "mail-threading-degraded", stringFor("mailThreadingDegraded"))
      );
    }
    if (job.sentAt) {
      actions.append(node("span", "mail-send-state sent", stringFor("mailSent")));
    } else {
      const sendState = node("div", "mail-send-state");
      const send = node("button", "mail-send-button", stringFor("mailSendButton"));
      send.type = "button";
      send.disabled = revising;
      let confirming = false;
      const resetConfirm = () => {
        confirming = false;
        send.classList.remove("confirming");
        send.textContent = stringFor("mailSendButton");
      };
      send.addEventListener("click", async () => {
        if (send.disabled) {
          return;
        }
        if (!confirming) {
          confirming = true;
          send.classList.add("confirming");
          send.textContent = stringFor("mailSendConfirm");
          window.clearTimeout(confirmTimer);
          confirmTimer = window.setTimeout(resetConfirm, MAIL_SEND_CONFIRM_MS);
          return;
        }
        window.clearTimeout(confirmTimer);
        send.disabled = true;
        send.classList.remove("confirming");
        send.textContent = stringFor("mailSending");
        sendState.classList.remove("failed");
        sendState.textContent = stringFor("mailSending");
        try {
          await postJson("api/mail/send", {
            draftId: loadedDraft.draftId,
            threadId,
          });
          send.remove();
          sendState.classList.add("sent");
          sendState.textContent = stringFor("mailSent");
          await refreshJobs();
        } catch (error) {
          sendState.classList.add("failed");
          sendState.textContent = stringFor("mailSendFailed");
          send.disabled = false;
          resetConfirm();
        }
      });
      actions.append(send, sendState);
    }
    panel.append(metadata, body);
    if (revising) {
      const status = node("div", "mail-revise-status", stringFor("mailRevising"));
      status.setAttribute("role", "status");
      status.setAttribute("aria-live", "polite");
      panel.append(status);
    }
    panel.append(actions);
    if (editOpen && !revising && !job.sentAt) {
      const form = node("form", "mail-revise-form");
      form.id = "mail-revise-form";
      const label = node("label", "mail-revise-label", stringFor("mailEditDraft"));
      label.htmlFor = "mail-revise-instruction";
      const input = node("textarea", "mail-revise-input");
      input.id = "mail-revise-instruction";
      input.maxLength = 500;
      input.required = true;
      input.placeholder = stringFor("mailEditPlaceholder");
      input.setAttribute("aria-label", stringFor("mailEditPlaceholder"));
      input.value = editInstruction;
      input.addEventListener("input", () => {
        editInstruction = input.value;
      });
      input.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          editOpen = false;
          render(job);
          window.requestAnimationFrame(() => {
            const nextEdit = panel.querySelector(".mail-edit-button");
            if (nextEdit) {
              nextEdit.focus();
            }
          });
        }
      });
      const revise = node("button", "mail-revise-button", stringFor("mailEditDraft"));
      revise.type = "submit";
      const status = node("div", "mail-revise-status");
      status.setAttribute("role", "status");
      status.setAttribute("aria-live", "polite");
      status.textContent = stringFor(failedRevision ? "mailDraftFailed" : "mailRevising");
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (revise.disabled) {
          return;
        }
        revise.disabled = true;
        status.textContent = stringFor("mailRevising");
        let result;
        try {
          result = await postJson("api/mail/revise", {
            threadId,
            draftId: loadedDraft.draftId,
            instruction: input.value,
          });
        } catch (error) {
          status.textContent = stringFor("mailDraftFailed");
          revise.disabled = false;
          return;
        }
        revisingJobId = result.jobId;
        editOpen = false;
        render({
          ...job,
          jobId: result.jobId,
          type: "revise",
          status: "queued",
        });
        try {
          await refreshJobs();
        } catch (error) {
          startPolling(refreshJobs, 3000);
        }
      });
      form.append(label, input, revise, status);
      panel.append(form);
      window.requestAnimationFrame(() => input.focus());
    }
  };

  panel.updateJob = async (job) => {
    const active = job && ["queued", "drafting", "checking"].includes(job.status);
    if (active && job.type === "revise") {
      render(job);
      return false;
    }
    if (job && job.type === "revise" && job.status === "failed") {
      editOpen = true;
      render(job);
      return false;
    }
    if (!job || job.status !== "done" || !job.draftId) {
      loadedDraft = null;
      loadedJobId = null;
      loadingJobId = null;
      panel.replaceChildren(...(active ? [
        node("div", "mail-job-chip drafting", stringFor("mailDrafting"))
      ] : []));
      return false;
    }
    if (job.sentAt && (!loadedDraft || loadedDraft.draftId !== job.draftId)) {
      loadedDraft = null;
      loadedJobId = job.jobId;
      render(job);
      return false;
    }
    if (loadedDraft && loadedDraft.draftId === job.draftId && loadedJobId === job.jobId) {
      render(job);
      return false;
    }
    if (loadingJobId === job.jobId) {
      return false;
    }
    loadingJobId = job.jobId;
    panel.replaceChildren(
      node("div", "mail-job-chip drafting", stringFor("mailDrafting"))
    );
    try {
      const draft = await fetchJson(
        `api/mail/draft?threadId=${encodeURIComponent(threadId)}`
      );
      if (routeId !== routeToken) {
        return;
      }
      loadedDraft = draft;
      loadedJobId = job.jobId;
      if (job.type === "revise" && job.jobId === revisingJobId) {
        revisingJobId = null;
        editInstruction = "";
        revisedUntil = Date.now() + 2500;
        window.clearTimeout(revisedTimer);
        revisedTimer = window.setTimeout(() => {
          revisedUntil = 0;
          if (routeId === routeToken && currentJob) {
            render(currentJob);
          }
        }, 2500);
      }
      render(job);
    } catch (error) {
      if (routeId === routeToken) {
        panel.replaceChildren(
          node("div", "mail-job-chip failed", stringFor("mailDraftFailed"))
        );
      }
      return true;
    } finally {
      loadingJobId = null;
    }
    return false;
  };
  return panel;
}

async function renderMailThread(threadId) {
  const token = ++routeToken;
  clearPolling();
  const { view, content } = screen(stringFor("mailThreadFrom"), "#/mail");
  view.classList.add("mail-thread-screen");
  content.classList.add("mail-thread-content");
  const timeline = node("div", "timeline mail-timeline");
  const preview = mailDraftPreview(threadId, token, () => refreshJobs());
  content.replaceChildren(timeline, preview);
  const refreshJobs = async () => {
    let data;
    try {
      data = await fetchJson(`api/mail/jobs?thread=${encodeURIComponent(threadId)}`);
    } catch (error) {
      if (token === routeToken) fetchNotice(composer, error);
      return;
    }
    if (token === routeToken) {
      fetchNotice(composer, null);
      const jobs = Array.isArray(data.jobs) ? data.jobs : [];
      const activeJob = jobs.find((item) =>
        ["queued", "drafting", "checking"].includes(item.status)
      );
      const doneJob = jobs.find((item) => item.status === "done") || null;
      composer.updateJob(activeJob || jobs[0] || null);
      const retryPreview = await preview.updateJob(activeJob || jobs[0] || doneJob);
      if (activeJob || retryPreview) {
        startPolling(refreshJobs, 3000);
      } else {
        clearPolling();
      }
    }
  };
  const composer = mailComposer(threadId, refreshJobs);
  view.append(composer);
  app.replaceChildren(view);
  delayedSkeleton(timeline, "mail");
  try {
    let threadListError = null;
    const [messages, threads] = await Promise.all([
      fetchJson(`api/mail/${encodeURIComponent(threadId)}`),
      fetchJson("api/mail").catch((error) => { threadListError = error; return []; }),
    ]);
    if (token !== routeToken) {
      return;
    }
    const thread = threads.find((item) => item.threadId === threadId);
    view.querySelector(".topbar-title").textContent = thread ? thread.from : stringFor("mailThreadFrom");
    if (thread && thread.sourceWarning) {
      const warning = mailBadge(window.STRINGS?.mailSourceWarning || "!", "source-warning");
      warning.title = window.STRINGS?.mailSourceWarningDetail || stringFor("statusAttention");
      timeline.before(warning);
    }
    if (thread && thread.threadingDegraded) {
      timeline.before(
        node("div", "mail-threading-degraded", stringFor("mailThreadingDegraded"))
      );
    }
    emptyContent(timeline, messages.map(mailMessageBubble));
    fetchNotice(content, threadListError);
    await refreshJobs();
    window.requestAnimationFrame(() => {
      if (!drawerOpen) (app || window).scrollTo(0, app ? app.scrollHeight : document.body.scrollHeight);
    });
  } catch (error) {
    if (token === routeToken) {
      showError(content, error);
    }
  }
}

function safeDecode(value) {
  try {
    return decodeURIComponent(value);
  } catch (error) {
    return null;
  }
}

async function route() {
  const serial = ++motionRouteSerial;
  const hash = window.location.hash || "#/";
  const old = app.firstElementChild;
  // Sample before cleanup removes the animation. A second push keeps the
  // same curve's elapsed time; a reversal starts at the exposed destination.
  const running = old?.getAnimations().find((item) => item.animationName === "screen-enter" && item.playState === "running");
  const oldX = running ? new DOMMatrixReadOnly(getComputedStyle(old).transform).m41 : 0;
  const timing = running?.effect.getTiming();
  const elapsed = running ? Math.max(0, Number(running.currentTime) - timing.delay) : 0;
  const previousCopy = document.querySelector(".motion-outgoing:not(.back-preview)");
  const returningX = running && previousCopy?.dataset.route === hash
    ? new DOMMatrixReadOnly(getComputedStyle(previousCopy).transform).m41 : null;
  let continuation = null;
  if (running && elapsed < timing.duration) {
    continuation = returningX !== null
      ? { from: `${returningX}px`, duration: Math.max(60, timing.duration - elapsed) }
      : { from: old.style.getPropertyValue("--motion-from") || "16%", duration: timing.duration, delay: -elapsed };
  }
  routeMotionCleanup?.();
  cancelPullRefresh?.();
  releasePressedControl();
  rememberBackScreen(old);
  saveScroll();
  displayedCache = null;
  activeTalkView = null;
  // A question is read again only while its talk screen is the one open.
  activeAskCard?.stop();
  activeAskCard = null;
  const entryIndex = history.state?.pocketMotionIndex;
  const traversal = Number.isSafeInteger(entryIndex) && motionHistoryIndex !== null &&
    entryIndex !== motionHistoryIndex;
  const back = traversal ? entryIndex < motionHistoryIndex :
    hash === "#/" && activeRouteHash !== "#/";
  if (!Number.isSafeInteger(entryIndex) ||
      (hash !== activeRouteHash && entryIndex === motionHistoryIndex)) {
    motionHistoryIndex = ++motionHistorySerial;
    history.replaceState({ ...(history.state || {}), pocketMotionIndex: motionHistoryIndex }, "");
  } else {
    motionHistoryIndex = entryIndex;
    motionHistorySerial = Math.max(motionHistorySerial, entryIndex);
  }
  const knownEntry = backEntries.findIndex((entry) => entry.index === motionHistoryIndex);
  if (knownEntry >= 0) backEntryPosition = knownEntry;
  else if (traversal) {
    // A history entry from before reload has no verified predecessor here.
    // The screen we just left may be ahead of it, never a back destination.
    backEntries.splice(0, backEntries.length, { index: motionHistoryIndex, hash });
    backEntryPosition = 0;
  } else {
    backEntries.splice(backEntryPosition + 1);
    backEntries.push({ index: motionHistoryIndex, hash });
    backEntryPosition = backEntries.length - 1;
  }
  app.classList.toggle("has-back-screen", Boolean(backDestination()));
  const wasOpen = drawerOpen;
  setDrawerOpen(false, false);
  if (old) cancelTransientMotion(app);
  stopTalkFollowing();
  const moving = old && !reducedMotion() && !wasOpen && !backCommitted;
  backCommitted = false;
  const outgoing = moving ? departingScreen(old, back) : null;
  if (outgoing) {
    outgoing.dataset.route = activeRouteHash;
    if (running) {
      outgoing.style.setProperty("--motion-exit-from", `${oldX}px`);
      outgoing.style.setProperty("--motion-exit-duration", `${Math.max(60, timing.duration - elapsed)}ms`);
    }
  }
  incomingMotion = moving ? continuation || { from: back ? "-16%" : "16%" } : null;
  if (moving) app.classList.add("motion-active");
  renderRoute();
  syncThemeColor();
  backSettleCleanup?.();
  incomingMotion = null;
  if (wasOpen) setDrawerOpen(true, false);
  if (outgoing) {
    const view = app.firstElementChild;
    const finish = () => {
      outgoing.remove();
      view?.classList.remove("motion-enter");
      if (serial === motionRouteSerial) {
        app.classList.remove("motion-active");
        routeMotionCleanup = null;
      }
    };
    routeMotionCleanup = finish;
    const animation = view?.getAnimations().find((item) => item.animationName === "screen-enter");
    if (animation) animation.finished.then(finish, finish);
    else finish();
  }
}

function renderRoute() {
  clearActiveQuickCommands();
  const hash = window.location.hash || "#/";
  // Keep the existing Phase 15 route identity for workspace freshness/resume.
  if (/^#\/w\/[^/?#]+$/.test(hash)) {
    window.location.replace(hash.replace("#/w/", "#/ws/"));
    return;
  }
  if (hash !== activeRouteHash) {
    closeActiveMailSwipe("route-change");
    activeRouteHash = hash;
    staggerRouteKey = null;
  }
  const workspaceMatch = hash.match(/^#\/(?:w|ws)\/([^/?#]+)$/);
  const talkMatch = hash.match(/^#\/talk\/([^/?#]+)$/);
  const terminalMatch = hash.match(/^#\/term\/([^/?#]+)$/);
  const mailThreadMatch = hash.match(/^#\/mail\/([^/?#]+)$/);
  const fileMatch = hash.match(/^#\/file\?(.*)$/);
  if (workspaceMatch) {
    const value = safeDecode(workspaceMatch[1]);
    value === null ? renderHome() : renderWorkspace(value);
  } else if (talkMatch) {
    const value = safeDecode(talkMatch[1]);
    if (value !== null) writeDrawerStorage(LAST_TALK_STORAGE_KEY, value);
    value === null ? renderHome() : renderTalk(value);
  } else if (terminalMatch) {
    const value = safeDecode(terminalMatch[1]);
    if (value !== null) writeDrawerStorage(LAST_TALK_STORAGE_KEY, `term:${value}`);
    value === null ? renderHome() : renderTerminal(value);
  } else if (mailThreadMatch) {
    const value = safeDecode(mailThreadMatch[1]);
    value === null ? renderHome() : renderMailThread(value);
  } else if (fileMatch) {
    const params = new URLSearchParams(fileMatch[1]);
    const path = params.get("path");
    if (path) {
      renderFile(path);
    } else {
      renderHome();
    }
  } else if (hash === "#/mail") {
    renderMailList();
  } else if (hash === "#/launch") {
    renderLaunch();
  } else if (hash === "#/pad") {
    renderPad();
  } else {
    renderHome();
  }
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    quotaActionResultKey,
    layoutColumnCount,
    layoutPlacementCount,
    layoutPanePosition,
    displaySeatTitle,
    askAnswerBody,
    askFromPayload,
    askInitialSelection,
    askVisibleOptions,
    WINDOWS_PATH_RE,
    ENTRY_ORIGINS,
    entryMachineName,
    entryPeerOrigin,
    entryHopCount,
    probeOrigin,
    runEntryFailover,
    noteOriginUnreachable,
    announceEntryHop,
    deleteJson,
    detectWindowsPaths,
    eventStableKey,
    groupTimelineEvents,
    chooseLaunchWorkspace,
    composerDraftKey,
    readComposerDraft,
    writeComposerDraft,
    pruneComposerDrafts,
    composerDraftController,
    quickCommandLabel,
    addQuickCommand,
    quickInsertValue,
    readQuickCommands,
    writeQuickCommands,
    quickCommandRow,
    readLaunchWorkspace,
    rememberLaunchWorkspace,
    mailDraftingBadge,
    mailJobChip,
    mailSwipeDragOffset,
    mailSwipeDecision,
    mailSwipeTransition,
    mergeTranscriptEvents,
    postJson,
    paneFromTalk,
    renderMarkdown,
    rewriteHubUrl,
    joinWrappedWindowsPaths,
    safeDecode,
    updateThinkingState,
    emptyLaneState,
    advanceLane,
    formatLaneElapsed,
    formatLaneDelta,
    formatContextTokens,
    formatSessionStatus,
    laneUsualMedian,
    readLaneHistory,
    recordLaneDuration,
    progressLaneView,
    maxTranscriptCursor,
    eventsAfterCursor,
    assistantAnsweredAfter,
    registerProgressLane,
    findLane,
    lanesFor,
    removeProgressLane,
    pruneProgressLanes,
    reconcileOptimistic,
    syncSessionLanes,
    commitLaneMessageStatus,
    messagesFor,
    updateLaneLiveFields,
    syncLaneTicker,
    stopLaneTicker,
    handleLaneVisibilityChange,
    laneTickerId,
    isLaneDuration,
    validAgent,
    terminalComposer,
    talkComposer,
    optimisticBubble,
    sessionStatusView,
    renderTalk,
    workspaceTabRow,
    workspaceListView,
    layoutWorkspaceHeader,
    layoutPaneRow,
    sortedWorkspaceTabs,
    countSummary,
    homeListCounts,
    readTalkSort,
    writeTalkSort,
    displayStatus,
    statusText,
    tabRoute,
    situationGroup,
    situationOrder,
    situationAge,
    situationFolded,
    SITUATION_GROUPS,
    SITUATION_STALL_MS,
    numericCursor,
    validCursorWatermark,
    pruneProgressLaneSessions,
    progressLaneSessionCount,
  };
}
if (app) {
  window.addEventListener("scroll", scheduleListBadges, { passive: true, capture: true });
  window.addEventListener("resize", scheduleListBadges);
  window.addEventListener("transitionend", scheduleListBadges);
  themeMediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
  applyThemePreference(readThemePreference());
  themeMediaQuery.addEventListener("change", () => {
    if (themePreference === "system") {
      syncThemeColor();
    }
  });
  document.title = stringFor("appTitle");
  document.querySelector('meta[name="apple-mobile-web-app-title"]').content = stringFor("appTitle");
  document.addEventListener("pointerdown", (event) => {
    if (
      activeMailSwipeShell &&
      !activeMailSwipeShell.contains(event.target) &&
      !event.target.closest(".mail-list")
    ) {
      closeActiveMailSwipe("outside-tap");
    }
  }, true);
  document.addEventListener("click", (event) => {
    if (
      activeMailSwipeShell &&
      !activeMailSwipeShell.contains(event.target) &&
      event.target.closest(".mail-list")
    ) {
      event.preventDefault();
      event.stopPropagation();
      closeActiveMailSwipe("other-row-tap");
    }
  }, true);
  window.addEventListener("scroll", () => closeActiveMailSwipe("scroll"), {
    passive: true,
    capture: true,
  });
  installPressFeedback();
  installBackSwipe();
  window.addEventListener("hashchange", route);
  window.addEventListener("pagehide", () => saveScroll());
  if (window.visualViewport) {
    // A second look on the next frame catches a layout iOS resizes late; the
    // window resize catches one that sends no visualViewport event at all.
    const resyncViewport = () => {
      syncVisualViewport();
      window.requestAnimationFrame(syncVisualViewport);
    };
    window.visualViewport.addEventListener("resize", resyncViewport);
    window.visualViewport.addEventListener("scroll", resyncViewport);
    window.addEventListener("resize", resyncViewport);
    syncVisualViewport();
    // An installed copy on iOS 26 (simulator, 2026-09-14) is laid out at the
    // whole screen for a moment and settles at the screen minus the status
    // bar with no resize event; the first sync had kept a 68px keyboard that
    // was never there. Two more looks after boot catch the settled layout.
    window.addEventListener("load", resyncViewport);
    window.setTimeout(resyncViewport, 400);
    window.setTimeout(resyncViewport, 1500);
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      lastResumeAt = -Infinity;
      if (healthTimer !== null) window.clearTimeout(healthTimer);
      healthTimer = null;
      stopLaneTicker();
      return;
    }
    refreshVisiblePage();
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) refreshVisiblePage();
  });
  // The browser knows the signal is back before any request of ours can find
  // out. Without this the app waited out its own backoff - after twenty seconds
  // without a network the next poll is twenty seconds away, so it kept saying
  // it could not reach the machine while the phone was plainly back on Wi-Fi.
  // Measured: eight seconds after the signal returned, the screen still read
  // "cannot reach the PC".
  installPullToRefresh(app);
  window.addEventListener("online", () => {
    pollFailures = 0;
    refreshVisiblePage();
  });
  // And the moment it goes, say so, rather than waiting for a request to time
  // out to discover what the device already knows.
  window.addEventListener("offline", () => {
    connectionOffline = true;
    renderConnection();
  });
  startConnection();
  initializeDrawer();
  route();
  // Prefetch and the optional entry notice do not block the first screen.
  window.requestAnimationFrame(() => window.setTimeout(() => {
    announceEntryHop();
    csrfSession().catch(() => {});
  }, 0));
  watchForFaults();
  window.setTimeout(() => { reportViewportGeometry("boot"); viewportGeometryLogged = true; }, 1500);
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/pocket/sw.js", { scope: "/pocket/" }).catch(() => {});
    });
    // A worker taking over means a newer shell is on disk - unless this is the
    // first one, which claims the page it was registered from. Without that
    // distinction the very first visit reloads itself: the app would flash
    // white and start again every time someone installed it.
    const hadController = Boolean(navigator.serviceWorker.controller);
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (!hadController) {
        return;
      }
      // A new worker in control is what guarantees a reload brings a new
      // shell, so it also lifts the once-per-build guard.
      if (staleReloadFallback !== null) {
        window.clearTimeout(staleReloadFallback);
        staleReloadFallback = null;
      }
      try { window.sessionStorage.removeItem(BUILD_RELOAD_KEY); } catch (error) {}
      buildIsStale = true;
      applyNewBuild();
    });
  }
  document.addEventListener("input", (event) => {
    const field = event.target;
    if (field && typeof field.matches === "function" && field.matches(".composer-input, .mail-direction")) {
      lastFieldInputAt = performance.now();
    }
  }, true);
  // Coming back to an app that has been sitting on a home screen is the moment
  // it is most likely to be out of date, and the moment nobody is typing.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") {
      // Put away: a shell that was found to be old goes now, off screen.
      applyNewBuild();
      return;
    }
    resetCsrfSession();
    csrfSession().catch(() => {});
  });
}
