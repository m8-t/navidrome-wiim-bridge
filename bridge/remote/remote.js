import {
  makeAuth, apiParams, formatTime, previousTarget, nextTarget, flowTileStyle, flowPosition, flowVelocity, flowTarget, nextPage, mixCalls, randomSongParams, collectMix, playCalls, addCalls,
  estimatePosition, reconcilePosition, gainToPercent, percentToGain, unwrap,
  formatDuration, queueSummary, progressFraction, trackArtistIfDifferent, silenceWav,
  mediaMetadataInit, positionState, needsRefresh, needsEntries, isAddOnly, createLanes,
  playNextCalls, moveEntry, indexAfterMove, dragTargetIndex, dragShift, autoScrollStep, sortAlbumsByYear,
  countLabel, playlistInfo, ownPlaylists, createViewStack, normalizeRepeat, nextRepeat,
  THEME_COLORS, loadTheme, saveTheme, isTheme, parseThemeMessage,
} from "./logic.js";

const STORAGE_KEY = "wiimRemoteAuth";
const LOCK_KEY = "wiimRemoteLockControls";
const MIX_KEY = "wiimRemoteRandomMix";
const MIX_COUNTS = [100, 250, 500];
const MIX_HEAD_CALLS = 3;
const POLL_MS = 1500;
const HIDDEN_POLL_MS = 3000;
const IDLE_TICK_MS = 1000;
const TICK_LEAD_MS = 20;
const GAIN_HOLD_MS = 1500;
const STATUS_MS = 4000;
const FLOW_DRAG_PX = 8;
const FLOW_ANIM_MS = 250;
const FLOW_REACH = 3;
const FLOW_SPACING_RATIO = 0.6;
const FLOW_GUTTER_PX = 24;
const FLOW_VELOCITY_MS = 100;
const FLOW_CENTRE_SIZE = 600;
const FLOW_SIDE_SIZE = 300;
const PAGE_SIZE = 40;
const RECENT_SIZE = 20;

const $ = (id) => document.getElementById(id);
const icon = (name) => $(`icon-${name}`).content.cloneNode(true);
const setIcon = (button, name) => {
  let wrap = button.querySelector(".ic");
  if (!wrap) {
    wrap = el("span", "ic");
    button.replaceChildren(wrap);
  }
  if (wrap.dataset.icon === name && wrap.firstChild) return;
  wrap.dataset.icon = name;
  wrap.replaceChildren(icon(name));
};
const fillIcons = (root) => {
  for (const node of root.querySelectorAll("[data-icon]")) {
    if (!node.firstChild) node.append(icon(node.dataset.icon));
  }
};
const clone = (id) => {
  const fragment = $(id).content.cloneNode(true);
  fillIcons(fragment);
  return fragment;
};
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

let auth = null;
let status = { currentIndex: 0, playing: false, gain: 0, position: 0, muted: false, shuffle: false, repeat: "off" };
let entries = [];
let polledAt = Date.now();
let anchorGeneration = 0;
let forceAnchor = false;
let tickTimer = null;
let generation = 0;
let knownQueueVersion = null;
const lanes = createLanes();
let coverBusy = false;
let pollInFlight = false;
let pollAgain = false;
let dragging = false;
let gainTouchedAt = -Infinity;
let gainTimer = null;
let statusTimer = null;
let queueKey = null;
let queueIndex = -1;
let mixGenres = null;
let mixBusy = false;
let searchTimer = null;
let searchToken = 0;
let currentTab = "player";
let lockActive = false;
let silentAudio = null;
let silentUrl = null;
let audioPrimed = false;
let needGesture = false;
let metaKey = null;
let lastPollAt = 0;
let sheetReturnFocus = null;
let flow = { el: null, tiles: [], position: null, spacing: 0, anim: null };
let paging = { offset: 0, done: false, loading: false, observer: null, sentinel: null, grid: null };
const views = createViewStack();
let navToken = 0;
let playlists = null;
let home = { recentBox: null, playlistBox: null };
let qdrag = null;
let plSong = null;
let plBusy = false;

const VIEW_NODES = { album: "album-view", playlist: "album-view", artist: "artist-view" };

function showStatus(message, info = false) {
  const node = $("status");
  node.textContent = message;
  node.classList.toggle("info", info);
  node.hidden = false;
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => { node.hidden = true; }, STATUS_MS);
}

function hideStatus() {
  clearTimeout(statusTimer);
  $("status").hidden = true;
}

function showLogin(message = "") {
  $("login-error").textContent = message;
  $("login").hidden = false;
  $("app").hidden = true;
  document.body.classList.remove("in-app", "has-mini");
}

function showApp() {
  $("login").hidden = true;
  $("app").hidden = false;
  document.body.classList.add("in-app");
}

function signOut(message = "") {
  localStorage.removeItem(STORAGE_KEY);
  closeSheet(false);
  stopLockSession();
  auth = null;
  entries = [];
  knownQueueVersion = null;
  queueKey = null;
  mixGenres = null;
  playlists = null;
  plSong = null;
  if (qdrag) finishRowDrag();
  views.reset();
  generation += 1;
  showLogin(message);
}

function handleError(err) {
  if (err && err.code === 40) {
    signOut("Wrong username or password");
    return;
  }
  showStatus((err && err.message) || "Request failed");
}

async function api(method, params = {}, body = null, withAuth = auth) {
  const query = new URLSearchParams({ ...apiParams(withAuth) });
  for (const [key, value] of Object.entries(params)) query.set(key, String(value));
  const init = body
    ? {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body.toString(),
      }
    : { method: "GET" };
  const resp = await fetch(`/rest/${method}?${query}`, init);
  let json;
  try {
    json = await resp.json();
  } catch {
    throw new Error(`Bad reply (HTTP ${resp.status})`);
  }
  return unwrap(json);
}

function jukebox(call) {
  const { action, ids, ...rest } = call;
  if (ids) {
    const body = new URLSearchParams({ action });
    for (const id of ids) body.append("id", id);
    return api("jukeboxControl", {}, body);
  }
  return api("jukeboxControl", { action, ...rest });
}

function coverUrl(coverArt, size) {
  const query = new URLSearchParams({ ...apiParams(auth), id: coverArt, size: String(size) });
  return `/rest/getCoverArt?${query}`;
}

function reportedPosition(next) {
  const ms = Number(next.positionMs);
  if (next.positionMs != null && Number.isFinite(ms)) return ms / 1000;
  return Number(next.position) || 0;
}

function applyStatus(next, list) {
  const currentIndex = next.currentIndex ?? 0;
  const playing = Boolean(next.playing);
  const position = reconcilePosition({
    shown: estimated(),
    reported: reportedPosition(next),
    playing,
    wasPlaying: status.playing,
    sameTrack: currentIndex === status.currentIndex,
    forced: forceAnchor || generation !== anchorGeneration,
  });
  forceAnchor = false;
  anchorGeneration = generation;
  status = {
    currentIndex,
    playing,
    gain: Number(next.gain) || 0,
    position,
    muted: Boolean(next.muted),
    shuffle: Boolean(next.shuffle),
    repeat: normalizeRepeat(next.repeat),
  };
  polledAt = Date.now();
  if (list) {
    entries = list;
    knownQueueVersion = typeof next.queueVersion === "string" ? next.queueVersion : null;
  }
  render();
  applyMedia();
}

function currentSong() {
  return entries[status.currentIndex] || null;
}

function setFill(input) {
  const max = Number(input.max) || 0;
  const pct = max > 0 ? Math.min(100, (Number(input.value) / max) * 100) : 0;
  input.style.setProperty("--fill", `${pct}%`);
}

function setThumb(img, coverArt, size, fallbackUrl) {
  if (coverArt) {
    img.src = coverUrl(coverArt, size);
    img.hidden = false;
  } else if (fallbackUrl) {
    img.referrerPolicy = "no-referrer";
    img.src = fallbackUrl;
    img.hidden = false;
  } else {
    img.hidden = true;
    img.removeAttribute("src");
  }
}

function updateChrome() {
  const showMini = entries.length > 0 && currentTab !== "player";
  $("mini").hidden = !showMini;
  document.body.classList.toggle("has-mini", showMini);
}

function setPlayIcon(button) {
  const label = status.playing ? "Pause" : "Play";
  setIcon(button, status.playing ? "pause" : "play");
  button.setAttribute("aria-label", label);
  button.title = label;
}

function showBackdrop(song) {
  const backdrop = $("backdrop");
  if (song && song.coverArt) {
    const url = coverUrl(song.coverArt, FLOW_CENTRE_SIZE);
    if (backdrop.getAttribute("src") !== url) backdrop.src = url;
    backdrop.hidden = false;
  } else {
    backdrop.hidden = true;
    backdrop.removeAttribute("src");
  }
}

// Rebuilt only when the text changes, so a poll cannot swap the button out from under a tap.
function setLinkLine(node, text, artistId) {
  const key = `${artistId || ""}|${text}`;
  if (node.dataset.key === key) return;
  node.dataset.key = key;
  const label = el("span", "link-text", text);
  if (!artistId || !text) {
    delete node.dataset.artistId;
    node.replaceChildren(label);
    return;
  }
  const button = el("button", "link");
  button.type = "button";
  button.append(label);
  node.dataset.artistId = artistId;
  node.replaceChildren(button);
}

function renderMeta(song) {
  $("title").textContent = song ? song.title || "" : "";
  setLinkLine($("artist"), song ? song.artist || "" : "", song && song.artistId);
  $("album").textContent = song ? song.album || "" : "";
}

function render() {
  const song = currentSong();
  const hasQueue = entries.length > 0;
  $("empty").hidden = hasQueue;
  $("player-main").hidden = !hasQueue;
  showBackdrop(song);
  if (!coverBusy) {
    renderMeta(song);
    syncFlow();
  }
  const duration = song ? Number(song.duration) || 0 : 0;
  $("seek").max = String(duration);
  $("total").textContent = formatTime(duration);
  setPlayIcon($("play"));
  setPlayIcon($("mini-play"));
  $("play").disabled = !hasQueue;
  $("prev").disabled = !hasQueue;
  $("next").disabled = nextTarget(status.currentIndex, entries.length, status.repeat) === null;
  $("mini-next").disabled = $("next").disabled;
  $("shuffle").setAttribute("aria-pressed", String(status.shuffle));
  const repeat = $("repeat");
  repeat.setAttribute("aria-pressed", String(status.repeat !== "off"));
  setIcon(repeat, status.repeat === "one" ? "repeat-one" : "repeat");
  repeat.setAttribute("aria-label", `Repeat ${status.repeat}`);
  repeat.title = `Repeat ${status.repeat}`;
  const mute = $("mute");
  mute.setAttribute("aria-pressed", String(status.muted));
  setIcon(mute, status.muted ? "volume-muted" : "volume");
  mute.setAttribute("aria-label", status.muted ? "Unmute" : "Mute");
  mute.title = status.muted ? "Unmute" : "Mute";
  if (performance.now() - gainTouchedAt > GAIN_HOLD_MS) {
    const percent = gainToPercent(status.gain);
    $("volume").value = String(percent);
    $("volume-label").textContent = String(percent);
  }
  setFill($("volume"));
  $("mini-title").textContent = song ? song.title || "" : "";
  $("mini-artist").textContent = song ? song.artist || "" : "";
  setThumb($("mini-cover"), song && song.coverArt, 96);
  $("queue-summary").textContent = queueSummary(entries);
  $("queue").classList.toggle("is-playing", status.playing);
  updateChrome();
  renderTime();
  renderQueue();
}

function estimated() {
  const song = currentSong();
  const duration = song ? Number(song.duration) || 0 : 0;
  return estimatePosition(status.position, status.playing, polledAt, Date.now(), duration);
}

function renderTime() {
  paintTime();
  scheduleTick();
}

function scheduleTick() {
  clearTimeout(tickTimer);
  tickTimer = null;
  if (!auth || !visible()) return;
  let delay = IDLE_TICK_MS;
  if (status.playing) {
    const position = estimated();
    delay = (Math.floor(position) + 1 - position) * 1000 + TICK_LEAD_MS;
  }
  tickTimer = setTimeout(renderTime, delay);
}

function paintTime() {
  if (dragging) return;
  const position = estimated();
  $("seek").value = String(Math.floor(position));
  setFill($("seek"));
  $("elapsed").textContent = formatTime(position);
  const song = currentSong();
  const fraction = progressFraction(position, song ? song.duration : 0);
  $("mini-fill").style.setProperty("--p", String(fraction));
}

function queueIds() {
  return entries.map((e) => e.id).join(",");
}

function renderQueue(force = false) {
  if (qdrag) return;
  const ids = queueIds();
  if (!force && ids === queueKey) {
    if (status.currentIndex !== queueIndex) markCurrentRow();
    return;
  }
  queueKey = ids;
  queueIndex = status.currentIndex;
  const list = $("queue");
  const scroll = list.scrollTop;
  const wasEmpty = list.childElementCount === 0;
  const indexOf = (row) => Array.prototype.indexOf.call(list.children, row);
  const rows = document.createDocumentFragment();
  entries.forEach((song, i) => {
    const fragment = clone("tpl-queue-row");
    const row = fragment.firstElementChild;
    if (i === status.currentIndex) row.classList.add("current");
    row.querySelector(".name").textContent = song.title || "";
    row.querySelector(".sub").textContent = song.artist || "";
    row.querySelector(".dur").textContent = formatTime(song.duration);
    setThumb(row.querySelector("img"), song.coverArt, 96);
    row.querySelector(".main").addEventListener("click", () => {
      gesture();
      const calls = [{ action: "skip", index: indexOf(row), offset: 0 }];
      if (!status.playing) calls.push({ action: "start" });
      runCommands(calls);
    });
    row.querySelector(".remove").addEventListener("click", () => runCommands([{ action: "remove", index: indexOf(row) }]));
    rows.append(fragment);
  });
  list.replaceChildren(rows);
  list.scrollTop = scroll;
  if (wasEmpty && currentTab === "queue") scrollCurrentIntoView();
}

function markCurrentRow() {
  if (qdrag) return;
  const rows = $("queue").children;
  if (rows[queueIndex]) rows[queueIndex].classList.remove("current");
  queueIndex = status.currentIndex;
  if (rows[queueIndex]) rows[queueIndex].classList.add("current");
  if (currentTab === "queue") scrollCurrentIntoView();
}

function dragMetrics(drag) {
  const list = $("queue");
  const min = -drag.from * drag.rowHeight;
  const max = (drag.rows.length - 1 - drag.from) * drag.rowHeight;
  const raw = drag.y - drag.startY + (list.scrollTop - drag.startScroll);
  const offset = Math.min(max, Math.max(min, raw));
  return { offset, to: dragTargetIndex(drag.from, offset, drag.rowHeight, drag.rows.length) };
}

function paintRowDrag() {
  const drag = qdrag;
  const { offset, to } = dragMetrics(drag);
  drag.row.style.transform = `translateY(${offset}px)`;
  if (to === drag.to) return;
  drag.to = to;
  drag.rows.forEach((row, i) => {
    if (row === drag.row) return;
    const shift = dragShift(i, drag.from, to);
    row.style.transform = shift ? `translateY(${shift * drag.rowHeight}px)` : "";
  });
}

function dragFrame() {
  const drag = qdrag;
  if (!drag) return;
  const list = $("queue");
  const box = list.getBoundingClientRect();
  const step = autoScrollStep(drag.y, box.top, box.bottom);
  if (step) list.scrollTop += step;
  paintRowDrag();
  drag.frame = requestAnimationFrame(dragFrame);
}

function startRowDrag(event, row, grip) {
  if (qdrag || !event.isPrimary || (event.pointerType === "mouse" && event.button !== 0)) return;
  const list = $("queue");
  const rows = [...list.children];
  const from = rows.indexOf(row);
  if (from < 0 || from === status.currentIndex) return;
  event.preventDefault();
  try {
    grip.setPointerCapture(event.pointerId);
  } catch {
    // pointer already gone; the drag still works from the events on the list
  }
  qdrag = {
    id: event.pointerId, row, rows, from, to: from, key: queueIds(),
    rowHeight: row.getBoundingClientRect().height,
    startY: event.clientY, y: event.clientY, startScroll: list.scrollTop, frame: 0,
  };
  row.classList.add("dragging");
  list.classList.add("reordering");
  qdrag.frame = requestAnimationFrame(dragFrame);
}

function moveRowDrag(event) {
  if (qdrag && event.pointerId === qdrag.id) qdrag.y = event.clientY;
}

function finishRowDrag() {
  const drag = qdrag;
  qdrag = null;
  cancelAnimationFrame(drag.frame);
  $("queue").classList.remove("reordering");
  drag.row.classList.remove("dragging");
  for (const row of drag.rows) row.style.transform = "";
  return drag;
}

function endRowDrag(event) {
  if (!qdrag || event.pointerId !== qdrag.id) return;
  qdrag.y = event.clientY;
  const { to } = dragMetrics(qdrag);
  const drag = finishRowDrag();
  const intact = queueIds() === drag.key && drag.from !== status.currentIndex;
  if (to !== drag.from && intact) commitMove(drag.row, drag.from, to);
  else renderQueue();
}

function cancelRowDrag(event) {
  if (!qdrag || event.pointerId !== qdrag.id) return;
  finishRowDrag();
  renderQueue();
}

function commitMove(row, from, to) {
  const list = $("queue");
  const scroll = list.scrollTop;
  entries = moveEntry(entries, from, to);
  status = { ...status, currentIndex: indexAfterMove(status.currentIndex, from, to) };
  queueKey = queueIds();
  queueIndex = status.currentIndex;
  const others = [...list.children].filter((other) => other !== row);
  list.insertBefore(row, others[to] || null);
  list.scrollTop = scroll;
  const stale = list.querySelector(".row.current");
  const live = list.children[status.currentIndex];
  if (stale !== live) {
    if (stale) stale.classList.remove("current");
    if (live) live.classList.add("current");
  }
  render();
  runCommands([{ action: "move", index: from, toIndex: to }]);
}

function scrollCurrentIntoView() {
  const row = $("queue").querySelector(".current");
  if (row) row.scrollIntoView({ block: "center" });
}

function runCommands(calls) {
  const addLane = isAddOnly(calls);
  const task = async () => {
    if (!addLane) generation += 1;
    const sent = generation;
    let ok = true;
    try {
      for (const call of calls) {
        const body = await jukebox(call);
        if (!auth) return false;
        if (call.action !== "add" && body.jukeboxStatus) applyStatus(body.jukeboxStatus);
      }
      if (needsRefresh(calls)) {
        const { jukeboxPlaylist: list } = await api("jukeboxControl", { action: "get" });
        if (!auth) return false;
        const superseded = addLane && (sent !== generation || lanes.pendingMain() !== 0);
        if (!superseded) applyStatus(list, Array.isArray(list.entry) ? list.entry : []);
      }
    } catch (err) {
      ok = false;
      handleError(err);
    } finally {
      if (!addLane) generation += 1;
    }
    if (!ok && auth && needsRefresh(calls)) poll();
    return ok;
  };
  return lanes.schedule(calls, task);
}

async function poll() {
  if (!auth) return;
  if (pollInFlight) {
    pollAgain = true;
    return;
  }
  pollInFlight = true;
  lastPollAt = Date.now();
  const sent = generation;
  const stale = () => sent !== generation || lanes.pendingMain() !== 0 || !auth;
  try {
    const { jukeboxStatus: brief } = await api("jukeboxControl", { action: "status" });
    if (stale()) return;
    if (needsEntries(brief, knownQueueVersion)) {
      const { jukeboxPlaylist: list } = await api("jukeboxControl", { action: "get" });
      if (stale()) return;
      applyStatus(list, Array.isArray(list.entry) ? list.entry : []);
    } else {
      applyStatus(brief);
    }
  } catch (err) {
    handleError(err);
  } finally {
    pollInFlight = false;
    if (pollAgain) {
      pollAgain = false;
      poll();
    }
  }
}

function visible() {
  return document.visibilityState === "visible";
}

function startLoops() {
  setInterval(() => {
    if (!auth) return;
    if (visible() || (lockActive && Date.now() - lastPollAt >= HIDDEN_POLL_MS - 100)) poll();
  }, POLL_MS);
  document.addEventListener("visibilitychange", () => {
    renderTime();
    if (auth && visible()) {
      knownQueueVersion = null;
      poll();
    }
  });
}

function setTab(name) {
  currentTab = name;
  for (const tab of document.querySelectorAll(".tab")) tab.hidden = tab.dataset.tab !== name;
  for (const button of document.querySelectorAll(".tabs button")) {
    if (button.dataset.go === name) button.setAttribute("aria-current", "true");
    else button.removeAttribute("aria-current");
  }
  updateChrome();
  if (name === "queue") scrollCurrentIntoView();
  if (name === "library") {
    if (!$("library-list").childElementCount) loadLibrary();
    else refreshRecent();
  }
}

function togglePlay() {
  gesture();
  runCommands([{ action: status.playing ? "stop" : "start" }]);
}

function goPrevious() {
  const target = previousTarget(status.currentIndex, estimated());
  runCommands([{ action: "skip", index: target.index, offset: target.offset }]);
}

function goNext() {
  const index = nextTarget(status.currentIndex, entries.length, status.repeat);
  if (index !== null) runCommands([{ action: "skip", index, offset: 0 }]);
}

function seekTo(seconds) {
  const song = currentSong();
  const duration = song ? Number(song.duration) || 0 : 0;
  let target = Math.max(0, Number(seconds) || 0);
  if (duration > 0) target = Math.min(target, duration);
  runCommands([{ action: "skip", index: status.currentIndex, offset: Math.floor(target) }]);
}

function makeTile() {
  const tile = el("div", "tile");
  const img = el("img");
  img.alt = "";
  img.draggable = false;
  img.hidden = true;
  const ph = el("span", "ic ph");
  ph.append(icon("music"));
  tile.append(img, ph);
  tile.hidden = true;
  return { tile, img, ph, index: -1, key: "", art: null, size: 0 };
}

function setTileSong(node, index, song, wanted) {
  const key = `${song.id}|${song.coverArt || ""}`;
  const changed = node.index !== index || node.key !== key;
  node.index = index;
  node.key = key;
  const art = song.coverArt || null;
  if (art !== node.art) {
    node.art = art;
    node.size = 0;
  }
  if (art && node.size < wanted) {
    node.img.src = coverUrl(art, wanted);
    node.size = wanted;
  }
  node.img.hidden = !art;
  node.ph.hidden = Boolean(art);
  node.tile.classList.toggle("centre", index === status.currentIndex);
  return changed;
}

function paintTile(node, offset) {
  const style = flowTileStyle(offset);
  const { tile } = node;
  tile.style.transform =
    `translateX(${(style.x * flow.spacing).toFixed(1)}px) rotateY(${style.rotateY.toFixed(1)}deg) scale(${style.scale.toFixed(3)})`;
  tile.style.opacity = String(style.opacity);
  tile.style.zIndex = String(style.z);
}

function layoutFlow(position, from = position) {
  if (!(flow.spacing > 0)) return;
  const base = Math.round(position);
  const jumped = [];
  const live = new Set();
  for (let i = base - FLOW_REACH; i <= base + FLOW_REACH; i++) {
    if (i < 0 || i >= entries.length) continue;
    const node = flow.tiles[((i % flow.tiles.length) + flow.tiles.length) % flow.tiles.length];
    live.add(node);
    const wanted = i === status.currentIndex ? FLOW_CENTRE_SIZE : FLOW_SIDE_SIZE;
    if (setTileSong(node, i, entries[i], wanted) || node.tile.hidden) {
      node.tile.hidden = false;
      node.tile.style.transition = "none";
      paintTile(node, i - from);
      jumped.push(node);
    }
  }
  for (const node of flow.tiles) {
    if (live.has(node)) continue;
    node.tile.hidden = true;
    node.index = -1;
    node.key = "";
  }
  if (jumped.length) {
    void flow.el.offsetWidth;
    for (const node of jumped) node.tile.style.transition = "";
  }
  for (const node of live) paintTile(node, node.index - position);
  flow.position = position;
}

function clampedIndex() {
  return Math.min(Math.max(status.currentIndex, 0), entries.length - 1);
}

function syncFlow() {
  if (!entries.length) {
    flow.position = null;
    return;
  }
  const index = clampedIndex();
  if (flow.position === null || !(flow.spacing > 0) || flow.position === index) layoutFlow(index);
  else glideTo(index);
}

function finishGlide(redraw = true) {
  if (!flow.anim) return;
  const { timer, onEnd } = flow.anim;
  flow.anim = null;
  clearTimeout(timer);
  flow.el.removeEventListener("transitionend", onEnd);
  flow.el.classList.remove("animating");
  coverBusy = false;
  if (redraw) render();
}

function glideTo(index) {
  finishGlide(false);
  const from = flow.position ?? index;
  coverBusy = true;
  flow.el.classList.add("animating");
  layoutFlow(index, from);
  const instant = window.matchMedia("(prefers-reduced-motion: reduce)").matches || from === index;
  const onEnd = (event) => { if (event.propertyName === "transform") finishGlide(); };
  flow.anim = { onEnd, timer: setTimeout(() => finishGlide(), FLOW_ANIM_MS + 100) };
  flow.el.addEventListener("transitionend", onEnd);
  if (instant) finishGlide();
}

function goToIndex(index, { start = false } = {}) {
  if (index !== status.currentIndex) {
    status = { ...status, currentIndex: index, position: 0 };
    polledAt = Date.now();
    forceAnchor = true;
    const calls = [{ action: "skip", index, offset: 0 }];
    if (start && !status.playing) calls.push({ action: "start" });
    runCommands(calls).then((ok) => {
      if (!ok) poll();
    });
  }
  renderMeta(entries[index] || null);
  glideTo(index);
}

function measureFlow() {
  const wrap = $("cover-wrap");
  const size = Math.max(0, Math.min(wrap.clientWidth - 2 * FLOW_GUTTER_PX, wrap.clientHeight));
  flow.spacing = size * FLOW_SPACING_RATIO;
  flow.el.style.setProperty("--tile", `${size}px`);
  if (size > 0 && entries.length && !coverBusy) layoutFlow(clampedIndex());
}

function bindCoverflow() {
  const wrap = $("cover-wrap");
  flow.el = $("flow");
  flow.tiles = Array.from({ length: 2 * FLOW_REACH + 1 }, makeTile);
  flow.el.append(...flow.tiles.map((node) => node.tile));
  new ResizeObserver(measureFlow).observe(wrap);
  let drag = null;
  const nodeAt = (target) => flow.tiles.find((node) => node.tile === target.closest(".tile"));
  const nearest = (position) => Math.min(Math.max(Math.round(position), 0), entries.length - 1);
  const reset = () => {
    const done = drag;
    drag = null;
    return done;
  };
  wrap.addEventListener("pointerdown", (event) => {
    if (drag || !event.isPrimary || (event.pointerType === "mouse" && event.button !== 0)) return;
    finishGlide();
    const node = nodeAt(event.target);
    drag = {
      id: event.pointerId, x: event.clientX, y: event.clientY, active: false, samples: [],
      index: status.currentIndex, shown: status.currentIndex, tapped: node ? node.index : -1,
    };
    wrap.setPointerCapture(event.pointerId);
  });
  wrap.addEventListener("pointermove", (event) => {
    if (!drag || event.pointerId !== drag.id) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (!drag.active) {
      if (Math.abs(dx) < FLOW_DRAG_PX || Math.abs(dx) <= 1.5 * Math.abs(dy)) return;
      drag.active = true;
      coverBusy = true;
      flow.el.classList.remove("animating");
    }
    const position = flowPosition(drag.index, dx, flow.spacing, entries.length);
    drag.position = position;
    drag.samples.push({ t: event.timeStamp, p: position });
    while (drag.samples.length > 1 && event.timeStamp - drag.samples[0].t > 2 * FLOW_VELOCITY_MS) {
      drag.samples.shift();
    }
    layoutFlow(position);
    const shown = nearest(position);
    if (shown !== drag.shown) {
      drag.shown = shown;
      renderMeta(entries[shown]);
    }
  });
  wrap.addEventListener("pointerup", (event) => {
    if (!drag || event.pointerId !== drag.id) return;
    const done = reset();
    if (!done.active) {
      const dx = event.clientX - done.x;
      if (Math.abs(dx) < FLOW_DRAG_PX && done.tapped >= 0 && done.tapped !== status.currentIndex) {
        gesture();
        goToIndex(done.tapped, { start: true });
      }
      return;
    }
    const position = flowPosition(done.index, event.clientX - done.x, flow.spacing, entries.length);
    done.samples.push({ t: event.timeStamp, p: position });
    const velocity = flowVelocity(done.samples, event.timeStamp, FLOW_VELOCITY_MS);
    goToIndex(flowTarget(position, velocity, entries.length));
  });
  const abort = (event) => {
    if (!drag || event.pointerId !== drag.id) return;
    const done = reset();
    if (done.active) goToIndex(clampedIndex());
  };
  wrap.addEventListener("pointercancel", abort);
  wrap.addEventListener("lostpointercapture", abort);
}

function bindPlayer() {
  bindCoverflow();
  $("play").addEventListener("click", togglePlay);
  $("mini-play").addEventListener("click", togglePlay);
  $("prev").addEventListener("click", goPrevious);
  $("next").addEventListener("click", goNext);
  $("mini-next").addEventListener("click", goNext);
  $("mini-open").addEventListener("click", () => setTab("player"));
  $("empty-open").addEventListener("click", () => setTab("library"));
  $("mute").addEventListener("click", () => {
    runCommands([{ action: "setMute", mute: String(!status.muted) }]);
  });
  $("shuffle").addEventListener("click", () => {
    const shuffle = !status.shuffle;
    status = { ...status, shuffle };
    render();
    runCommands([{ action: "setShuffle", shuffle: String(shuffle) }]);
  });
  $("repeat").addEventListener("click", () => {
    const repeat = nextRepeat(status.repeat);
    status = { ...status, repeat };
    render();
    runCommands([{ action: "setRepeat", repeat }]);
  });
  $("playlist-add").addEventListener("click", openPlaylistSheet);
  $("artist").addEventListener("click", (event) => {
    if (event.target.closest("button.link")) openArtist($("artist").dataset.artistId, { fromPlayer: true });
  });
  const seek = $("seek");
  seek.addEventListener("input", () => {
    dragging = true;
    $("elapsed").textContent = formatTime(Number(seek.value));
    setFill(seek);
  });
  seek.addEventListener("change", () => {
    dragging = false;
    runCommands([{ action: "skip", index: status.currentIndex, offset: Number(seek.value) }]);
  });
  const volume = $("volume");
  volume.addEventListener("input", () => {
    gainTouchedAt = performance.now();
    $("volume-label").textContent = volume.value;
    setFill(volume);
    clearTimeout(gainTimer);
    gainTimer = setTimeout(() => {
      runCommands([{ action: "setGain", gain: percentToGain(Number(volume.value)) }]);
    }, 200);
  });
}

function bindQueue() {
  $("clear").addEventListener("click", () => {
    if (window.confirm("Clear the whole queue?")) runCommands([{ action: "clear" }]);
  });
  const list = $("queue");
  list.addEventListener("pointerdown", (event) => {
    const grip = event.target.closest(".grip");
    if (grip) startRowDrag(event, grip.closest(".row"), grip);
  });
  list.addEventListener("pointermove", moveRowDrag);
  list.addEventListener("pointerup", endRowDrag);
  list.addEventListener("pointercancel", cancelRowDrag);
  list.addEventListener("lostpointercapture", cancelRowDrag);
  list.addEventListener("contextmenu", (event) => {
    if (event.target.closest(".grip")) event.preventDefault();
  });
}

async function playAndShow(calls) {
  gesture();
  if (await runCommands(calls)) setTab("player");
}

async function addAndReport(calls) {
  if (await runCommands(calls)) showStatus("Added", true);
}

async function playNextAndReport(ids) {
  if (await runCommands(playNextCalls(ids, entries.length))) showStatus("Playing next", true);
}

const soft = (promise) => promise.catch(() => []);

function albumCard(album, sub = album.artist || "") {
  const fragment = clone("tpl-album-card");
  const card = fragment.firstElementChild;
  setThumb(card.querySelector("img"), album.coverArt, 300);
  card.querySelector(".card-name").textContent = album.name || album.title || "";
  card.querySelector(".sub").textContent = sub;
  card.addEventListener("click", () => openAlbum(album));
  return card;
}

function albumGrid(albums, subOf) {
  const grid = el("div", "grid");
  for (const album of albums) grid.append(albumCard(album, subOf ? subOf(album) : undefined));
  return grid;
}

function playlistCard(playlist) {
  const fragment = clone("tpl-album-card");
  const card = fragment.firstElementChild;
  setThumb(card.querySelector("img"), playlist.coverArt, 300);
  card.querySelector(".card-name").textContent = playlist.name || "";
  card.querySelector(".sub").textContent = countLabel(playlist.songCount, "song");
  card.addEventListener("click", () => openPlaylist(playlist));
  return card;
}

function songRow(song) {
  const fragment = clone("tpl-song-row");
  const row = fragment.firstElementChild;
  row.querySelector(".name").textContent = song.title || "";
  row.querySelector(".sub").textContent = song.artist || "";
  row.querySelector(".act-play").addEventListener("click", () => playAndShow(playCalls([song.id], 0)));
  row.querySelector(".act-next").addEventListener("click", () => {
    gesture();
    playNextAndReport([song.id]);
  });
  row.querySelector(".act-add").addEventListener("click", () => {
    gesture();
    addAndReport(addCalls([song.id]));
  });
  return row;
}

function artistRow(artist) {
  const fragment = clone("tpl-artist-row");
  const row = fragment.firstElementChild;
  row.querySelector(".name").textContent = artist.name || "";
  row.querySelector(".sub").textContent = artist.albumCount != null ? countLabel(artist.albumCount, "album") : "";
  setThumb(row.querySelector("img"), artist.coverArt, 96, artist.artistImageUrl);
  row.querySelector(".main").addEventListener("click", () => openArtist(artist.id));
  return row;
}

function resetPaging() {
  if (paging.observer) paging.observer.disconnect();
  paging = { offset: 0, done: false, loading: false, observer: null, sentinel: null, grid: null };
}

async function fetchAlbumPage(offset) {
  const body = await api("getAlbumList2", { type: "newest", size: PAGE_SIZE, offset });
  return (body.albumList2 && body.albumList2.album) || [];
}

async function fetchRecentAlbums() {
  const body = await api("getAlbumList2", { type: "recent", size: RECENT_SIZE });
  return (body.albumList2 && body.albumList2.album) || [];
}

async function fetchPlaylists() {
  if (!playlists) {
    const body = await api("getPlaylists");
    playlists = (body.playlists && body.playlists.playlist) || [];
  }
  return playlists;
}

function stopPaging(state) {
  state.observer.disconnect();
  state.sentinel.remove();
}

function watchSentinel(state) {
  state.sentinel = el("div", "sentinel");
  state.sentinel.setAttribute("aria-hidden", "true");
  state.observer = new IntersectionObserver(
    (changes) => { if (changes.some((c) => c.isIntersecting)) loadMore(); },
    { root: $("library-list"), rootMargin: "0px 0px 800px 0px" },
  );
  $("library-list").append(state.sentinel);
  state.observer.observe(state.sentinel);
}

function advancePaging(state, count) {
  Object.assign(state, nextPage(state, count, PAGE_SIZE));
  if (state.done) {
    stopPaging(state);
    return;
  }
  state.observer.unobserve(state.sentinel);
  state.observer.observe(state.sentinel);
}

async function loadMore() {
  const state = paging;
  const token = searchToken;
  if (state.loading || state.done || !state.grid) return;
  state.loading = true;
  try {
    const albums = await fetchAlbumPage(state.offset);
    if (token !== searchToken) return;
    for (const album of albums) state.grid.append(albumCard(album));
    advancePaging(state, albums.length);
  } catch (err) {
    if (token === searchToken) handleError(err);
  } finally {
    state.loading = false;
  }
}

function homeBox(title) {
  const box = el("section", "home-box");
  box.dataset.title = title;
  box.hidden = true;
  return box;
}

function fillShelf(box, cards, key) {
  if (!box || box.dataset.key === key) return;
  box.dataset.key = key;
  box.hidden = cards.length === 0;
  const row = el("div", "shelf");
  row.append(...cards);
  box.replaceChildren(el("h2", "section", box.dataset.title), row);
}

const fillRecent = (albums) => fillShelf(home.recentBox, albums.map((a) => albumCard(a)), albums.map((a) => a.id).join(","));
const fillPlaylists = (lists) => fillShelf(
  home.playlistBox,
  lists.map(playlistCard),
  lists.map((p) => `${p.id}|${p.name}|${p.songCount}|${p.changed || ""}|${p.coverArt || ""}`).join(","),
);

function refreshRecent() {
  if (!home.recentBox || !home.recentBox.isConnected) return;
  const token = searchToken;
  fetchRecentAlbums().then((albums) => { if (token === searchToken) fillRecent(albums); }, () => {});
}

function invalidatePlaylists() {
  playlists = null;
  if (!home.playlistBox || !home.playlistBox.isConnected) return;
  fetchPlaylists().then(fillPlaylists, () => {});
}

async function loadRecent(token, list) {
  let failure = null;
  const [albums, recent, lists] = await Promise.all([
    fetchAlbumPage(0).catch((err) => { failure = err; return []; }),
    soft(fetchRecentAlbums()),
    soft(fetchPlaylists()),
  ]);
  if (token !== searchToken) return;
  home = { recentBox: homeBox("Recently played"), playlistBox: homeBox("Playlists") };
  fillRecent(recent);
  fillPlaylists(lists);
  const nodes = [home.recentBox, home.playlistBox];
  const grid = albums.length ? albumGrid(albums) : null;
  if (grid) nodes.push(el("h2", "section", "Recently added"), grid);
  else if (!failure && !recent.length && !lists.length) nodes.push(el("p", "nothing", "Nothing found"));
  list.replaceChildren(...nodes);
  if (grid) {
    paging.grid = grid;
    watchSentinel(paging);
    advancePaging(paging, albums.length);
  }
  if (failure) handleError(failure);
}

async function loadSearch(token, list, query) {
  const body = await api("search3", {
    query, albumCount: 20, songCount: 20, artistCount: 10,
  });
  const result = body.searchResult3 || {};
  const artists = result.artist || [];
  const albums = result.album || [];
  const songs = result.song || [];
  const nodes = [];
  if (artists.length) {
    const ul = el("ul", "list songs");
    for (const artist of artists) ul.append(artistRow(artist));
    nodes.push(el("h2", "section", "Artists"), ul);
  }
  if (albums.length) nodes.push(el("h2", "section", "Albums"), albumGrid(albums));
  if (songs.length) {
    const ul = el("ul", "list songs");
    for (const song of songs) ul.append(songRow(song));
    nodes.push(el("h2", "section", "Songs"), ul);
  }
  if (!nodes.length) nodes.push(el("p", "nothing", "Nothing found"));
  if (token === searchToken) list.replaceChildren(...nodes);
}

async function loadLibrary() {
  const token = ++searchToken;
  resetPaging();
  const query = $("search").value.trim();
  const list = $("library-list");
  try {
    if (query === "") await loadRecent(token, list);
    else await loadSearch(token, list, query);
  } catch (err) {
    if (token === searchToken) handleError(err);
  }
}

function renderTracks(songs, { numbered, albumArtist }) {
  const tracks = $("album-tracks");
  tracks.replaceChildren();
  const ids = songs.map((s) => s.id);
  songs.forEach((song, i) => {
    const fragment = clone("tpl-track-row");
    const row = fragment.firstElementChild;
    const num = row.querySelector(".num");
    if (numbered) num.textContent = String(song.track || i + 1);
    else num.remove();
    row.querySelector(".name").textContent = song.title || "";
    row.querySelector(".sub").textContent = numbered ? trackArtistIfDifferent(song, albumArtist) : song.artist || "";
    row.querySelector(".dur").textContent = formatTime(song.duration);
    row.querySelector(".main").addEventListener("click", () => playAndShow(playCalls(ids, i)));
    tracks.append(fragment);
  });
}

function paintAlbumView(frame) {
  const isAlbum = frame.kind === "album";
  $("album-name").textContent = frame.name;
  $("album-artist").hidden = !isAlbum;
  setLinkLine($("album-artist"), isAlbum ? frame.artist : "", isAlbum && frame.artistId);
  $("album-info").textContent = isAlbum ? queueSummary(frame.songs) : playlistInfo(frame.owner, frame.songs);
  setThumb($("album-cover"), frame.coverArt, 240);
  renderTracks(frame.songs, { numbered: isAlbum, albumArtist: frame.artist });
  for (const id of ["album-play", "album-next", "album-add"]) $(id).disabled = frame.songs.length === 0;
}

function paintArtistView(frame) {
  $("artist-name").textContent = frame.name;
  $("artist-info").textContent = countLabel(frame.count, "album");
  $("artist-albums").replaceChildren(
    frame.albums.length
      ? albumGrid(frame.albums, (album) => (album.year ? String(album.year) : ""))
      : el("p", "nothing", "No albums"),
  );
}

const PAINT = { album: paintAlbumView, playlist: paintAlbumView, artist: paintArtistView };

// Every view is repainted from the data its frame holds, so the same artist or album can sit on the stack twice.
function paintViews() {
  const frame = views.top();
  $("library-list").hidden = Boolean(frame);
  $("library-bar").hidden = Boolean(frame);
  for (const id of new Set(Object.values(VIEW_NODES))) $(id).hidden = !frame || VIEW_NODES[frame.kind] !== id;
  if (!frame) {
    $("library-list").scrollTop = views.rootScroll();
    return;
  }
  PAINT[frame.kind](frame);
  $(VIEW_NODES[frame.kind]).scrollTop = frame.scroll;
}

function pushView(frame) {
  const top = views.top();
  views.push(frame, top ? $(VIEW_NODES[top.kind]).scrollTop : $("library-list").scrollTop);
  paintViews();
}

function popView() {
  views.pop();
  paintViews();
}

function closeAllViews() {
  if (!views.depth()) return;
  views.clear();
  paintViews();
}

async function openAlbum(album) {
  const token = ++navToken;
  try {
    const body = await api("getAlbum", { id: album.id });
    if (token !== navToken) return;
    const data = body.album || {};
    const songs = [...(data.song || [])].sort(
      (a, b) => (a.discNumber || 0) - (b.discNumber || 0) || (a.track || 0) - (b.track || 0),
    );
    pushView({
      kind: "album",
      name: data.name || album.name || "",
      artist: data.artist || album.artist || "",
      artistId: data.artistId || album.artistId || null,
      coverArt: data.coverArt || album.coverArt,
      songs,
    });
  } catch (err) {
    if (token === navToken) handleError(err);
  }
}

async function openPlaylist(playlist) {
  const token = ++navToken;
  try {
    const body = await api("getPlaylist", { id: playlist.id });
    if (token !== navToken) return;
    const data = body.playlist || {};
    pushView({
      kind: "playlist",
      id: playlist.id,
      name: data.name || playlist.name || "",
      owner: data.owner || playlist.owner || "",
      coverArt: data.coverArt || playlist.coverArt,
      songs: Array.isArray(data.entry) ? data.entry : [],
    });
  } catch (err) {
    if (token === navToken) handleError(err);
  }
}

async function openArtist(id, { fromPlayer = false } = {}) {
  if (!id) return;
  const token = ++navToken;
  try {
    const body = await api("getArtist", { id });
    if (token !== navToken) return;
    const data = body.artist || {};
    const albums = sortAlbumsByYear(data.album || []);
    const frame = {
      kind: "artist", id, name: data.name || "", count: Number(data.albumCount) || albums.length, albums,
    };
    if (fromPlayer) {
      setTab("library");
      closeAllViews();
    }
    pushView(frame);
  } catch (err) {
    if (token === navToken) handleError(err);
  }
}

function bindLibrary() {
  $("search").addEventListener("input", () => {
    $("search-clear").hidden = $("search").value === "";
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadLibrary, 300);
  });
  $("search-clear").addEventListener("click", () => {
    $("search").value = "";
    $("search-clear").hidden = true;
    clearTimeout(searchTimer);
    loadLibrary();
    $("search").focus();
  });
  $("album-back").addEventListener("click", popView);
  $("artist-back").addEventListener("click", popView);
  const ids = () => ((views.top() || {}).songs || []).map((s) => s.id);
  $("album-play").addEventListener("click", () => playAndShow(playCalls(ids(), 0)));
  $("album-next").addEventListener("click", () => {
    gesture();
    playNextAndReport(ids());
  });
  $("album-add").addEventListener("click", () => addAndReport(addCalls(ids())));
  $("album-artist").addEventListener("click", (event) => {
    if (event.target.closest("button.link")) openArtist($("album-artist").dataset.artistId);
  });
}

const mediaSupported = () => "mediaSession" in navigator;
const lockPreferred = () => localStorage.getItem(LOCK_KEY) === "1";

function gesture() {
  if (lockActive && silentAudio && (!audioPrimed || needGesture)) tryPlaySilence();
}

function tryPlaySilence() {
  if (!silentAudio) return;
  let promise;
  try {
    promise = silentAudio.play();
  } catch {
    return;
  }
  if (!promise) return;
  promise.then(() => {
    audioPrimed = true;
    needGesture = false;
  }).catch((err) => {
    if (err && err.name === "AbortError") return;
    needGesture = true;
    showStatus("Tap play once to enable the lock screen controls");
  });
}

function setHandler(action, handler) {
  try {
    navigator.mediaSession.setActionHandler(action, handler);
  } catch {
    // unknown action in this browser
  }
}

function installHandlers() {
  setHandler("play", () => { runCommands([{ action: "start" }]); });
  setHandler("pause", () => { runCommands([{ action: "stop" }]); });
  setHandler("nexttrack", goNext);
  setHandler("previoustrack", goPrevious);
  setHandler("seekto", (details) => seekTo(details && details.seekTime));
  setHandler("seekbackward", null);
  setHandler("seekforward", null);
}

function removeHandlers() {
  for (const action of ["play", "pause", "nexttrack", "previoustrack", "seekto", "seekbackward", "seekforward"]) {
    setHandler(action, null);
  }
}

function startLockSession(fromGesture) {
  if (!mediaSupported()) return;
  if (!lockActive) {
    lockActive = true;
    silentAudio = new Audio();
    silentAudio.loop = true;
    silentUrl = URL.createObjectURL(new Blob([silenceWav()], { type: "audio/wav" }));
    silentAudio.src = silentUrl;
    installHandlers();
  }
  if (fromGesture) tryPlaySilence();
  applyMedia();
}

function stopLockSession() {
  if (!lockActive) return;
  lockActive = false;
  if (silentAudio) {
    silentAudio.pause();
    silentAudio.removeAttribute("src");
    silentAudio.load();
    silentAudio = null;
  }
  if (silentUrl) URL.revokeObjectURL(silentUrl);
  silentUrl = null;
  audioPrimed = false;
  needGesture = false;
  metaKey = null;
  removeHandlers();
  try {
    navigator.mediaSession.metadata = null;
    navigator.mediaSession.playbackState = "none";
    navigator.mediaSession.setPositionState();
  } catch {
    // best effort
  }
}

function applyMedia() {
  if (!lockActive) return;
  const session = navigator.mediaSession;
  const song = currentSong();
  const key = song ? `${song.id}|${song.coverArt || ""}|${auth ? auth.t : ""}` : "";
  if (key !== metaKey) {
    metaKey = key;
    try {
      session.metadata = song && typeof MediaMetadata === "function"
        ? new MediaMetadata(mediaMetadataInit(song, coverUrl))
        : null;
    } catch {
      // ignore
    }
  }
  session.playbackState = status.playing ? "playing" : "paused";
  if (silentAudio) {
    if (status.playing) {
      if (audioPrimed && silentAudio.paused) tryPlaySilence();
    } else if (!silentAudio.paused) {
      silentAudio.pause();
    }
  }
  const duration = song ? Number(song.duration) || 0 : 0;
  const state = positionState(estimated(), duration);
  try {
    if (state) session.setPositionState(state);
    else session.setPositionState();
  } catch {
    // ignore
  }
}

const activeSheet = () => document.querySelector(".sheet-root:not([hidden])");

function showSheet(root) {
  sheetReturnFocus = document.activeElement;
  root.hidden = false;
  root.querySelector(".sheet").focus();
}

function openSettings() {
  $("sheet-user").textContent = auth ? `Signed in as ${auth.u}` : "";
  const supported = mediaSupported();
  const sw = $("lock-switch");
  sw.disabled = !supported;
  sw.checked = supported && lockPreferred();
  $("lock-help").textContent = supported
    ? "Plays silence on this phone so the lock screen shows the controls. Other audio on the phone will pause."
    : "Not supported by this browser.";
  showSheet($("sheet"));
}

function closeSheet(restoreFocus = true) {
  const root = activeSheet();
  if (!root) return;
  root.hidden = true;
  if (restoreFocus && sheetReturnFocus && sheetReturnFocus.focus) sheetReturnFocus.focus();
  sheetReturnFocus = null;
}

function loadMixChoice() {
  const fallback = { count: 500, fromYear: "", toYear: "", genres: [], skipInterludes: false };
  try {
    const data = JSON.parse(localStorage.getItem(MIX_KEY));
    if (!data || typeof data !== "object") return fallback;
    return {
      count: MIX_COUNTS.includes(data.count) ? data.count : 500,
      fromYear: String(data.fromYear ?? ""),
      toYear: String(data.toYear ?? ""),
      genres: Array.isArray(data.genres) ? data.genres.filter((g) => typeof g === "string") : [],
      skipInterludes: data.skipInterludes === true,
    };
  } catch {
    return fallback;
  }
}

function saveMixChoice(choice) {
  try {
    localStorage.setItem(MIX_KEY, JSON.stringify(choice));
  } catch {
    // storage unavailable, the page works without it
  }
}

function readMixChoice() {
  const pressed = document.querySelector("#mix-count [aria-pressed='true']");
  return {
    count: pressed ? Number(pressed.dataset.count) : 500,
    fromYear: $("mix-from").value.trim(),
    toYear: $("mix-to").value.trim(),
    genres: [...document.querySelectorAll("#mix-genres [aria-pressed='true']")].map((b) => b.dataset.genre),
    skipInterludes: $("mix-skip").checked,
  };
}

function updateGenreHint() {
  const picked = readMixChoice().genres;
  $("mix-genre-hint").textContent = picked.length ? `${picked.length} selected` : "Any genre";
}

function renderGenres(genres, picked) {
  const box = $("mix-genres");
  box.replaceChildren();
  for (const genre of genres) {
    const chip = el("button", "chip", genre.value);
    chip.type = "button";
    chip.dataset.genre = genre.value;
    chip.setAttribute("aria-pressed", String(picked.includes(genre.value)));
    box.append(chip);
  }
  updateGenreHint();
}

async function loadGenres(picked) {
  if (mixGenres) {
    renderGenres(mixGenres, picked);
    return;
  }
  try {
    const body = await api("getGenres");
    const list = (body.genres && body.genres.genre) || [];
    mixGenres = [...list]
      .filter((g) => g && g.value)
      .sort((a, b) => (b.songCount || 0) - (a.songCount || 0));
    renderGenres(mixGenres, picked);
  } catch (err) {
    handleError(err);
  }
}

function openMix() {
  const choice = loadMixChoice();
  for (const button of document.querySelectorAll("#mix-count button")) {
    button.setAttribute("aria-pressed", String(Number(button.dataset.count) === choice.count));
  }
  $("mix-from").value = choice.fromYear;
  $("mix-to").value = choice.toYear;
  $("mix-skip").checked = choice.skipInterludes;
  showSheet($("mix-sheet"));
  loadGenres(choice.genres);
}

async function fetchRandom(params) {
  const body = await api("getRandomSongs", params);
  return (body.randomSongs && body.randomSongs.song) || [];
}

async function playMix() {
  if (mixBusy) return;
  gesture();
  const choice = readMixChoice();
  saveMixChoice(choice);
  mixBusy = true;
  $("mix-play").disabled = true;
  showStatus("Loading mix\u2026", true);
  try {
    const base = { count: choice.count, fromYear: choice.fromYear, toYear: choice.toYear };
    const genres = choice.genres.length ? choice.genres : [undefined];
    const { songs, short } = await collectMix(
      () => Promise.all(genres.map((genre) => fetchRandom(randomSongParams({ ...base, genre })))),
      choice.count,
      choice.skipInterludes,
    );
    if (!songs.length) {
      showStatus("No songs match these filters");
      return;
    }
    const calls = mixCalls(songs.map((song) => song.id));
    if (!(await runCommands(calls.slice(0, MIX_HEAD_CALLS)))) return;
    if (short) showStatus(`Mix has ${songs.length} songs (interludes and acapellas skipped)`, true);
    else hideStatus();
    closeSheet();
    setTab("player");
    if (calls.length > MIX_HEAD_CALLS) runCommands(calls.slice(MIX_HEAD_CALLS));
  } catch (err) {
    handleError(err);
  } finally {
    mixBusy = false;
    $("mix-play").disabled = false;
  }
}

function bindMix() {
  $("mix-open").addEventListener("click", openMix);
  $("mix-close").addEventListener("click", () => closeSheet());
  $("mix-dim").addEventListener("click", () => closeSheet());
  $("mix-play").addEventListener("click", playMix);
  $("mix-count").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    for (const other of $("mix-count").children) other.setAttribute("aria-pressed", String(other === button));
  });
  $("mix-genres").addEventListener("click", (event) => {
    const chip = event.target.closest(".chip");
    if (!chip) return;
    chip.setAttribute("aria-pressed", String(chip.getAttribute("aria-pressed") !== "true"));
    updateGenreHint();
  });
}

function setPlaylistBusy(busy) {
  plBusy = busy;
  for (const button of $("pl-panel").querySelectorAll(".pl-item, #pl-create")) button.disabled = busy;
}

async function renderPlaylistChoices(song) {
  const box = $("pl-list");
  try {
    const own = ownPlaylists(await fetchPlaylists(), auth && auth.u);
    if ($("pl-sheet").hidden || plSong !== song) return;
    if (!own.length) {
      box.replaceChildren(el("li", "pl-empty", "No playlists yet"));
      return;
    }
    box.replaceChildren(...own.map((playlist) => {
      const item = el("li");
      const button = el("button", "pl-item");
      button.type = "button";
      button.append(el("span", "name", playlist.name || ""), el("span", "sub", countLabel(playlist.songCount, "song")));
      button.addEventListener("click", () => saveToPlaylist(
        "updatePlaylist", { playlistId: playlist.id, songIdToAdd: song.id }, playlist.name || "playlist",
      ));
      item.append(button);
      return item;
    }));
  } catch (err) {
    handleError(err);
  }
}

function openPlaylistSheet() {
  const song = currentSong();
  if (!song) return;
  plSong = song;
  $("pl-song").textContent = [song.title, song.artist].filter(Boolean).join(" \u00b7 ");
  $("pl-name").value = "";
  $("pl-list").replaceChildren(el("li", "pl-empty", "Loading\u2026"));
  setPlaylistBusy(false);
  showSheet($("pl-sheet"));
  renderPlaylistChoices(song);
}

function patchPlaylistViews(playlistId, song) {
  for (const frame of views.all()) {
    if (frame.kind === "playlist" && frame.id === playlistId) frame.songs = [...frame.songs, song];
  }
  const top = views.top();
  if (top && top.kind === "playlist" && top.id === playlistId) paintAlbumView(top);
}

async function saveToPlaylist(method, params, name) {
  const song = plSong;
  if (plBusy || !song) return;
  setPlaylistBusy(true);
  try {
    await api(method, params);
    if (params.playlistId) patchPlaylistViews(params.playlistId, song);
    showStatus(`Added to ${name}`, true);
    closeSheet();
    invalidatePlaylists();
  } catch (err) {
    handleError(err);
  } finally {
    setPlaylistBusy(false);
  }
}

function bindPlaylistSheet() {
  $("pl-close").addEventListener("click", () => closeSheet());
  $("pl-dim").addEventListener("click", () => closeSheet());
  $("pl-new").addEventListener("submit", (event) => {
    event.preventDefault();
    const name = $("pl-name").value.trim();
    if (!name) {
      $("pl-name").focus();
      return;
    }
    saveToPlaylist("createPlaylist", { name, songId: plSong && plSong.id }, name);
  });
}

const embedded = window.parent !== window;
let parentTheme = null;

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", THEME_COLORS[theme]);
  for (const button of document.querySelectorAll("#theme-group [data-theme]")) {
    button.setAttribute("aria-pressed", String(button.dataset.theme === theme));
  }
}

function bindTheme() {
  $("theme-group").hidden = embedded;
  if (embedded) {
    window.addEventListener("message", (event) => {
      if (event.source !== window.parent) return;
      const theme = parseThemeMessage(event.data);
      if (!theme) return;
      parentTheme = theme;
      applyTheme(theme);
    });
  }
  applyTheme(parentTheme || loadTheme(localStorage));
  for (const button of document.querySelectorAll("#theme-group [data-theme]")) {
    button.addEventListener("click", () => {
      const theme = button.dataset.theme;
      if (!isTheme(theme)) return;
      saveTheme(localStorage, theme);
      applyTheme(theme);
    });
  }
}

function bindSheet() {
  $("open-settings").addEventListener("click", openSettings);
  $("sheet-close").addEventListener("click", () => closeSheet());
  $("sheet-dim").addEventListener("click", () => closeSheet());
  $("signout").addEventListener("click", () => signOut());
  $("lock-switch").addEventListener("change", (event) => {
    if (event.target.checked) {
      localStorage.setItem(LOCK_KEY, "1");
      startLockSession(true);
    } else {
      localStorage.removeItem(LOCK_KEY);
      stopLockSession();
    }
  });
  document.addEventListener("keydown", (event) => {
    const root = activeSheet();
    if (!root) return;
    if (event.key === "Escape") {
      closeSheet();
      return;
    }
    if (event.key !== "Tab") return;
    const panel = root.querySelector(".sheet");
    const items = [...panel.querySelectorAll("button:not([disabled]), input:not([disabled])")];
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || active === panel)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  });
}

function randomSalt() {
  const bytes = crypto.getRandomValues(new Uint8Array(8));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function bindLogin() {
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const user = $("login-user").value.trim();
    const candidate = makeAuth(user, $("login-pass").value, randomSalt());
    $("login-pass").value = "";
    $("login-error").textContent = "";
    try {
      await api("ping", {}, null, candidate);
    } catch (err) {
      $("login-error").textContent =
        err.code === 40 ? "Wrong username or password" : err.message || "Sign in failed";
      return;
    }
    auth = candidate;
    localStorage.setItem(STORAGE_KEY, JSON.stringify({ u: auth.u, t: auth.t, s: auth.s }));
    enterApp();
  });
}

function enterApp() {
  showApp();
  setTab("player");
  views.reset();
  paintViews();
  $("library-list").replaceChildren();
  searchToken += 1;
  resetPaging();
  playlists = null;
  home = { recentBox: null, playlistBox: null };
  if (lockPreferred() && mediaSupported()) startLockSession(false);
  poll();
}

function loadStoredAuth() {
  try {
    const data = JSON.parse(localStorage.getItem(STORAGE_KEY));
    if (data && data.u && data.t && data.s) return { u: data.u, t: data.t, s: data.s };
  } catch {
    // fall through to login
  }
  return null;
}

function init() {
  fillIcons(document);
  for (const button of document.querySelectorAll(".tabs button")) {
    button.addEventListener("click", () => setTab(button.dataset.go));
  }
  bindLogin();
  bindPlayer();
  bindQueue();
  bindLibrary();
  bindTheme();
  bindSheet();
  bindMix();
  bindPlaylistSheet();
  startLoops();
  auth = loadStoredAuth();
  if (auth) enterApp();
  else showLogin();
}

init();
