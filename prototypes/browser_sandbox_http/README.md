# Browser-to-Sandbox HTTP Prototype

This prototype derisks the `docs/design_dashboard.md` browser -> dashboard
sandbox path with the smallest possible HTTP service.

The important distinction:

- Outbound sandbox HTTP(S) egress is granted by `policy.yaml`.
- Inbound browser/host reachability is created by the operator with
  `openshell sandbox create --forward 8000`. The current OpenShell docs in this
  repo do not show a policy-declared inbound HTTP endpoint.

Port `8000` is used instead of `8080` because the local OpenShell gateway often
listens on `https://127.0.0.1:8080`.

## Run End-to-End

From this directory:

```bash
make smoke
```

That target starts the OpenShell sandbox with `--forward 8000`, runs the
host-side client, and cleans up the sandbox/forward afterward.

For local script debugging without OpenShell:

```bash
make local-smoke
```

## Manual Commands

From the repo root:

```bash
openshell sandbox delete browser-http-probe 2>/dev/null || true
openshell sandbox create \
  --name browser-http-probe \
  --from prototypes/browser_sandbox_http \
  --policy prototypes/browser_sandbox_http/policy.yaml \
  --forward 8000 \
  -- python /app/sandbox_http_server.py --host 0.0.0.0 --port 8000
```

In another terminal, run the host-side probe:

```bash
python prototypes/browser_sandbox_http/host_client.py \
  --base-url http://127.0.0.1:8000 \
  --outbound-url https://example.com/
```

Expected result:

- `/api/health` returns JSON from inside the sandbox.
- `/api/echo` round-trips a host-supplied message through the forwarded port.
- `/api/outbound` asks the sandbox server to fetch `https://example.com/`,
  exercising the outbound HTTPS rule in `policy.yaml`.

To test only the host/browser -> sandbox path without external internet:

```bash
python prototypes/browser_sandbox_http/host_client.py --skip-outbound
```

## Findings

The browser/host reachability path works end-to-end with OpenShell port
forwarding.

Verified behavior:

- A server inside the sandbox can bind `0.0.0.0:8000`.
- `openshell sandbox create --forward 8000` exposes that server at
  `http://127.0.0.1:8000/` on the host.
- A host-side client can call `/api/health` and `/api/echo` through the forward.
- The sandbox server can make an outbound HTTPS request to `https://example.com/`
  when `policy.yaml` grants that egress.
- The `make smoke` target cleans up the sandbox and forward after the probe.

The key design conclusion is that the documented MVP ingress mechanism is
operator-created port forwarding, not a policy-declared inbound HTTP endpoint.
For dashboard MVP purposes, the browser reaches the sandbox through a local
forward while sandbox outbound traffic remains governed by `network_policies`.

## Policy Lessons

OpenShell policy controls sandbox egress. In `policy.yaml`, the outbound HTTP(S)
rules are deliberately narrow and point only at `example.com` for the probe.

Inbound host/browser traffic is configured outside `network_policies` via
`--forward`. Treat this as an operator-controlled reach mechanism. It proves the
dashboard MVP can be exposed on localhost, but it does not by itself settle
whether forwarded traffic is inspected by the OpenShell policy engine or bypasses
it as a tunnel.

## Image Lessons

The first sandbox attempt timed out during provisioning with the sandbox stuck
around:

```text
DependenciesNotReady: Pod is Running but not Ready; Service Exists
```

The logs showed only the OpenShell supervisor bootstrap command, not the probe
server. Matching the production orchestrator image fixed it: install `iproute2`
in the prototype image. OpenShell's sandbox supervisor needs those networking
tools before it can apply its runtime setup and launch the requested command.

For future sandbox images:

- Do not set `USER` or `ENTRYPOINT`; OpenShell replaces the entrypoint and drops
  privileges according to the policy.
- Include a `sandbox` user/group matching the policy's `process` section.
- Include `iproute2` unless there is a proven reason the OpenShell runtime no
  longer needs it.
- Keep the prototype image minimal, but mirror production runtime prerequisites.

## Development Lessons

Make prototypes runnable through `make`. This prototype has:

- `make smoke` for the full OpenShell path.
- `make local-smoke` for server/client debugging without OpenShell.
- `make probe-no-outbound` for host/browser -> sandbox reachability when external
  internet access should not be part of the test.
- `make logs`, `make stop`, and `make clean` for iteration hygiene.

Validate the plain Python flow before debugging OpenShell. The local smoke test
separates HTTP handler/client bugs from sandbox, image, and forwarding issues.

Use a retrying host client. Sandbox startup and image pulls are asynchronous, so
the client should wait for `/api/health` instead of assuming the forward is ready
immediately.

Always clean up sandboxes and forwards after smoke tests. A successful prototype
should leave `openshell forward list` empty unless the caller intentionally kept
the service running for manual browser testing.

## Remaining Unknowns

- Whether OpenShell port-forward traffic is inspected by policy or behaves as an
  operator-created tunnel outside the policy engine.
- Which production browser reach option is right after MVP: localhost forward,
  Tailscale sidecar, or gateway-routed HTTPS endpoint.
- Whether the final dashboard needs additional auth in front of the forwarded
  backend beyond localhost/operator trust.
