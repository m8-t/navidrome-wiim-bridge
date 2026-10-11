# Kubernetes deployment

Plain manifests plus a kustomization. No Helm.

## What gets created

- `wiim-bridge` Deployment: one pod, strategy `Recreate`.
- `wiim-bridge` ConfigMap: the non-secret settings.
- `wiim-bridge` Service (ClusterIP): for an ingress or gateway and in-cluster clients.
- `wiim-bridge-lan` Service (LoadBalancer): the fixed address the amplifier uses to fetch audio.

The Secret `wiim-bridge` is not part of the kustomization. You create it yourself.
`ingress.example.yaml` and `httproute.example.yaml` are optional examples.

## Before you start

- A free LAN address for the LoadBalancer, outside your DHCP range, and a load balancer implementation that can assign it (for example MetalLB).
- The amplifier's IP address.
- A Navidrome user for the bridge.

## Steps

1. Edit `configmap.yaml`: set `BRIDGE_ADVERTISE_HOST` (the LoadBalancer address), `BRIDGE_DEVICE_HOST` (the amplifier) and `BRIDGE_NAVIDROME_URL`.
2. Set the same address in `service.yaml` on `wiim-bridge-lan`. The annotation is MetalLB's; use your load balancer's equivalent otherwise.
3. Create the secret (see `secret.example.yaml` for the manifest form):

   ```sh
   kubectl create secret generic wiim-bridge \
     --from-literal=BRIDGE_NAVIDROME_USER=bridge \
     --from-literal=BRIDGE_NAVIDROME_PASSWORD='your-navidrome-password' \
     --from-literal=BRIDGE_SECRET="$(openssl rand -hex 16)"
   ```

   Add `-n <namespace>` if you use a namespace.

4. Apply:

   ```sh
   kubectl apply -k deploy/kubernetes/
   ```

   For a namespace, create it first, uncomment `namespace:` in `kustomization.yaml`, and create the secret in that namespace. To change the version, edit `newTag` in `kustomization.yaml`.

5. Check:

   ```sh
   kubectl logs deploy/wiim-bridge
   curl http://<address>:8098/healthz
   ```

## Exposing it over HTTPS

A web page served over HTTPS can only call the bridge over HTTPS. Pick one:

- `ingress.example.yaml` for an Ingress controller.
- `httproute.example.yaml` for the Gateway API.

Edit the host, TLS secret or parentRef, then apply it with `kubectl apply -f`.

## Notes

- Run exactly one replica. Two bridges would fight over the amplifier's queue.
- A restart or redeploy clears the amplifier's queue.
- The amplifier fetches audio by IP over plain HTTP on port 8098. It cannot resolve cluster DNS names or use TLS, so do not put the audio path behind TLS only. Keep the `wiim-bridge-lan` Service.
- If you use network policies, allow ingress from the amplifier's subnet to port 8098, and egress from the pod to the amplifier on TCP 49152 and 443 and to Navidrome.
- At start the bridge checks the Navidrome login and exits on failure, so the pod restarts until Navidrome is reachable.
