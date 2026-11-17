# Kubernetes deployment

Plain manifests plus a kustomization. No Helm.

They deploy the whole stack: Navidrome, the bridge, and the patched
[Feishin](https://github.com/m8-t/feishin/tree/jukebox-bridge) web UI that controls it.
If you already run Navidrome, see [Using your own Navidrome](#using-your-own-navidrome).

## What gets created

Navidrome:

- `navidrome` Deployment: one pod, strategy `Recreate`, the music library mounted read-only.
- `navidrome` ConfigMap: log level and scan schedule.
- `navidrome-data` PersistentVolumeClaim: database and cache, 5Gi from the default storage class.
- `navidrome` Service (ClusterIP): for the bridge and an ingress or gateway.

Bridge:

- `wiim-bridge` Deployment: one pod, strategy `Recreate`.
- `wiim-bridge` ConfigMap: the non-secret settings.
- `wiim-bridge` Service (ClusterIP): for an ingress or gateway and in-cluster clients.
- `wiim-bridge-lan` Service (LoadBalancer): the fixed address the amplifier uses to fetch audio, on port 8099 (`BRIDGE_DEVICE_PORT`), which serves only the audio URLs and `/healthz`.

Web UI:

- `feishin` Deployment: one pod, nginx with the web build.
- `feishin` ConfigMap: the bridge's address and the login screen settings.
- `feishin` Service (ClusterIP): for an ingress or gateway.

The Secret `wiim-bridge` is not part of the kustomization. You create it yourself.
`ingress.example.yaml` and `httproute.example.yaml` are examples; you need one of them, or your own equivalent.

## Before you start

- The music library on a share the cluster can mount. The manifests use NFS.
- A default storage class for Navidrome's database.
- A free LAN address for the LoadBalancer, outside your DHCP range, and a load balancer implementation that can assign it (for example MetalLB).
- The amplifier's IP address.
- Three host names with TLS: Navidrome, the bridge and the web UI (for example `navidrome.example.org`, `bridge.example.org` and `music.example.org`).

## Steps

1. Edit `navidrome-deployment.yaml`: set the NFS server and path of the `music` volume, or replace it with another volume type. The files must be readable by user 1000.
2. Edit `configmap.yaml`: set `BRIDGE_ADVERTISE_HOST` (the LoadBalancer address) and `BRIDGE_DEVICE_HOST` (the amplifier).
3. Set the same LoadBalancer address in `service.yaml` on `wiim-bridge-lan`. The annotation is MetalLB's; use your load balancer's equivalent otherwise.
4. Edit `feishin-configmap.yaml`: set `SERVER_URL` to the bridge's HTTPS address. It is the address the browser uses, so a cluster-internal name does not work, and it is the bridge, not Navidrome.
5. Create the secret (see `secret.example.yaml` for the manifest form). The user does not exist yet; you create it in step 8 with the password you pick here.

   ```sh
   kubectl create secret generic wiim-bridge \
     --from-literal=BRIDGE_NAVIDROME_USER=bridge \
     --from-literal=BRIDGE_NAVIDROME_PASSWORD='your-navidrome-password' \
     --from-literal=BRIDGE_SECRET="$(openssl rand -hex 16)"
   ```

   Add `-n <namespace>` if you use a namespace.

6. Apply:

   ```sh
   kubectl apply -k deploy/kubernetes/
   ```

   For a namespace, create it first, uncomment `namespace:` in `kustomization.yaml`, and create the secret in that namespace.

7. Expose all three over HTTPS. Pick one, edit the hosts and the TLS secrets or the parentRef, then apply it with `kubectl apply -f`:

   - `ingress.example.yaml` for an Ingress controller.
   - `httproute.example.yaml` for the Gateway API.

   The bridge's host must be the one set as `SERVER_URL` in step 4.

8. Open Navidrome's host. The first visit asks you to create the admin user. Then create the user from step 5 under Users, with the same password. Until that user exists the bridge pod exits at start and is restarted; it comes up by itself afterwards.

9. Check:

   ```sh
   kubectl get pods
   kubectl logs deploy/wiim-bridge
   curl http://<LoadBalancer address>:8099/healthz
   curl https://music.example.org/healthz
   ```

10. Open the web UI, log in with a Navidrome user, and choose "Jukebox" as the player in the settings. The phone page is at `/remote/` on the bridge's host.

## Versions

All three image tags are set in `kustomization.yaml` under `images:`. They are released separately:

- `deluan/navidrome`: Navidrome's version. The bridge is tested with the one pinned here.
- `ghcr.io/m8-t/navidrome-wiim-bridge`: the bridge's version, see the [changelog](../../CHANGELOG.md).
- `ghcr.io/m8-t/feishin-jukebox`: the upstream Feishin version with the suffix `-jukebox`.

## Using your own Navidrome

Remove the four `navidrome-` lines and the `deluan/navidrome` image entry from `kustomization.yaml`, and the `navidrome` host from the Ingress or route. Set `BRIDGE_NAVIDROME_URL` in `configmap.yaml` to an address the bridge pod can reach.

## Without the web UI

The bridge works with any Subsonic client that has a jukebox mode and with its own phone page. To leave Feishin out, remove the three `feishin-` lines and the `feishin-jukebox` image entry from `kustomization.yaml`, and the `music` host from the Ingress or route. The bridge then needs HTTPS only if another HTTPS page calls it.

## Radio

Optional and separate from the default kustomization. It adds an Icecast server and a
Liquidsoap encoder that pulls an endless shuffled mix from the bridge and streams it as
MP3, for anything that can play an Icecast URL (for example Euro Truck Simulator 2).

What the `radio/` component adds:

- `wiim-radio-icecast` Deployment and Service (ClusterIP, port 8000).
- `wiim-radio-liquidsoap` Deployment, with its script in the `wiim-radio-liquidsoap` ConfigMap.
- `wiim-bridge-radio-state` PersistentVolumeClaim (64Mi) so the mix settings survive restarts.
- Patches to the bridge: `BRIDGE_RADIO=true`, `BRIDGE_RADIO_STATE_PATH`, the state volume, and the Secret `wiim-bridge-radio` as extra environment.

Images, pinned and checked to be multi-arch (the manifest lists contain `linux/arm64` and `linux/amd64`):

- `savonet/liquidsoap:v2.4.6`, the official image, full variant (not `-minimal`: the bridge hands out original files, which need the ffmpeg decoders).
- `libretime/icecast:2.4.4`, a maintained image that follows upstream Icecast and configures it from environment variables. Its tags are rebuilt when the base image changes; pin by digest if you want fully reproducible pods.

Steps:

1. Create the Secret `wiim-bridge-radio` (see `radio/secret.example.yaml`). It holds the token shared by the bridge and Liquidsoap and the two Icecast passwords.

   ```sh
   kubectl create secret generic wiim-bridge-radio \
     --from-literal=BRIDGE_RADIO_TOKEN="$(openssl rand -hex 16)" \
     --from-literal=ICECAST_SOURCE_PASSWORD="$(openssl rand -hex 16)" \
     --from-literal=ICECAST_ADMIN_PASSWORD="$(openssl rand -hex 16)"
   ```

2. Make an overlay that includes the base and the component, and apply that instead of the base:

   ```yaml
   # overlay/kustomization.yaml
   apiVersion: kustomize.config.k8s.io/v1beta1
   kind: Kustomization
   resources:
     - ../deploy/kubernetes
   components:
     - ../deploy/kubernetes/radio
   ```

   ```sh
   kubectl apply -k overlay/
   ```

3. Expose Icecast to the listener. The Service is ClusterIP. For listeners on the LAN, change `type` in `radio/icecast-service.yaml` to `LoadBalancer` (add your load balancer's fixed-address annotation, as in `service.yaml`) or to `NodePort`. Use plain HTTP: games and old players rarely handle TLS.

4. Set `BRIDGE_RADIO_STREAM_URL` to the public listen URL, for example `http://<address>:8000/radio.mp3`, in `configmap.yaml`. The remote page then shows it with a copy button.

5. Open the remote page, Settings > Radio, and pick genres or leave them all off for the whole library.

Notes:

- Liquidsoap reaches the bridge on its ClusterIP Service (`RADIO_BRIDGE_URL`, port 8098). The track addresses the bridge returns use the same host, so they work from inside the cluster. The radio endpoints are not served on the device port.
- Stream name, description and genre are the `STREAM_*` variables in `radio/liquidsoap-deployment.yaml`. Crossfade is off. The mount is `/radio.mp3` at 320 kbit/s.
- When the bridge or Navidrome is down, Liquidsoap plays silence and retries every 3 seconds, so the stream stays up.
- The "now playing" line on the page is the last track handed to Liquidsoap, which prefetches, so it can be one track ahead of what you hear.
- Icecast's start script edits `/etc/icecast.xml`, so these containers keep a writable root filesystem.

### Euro Truck Simulator 2

Add the stream to the game's `live_streams.sii` with the game closed. Its location is `Documents/Euro Truck Simulator 2/` on Windows and `~/.local/share/Euro Truck Simulator 2/` on Linux. Each entry is one line, numbered from 0, and `stream_data:` holds the number of entries:

```
 stream_data: 2
 stream_data[0]: "http://radio.example.org:8000/radio.mp3|My radio|Various|EN|320|0"
 stream_data[1]: "http://other.example/stream|Other|Rock|EN|128|0"
```

The fields are `url|name|genre|language|bitrate|favorite`. Keep the existing header lines of the file, add your lines, and raise the `stream_data:` count to match. Use a plain `http://` address; the game's player does not reliably handle HTTPS. The new station then appears in the in-game radio list.

## Notes

- Run exactly one bridge replica. Two bridges would fight over the amplifier's queue.
- A restart or redeploy of the bridge clears the amplifier's queue. Restarting Navidrome or the web UI does not touch it.
- The amplifier fetches audio by IP over plain HTTP on the device port, 8099. It cannot resolve cluster DNS names or use TLS, so do not put the audio path behind TLS only. Keep the `wiim-bridge-lan` Service. Everything else (remote page, jukebox API, Navidrome pass-through) stays on port 8098 behind your ingress or gateway.
- A browser refuses to call a plain HTTP address from an HTTPS page. That is why the bridge needs HTTPS as soon as the web UI has it.
- Navidrome keeps its database in `navidrome-data`. Deleting that claim deletes users, playlists and play counts; the music files are never written to.
- If you use network policies, allow ingress from the amplifier's subnet to port 8099, and egress from the bridge pod to the amplifier on TCP 49152 and 443 and to Navidrome on 4533. Navidrome needs egress to the NFS server. The web UI pod needs ingress on 9180 only.
