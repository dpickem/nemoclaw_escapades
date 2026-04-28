# NMB Sandbox-to-Sandbox Communication Prototype

This prototype derisks the dashboard design's NMB path:

```text
client sandbox -> NMB broker running inside a separate broker/orchestrator sandbox
```

It proves the path with real OpenShell sandboxes by launching:

- an NMB broker sandbox that runs `nemoclaw_escapades.nmb.broker`,
- a client sandbox with an NMB-only egress policy,
- a host-side preflight probe, and
- a task smoke flow: `client.ready -> task.assign -> task.complete`.

## Run

From the repository root:

```bash
make -C prototypes/nmb_sandbox_communication smoke
```

Useful diagnostics:

```bash
make -C prototypes/nmb_sandbox_communication debug-client
make -C prototypes/nmb_sandbox_communication logs
make -C prototypes/nmb_sandbox_communication stop
```

## Final Working Route

The working topology mirrors the `browser_sandbox_http` prototype:

1. The broker process binds `0.0.0.0:9876` inside the broker sandbox.
2. OpenShell creates an operator-controlled forward:

   ```bash
   openshell sandbox create ... --forward 0.0.0.0:9876
   ```

3. The host reaches the broker at:

   ```text
   ws://127.0.0.1:9876
   ```

4. The client sandbox reaches the same forwarded listener through OpenShell's
   outbound proxy at:

   ```text
   ws://host.docker.internal:9876
   ```

The client policy grants only NMB WebSocket egress for this route.

## Verified Result

The full smoke test completed end-to-end:

- host WebSocket preflight connected to the forwarded broker,
- client sandbox WebSocket probe connected through the OpenShell proxy,
- client sandbox published `client.ready`,
- broker-side peer sent `task.assign`,
- client sandbox sent `task.complete`, and
- the broker audit DB recorded the three expected messages.

Audit summary from the successful run:

```text
publish | client.ready   | client -> smoke.ready
send    | task.assign    | broker-side peer -> client
send    | task.complete  | client -> broker-side peer
```

## Findings

`messages.local:9876` was not automatically bound to the broker sandbox in the
tested OpenShell setup. The client sandbox received HTTP 403 from the OpenShell
proxy when attempting that route.

The Kubernetes headless service name for the broker sandbox existed, but it did
not reach the broker process. OpenShell runs the command inside a nested runtime
network namespace, so ordinary sandbox service DNS was not enough to reach the
process listening on `0.0.0.0:9876`.

Direct TCP from the client sandbox to `host.docker.internal:9876` timed out when
the WebSocket client bypassed proxy handling. The successful path is through the
OpenShell proxy, not a raw bypass.

`host.docker.internal` resolved to a Docker host-gateway address in the
`172.16.0.0/12` range during the test. The client policy must include that range
in `allowed_ips`; a narrower stale range caused HTTP 403 even though the forward
was working.

Reusing the broker image for the client sandbox avoids repeated image builds and
slow image pulls. The Makefile extracts the generated `openshell/sandbox-from:*`
image tag from the broker log and uses it for the client sandbox.

## Development Lessons

Stage the proof instead of jumping straight to two sandboxes:

1. Prove the broker starts inside a sandbox.
2. Prove the host can reach it through the OpenShell forward.
3. Prove the client sandbox can reach that forwarded broker with a diagnostic
   probe.
4. Only then run the task exchange.

Keep a client-side diagnostic mode. The `debug-client` target prints proxy
environment, discovered proxies, DNS resolution, raw TCP results, WebSocket via
proxy, and WebSocket with proxy disabled. That made it clear that policy/proxy
egress was the relevant layer.

Treat `--forward` as an operator-created reach mechanism. It works for the MVP
style spike, but it is distinct from a production service-discovery route like
`messages.local`.

Make cleanup automatic. The `smoke` and `debug-client` targets trap exits and
delete smoke sandboxes/forwards so iteration does not leave stale routes behind.

Keep runtime images compatible with OpenShell. As in the browser HTTP prototype,
the image includes `iproute2`, creates a `sandbox` user/group, and avoids setting
`USER` or `ENTRYPOINT` so OpenShell can apply its supervisor and policy setup.

## Remaining Questions

- What is the production mechanism for binding `messages.local:9876` to a
  broker process inside the orchestrator sandbox?
- Can OpenShell expose a sandbox-resident TCP service to other sandboxes without
  going through a host forward?
- Should the dashboard MVP intentionally use the forward-rendezvous route, or
  should implementation wait for a gateway-level service binding?
- How should production policy constrain the Docker host-gateway route if this
  remains the MVP path?

