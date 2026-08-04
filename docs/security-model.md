# Security model

The benchmark environment runs untrusted model output against a disposable
nested desktop, but this is research software—not a hardened multi-tenant
sandbox.

## Enforced boundaries

- `semantic-simple-v1` exposes exactly three text-result tools.
- The OSWorld observation layer is configured not to capture screenshots.
- Tool result validation rejects image blocks and data-image payloads.
- The final provider payload is recursively audited before every model call.
- The model has no shell, host Python, arbitrary browser JavaScript, raw keys,
  coordinates, CDP address, display socket, or AT-SPI object path.
- Process execution uses argv rather than a shell and is separated from GUI
  automation credentials.
- Guest control uses an episode-scoped bearer token not shown to the model.
- The guest agent receives no evaluator configuration or expected answer.
- Evaluators run only after the model has stopped.

Strict traces include counters for screenshots captured, image parts created,
image parts retained, image parts sent, pixels sent to the policy model, and
visual sidecar calls. A valid strict result requires every counter to be zero.

## Network and credentials

The environment needs public network access for OSWorld task setup and research
tasks. Research routes reject loopback, private, link-local, metadata-service,
and special-use targets. Do not place cloud service-account credentials or
private API keys inside the guest image.

The GCP scripts never put provider keys into source bundles or result
manifests. Still use a dedicated low-privilege project and model key, and audit
result artifacts before publishing because task content can contain external
data.

## Not guaranteed

The OSWorld Docker image, browser, office suite, and native application bridges
were not designed as a hostile multi-tenant boundary. Run one trust domain per
outer host. Do not expose port 8079 publicly. Do not use the environment to
open secrets or accounts you would not place in a disposable test VM.

Report security issues privately through the repository's GitHub security
advisory interface rather than a public benchmark trace.
