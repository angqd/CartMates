# CartMates

A household Telegram bot that keeps a shared grocery queue and orders it from Swiggy Instamart.
Design and milestones: [docs/DESIGN.md](docs/DESIGN.md).

## Run locally

    python -m venv .venv && .venv/bin/pip install -r requirements.txt
    cp .env.example .env
    .venv/bin/python -m cartmates

## Tracing (Phoenix)

`docker compose up -d` also starts [Arize Phoenix](https://github.com/Arize-ai/phoenix), which receives the bot's
OpenTelemetry traces. Open http://localhost:6007 and pick the **cartmates** project.

- Each Telegram message or button tap is one trace: the agent turn, each LLM round (prompt, tool calls, tokens),
  each tool call, each Swiggy call, the order steps and the Telegram replies. Each chat is a session.
- `/resolve <id>` shows the trace ids for an order.
- Traces stay on this machine, in the `phoenix-data` Docker volume.
- The bot token, Swiggy tokens and login URLs are never recorded.

Settings in `.env`:

| Variable | Meaning |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://phoenix:6006` in Docker; unset turns tracing off |
| `CARTMATES_TRACE_CONTENT` | `0` leaves prompts, messages and results out of traces (default on) |
| `CARTMATES_TRACE_MAX_CHARS` | longest text kept per attribute (default 8000) |

## Tests

    .venv/bin/python -m pytest -q

## Design doc site

`docs/site/index.html` is a single static page that renders `docs/DESIGN.md` as an interactive read-only view. It has no build step.

Preview on localhost:

    python3 -m http.server 8000 --bind 127.0.0.1 --directory docs/site

Then open http://localhost:8000. Stop it with `Ctrl+C`.

Put it online:

1. Go to https://app.netlify.com/drop (or any static host such as Cloudflare Pages).
2. Drag the `docs/site` folder onto the page.
3. Netlify returns a public URL. Re-drop the folder after any change to the page.

Keep the page in sync with `docs/DESIGN.md`: the page is a hand-written copy, so edit both when the design changes. GitHub Pages can only serve the repo root or `/docs`, so it can't serve `docs/site` directly without a deploy workflow.
