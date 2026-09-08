# Chatbot Guide

The host is a terminal chatbot that connects to four MCP servers (expansible) at once and gives a language model every tool they publish. 

## Setup

Copy the example environment file and fill in a Gemini API key. The free tier needs no card and takes a minute to obtain at [Google AI Studio](https://aistudio.google.com).

```bash
cp .env.example .env
```

| Variable               | Required            | Use                                                                        |
| ---------------------- | ------------------- | -------------------------------------------------------------------------- |
| `GEMINI_API_KEY`       | yes                 | the model API key. `GOOGLE_API_KEY` also works                             |
| `GEMINI_MODEL`         | no                  | overrides the model. The default is `gemini-3.1-flash-lite`                |
| `FRAUD_MCP_REMOTE_URL` | only for `--remote` | the deployed server endpoint                                               |
| `FRAUD_MCP_API_KEY`    | only for `--remote` | the shared secret of that service                                          |
| `SSLKEYLOGFILE`        | no                  | exports TLS session keys, which is what makes a Wireshark capture readable |

## Running it

```bash
uv run python -m host.main
```

| Flag         | Effect                                                             |
| ------------ | ------------------------------------------------------------------ |
| `--remote`   | use the deployed server over HTTPS instead of the local subprocess |
| `--model`    | override the model for this run                                    |
| `--config`   | point at a different server list                                   |
| `--log`      | write the MCP message log somewhere else                           |
| `--no-trace` | start with the live protocol trace hidden                          |

On startup the host opens every configured server, prints what connected and what did not, and waits. A server that fails to open is reported and skipped rather than being fatal, since the remote one sleeps after fifteen minutes on the free plan and takes about a minute to wake up.

## Commands

| Command    | What it does                                      |
| ---------- | ------------------------------------------------- |
| `/help`    | the command list                                  |
| `/log [n]` | the last MCP messages, requests and answers alike |
| `/trace`   | show or hide the live protocol trace              |
| `/tools`   | every tool available, grouped by server           |
| `/servers` | what is connected, and why anything is missing    |
| `/reset`   | forget the conversation and start fresh           |
| `/quit`    | leave. Ctrl-D does the same                       |

Ctrl-C interrupts the current turn without ending the session.

## The message log

Every JSON-RPC message that crosses to any server is written to `logs/mcp-session.log`, labelled with the server it belongs to and with its direction. The same lines appear in the terminal as a dim trace while the conversation runs, and `/log` prints the recent ones on demand.

The trace shows protocol messages only. A server's own logging, drained from its `stderr`, goes to the file so it does not appear twice on screen.

## The servers it loads

`config/servers.json` declares four servers. Values may carry `${VARIABLES}`, resolved from the environment plus `${REPO_ROOT}` and `${WORKSPACE}`.

| Key            | Transport | What it is                                                 |
| -------------- | --------- | ---------------------------------------------------------- |
| `filesystem`   | stdio     | the official Filesystem server, restricted to `workspace/` |
| `git`          | stdio     | the official Git server, launched inside `workspace/`      |
| `fraud-local`  | stdio     | this project's server as a subprocess                      |
| `fraud-remote` | HTTP      | the same server, deployed                                  |

The host opens one fraud transport, never both. Loading them together would publish the six tools twice under prefixed names, since they are the same server. Adding another MCP server is a matter of adding an entry, not of changing code.

The official servers are pointed at `workspace/`, a scratch directory outside version control, so nothing the chatbot does there can reach the repository. The Filesystem server enforces that root itself. The Git server cannot be confined, since its tools take a path on every call, so the host refuses any git call aimed outside the workspace.

## What to ask it

Three requests worth trying, in order.

**A general question.** The chatbot answers from the model's own knowledge when no tool applies, and it keeps the thread of the conversation.

> Who was Alan Turing?

> What date was he born?

**The investigation.** This is what the server was built for. It chains the six fraud tools, finds a purchase in Guatemala fourteen minutes after one in Minnesota at an impossible speed, and opens a case.

> Account 4471 has an alert. Review the last 7 days, freeze the card if there is fraud, and open the disputes that apply.

**Files and version control.** The official Git server has twelve tools and none of them creates a repository, so make one first.

```bash
git init workspace/demo
```

> In the demo repository, write a README describing this project, add it and make the first commit.

Add `--remote` to any of them and the same conversation runs against the deployed server, with the same tool names and the same answers.

## Before something changes

The host stops and asks for confirmation before a card is frozen or a dispute is opened. It shows the tool, the server and the arguments, and waits.

This is not the same control as the server's confirmation token. The token forces a deliberate two step call, but the model can perform both steps on its own, so it protects against an accident rather than against an autonomous decision. The prompt in the terminal is what puts a person in the loop. A refusal travels back to the model as a result, so it explains itself instead of going quiet.

## About the free tier

The current Gemini flash models allow **twenty requests a day, per model**, and one full investigation spends most of that. The default is a lite model whose quota supports development. For a demonstration, `--model gemini-3.5-flash` reasons better and twenty requests is plenty for one run.

Requests are spent by the conversation loop alone, once per round. Opening the servers, running tools and every slash command cost nothing. Asking the model for several independent tools in a single turn is therefore much cheaper than asking one at a time, which is why the system prompt asks for exactly that.
