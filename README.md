# Fraud Detection MCP

A bank fraud monitoring server for the Model Context Protocol, and a terminal chatbot that uses it. Both sides of the protocol are written from scratch, without an MCP SDK.

Everything runs on simulated data, so no real cardholder, card number or transaction is involved.

## What it does

A fraud analyst normally moves between several systems to resolve a single alert. This project turns those steps into MCP tools, so the whole investigation happens in one conversation while the decisions that matter stay on the server and out of the model's reach.

The chatbot acts as an MCP host. It connects to four servers at once, the official Filesystem and Git servers plus this project's own over either stdio or HTTPS, and offers every tool they publish to a language model.

## Features

- **Six fraud tools.** Account lookup, transaction listing, risk scoring, card freeze, dispute filing and case status.
- **Explainable verdicts.** A logistic regression supplies the score and a layer of explicit rules supplies the reasons, with the evidence behind each one.
- **Two controls the model cannot bypass.** Freezing a card takes a confirmation token, and a dispute is refused outside the claim window or below medium risk. Both live in the server, not in the prompt.
- **One server, two transports.** The same tool registry runs as a local subprocess and as a deployed HTTPS service. The chatbot uses either one the same way.
- **Hand written protocol.** JSON-RPC 2.0, the MCP handshake, session management and both transports are project code, on the client and on the server.
- **A terminal interface** built with `rich`, with a live log of every MCP message.

## Repository structure

```
src/
  mcp_core/        protocol core, shared by client and server
  fraud_mcp/       the fraud server, its tools, domain and storage
  host/            the chatbot, its MCP clients and terminal interface
config/            the four MCP servers the host loads
scripts/           offline commands for the dataset, the model and the database
notebooks/         exploratory analysis behind the model
tests/             unit tests and full sessions over both transports
deploy/            container image and cloud blueprint
captures/          Wireshark captures and screenshots
docs/              documentation
```

## Quick start

Only [uv](https://docs.astral.sh/uv/) is needed. It installs the pinned Python 3.12 for you.

```bash
git clone https://github.com/Tunchxz/fraud-detection-mcp.git
cd fraud-detection-mcp
uv sync
```

Run the MCP server on its own.

```bash
uv run fraud-mcp-stdio
```

On its first run it builds `data/fraud.db` from the seed extract stored in the repository, which takes about a second, and then waits for JSON-RPC messages on standard input.

Or run the chatbot that uses it. It needs a Gemini API key, which is free and takes a minute to obtain at [Google AI Studio](https://aistudio.google.com).

```bash
cp .env.example .env        # then put your key in GEMINI_API_KEY
uv run python -m host.main
```

Then ask it something.

> Account 4471 has an alert. Review the last 7 days, freeze the card if there is fraud, and open the disputes that apply.

## Documentation

| Document                                | Contents                                                                     |
| --------------------------------------- | ---------------------------------------------------------------------------- |
| [Server reference](docs/SERVER.md)      | The MCP server in full, with its tools, endpoints, errors and usage examples |
| [Chatbot guide](docs/HOST.md)           | How to configure and use the terminal host                                   |
| [Wireshark analysis](docs/WIRESHARK.md) | Traffic capture and layer by layer analysis, in Spanish                      |
| [Project report](docs/REPORTE.md)       | Course report and conclusions, in Spanish                                    |

## Development

```bash
uv sync --group dev
uv run pytest
```

The suite covers the protocol codec, the domain rules, the critical policies and full sessions against the real server over both transports. No test calls a model API, so it needs no key and spends no quota.
