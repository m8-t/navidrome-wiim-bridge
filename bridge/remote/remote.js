import {
  makeAuth, apiParams, formatTime, previousTarget, nextTarget, flowTileStyle, flowPosition, flowVelocity, flowTarget, nextPage, mixCalls, randomSongParams, mergeRandom, playCalls, addCalls,
  estimatePosition, gainToPercent, percentToGain, unwrap,
  formatDuration, queueSummary, progressFraction, trackArtistIfDifferent, silenceWav,
  mediaMetadataInit, positionState,
} from "./logic.js";

const STORAGE_KEY = "wiimRemoteAuth";
const LOCK_KEY = "wiimRemoteLockControls";
const MIX_KEY = "wiimRemoteRandomMix";
const MIX_COUNTS = [100, 250, 500];
const MIX_HEAD_CALLS = 3;
const POLL_MS = 1500;
const HIDDEN_POLL_MS = 3000;
const TICK_MS = 500;
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

const $ = (id) => document.getElementById(id);
const icon = (name) => $(`icon-${name}`).content.cloneNode(true);
const setIcon = (button, name) => {
  let wrap = button.querySelector(".ic");
  if (!wrap) {
    wrap = el("span", "ic");
    button.replaceChildren(wrap);
  }
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
let status = { currentIndex: 0, playing: false, gain: 0, position: 0, muted: false };
let entries = [];
let polledAt = Date.now();
let generation = 0;
let pendingCommands = 0;
let coverBusy = false;
let pollInFlight = false;
let pollAgain = false;
let queueChain = Promise.resolve();
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
let albumSongs = [];
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
let libraryScroll = 0;

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
  queueKey = null;
  mixGenres = null;
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

function applyStatus(next, list) {
  status = {
    currentIndex: next.currentIndex ?? 0,
    playing: Boolean(next.playing),
    gain: Number(next.gain) || 0,
    position: Number(next.position) || 0,
    muted: Boolean(next.muted),
  };
  polledAt = Date.now();
  if (list) entries = list;
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

function setThumb(img, coverArt, size) {
  if (coverArt) {
    img.src = coverUrl(coverArt, size);
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

function renderMeta(song) {
  $("title").textContent = song ? song.title || "" : "";
  $("artist").textContent = song ? song.artist || "" : "";
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
  $("next").disabled = nextTarget(status.currentIndex, entries.length) === null;
  $("mini-next").disabled = $("next").disabled;
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
  if (dragging) return;
  const position = estimated();
  $("seek").value = String(Math.floor(position));
  setFill($("seek"));
  $("elapsed").textContent = formatTime(position);
  const song = currentSong();
  const fraction = progressFraction(position, song ? song.duration : 0);
  $("mini-fill").style.setProperty("--p", String(fraction));
}

function renderQueue(force = false) {
  const ids = entries.map((e) => e.id).join(",");
  if (!force && ids === queueKey) {
    if (status.currentIndex !== queueIndex) markCurrentRow();
    return;
  }
  queueKey = ids;
  queueIndex = status.currentIndex;
  const list = $("queue");
  list.replaceChildren();
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
      const calls = [{ action: "skip", index: i, offset: 0 }];
      if (!status.playing) calls.push({ action: "start" });
      runCommands(calls);
    });
    row.querySelector(".remove").addEventListener("click", () => runCommands([{ action: "remove", index: i }]));
    list.append(fragment);
  });
  if (currentTab === "queue") scrollCurrentIntoView();
}

function markCurrentRow() {
  const rows = $("queue").children;
  if (rows[queueIndex]) rows[queueIndex].classList.remove("current");
  queueIndex = status.currentIndex;
  if (rows[queueIndex]) rows[queueIndex].classList.add("current");
  if (currentTab === "queue") scrollCurrentIntoView();
}

function scrollCurrentIntoView() {
  const row = $("queue").querySelector(".current");
  if (row) row.scrollIntoView({ block: "center" });
}

const REFRESHING = new Set(["set", "add", "remove", "clear"]);

function runCommands(calls) {
  const task = async () => {
    generation += 1;
    let refresh = false;
    let ok = true;
    try {
      for (const call of calls) {
        if (REFRESHING.has(call.action)) refresh = true;
        const body = await jukebox(call);
        if (!auth) return false;
        if (body.jukeboxStatus) applyStatus(body.jukeboxStatus);
      }
    } catch (err) {
      ok = false;
      handleError(err);
    } finally {
      generation += 1;
    }
    if (auth && refresh) poll();
    return ok;
  };
  pendingCommands += 1;
  const result = queueChain.then(task).finally(() => {
    pendingCommands -= 1;
  });
  queueChain = result.catch(() => {});
  return result;
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
  try {
    const body = await api("jukeboxControl", { action: "get" });
    if (sent === generation && pendingCommands === 0 && auth) {
      const list = body.jukeboxPlaylist;
      applyStatus(list, Array.isArray(list.entry) ? list.entry : []);
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
  setInterval(() => { if (auth && visible()) renderTime(); }, TICK_MS);
  document.addEventListener("visibilitychange", () => {
    if (auth && visible()) poll();
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
  if (name === "library" && !$("library-list").childElementCount) loadLibrary();
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
  const index = nextTarget(status.currentIndex, entries.length);
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
}

async function playAndShow(calls) {
  gesture();
  if (await runCommands(calls)) setTab("player");
}

async function addAndReport(calls) {
  if (await runCommands(calls)) showStatus("Added", true);
}

function albumCard(album) {
  const fragment = clone("tpl-album-card");
  const card = fragment.firstElementChild;
  setThumb(card.querySelector("img"), album.coverArt, 300);
  card.querySelector(".card-name").textContent = album.name || album.title || "";
  card.querySelector(".sub").textContent = album.artist || "";
  card.addEventListener("click", () => openAlbum(album));
  return card;
}

function albumGrid(albums) {
  const grid = el("div", "grid");
  for (const album of albums) grid.append(albumCard(album));
  return grid;
}

function songRow(song) {
  const fragment = clone("tpl-song-row");
  const row = fragment.firstElementChild;
  row.querySelector(".name").textContent = song.title || "";
  row.querySelector(".sub").textContent = song.artist || "";
  row.querySelector(".act-play").addEventListener("click", () => playAndShow(playCalls([song.id], 0)));
  row.querySelector(".act-add").addEventListener("click", () => {
    gesture();
    addAndReport(addCalls([song.id]));
  });
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

async function loadRecent(token, list) {
  const albums = await fetchAlbumPage(0);
  if (token !== searchToken) return;
  if (!albums.length) {
    list.replaceChildren(el("p", "nothing", "Nothing found"));
    return;
  }
  const grid = albumGrid(albums);
  list.replaceChildren(el("h2", "section", "Recently added"), grid);
  paging.grid = grid;
  watchSentinel(paging);
  advancePaging(paging, albums.length);
}

async function loadSearch(token, list, query) {
  const body = await api("search3", {
    query, albumCount: 20, songCount: 20, artistCount: 0,
  });
  const result = body.searchResult3 || {};
  const albums = result.album || [];
  const songs = result.song || [];
  const nodes = [];
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

async function openAlbum(album) {
  try {
    const body = await api("getAlbum", { id: album.id });
    const data = body.album || {};
    albumSongs = [...(data.song || [])].sort(
      (a, b) => (a.discNumber || 0) - (b.discNumber || 0) || (a.track || 0) - (b.track || 0),
    );
    const albumArtist = data.artist || album.artist || "";
    $("album-name").textContent = data.name || album.name || "";
    $("album-artist").textContent = albumArtist;
    $("album-info").textContent = queueSummary(albumSongs);
    setThumb($("album-cover"), data.coverArt || album.coverArt, 240);
    const tracks = $("album-tracks");
    tracks.replaceChildren();
    const allIds = albumSongs.map((s) => s.id);
    albumSongs.forEach((song, i) => {
      const fragment = clone("tpl-track-row");
      const row = fragment.firstElementChild;
      row.querySelector(".num").textContent = String(song.track || i + 1);
      row.querySelector(".name").textContent = song.title || "";
      row.querySelector(".sub").textContent = trackArtistIfDifferent(song, albumArtist);
      row.querySelector(".dur").textContent = formatTime(song.duration);
      row.querySelector(".main").addEventListener("click", () => playAndShow(playCalls(allIds, i)));
      tracks.append(fragment);
    });
    libraryScroll = $("library-list").scrollTop;
    $("album-view").hidden = false;
    $("library-list").hidden = true;
    $("library-bar").hidden = true;
    $("album-view").scrollTop = 0;
  } catch (err) {
    handleError(err);
  }
}

function closeAlbum() {
  $("album-view").hidden = true;
  $("library-list").hidden = false;
  $("library-bar").hidden = false;
  $("library-list").scrollTop = libraryScroll;
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
  $("album-back").addEventListener("click", closeAlbum);
  const ids = () => albumSongs.map((s) => s.id);
  $("album-play").addEventListener("click", () => playAndShow(playCalls(ids(), 0)));
  $("album-add").addEventListener("click", () => addAndReport(addCalls(ids())));
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
  const fallback = { count: 500, fromYear: "", toYear: "", genres: [] };
  try {
    const data = JSON.parse(localStorage.getItem(MIX_KEY));
    if (!data || typeof data !== "object") return fallback;
    return {
      count: MIX_COUNTS.includes(data.count) ? data.count : 500,
      fromYear: String(data.fromYear ?? ""),
      toYear: String(data.toYear ?? ""),
      genres: Array.isArray(data.genres) ? data.genres.filter((g) => typeof g === "string") : [],
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
    const lists = await Promise.all(genres.map((genre) => fetchRandom(randomSongParams({ ...base, genre }))));
    const songs = mergeRandom(lists, choice.count);
    if (!songs.length) {
      showStatus("No songs match these filters");
      return;
    }
    const calls = mixCalls(songs.map((song) => song.id));
    if (!(await runCommands(calls.slice(0, MIX_HEAD_CALLS)))) return;
    hideStatus();
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
  closeAlbum();
  $("library-list").replaceChildren();
  searchToken += 1;
  resetPaging();
  libraryScroll = 0;
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
  bindSheet();
  bindMix();
  startLoops();
  auth = loadStoredAuth();
  if (auth) enterApp();
  else showLogin();
}

init();
