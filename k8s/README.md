# KnowPilot on Kubernetes

Production runs on one VM with Docker Compose ([ADR-0020](../docs/adr/0020-single-vm-compose-deployment.md)).
These manifests are the same system expressed as Kubernetes objects, run on a
local k3d cluster. What they are for, and what they deliberately are not, is
in [ADR-0022](../docs/adr/0022-kubernetes-manifests.md).

## The layout

```
k8s/
  base/                 every object, with production-shaped defaults
  overlays/local/       secrets, image tags, and requests a laptop can meet
  test-isolation.sh     proves the NetworkPolicies from inside the cluster
```

## The cluster

**k3d, not kind**, and the reason is the whole point of the exercise: k3s
ships a CNI that enforces `NetworkPolicy`. kind's default (kindnet) does not -
`kubectl apply` succeeds, the objects exist, and nothing is blocked. A network
policy you cannot test is a comment.

```bash
# Pinned and checksummed rather than piped from the internet into a shell.
curl -fsSLO https://github.com/k3d-io/k3d/releases/download/v5.9.0/k3d-linux-amd64
echo "06d8f25bc3a971c4eb29e0ff08429b180402db0f4dec838c9eac427e296800a0  k3d-linux-amd64" | sha256sum -c
sudo install -m 755 k3d-linux-amd64 /usr/local/bin/k3d && rm k3d-linux-amd64

k3d cluster create knowpilot --port "80:80@loadbalancer" --agents 0
```

The stack needs about 7 GB. On Windows, WSL takes half the host by default -
raise it in `%USERPROFILE%\.wslconfig` and run `wsl --shutdown` first.

## Images

Built locally and imported, not pulled: the backend image is 2.5 GB and a
registry round trip for every change is a minute nobody spends twice.

```bash
docker build -t knowpilot-backend:dev backend
docker build -t knowpilot-frontend:dev frontend
docker build -t knowpilot-keycloak:dev infra/keycloak
k3d image import knowpilot-backend:dev knowpilot-frontend:dev knowpilot-keycloak:dev -c knowpilot
```

## Secrets

Never in git. The same script the server uses generates them:

```bash
cd k8s/overlays/local
cp secrets.env.example secrets.env
../../../infra/server/fill-secrets.sh secrets.env
```

## Apply

```bash
kubectl apply -k k8s/overlays/local
kubectl -n knowpilot get pods -w
```

Expect this order, and it is the interesting part: the `model` Job downloads
the weights (a few minutes the first time), the `migrate` Job applies the
schema, and only then do the API and the worker leave `Init:0/2`. Compose said
that with `service_completed_successfully`; **Kubernetes has no equivalent**,
so the wait is rebuilt by initContainers - one watching for the weights, one
comparing the database's Alembic revision with the newest one in the image.

Then open <http://knowpilot.localhost>.

## Prove the isolation

```bash
./k8s/test-isolation.sh
```

Seven connection attempts from pods wearing the labels the policies select
on. The one that matters: a pod labelled `worker` must **fail** to reach the
internet, exactly as the `internal` network does in the Compose stack, while
still reaching PostgreSQL and Redis. Reading the YAML tells you what was
intended; only this tells you what happens.

## Tear down

```bash
k3d cluster delete knowpilot
```
