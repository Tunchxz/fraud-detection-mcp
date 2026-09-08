# Server Reference

The `fraud-monitoring` MCP server, for transactional bank fraud monitoring over simulated data.

|                       |                                        |
| --------------------- | -------------------------------------- |
| Name                  | `fraud-monitoring`                     |
| Version               | `0.1.0`                                |
| Transports            | stdio and HTTP                         |
| Declared capabilities | `tools`, without `listChanged`         |
| Tools                 | 6                                      |
| Runtime dependencies  | none, only the Python standard library |

The same code serves both transports. Methods, tools, errors, rules and the model are identical in each, and only the first section below differs.

## Transports

### stdio, the local server

The host launches the server as a subprocess and talks to it over the standard pipes.

- Messages are JSON-RPC 2.0 in UTF-8, one per line, with no embedded line breaks.
- The server reads requests from `stdin` and writes answers to `stdout`.
- Nothing that is not a valid MCP message goes to `stdout`. All logging goes to `stderr`.
- The host starts the shutdown by closing `stdin`, and the server exits with code 0.

### HTTP, the remote server

The minimal shape of Streamable HTTP that the specification allows. One endpoint, `POST` answered with JSON, no SSE.

| Method and path | Auth | What it does                                                                   |
| --------------- | ---- | ------------------------------------------------------------------------------ |
| `POST /mcp`     | yes  | the MCP endpoint, one JSON-RPC message per request                             |
| `GET /mcp`      | yes  | `405` with `Allow: POST, DELETE`, which declares that no SSE stream is offered |
| `DELETE /mcp`   | yes  | ends the session                                                               |
| `GET /health`   | no   | service status, for the platform health check                                  |

`GET /health` is the only route without authentication, and it never reaches protocol dispatch. The transport answers it.

**Headers**

| Header                           | Direction                            | Use                                                                               |
| -------------------------------- | ------------------------------------ | --------------------------------------------------------------------------------- |
| `X-API-Key`                      | request                              | shared secret, required on `/mcp` when the server starts with `FRAUD_MCP_API_KEY` |
| `Content-Type: application/json` | request                              | required on `POST`                                                                |
| `Mcp-Session-Id`                 | answer to `initialize`, then request | identifies the session, 32 hexadecimal characters                                 |
| `MCP-Protocol-Version`           | request                              | optional, and must be a supported version when present                            |
| `Origin`                         | request                              | must be allowed when present, which is the defence against DNS rebinding          |

**Session lifecycle.** HTTP keeps no state between requests, so the handshake and the negotiated version live in the server behind an identifier.

1. `POST /mcp` with `initialize` and no session header. The answer carries the `Mcp-Session-Id`.
2. Every later request repeats it. A missing header gets `400`.
3. An identifier the server no longer knows gets `404`, which is the agreed signal to start again from step 1. Sessions are evicted after thirty minutes without use.
4. `DELETE /mcp` ends the session, with `204` when it existed.

**Status codes**

| Code                | When                                                                                                  |
| ------------------- | ----------------------------------------------------------------------------------------------------- |
| `200`               | request answered, the body is the JSON-RPC answer                                                     |
| `202`               | notification accepted, with no body, because a notification is never answered                         |
| `204`               | session ended with `DELETE`                                                                           |
| `400`               | missing session header, unsupported protocol version, invalid length or a body that is not valid JSON |
| `401`               | missing or wrong `X-API-Key`                                                                          |
| `403`               | `Origin` not allowed                                                                                  |
| `404`               | unknown path or unknown session                                                                       |
| `405`               | `GET /mcp`                                                                                            |
| `411`, `413`, `415` | missing `Content-Length`, body over 1 MiB, wrong content type                                         |

These are transport failures rather than MCP ones. The ones that arise from an already identified message carry the JSON-RPC `error` object in the body so the client can read the reason.

## Protocol versions

Supported, newest first.

```
2025-11-25   2025-06-18   2025-03-26   2024-11-05
```

The server answers with the same version the client asked for when it supports it, and with the newest one otherwise. Deciding whether to continue is the client's call.

## Lifecycle

**`initialize`** is the first required request. Before it the server accepts only `ping`, and answers `-32600` to anything else.

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1.0"}}}
```

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2025-06-18",
    "capabilities": { "tools": { "listChanged": false } },
    "serverInfo": { "name": "fraud-monitoring", "title": "Bank Fraud Monitoring", "version": "0.1.0" },
    "instructions": "Tools for a bank fraud monitoring analyst working on simulated data..."
  }
}
```

The `instructions` field orients the model on the intended workflow and warns it about the two deterministic controls.

**`notifications/initialized`** follows. It carries no identifier and is never answered.

**`ping`** is valid at any point, even before the handshake, and returns an empty result.

## Methods

| Method                      | Kind         | Answer                                                    |
| --------------------------- | ------------ | --------------------------------------------------------- |
| `initialize`                | request      | capabilities, identity and negotiated version             |
| `notifications/initialized` | notification | none                                                      |
| `ping`                      | request      | `{}`                                                      |
| `tools/list`                | request      | the six tools with their schema, always in the same order |
| `tools/call`                | request      | the tool result                                           |
| other `notifications/*`     | notification | ignored without error                                     |
| anything else               | request      | error `-32601`                                            |

The `resources`, `prompts`, `logging` and `completions` capabilities are not declared, so their methods answer `-32601`.

## Tools

Every answer carries `content` with a readable summary, `isError`, and `structuredContent` with the same data in object form. Arguments are validated against the input schema before anything runs.

| Tool                | Parameter            | Required | Type and limits                 |
| ------------------- | -------------------- | -------- | ------------------------------- |
| `get_account`       | `account_id`         | yes      | string, 1 to 16                 |
| `list_transactions` | `account_id`         | yes      | string, 1 to 16                 |
|                     | `days`               | no       | integer, 1 to 365, default 7    |
|                     | `limit`              | no       | integer, 1 to 200, default 50   |
|                     | `min_amount`         | no       | number, 0 or more               |
|                     | `channel`            | no       | string, `online` or `in_person` |
| `score_transaction` | `transaction_id`     | yes      | string, 1 to 64                 |
| `freeze_card`       | `account_id`         | yes      | string, 1 to 16                 |
|                     | `reason`             | yes      | string, 10 to 500               |
|                     | `confirmation_token` | no       | string, 4 to 32                 |
| `open_dispute`      | `transaction_ids`    | yes      | array of string, 1 to 20 items  |
|                     | `reason`             | yes      | string, 10 to 500               |
| `get_case_status`   | `case_id`            | yes      | string, 1 to 32                 |

**`get_account`** returns the cardholder, location, occupation, the card with its status and freeze reason, a ninety day spending profile and the number of open cases.

**`list_transactions`** returns recent movements, newest first, each with identifier, timestamp, amount, merchant, category, channel and country. A wide window with the default limit returns the most recent page, not the oldest.

**`score_transaction`** returns `risk_score` between 0 and 1, the `threshold`, a `risk_level` of `low`, `medium` or `high`, the rules that fired with their evidence, the model features that pushed the score, and the card history used.

**`freeze_card`** takes two calls. Without a token the answer arrives with `isError: true` and a `confirmation_token` in `structuredContent`, and nothing is frozen yet. The second call, with that token and the same reason, applies the freeze. The token lasts ten minutes, is bound to the card and the reason, and cannot be reused.

**`open_dispute`** checks each transaction on its own. It must fall inside the 120 day claim window, have been assessed at medium or high risk, and not be disputed already. Accepted transactions are grouped into a case, and rejected ones come back with their reason. If all of them are rejected the answer arrives with `isError: true` and nothing is written.

**`get_case_status`** returns the status, the opening date, the reason, the total amount under dispute and the detail of each dispute.

### Example

A call and its answer, taken from a real session.

```json
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"score_transaction","arguments":{"transaction_id":"txn_053503"}}}
```

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "result": {
    "content": [{ "type": "text", "text": "txn_053503 — 318.75 USD at Estacion Zona 10 [gas_transport, in_person, GT].\nRisk: HIGH (score 0.9313, threshold 0.90 exceeded)." }],
    "isError": false,
    "structuredContent": {
      "risk_score": 0.9313,
      "threshold": 0.9,
      "risk_level": "high",
      "exceeds_threshold": true,
      "triggered_rules": [
        {
          "rule": "impossible_geovelocity",
          "severity": "high",
          "reason": "Card-present purchase 3,692 km away from the previous one (US to GT) only 14 minutes later, which implies travelling at 15,822 km/h.",
          "evidence": { "previous_transaction_id": "txn_053502", "distance_km": 3692.4, "minutes_apart": 14.0, "implied_speed_kmh": 15822.4 }
        }
      ]
    }
  }
}
```

## Errors

The server keeps two classes apart, as the specification requires.

**Protocol errors** travel in the JSON-RPC `error` object. They are problems with the request itself.

| Code     | Meaning                                                         |
| -------- | --------------------------------------------------------------- |
| `-32700` | the line is not valid JSON, answered with a null identifier     |
| `-32600` | missing `jsonrpc` or `method`, or a request before `initialize` |
| `-32601` | unknown method or unknown tool                                  |
| `-32602` | arguments that do not satisfy the input schema                  |
| `-32603` | unexpected failure, and the session survives                    |

**Execution errors** travel as a successful result carrying `isError: true`. These are business refusals such as an unknown account, a missing or expired token, an expired claim window, insufficient risk or a duplicate dispute. They reach the model so it can read the reason and correct itself.

```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "result": {
    "content": [{ "type": "text", "text": "Case CASE-9999 does not exist. Case identifiers look like CASE-0001 and are returned by open_dispute." }],
    "isError": true,
    "structuredContent": { "error": "Case CASE-9999 does not exist.", "case_id": "CASE-9999" }
  }
}
```

## Rules and model

The score comes from a logistic regression trained offline, with a decision threshold of **0.90**, evaluated in plain Python. The rules supply the auditable justification, and their thresholds come from the exploratory analysis in `notebooks/01_eda.ipynb`.

| Rule                     | Severity | Condition                                                                                               |
| ------------------------ | -------- | ------------------------------------------------------------------------------------------------------- |
| `impossible_geovelocity` | high     | implied speed over 900 km/h **and** distance over 500 km between two consecutive card-present purchases |
| `amount_vs_profile`      | high     | amount at least 5 times the card's historical average                                                   |
| `night_window`           | medium   | between 22:00 and 03:59                                                                                 |
| `high_risk_category`     | medium   | `shopping_net`, `misc_net` or `grocery_pos`                                                             |
| `rapid_succession`       | medium   | under 10 minutes since the previous movement                                                            |
| `high_amount`            | medium   | amount of 200 USD or more                                                                               |

The distance floor in the first rule deserves a note. Speed alone fires on 1,358 of the 43,887 consecutive card-present pairs in the seeded database, because the dataset generator places every merchant within 150 km of the cardholder and two purchases minutes apart produce absurd speeds without anyone travelling. With the 500 km floor a single pair fires, the injected case.

The risk level is deterministic. It is **high** when the score reaches the threshold or a high severity rule fires, **medium** when the score reaches half the threshold or two or more rules fire, and **low** otherwise.

## Data model

SQLite in `data/fraud.db`, built by the seeding step. Twenty accounts numbered 4471 to 4490 with their full history.

| Table           | Contents                                                                 |
| --------------- | ------------------------------------------------------------------------ |
| `accounts`      | cardholder, location, coordinates, date of birth, occupation             |
| `cards`         | one card per account, with status and freeze reason                      |
| `transactions`  | date, amount, merchant, category, channel, country, merchant coordinates |
| `cases`         | fraud case per account, with status and reason                           |
| `disputes`      | one dispute per transaction, linked to a case                            |
| `freeze_tokens` | issued confirmation tokens and their use                                 |

Timestamps are local time without a zone. Seeding shifts the whole dataset so the newest transaction lands on the moment of seeding, otherwise the last seven days would return nothing and everything would fall outside the claim window. Dates of birth shift by the same amount so the cardholder's age stays the one the model saw during training.

Seeding also leaves prepared state so the policies have material from the first run.

| Account | State                                                                            |
| ------- | -------------------------------------------------------------------------------- |
| `4471`  | clean, with the demonstration case. Use it to walk the whole investigation       |
| `4472`  | card already frozen, to see `freeze_card` refuse                                 |
| `4474`  | `CASE-0001` open with a dispute, for `get_case_status` and the duplicate refusal |

## Running the server

A clone needs nothing beyond installing dependencies. The model and the seed extract are stored in the repository, so the original dataset is not required.

```bash
uv sync
uv run fraud-mcp-stdio
```

Rebuild the database whenever you want a clean state. The same command prints which account demonstrates which rule.

```bash
uv run python scripts/seed_db.py --force
```

Over HTTP, locally or in a container.

```bash
FRAUD_MCP_API_KEY=local-secret uv run fraud-mcp-http

docker build -f deploy/Dockerfile -t fraud-mcp .
docker run -p 8080:8080 -e PORT=8080 -e FRAUD_MCP_API_KEY=local-secret fraud-mcp
```

A quick check by hand.

```bash
curl -s localhost:8080/health

curl -si -X POST localhost:8080/mcp \
  -H 'Content-Type: application/json' -H 'X-API-Key: local-secret' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}'
```

The `Mcp-Session-Id` in that answer is what `tools/list` and `tools/call` must repeat.

### Environment variables

| Variable                    | Default                | Use                                                                                                                              |
| --------------------------- | ---------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `FRAUD_MCP_DB`              | `data/fraud.db`        | database path                                                                                                                    |
| `FRAUD_MCP_MODEL`           | `artifacts/model.json` | model path                                                                                                                       |
| `FRAUD_MCP_SEED`            | `data/seed/`           | folder the database is built from                                                                                                |
| `FRAUD_MCP_LOG`             | unset                  | file for the full message log                                                                                                    |
| `PORT`                      | `8080`                 | listening port, injected by the platform in the cloud                                                                            |
| `FRAUD_MCP_HOST`            | `0.0.0.0`              | listening interface                                                                                                              |
| `FRAUD_MCP_API_KEY`         | unset                  | shared secret required on `/mcp`. Without it the endpoint answers to anyone, which is acceptable locally and never in production |
| `FRAUD_MCP_ALLOWED_ORIGINS` | empty                  | comma separated list of accepted origins                                                                                         |

Paths resolve relative to the package rather than to the working directory, because a host launches the process from wherever it prefers.

### Connecting any MCP host

Any host that speaks MCP over stdio works. All it needs is the command.

```
uv run --directory /path/to/fraud-detection-mcp fraud-mcp-stdio
```

For Claude Desktop, add this to `claude_desktop_config.json` and restart the application.

```json
{
  "mcpServers": {
    "fraud-monitoring": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/fraud-detection-mcp", "fraud-mcp-stdio"]
    }
  }
}
```

If Claude Desktop runs on Windows and the repository lives inside WSL, cross the bridge instead.

```json
{
  "mcpServers": {
    "fraud-monitoring": {
      "command": "wsl.exe",
      "args": ["-e", "bash", "-lc", "cd /path/to/fraud-detection-mcp && ~/.local/bin/uv run fraud-mcp-stdio"]
    }
  }
}
```

## Demonstration

Account **4471** is seeded with a complete case.

> Account 4471 has an alert. Review the last 7 days, freeze the card if there is fraud, and open the disputes that apply.

| Step | Tool                                              | What should happen                                                   |
| ---- | ------------------------------------------------- | -------------------------------------------------------------------- |
| 1    | `get_account`                                     | spending profile, active card, no open cases                         |
| 2    | `list_transactions`                               | the purchase in Guatemala and two night time online purchases appear |
| 3    | `score_transaction` on the Guatemala purchase     | high risk with `impossible_geovelocity` above 15,000 km/h            |
| 4    | `score_transaction` on the 1,180 USD purchase     | high risk with `amount_vs_profile` at 15 times the average           |
| 5    | `freeze_card` without a token                     | `isError: true` with the token, and the card stays active            |
| 6    | `freeze_card` with the token and the same reason  | card frozen                                                          |
| 7    | `open_dispute` on the risky transactions          | case opened with the amount under dispute                            |
| 8    | `open_dispute` on a transaction from 200 days ago | refused by the claim window                                          |
| 9    | `get_case_status`                                 | case status and the detail of each dispute                           |

Steps 5, 6 and 8 are the ones that show the business rules living in the server. No phrasing of the request gets around the token or the claim window.
