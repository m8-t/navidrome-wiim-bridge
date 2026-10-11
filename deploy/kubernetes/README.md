# Kubernetes deployment

Plain manifests plus a kustomization. No Helm.

They deploy the whole stack except Navidrome: the bridge and the patched
[Feishin](https://github.com/m8-t/feishin/tree/jukebox-bridge) web UI that controls it.
Navidrome is expected to run already, in the cluster or elsewhere.

## What gets created

Bridge:

- `wiim-bridge` Deployment: one pod, strategy `Recreate`.
- `wiim-bridge` ConfigMap: the non-secret settings.
- `wiim-bridge` Service (ClusterIP): for an ingress or gateway and in-cluster clients.
- `wiim-bridge-lan` Service (LoadBalancer): the fixed address the amplifier uses to fetch audio.

Web UI:

- `feishin` Deployment: one pod, nginx with the web build.
- `feishin` ConfigMap: the bridge's address and the login screen settings.
- `feishin` Service (ClusterIP): for an ingress or gateway.

The Secret `wiim-bridge` is not part of the kustomization. You create it yourself.
`ingress.example.yaml` and `httproute.example.yaml` are examples; you need one of them, or your own equivalent.

## Before you start

- A running Navidrome and a Navidrome user for the bridge.
- A free LAN address for the LoadBalancer, outside your DHCP range, and a load balancer implementation that can assign it (for example MetalLB).
- The amplifier's IP address.
- Two host names with TLS, one for the bridge and one for the web UI (for example `bridge.example.org` and `music.example.org`).

## Steps

1. Edit `configmap.yaml`: set `BRIDGE_ADVERTISE_HOST` (the LoadBalancer address), `BRIDGE_DEVICE_HOST` (the amplifier) and `BRIDGE_NAVIDROME_URL`.
2. Set the same address in `service.yaml` on `wiim-bridge-lan`. The annotation is MetalLB's; use your load balancer's equivalent otherwise.
3. Edit `feishin-configmap.yaml`: set `SERVER_URL` to the bridge's HTTPS address. It is the address the browser uses, so a cluster-internal name does not work, and it is the bridge, not Navidrome.
4. Create the secret (see `secret.example.yaml` for the manifest form):

   ```sh
   kubectl create secret generic wiim-bridge \
     --from-literal=BRIDGE_NAVIDROME_USER=bridge \
     --from-literal=BRIDGE_NAVIDROME_PASSWORD='your-navidrome-password' \
     --from-literal=BRIDGE_SECRET="$(openssl rand -hex 16)"
   ```

   Add `-n <namespace>` if you use a namespace.

5. Apply:

   ```sh
   kubectl apply -k deploy/kubernetes/
   ```

   For a namespace, create it first, uncomment `namespace:` in `kustomization.yaml`, and create the secret in that namespace.

6. Expose both over HTTPS. Pick one, edit the hosts and the TLS secrets or the parentRef, then apply it with `kubectl apply -f`:

   - `ingress.example.yaml` for an Ingress controller.
   - `httproute.example.yaml` for the Gateway API.

   The bridge's host must be the one set as `SERVER_URL` in step 3.

7. Check:

   ```sh
   kubectl logs deploy/wiim-bridge
   curl http://<LoadBalancer address>:8098/healthz
   curl https://music.example.org/healthz
   ```

8. Open the web UI, log in with your Navidrome user, and choose "Jukebox" as the player in the settings. The phone page is at `/remote/` on the bridge's host.

## Versions

Both image tags are set in `kustomization.yaml` under `images:`. The two are released separately:

- `ghcr.io/m8-t/navidrome-wiim-bridge`: the bridge's version, see the [changelog](../../CHANGELOG.md).
- `ghcr.io/m8-t/feishin-jukebox`: the upstream Feishin version with the suffix `-jukebox`.

## Without the web UI

The bridge works with any Subsonic client that has a jukebox mode and with its own phone page. To leave Feishin out, remove the three `feishin-` lines and the `feishin-jukebox` image entry from `kustomization.yaml`. HTTPS is then only needed if another HTTPS page calls the bridge.

## Notes

- Run exactly one bridge replica. Two bridges would fight over the amplifier's queue.
- A restart or redeploy of the bridge clears the amplifier's queue. Restarting the web UI does not touch it.
- The amplifier fetches audio by IP over plain HTTP on port 8098. It cannot resolve cluster DNS names or use TLS, so do not put the audio path behind TLS only. Keep the `wiim-bridge-lan` Service.
- A browser refuses to call a plain HTTP address from an HTTPS page. That is why the bridge needs HTTPS as soon as the web UI has it.
- If you use network policies, allow ingress from the amplifier's subnet to port 8098, and egress from the bridge pod to the amplifier on TCP 49152 and 443 and to Navidrome. The web UI pod needs ingress on 9180 only.
- At start the bridge checks the Navidrome login and exits on failure, so the pod restarts until Navidrome is reachable.
