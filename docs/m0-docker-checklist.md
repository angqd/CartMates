# M0 Docker checklist

You write `Dockerfile`, `.dockerignore` and `compose.yml` at the repo root. Claude reviews.

What the app expects:
- **Start command:** `python -m cartmates`
- **Dependencies:** `requirements.txt`
- **Data folder:** read from `DATA_DIR`, which must be `/data` inside the container.
- **Logs:** `cartmates ready` on boot, then `cartmates stopped` on a clean exit after SIGTERM.

## 0. Install Docker
`brew install --cask docker`, then open Docker Desktop once so the engine starts.
Check it with `docker run --rm hello-world`.

## 1. `Dockerfile`
1. **Base image:** `python:3.14-slim`. Debian with Python and little else, so the image stays small.
   Pin the version; never use `latest`.
2. **Environment variables:**
   - `PYTHONUNBUFFERED=1`: logs show up in `docker logs` straight away instead of sitting in a buffer.
   - `PYTHONDONTWRITEBYTECODE=1`: no `.pyc` clutter.
3. **`WORKDIR /app`:** where the code lives inside the image.
4. **Dependencies before code:** copy only `requirements.txt`, then `pip install --no-cache-dir -r requirements.txt`.
   Each instruction becomes a cached layer, so installing dependencies first means a code edit doesn't
   reinstall them.
5. **Copy only `cartmates/`.** Not the tests, `legacy/` or `instamart_poc/`.
6. **Set `DATA_DIR=/data`.**
7. **`CMD ["python", "-m", "cartmates"]` in exec form (the JSON array).** Shell form (`CMD python -m cartmates`) runs the
   app under `/bin/sh`, which doesn't pass SIGTERM on. `docker stop` then waits 10 s and kills the app, and the
   clean shutdown never runs.
8. *Stretch:* run as a non-root user (`useradd` + `USER`). It's a good habit, but it can cause permission problems
   with `./data` on Linux later. Skip it if it gets in the way.

## 2. `.dockerignore`
List what the build must never see: `.venv`, `.git`, `data/`, `legacy/`, `instamart_poc/`, `__pycache__`, `docs/`,
`tests/`, and above all **`.env`**. Secrets must never be baked into an image; they are passed in when the container starts.

## 3. `compose.yml`
One service, `cartmates`, with:
- `build: .`
- `env_file: .env`: secrets are passed in when the container starts and are never stored in the image.
- `volumes: ["./data:/data"]`: a bind mount, so the DB file lives on your Mac and survives the container being deleted.
- `restart: unless-stopped`: the container comes back after a crash or a reboot. This matters on the VPS in v2.
- **No `ports:`.** The bot polls Telegram outbound, so nothing needs to connect in.

Before the first run: `cp .env.example .env`. Leave `DATA_DIR` commented out (the reason is in `.env.example`).

## 4. Commands to learn
`docker compose build` · `docker compose up` (Ctrl-C to stop) · `docker compose up -d` (background) ·
`docker compose logs -f` · `docker compose ps` · `docker compose stop` · `docker compose down` ·
`docker compose exec cartmates sh` (a shell inside the container) · `docker image ls`

## 5. Done when
1. `docker compose up` logs `cartmates ready`.
2. `data/cartmates.db` appears on your Mac. Check with `sqlite3 data/cartmates.db ".tables"`.
3. `time docker compose stop` takes under ~1 s and logs `cartmates stopped`. About 10 s means your CMD is in shell form.
4. After `docker compose down` then `docker compose up`, the logs show no migrations applied. That proves the DB persisted.
5. `docker image ls` shows the image under ~200 MB.

## Questions to be able to answer (interview material)
- Why copy `requirements.txt` before the code?
- What's the difference between exec form and shell form, and why does it matter for `docker stop`?
- Bind mount or named volume: what's the difference, and when do you use each?
- Why does this service need no `ports:`?
- Why must `.env` be in `.dockerignore`, and how do secrets reach the container instead?
