# Fraud Detection MCP

An MCP server that turns a bank fraud investigation into tools a language model can use, and a terminal chatbot that acts as its host. Built for **CC3067-Redes** at Universidad del Valle de Guatemala, where the assignment requires implementing the Model Context Protocol **from scratch**.

A fraud analyst normally jumps between separate systems to resolve a single alert. This server exposes those steps (look up the account, list the movements, score a transaction, freeze the card, open a dispute) so the whole investigation happens in one conversation, while the decisions that matter stay on the server side and out of the model's reach.

Everything runs on **simulated data**. No real cardholder, card number or transaction is involved.

## Requirements

- [uv](https://docs.astral.sh/uv/). It installs the pinned Python 3.12 for you.
- Nothing else. The server itself runs on the Python standard library alone; the trained model
  travels in the repository as JSON file.

## Quick start

```bash
git clone https://github.com/Tunchxz/fraud-detection-mcp.git
cd fraud-detection-mcp
uv sync
uv run fraud-mcp-stdio
```

That is the whole setup. On its first run the server builds `data/fraud.db` from the seed extract versioned under `data/seed/`, which takes about a second, and then waits for JSON-RPC messages on standard input. To check it by hand:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"1"}}}' \
  | uv run fraud-mcp-stdio
```

The seeded database always carries **current dates**, every timestamp is shifted so the newest transaction lands on the moment of seeding. Rebuild it whenever you want a clean state:

```bash
uv run python scripts/seed_db.py --force
```

That command also prints which account demonstrates which rule.

## Connecting a host

The server speaks stdio, so any MCP host can launch it. All a host needs is the command:

```
uv run --directory </absolute/path/to/fraud-detection-mcp> fraud-mcp-stdio
```

### Example

For **Claude Desktop**, add this to `claude_desktop_config.json` (`%APPDATA%\Claude\` on Windows, `~/Library/Application Support/Claude/` on macOS) and restart the app:

```json
{
  "mcpServers": {
    "fraud-monitoring": {
      "command": "uv",
      "args": ["run", "--directory", "</absolute/path/to/fraud-detection-mcp>", "fraud-mcp-stdio"]
    }
  }
}
```

If Claude Desktop runs on Windows and the repository lives inside WSL (like my case), cross the bridge instead:

```json
{
  "mcpServers": {
    "fraud-monitoring": {
      "command": "wsl.exe",
      "args": ["-e", "bash", "-lc",
               "cd /home/<user>/fraud-detection-mcp && ~/.local/bin/uv run fraud-mcp-stdio"]
    }
  }
}
```

## What the server exposes

| Tool                | What it does (returns)                                                             |
| ------------------- | ---------------------------------------------------------------------------------- |
| `get_account`       | Cardholder, card status and spending habits                                        |
| `list_transactions` | Recent movements, newest first, filterable by amount and channel                   |
| `score_transaction` | Risk score plus the explicit rules it violates, with evidence                      |
| `freeze_card`       | Blocks the card (**needs a confirmation token**)                                   |
| `open_dispute`      | Opens a chargeback claim (refused outside the 120 day window or below medium risk) |
| `get_case_status`   | Case status, amount under dispute and the transactions it covers                   |

The risk verdict is hybrid: a logistic regression trained offline supplies the score, and a layer of explicit rules supplies the reasons.

The two controls above are enforced in the server, not suggested to the model; no phrasing of a request gets a card frozen without a confirmation token, or a stale transaction disputed.

## Try the demonstration

Account **4471** is seeded with a complete case. Ask the host:

> *"Account 4471 has an alert. Review the last 7 days, freeze the card if there is fraud, and open the disputes that apply."*

Expected: a purchase in Guatemala 14 minutes after one in the United States flagged as high risk at over 15,000 km/h, a freeze that first returns a confirmation token and only then takes effect, and a six-month-old charge refused for being past the claim window.

Accounts **4472** (card already frozen) and **4474** (open case with a dispute) cover the remaining paths.

## Development

```bash
uv sync --group dev          # test dependencies
uv run pytest                # unit tests plus a full session over stdio
```

Useful environment variables: `FRAUD_MCP_DB` and `FRAUD_MCP_MODEL` override the database and model paths, and `FRAUD_MCP_LOG` writes every JSON-RPC message to a file.

### Rebuilding the model

The model in `artifacts/model.json` is versioned, so this is only needed to retrain it. It requires the [Credit Card Transactions Fraud Detection Dataset](https://www.kaggle.com/datasets/kartik2112/fraud-detection)
(kartik2112 on Kaggle, ~500 MB) extracted into `data/raw/`:

```bash
uv sync --group train
uv run python scripts/prepare_dataset.py    # raw CSV  → data/processed/
uv run python scripts/train_model.py        # training → artifacts/model.json
uv run python scripts/extract_seed.py       # raw CSV  → data/seed/
```

`scripts/train_model.py` re-implements the model in pure Python and refuses to write the artifact if its score differs from scikit-learn's by more than `1e-9`. The exploration behind every preprocessing decision is in `notebooks/`.

## Repository layout

```
src/mcp_core/      MCP from scratch: JSON-RPC codec, session, transports
src/fraud_mcp/     the fraud server: tools, domain rules, SQLite access
scripts/           offline commands: dataset, training, seed extract, database
notebooks/         exploratory analysis and modelling
tests/             unit tests and an end-to-end stdio session
```
