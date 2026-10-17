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

## Notes

- Run exactly one bridge replica. Two bridges would fight over the amplifier's queue.
- A restart or redeploy of the bridge clears the amplifier's queue. Restarting Navidrome or the web UI does not touch it.
- The amplifier fetches audio by IP over plain HTTP on the device port, 8099. It cannot resolve cluster DNS names or use TLS, so do not put the audio path behind TLS only. Keep the `wiim-bridge-lan` Service. Everything else (remote page, jukebox API, Navidrome pass-through) stays on port 8098 behind your ingress or gateway.
- A browser refuses to call a plain HTTP address from an HTTPS page. That is why the bridge needs HTTPS as soon as the web UI has it.
- Navidrome keeps its database in `navidrome-data`. Deleting that claim deletes users, playlists and play counts; the music files are never written to.
- If you use network policies, allow ingress from the amplifier's subnet to port 8099, and egress from the bridge pod to the amplifier on TCP 49152 and 443 and to Navidrome on 4533. Navidrome needs egress to the NFS server. The web UI pod needs ingress on 9180 only.
