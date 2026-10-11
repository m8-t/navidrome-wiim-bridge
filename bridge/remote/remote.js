import {
  makeAuth, apiParams, formatTime, previousTarget, nextTarget, playCalls, addCalls,
  estimatePosition, gainToPercent, percentToGain, unwrap,
  formatDuration, queueSummary, progressFraction, trackArtistIfDifferent, silenceWav,
  mediaMetadataInit, positionState,
} from "./logic.js";

const STORAGE_KEY = "wiimRemoteAuth";
const LOCK_KEY = "wiimRemoteLockControls";
const POLL_MS = 1500;
const HIDDEN_POLL_MS = 3000;
const TICK_MS = 500;
const GAIN_HOLD_MS = 1500;
const STATUS_MS = 4000;

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
let pollInFlight = false;
let pollAgain = false;
let queueChain = Promise.resolve();
let dragging = false;
let gainTouchedAt = -Infinity;
let gainTimer = null;
let statusTimer = null;
let queueKey = null;
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

function showStatus(message, info = false) {
  const node = $("status");
  node.textContent = message;
  node.classList.toggle("info", info);
  node.hidden = false;
  clearTimeout(statusTimer);
  statusTimer = setTimeout(() => { node.hidden = true; }, STATUS_MS);
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

function render() {
  const song = currentSong();
  const hasQueue = entries.length > 0;
  $("empty").hidden = hasQueue;
  $("player-main").hidden = !hasQueue;
  $("title").textContent = song ? song.title || "" : "";
  $("artist").textContent = song ? song.artist || "" : "";
  $("album").textContent = song ? song.album || "" : "";
  const cover = $("cover");
  const backdrop = $("backdrop");
  if (song && song.coverArt) {
    const url = coverUrl(song.coverArt, 600);
    if (cover.getAttribute("src") !== url) cover.src = url;
    if (backdrop.getAttribute("src") !== url) backdrop.src = url;
    cover.hidden = false;
    backdrop.hidden = false;
    $("cover-ph").hidden = true;
  } else {
    cover.hidden = true;
    cover.removeAttribute("src");
    backdrop.hidden = true;
    backdrop.removeAttribute("src");
    $("cover-ph").hidden = false;
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
  const key = `${status.currentIndex}|${entries.map((e) => e.id).join(",")}`;
  if (!force && key === queueKey) return;
  queueKey = key;
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
    const remove = row.querySelector(".remove");
    if (i === status.currentIndex) remove.remove();
    else remove.addEventListener("click", () => runCommands([{ action: "remove", index: i }]));
    list.append(fragment);
  });
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
  const result = queueChain.then(task);
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
    if (sent === generation && auth) {
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

function bindPlayer() {
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

async function loadLibrary() {
  const token = ++searchToken;
  const query = $("search").value.trim();
  const list = $("library-list");
  try {
    let nodes = [];
    if (query === "") {
      const body = await api("getAlbumList2", { type: "newest", size: 40 });
      const albums = (body.albumList2 && body.albumList2.album) || [];
      nodes = albums.length
        ? [el("h2", "section", "Recently added"), albumGrid(albums)]
        : [el("p", "nothing", "Nothing found")];
    } else {
      const body = await api("search3", {
        query, albumCount: 20, songCount: 20, artistCount: 0,
      });
      const result = body.searchResult3 || {};
      const albums = result.album || [];
      const songs = result.song || [];
      if (albums.length) nodes.push(el("h2", "section", "Albums"), albumGrid(albums));
      if (songs.length) {
        const ul = el("ul", "list songs");
        for (const song of songs) ul.append(songRow(song));
        nodes.push(el("h2", "section", "Songs"), ul);
      }
      if (!nodes.length) nodes.push(el("p", "nothing", "Nothing found"));
    }
    if (token === searchToken) list.replaceChildren(...nodes);
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

function openSheet() {
  sheetReturnFocus = document.activeElement;
  $("sheet-user").textContent = auth ? `Signed in as ${auth.u}` : "";
  const supported = mediaSupported();
  const sw = $("lock-switch");
  sw.disabled = !supported;
  sw.checked = supported && lockPreferred();
  $("lock-help").textContent = supported
    ? "Plays silence on this phone so the lock screen shows the controls. Other audio on the phone will pause."
    : "Not supported by this browser.";
  $("sheet").hidden = false;
  $("sheet-panel").focus();
}

function closeSheet(restoreFocus = true) {
  if ($("sheet").hidden) return;
  $("sheet").hidden = true;
  if (restoreFocus && sheetReturnFocus && sheetReturnFocus.focus) sheetReturnFocus.focus();
  sheetReturnFocus = null;
}

function bindSheet() {
  $("open-settings").addEventListener("click", openSheet);
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
    if ($("sheet").hidden) return;
    if (event.key === "Escape") {
      closeSheet();
      return;
    }
    if (event.key !== "Tab") return;
    const items = [...$("sheet-panel").querySelectorAll("button, input:not([disabled])")];
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || active === $("sheet-panel"))) {
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
  startLoops();
  auth = loadStoredAuth();
  if (auth) enterApp();
  else showLogin();
}

init();
