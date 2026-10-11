import {
  makeAuth, apiParams, formatTime, previousTarget, nextTarget, playCalls, addCalls,
  estimatePosition, gainToPercent, percentToGain, unwrap,
} from "./logic.js";

const STORAGE_KEY = "wiimRemoteAuth";
const POLL_MS = 1500;
const TICK_MS = 500;
const GAIN_HOLD_MS = 1500;
const STATUS_MS = 4000;

const $ = (id) => document.getElementById(id);
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
}

function showApp() {
  $("login").hidden = true;
  $("app").hidden = false;
}

function signOut(message = "") {
  localStorage.removeItem(STORAGE_KEY);
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
}

function currentSong() {
  return entries[status.currentIndex] || null;
}

function render() {
  const song = currentSong();
  const hasQueue = entries.length > 0;
  $("empty").hidden = hasQueue;
  $("meta").hidden = !hasQueue;
  $("title").textContent = song ? song.title || "" : "";
  $("artist").textContent = song ? song.artist || "" : "";
  $("album").textContent = song ? song.album || "" : "";
  const cover = $("cover");
  if (song && song.coverArt) {
    const url = coverUrl(song.coverArt, 600);
    if (cover.getAttribute("src") !== url) cover.src = url;
    cover.hidden = false;
  } else {
    cover.hidden = true;
    cover.removeAttribute("src");
  }
  const duration = song ? Number(song.duration) || 0 : 0;
  $("seek").max = String(duration);
  $("total").textContent = formatTime(duration);
  $("play").textContent = status.playing ? "Pause" : "Play";
  $("play").disabled = !hasQueue;
  $("prev").disabled = !hasQueue;
  $("next").disabled = nextTarget(status.currentIndex, entries.length) === null;
  $("mute").setAttribute("aria-pressed", String(status.muted));
  if (performance.now() - gainTouchedAt > GAIN_HOLD_MS) {
    const percent = gainToPercent(status.gain);
    $("volume").value = String(percent);
    $("volume-label").textContent = String(percent);
  }
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
  $("elapsed").textContent = formatTime(position);
}

function renderQueue(force = false) {
  const key = `${status.currentIndex}|${entries.map((e) => e.id).join(",")}`;
  if (!force && key === queueKey) return;
  queueKey = key;
  const list = $("queue");
  list.replaceChildren();
  entries.forEach((song, i) => {
    const row = el("li", i === status.currentIndex ? "row current" : "row");
    const main = el("button", "main");
    main.type = "button";
    const text = el("span", "text");
    text.append(el("span", "name", song.title || ""), el("span", "sub", song.artist || ""));
    main.append(text, el("span", "dur", formatTime(song.duration)));
    main.addEventListener("click", () => {
      const calls = [{ action: "skip", index: i, offset: 0 }];
      if (!status.playing) calls.push({ action: "start" });
      runCommands(calls);
    });
    row.append(main);
    if (i !== status.currentIndex) {
      const remove = el("button", "", "Remove");
      remove.type = "button";
      remove.addEventListener("click", () => runCommands([{ action: "remove", index: i }]));
      row.append(remove);
    }
    list.append(row);
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
  setInterval(() => { if (auth && visible()) poll(); }, POLL_MS);
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
  if (name === "queue") scrollCurrentIntoView();
  if (name === "library" && !$("library-list").childElementCount) loadLibrary();
}

function bindPlayer() {
  $("play").addEventListener("click", () => {
    runCommands([{ action: status.playing ? "stop" : "start" }]);
  });
  $("prev").addEventListener("click", () => {
    const target = previousTarget(status.currentIndex, estimated());
    runCommands([{ action: "skip", index: target.index, offset: target.offset }]);
  });
  $("next").addEventListener("click", () => {
    const index = nextTarget(status.currentIndex, entries.length);
    if (index !== null) runCommands([{ action: "skip", index, offset: 0 }]);
  });
  $("mute").addEventListener("click", () => {
    runCommands([{ action: "setMute", mute: String(!status.muted) }]);
  });
  const seek = $("seek");
  seek.addEventListener("input", () => {
    dragging = true;
    $("elapsed").textContent = formatTime(Number(seek.value));
  });
  seek.addEventListener("change", () => {
    dragging = false;
    runCommands([{ action: "skip", index: status.currentIndex, offset: Number(seek.value) }]);
  });
  const volume = $("volume");
  volume.addEventListener("input", () => {
    gainTouchedAt = performance.now();
    $("volume-label").textContent = volume.value;
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
  if (await runCommands(calls)) setTab("player");
}

async function addAndReport(calls) {
  if (await runCommands(calls)) showStatus("Added", true);
}

function albumRow(album) {
  const row = el("div", "row");
  const main = el("button", "main");
  main.type = "button";
  if (album.coverArt) {
    const img = el("img");
    img.alt = "";
    img.loading = "lazy";
    img.src = coverUrl(album.coverArt, 120);
    main.append(img);
  }
  const text = el("span", "text");
  text.append(el("span", "name", album.name || album.title || ""), el("span", "sub", album.artist || ""));
  main.append(text);
  main.addEventListener("click", () => openAlbum(album));
  row.append(main);
  return row;
}

function songRow(song) {
  const row = el("div", "row");
  const text = el("span", "text main");
  text.append(el("span", "name", song.title || ""), el("span", "sub", song.artist || ""));
  const play = el("button", "", "Play");
  play.type = "button";
  play.addEventListener("click", () => playAndShow(playCalls([song.id], 0)));
  const add = el("button", "", "Add");
  add.type = "button";
  add.addEventListener("click", () => addAndReport(addCalls([song.id])));
  row.append(text, play, add);
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
      nodes = [el("div", "section", "Recently added")];
      for (const album of (body.albumList2 && body.albumList2.album) || []) nodes.push(albumRow(album));
    } else {
      const body = await api("search3", {
        query, albumCount: 20, songCount: 20, artistCount: 0,
      });
      const result = body.searchResult3 || {};
      nodes = [el("div", "section", "Albums")];
      for (const album of result.album || []) nodes.push(albumRow(album));
      nodes.push(el("div", "section", "Songs"));
      for (const song of result.song || []) nodes.push(songRow(song));
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
    $("album-name").textContent = data.name || album.name || "";
    const tracks = $("album-tracks");
    tracks.replaceChildren();
    const allIds = albumSongs.map((s) => s.id);
    albumSongs.forEach((song, i) => {
      const row = el("li", "row");
      const main = el("button", "main");
      main.type = "button";
      const text = el("span", "text");
      text.append(el("span", "name", song.title || ""), el("span", "sub", song.artist || ""));
      main.append(text, el("span", "dur", formatTime(song.duration)));
      main.addEventListener("click", () => playAndShow(playCalls(allIds, i)));
      row.append(main);
      tracks.append(row);
    });
    $("album-view").hidden = false;
    $("library-list").hidden = true;
    $("search").parentElement.hidden = true;
    $("library-footer").hidden = true;
  } catch (err) {
    handleError(err);
  }
}

function closeAlbum() {
  $("album-view").hidden = true;
  $("library-list").hidden = false;
  $("search").parentElement.hidden = false;
  $("library-footer").hidden = false;
}

function bindLibrary() {
  $("search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(loadLibrary, 300);
  });
  $("album-back").addEventListener("click", closeAlbum);
  const ids = () => albumSongs.map((s) => s.id);
  $("album-play").addEventListener("click", () => playAndShow(playCalls(ids(), 0)));
  $("album-add").addEventListener("click", () => addAndReport(addCalls(ids())));
  $("signout").addEventListener("click", () => signOut());
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
  for (const button of document.querySelectorAll(".tabs button")) {
    button.addEventListener("click", () => setTab(button.dataset.go));
  }
  bindLogin();
  bindPlayer();
  bindQueue();
  bindLibrary();
  startLoops();
  auth = loadStoredAuth();
  if (auth) enterApp();
  else showLogin();
}

init();
