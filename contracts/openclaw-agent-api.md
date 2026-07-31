# OpenClaw Agent API Contract

The OpenClaw bridge is for local app/source-host operations.

The bridge must only call approved local targets:

```text
http://localhost:3052/api/openclaw/*
http://127.0.0.1:3052/api/openclaw/*
```

The default port is `3052`.

## Request Envelope

ClawChat marketplace/local-app tool calls should preserve the OpenClaw contract
envelope:

```json
{
  "contractVersion": "2026-03-18",
  "input": {
    "taskId": "task-id",
    "resultSummary": "completed safely"
  }
}
```

When wrapped as a bridge tool invocation:

```json
{
  "requestId": "request-id",
  "appSlug": "local-linkcrest",
  "method": "POST",
  "baseUrl": "http://localhost:3052/api/openclaw",
  "path": "tasks/complete",
  "contractVersion": "2026-03-18",
  "credential": {
    "authorization": "Bearer LOCAL"
  },
  "body": {
    "contractVersion": "2026-03-18",
    "input": {
      "taskId": "task-id",
      "resultSummary": "completed safely"
    }
  }
}
```

## Security Requirements

- Reject target hosts other than `localhost` and `127.0.0.1`.
- Reject target ports other than the configured local app port.
- Reject paths outside `/api/openclaw`.
- Never log bearer tokens or credential payloads.
- Return sanitized response bodies to ClawChat.
- Treat write failures as commit-unknown until readback proves otherwise.

## Runtime Recovery

If the local app is not reachable, the runtime may attempt a safe configured
start command such as:

```text
pnpm dev
```

Do not run installs, migrations, deploys, resets, destructive operations, or
interactive shell commands without explicit approval.
