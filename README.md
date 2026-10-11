# navidrome-wiim-bridge

Plays the original files of a [Navidrome](https://www.navidrome.org) library on a WiiM
amplifier, controlled from any Subsonic client that has a jukebox mode, from a phone
page the bridge serves itself, and from the WiiM's physical remote.

No transcoding: the amplifier receives the same bytes that are on disk. Gapless playback,
seeking and hi-res files (tested up to 96 kHz / 24 bit) work.

Built and tested against one device, a WiiM Amp Ultra (firmware Linkplay 5.2), with
Navidrome 0.64 and a patched [Feishin](https://github.com/m8-t/feishin/tree/jukebox-bridge).

## The problem it solves

A WiiM is a network player. To play something, it needs a URL it can fetch. Navidrome
is a library server. It hands out files to apps, but it has no idea that a WiiM exists.
Something has to stand in between and say "WiiM, fetch this file, then that one".

Apps that cast to a WiiM directly do this from the phone or the desktop. That has two
weak points: the music stops when the app closes, and the physical remote's next and
previous buttons do nothing useful, because the queue lives in the app, not in the
device.

This bridge puts the queue into the WiiM itself and stays in the middle as a small
server. The app that started the music can be closed, and the remote works, because the
device knows what comes next.

## How it works

Three ideas carry the whole thing.

### 1. "Jukebox" is a remote control built into the Subsonic protocol

Navidrome speaks the Subsonic API. That API has a call named `jukeboxControl`. Its
meaning is: "server, do not send me the audio. Play it yourself, on your own
loudspeakers." The client then only sends commands:

| Command | Meaning |
|---|---|
| `set`, `add` | here is the list of song ids to play |
| `start`, `stop` | play, pause |
| `skip` | jump to track number N, optionally to second S |
| `setGain` | volume |
| `get` | tell me the queue and what is playing right now |

A client in jukebox mode decodes no audio at all. It is a remote control with a
library browser. Feishin calls this player type "Jukebox".

### 2. The bridge pretends to be a Navidrome whose loudspeaker is the WiiM

The client is pointed at the bridge instead of at Navidrome. The bridge looks at each
request:

- `jukeboxControl`: the bridge answers this itself.
- Everything else (login, albums, search, cover art): passed through to Navidrome
  unchanged.

So the client sees a normal Navidrome that happens to have a working jukebox.

When a `set` arrives with song ids, the bridge turns every id into a URL that points
back at the bridge, and loads that list into the WiiM's own play queue over UPnP. The
WiiM then fetches each track from the bridge, and the bridge fetches the original file
from Navidrome and hands the bytes on untouched.

```
  Feishin / phone page                      WiiM remote
        |  jukeboxControl                        |
        v                                        v
   +---------+   UPnP: load queue, play    +-----------+
   | bridge  | --------------------------> |   WiiM    |
   |         | <-------------------------- |           |
   +---------+   HTTP GET track (audio)    +-----------+
        |
        |  everything else, and the audio itself
        v
   +-----------+
   | Navidrome |
   +-----------+
```

### 3. The queue lives in the device, the bridge mirrors it

The WiiM has a queue service of its own (`PlayQueue1`). Once the list is in there, the
device handles next, previous, pause, the end of a track and gapless playback by itself.
That is why the physical remote works without any trick.

The bridge asks the device what it is doing and reports it in the answer to `get`:
current track, position, playing or paused, volume, mute. A client that polls `get`
therefore sees what the remote did a second ago and can update its display.

If several clients are open, the device's queue is the truth. The phone page and the
patched Feishin both follow it.

## What you need

- A WiiM device the bridge can reach, and that can reach the bridge by IP address over
  plain HTTP.
- A Navidrome server and a user in it. The bridge uses this user to look up songs and
  to fetch the audio.
- Python and `aiohttp`, nothing else. Developed and tested on Python 3.14 with aiohttp 3.14.

## Running it

Credentials come from a file or from the environment.

```
# file, readable only by you: {"user": "...", "password": "..."}
chmod 600 ~/.config/navidrome-wiim-bridge/credentials.json

python tools/bridge_server.py \
  --advertise-host 192.0.2.10 \
  --navidrome-url http://127.0.0.1:4533 \
  --device-base http://192.0.2.20:49152 \
  --status-url "https://192.0.2.20/httpapi.asp?command=getPlayerStatus"
```

| Option | Meaning |
|---|---|
| `--advertise-host` | the address the WiiM uses to reach the bridge |
| `--port` | port to listen on, default 8098 |
| `--navidrome-url` | where Navidrome is |
| `--device-base` | the WiiM's UPnP address, port 49152 |
| `--status-url` | the WiiM's status API (HTTPS, self-signed) |
| `--credentials` | the JSON file with the Navidrome user |
| `--secret` | fixed path secret for the audio URLs, default random per start |
| `--max-volume` | ceiling for the volume, 0 to 100, default 100 |
| `--keep-queue-on-start` | do not clear the device queue when the bridge starts |
| `--log` | file for the log of the device's audio requests |
| `--no-info-file` | do not write the helper file with the base URL |

Environment variables, meant for containers: `BRIDGE_NAVIDROME_USER`,
`BRIDGE_NAVIDROME_PASSWORD` (both or neither) and `BRIDGE_SECRET`.

At start the bridge checks that Navidrome accepts the user and exits if not. It also
clears its queue on the device, so a restart stops the music.

### Container

```
podman build -t navidrome-wiim-bridge .
podman run --rm -p 8098:8098 \
  -e BRIDGE_NAVIDROME_USER=... -e BRIDGE_NAVIDROME_PASSWORD=... -e BRIDGE_SECRET=... \
  navidrome-wiim-bridge \
  --advertise-host 192.0.2.10 --navidrome-url http://navidrome:4533 \
  --log /dev/null --no-info-file
```

`GET /healthz` answers `ok` without touching the device or Navidrome.

Run one copy only. Two bridges on one WiiM overwrite each other's queue.

## Using it

**From a Subsonic client:** add a server with the bridge's address and your Navidrome
login, and choose the jukebox player. The client must send the whole queue to the
server. Stock Feishin 1.17 sends only the current and the next track and keeps the rest
to itself, which is why a patched build exists:
[m8-t/feishin, branch `jukebox-bridge`](https://github.com/m8-t/feishin/tree/jukebox-bridge).

**From a phone:** open `http://<bridge>:8098/remote/`. It is a small page built for
portrait screens: a player with cover, seek bar, volume and mute, the queue, and a
library view with search. Sign in with your Navidrome user. The phone stores a derived
token, not the password.

**From the remote:** nothing to set up.

## Limits

- One device, set at start. No discovery, no multiroom.
- `jukeboxControl` has no call for mute. The bridge adds a `muted` field to its answers
  and a `setMute` action. Clients that do not know them simply never use them.
- `shuffle` as a server action is not supported. A client shuffles by sending the
  tracks in the order it wants.
- Removing the track that is playing stops playback, and it cannot be moved.
- A bridge restart clears the device queue.
- The audio URLs carry a secret in the path instead of a login, because the device
  cannot log in. Anyone on the network who sees such a URL can fetch that track. Keep
  the bridge on a trusted network.
- The control API has the same protection as Navidrome's: a valid Navidrome login on
  every call.

## Device notes

Things the WiiM does that the code works around, found by testing:

- Its UPnP server answers `Connection: keep-alive` and then closes the socket. Every
  call uses a fresh connection.
- `CreateQueue` starts playing at once.
- While its own queue plays, the device reports an empty `CurrentURI`.
- It fetches a track with one request for the whole file, and starts fetching the next
  track about 40 seconds before the current one ends.

## Development

```
python -m pytest -q tests        # bridge
node --test tests/js/            # phone page logic
```

Layout:

| Path | What |
|---|---|
| `bridge/app.py` | the service: routes, pass-through, phone page |
| `bridge/jukebox.py` | the `jukeboxControl` endpoint |
| `bridge/player.py` | mirrors a queue to the device |
| `bridge/wiim.py`, `bridge/soap.py` | UPnP and status calls to the WiiM |
| `bridge/proxy.py` | hands the original file to the device |
| `bridge/remote/` | the phone page |
| `tools/bridge_server.py` | start script |
| `tools/wiim_ctl.py` | command line tool to poke the device directly |
