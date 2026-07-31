# Hermes Agent Runtime Contract

## Dispatch Message

Websocket message from ClawChat to bridge:

```json
{
  "type": "hermes.run.dispatch",
  "data": {
    "dispatchId": "uuid",
    "runtimeRunId": "optional-runtime-run-id",
    "runtimeSessionId": "session-id",
    "externalAgentId": "social_hermes",
    "threadId": "thread-id",
    "workspaceId": "workspace-id",
    "inputText": "user request",
    "timeoutMs": 600000,
    "marketplaceTools": [],
    "availableRuntimeTools": [],
    "runtimeToolsets": {
      "additive": [],
      "disabled": []
    },
    "autonomyPolicy": {}
  }
}
```

Required fields:

- `dispatchId`
- `runtimeSessionId`
- `externalAgentId`

## Lifecycle Events

Bridge sends websocket envelopes:

```json
{
  "type": "hermes_runtime_event",
  "event": {
    "type": "run.accepted",
    "dispatchId": "uuid",
    "runtimeRunId": "uuid-or-runtime-id",
    "externalAgentId": "social_hermes",
    "metadata": {}
  }
}
```

Supported event types:

- `run.received`
- `run.accepted`
- `run.queued`
- `run.started`
- `run.worker_started`
- `run.model_started`
- `run.delta`
- `run.thinking`
- `run.context`
- `run.status`
- `run.tool`
- `run.completed`
- `run.failed`
- `run.cancelled`

Terminal event types:

- `run.completed`
- `run.failed`
- `run.cancelled`

Terminal events should be acknowledged by ClawChat so the bridge can stop
retrying them.

## Cancellation

```json
{
  "type": "hermes.run.cancel",
  "data": {
    "dispatchId": "uuid"
  }
}
```

If the bridge never saw the dispatch, it should log an unknown/missed cancel and
avoid emitting repeated synthetic terminal events.

## Reconnect Backfill

After websocket auth and agent registration, the bridge calls:

```text
POST /api/v1/bridge/runtime-dispatches/backfill
```

Request:

```json
{
  "devicePublicId": "bridge-device-id",
  "workspaceId": "workspace-id",
  "externalAgentIds": [
    "social_hermes",
    "linc_jr_hermes",
    "linc_snr_hermes",
    "postman_hermes"
  ],
  "states": ["pending", "started_unaccepted", "unaccepted"],
  "capabilities": ["clawchat.runtime.hermes"]
}
```

Response:

```json
{
  "dispatches": [
    {
      "dispatchId": "uuid",
      "runtimeRunId": "uuid",
      "runtimeSessionId": "session-id",
      "externalAgentId": "social_hermes",
      "inputText": "missed request",
      "status": "pending"
    }
  ]
}
```

Backfill must be scoped to the authenticated bridge/device/registered agents.

