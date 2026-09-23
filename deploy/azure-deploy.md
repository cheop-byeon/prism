# Deploying Prism to Azure (free-trial VM)

This is the corrected version of this guide, written after actually doing this once and hitting most of the ways it can go wrong. The biggest change from the first attempt: **it no longer migrates data from a local instance.** Migrating a full local Postgres + Weaviate database onto a fresh VM works, but it drags in a long tail of environment-specific problems (file ownership from a different OS, baked-in Postgres passwords, plugin records that reference plugins the new instance never installed) that aren't worth it. This guide gets you a correctly-built, empty Dify instance, and the last step is re-running the IETF import through the UI to populate it — slower than a migration, but every problem you hit from there on is a normal "using the app" problem, not an infrastructure archaeology problem.

Two scripts do the mechanical work — `deploy/deploy-azure` (run locally) and `deploy/vm-setup` (run on the VM, in phases, pausing for a few actions that must happen on your laptop). This doc is the connective tissue between them.

## 1. Prerequisites, once

- Azure CLI installed locally (`az --version`).
- An SSH key pair for the VM. If `ls ~/.ssh/id_ed25519_azure.pub` doesn't exist: `ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_azure`.
- Confirm you're on the **Free Trial** subscription (Azure portal → Subscriptions).

## 2. Provision the VM

Edit the placeholders in `deploy/deploy-azure` (`RESOURCE_GROUP`, `VM_NAME`, `ADMIN_USER`, `SSH_KEY_PATH`), then:

```bash
deploy/deploy-azure
```

A few things this script now handles that the first attempt didn't:

- **Region**: defaults to `westeurope`, not a smaller region like `norwayeast` — trial subscriptions frequently can't get D-series capacity outside Azure's largest regions (`SkuNotAvailable` on the first real attempt). If you want to check a region before committing: `az vm list-skus --location <region> --size Standard_D4 --all --output table` (genuinely slow, 30-60s+, that's normal).
- **A 128GB data disk, attached at creation time.** The default Azure Ubuntu OS disk is ~30GB, which is nowhere near enough once you add Docker images, the ~8GB embedding model (plus Ollama's own copy of it), and Postgres/Weaviate data. Running out of disk mid-build produces confusing "no space left on device" errors in places that have nothing obviously to do with disk space (a Docker layer export, a `uv sync` inside a build, Ollama's model conversion step) — this disk exists specifically so you don't have to debug that.

The script prints the VM's public IP and the SSH command at the end.

## 3. Run `vm-setup` on the VM

Copy it over and run it:

```bash
scp -i ~/.ssh/id_ed25519_azure deploy/vm-setup <admin-user>@<vm-ip>:~/
ssh -i ~/.ssh/id_ed25519_azure <admin-user>@<vm-ip>
chmod +x vm-setup
./vm-setup
```

It runs in phases and pauses at a few points for you to do something on your **laptop** (add a GitHub deploy key, `scp` two files over). Follow what it prints. Phases, and why each exists:

1. **Mount the data disk.** Deliberately interactive — it shows you `lsblk` and asks you to type the device name yourself, rather than guessing. Attaching a disk at VM-creation time vs. after can number it differently (we saw it come up as `sdb` both ways in practice, but don't trust that), and formatting the wrong device is destructive.
2. **Redirect Docker's *and* containerd's storage to the data disk.** These are two separate settings most people don't know are separate. `daemon.json`'s `data-root` only covers classic Docker image/container storage; `docker compose build` goes through BuildKit, which uses containerd's own storage root (`/etc/containerd/config.toml`), configured independently. Missing either one means the OS disk fills up again even though you thought you'd fixed it. This phase runs before anything is pulled or built, so there's no existing data to migrate — it just points both at `/mnt/data` from the start and verifies it with `docker info`.
3. **GitHub deploy key** for this VM (separate from your personal SSH key — this one only needs read access to the one repo).
4. **Clone the repo onto `/mnt/data`, not `$HOME`.** A third, separate storage gap from phase 2's: `docker-compose.yaml` bind-mounts every stateful service (Postgres, Weaviate, downloaded IETF mail/RFC archives, uploaded files) as host paths under `docker/volumes/`, relative to wherever the repo itself is checked out — not as named Docker volumes, so phase 2's data-root redirect does nothing for them. Cloning onto the data disk instead of `$HOME` puts all of that on it for free, with no compose-file changes needed.
5. **Copy over `docker-compose.override.yaml`.** This file is gitignored on purpose (a local dev convenience) — a fresh clone never has it, so it has to come from your laptop. The script also patches the copy's `web` service: your local override builds `web` from a pre-built `.next/standalone` folder (fast for local dev, since you build natively with `pnpm`), but nothing on the VM has that pre-built output. The VM needs the *other* Dockerfile — `web/Dockerfile`, not `web/Dockerfile.local-standalone` — which builds fully from source, with the build context set to `web/` itself (not the repo root, which is what that Dockerfile expects).
6. **Write a root `.dockerignore`.** There wasn't one. Without it, building `web` (context = repo root for some builds) tries to tar up the entire repo as build context, including `docker/volumes/` — Postgres/Weaviate data, and certbot's Let's Encrypt private keys, which are permission-locked and make the build fail outright.
7. **Install Ollama**, configured to listen on all interfaces (`OLLAMA_HOST=0.0.0.0`), not just `127.0.0.1` — Ollama's default only accepts connections from localhost, but a container reaching it via `host.docker.internal` arrives on the Docker bridge interface, not `127.0.0.1`, and gets silently connection-refused otherwise. Model storage points at the data disk.
8. **Register the embedding model** by writing a real `Modelfile` to disk and pointing `ollama create` at that file — piping the Modelfile via stdin (`ollama create name -f -`) is what the project's own local dev script does and works fine there, but was unreliable against a freshly-installed Ollama on this VM and failed with a misleading "no Modelfile or safetensors files found" error. The Modelfile also sets `PARAMETER num_thread` to the VM's vCPU count — Ollama has no working service-level env var for this (`OLLAMA_NUM_THREADS` looks like a real setting but isn't one; see [ollama/ollama#9784](https://github.com/ollama/ollama/issues/9784)) and otherwise leaves real CPU capacity idle during indexing, which showed up live as indexing pegging only ~2 of 4 vCPUs.
9. **`.env` with generated secrets** (`SECRET_KEY`, `DB_PASSWORD`, `REDIS_PASSWORD`, matching `CELERY_BROKER_URL`). Leave `CONSOLE_API_URL` / `APP_API_URL` / `APP_WEB_URL` / `FILES_URL` blank — nginx fronts everything on one origin, and blank (relative URLs) is the correct default for that, not a placeholder waiting to be filled in with the VM's IP.
10. **`host.docker.internal` routing** added to `api`, `worker`, *and* `plugin_daemon` — not just `api`/`worker`. Model-provider credential validation (e.g. testing the Ollama connection in the console) is dispatched through `plugin_daemon`, and it needs the same routing fix or that specific check fails even though `api` itself is fine.

## 4. Build and start — the part that stays manual

The script stops before this on purpose — builds take real time and are worth watching:

```bash
cd /mnt/data/dify/docker
docker compose -f docker-compose.yaml -f docker-compose.override.yaml build api worker worker_beat web
docker compose -f docker-compose.yaml -f docker-compose.override.yaml up -d
```

**The one rule that matters from here on: always pass both `-f` files together**, for any command that touches `api`/`worker`/`worker_beat`/`web` — including commands scoped to a *different* service, like `nginx`, because Compose also resolves that service's dependencies using whichever files you gave it. Using `-f docker-compose.yaml` alone for an `nginx`-only command will silently recreate `api`/`web` on the **stock** upstream images (which don't have this fork's custom Alembic migration and crash-loop) as a side effect of resolving `nginx`'s `depends_on`.

If you only want to touch one service and *don't* want Compose to also touch its dependencies, add `--no-deps`:

```bash
docker compose -f docker-compose.yaml -f docker-compose.override.yaml restart --no-deps nginx
```

**After any restart of `api`/`worker`/`web`, restart `nginx` too.** nginx resolves and caches its upstream container's IP; a plain `docker compose up -d nginx` with no config change is a no-op and won't re-resolve, so it keeps trying to reach the old IP and 502s. A real `restart` is what forces re-resolution.

## 5. First-run setup

Open `http://<vm-ip>/install`, create the admin account, then in the console:

**Settings → Model Provider → Ollama** (install the plugin first if prompted, same as any marketplace plugin):
- `base_url`: `http://host.docker.internal:11434`
- model: `rfc-dralign-q8`, type Text Embedding, 4096 dims

**Settings → Model Provider → OpenRouter**: install, add your API key. If you need `llama-3.3-70b-instruct` and it's not in the predefined list, add it as a **custom model** with the real OpenRouter ID: `meta-llama/llama-3.3-70b-instruct`.

**Settings → Model Provider → System Model Settings**: make sure the default **Embedding Model** is actually set to `rfc-dralign-q8`, not left on whatever it defaulted to — a dataset can silently fall back to a different embedding model at indexing time if this isn't set explicitly.

If any plugin's credential validation errors with something like `PluginDaemonInternalServerError: no available node, plugin runtime not found`, that plugin's install didn't actually complete — check the **Plugins** page (puzzle-piece icon), uninstall it there, and reinstall fresh.

## 6. Repopulate the data

This is the step that replaces the old migration section. In the console, use the **"Import from IETF or GitHub"** option in the dataset creation wizard (your fork's custom sidecar UI) to re-run the mailarchive/RFC import against this fresh instance, and re-publish the "Decision → Discussion" workflow from Studio. Slower than restoring a backup, but everything that goes wrong along the way is a normal Dify usage problem, not a cross-machine migration problem.

## 7. Domain + HTTPS

Free and takes about 10 minutes total.

1. **DuckDNS**: sign in at duckdns.org (GitHub/Google login, no new account), complete the reCAPTCHA gate first (the domain-registration field is hidden behind it), pick a subdomain, and set its IP to **the VM's public IP** (it defaults to auto-detecting *your laptop's* IP — overwrite it manually).
2. **Get the certificate** (`docker-compose.yaml` already has an nginx + certbot setup for this):
   ```bash
   cd /mnt/data/dify/docker
   sed -i "s|^CERTBOT_EMAIL=.*|CERTBOT_EMAIL=<your email>|" .env
   sed -i "s|^CERTBOT_DOMAIN=.*|CERTBOT_DOMAIN=<yourname>.duckdns.org|" .env
   sed -i "s|^NGINX_SERVER_NAME=.*|NGINX_SERVER_NAME=<yourname>.duckdns.org|" .env
   sed -i "s|^NGINX_ENABLE_CERTBOT_CHALLENGE=.*|NGINX_ENABLE_CERTBOT_CHALLENGE=true|" .env

   docker compose -f docker-compose.yaml -f docker-compose.override.yaml up -d --no-deps nginx
   # db_postgres has its own profile — include it explicitly whenever you pass --profile at all,
   # or its profile-based auto-inclusion (via other services' depends_on) breaks and the project
   # fails to validate.
   docker compose -f docker-compose.yaml --profile certbot --profile postgresql up -d certbot
   # the certbot image is Alpine-based with no bash, but update-cert.sh's shebang assumes #!/bin/bash —
   # run it explicitly under sh rather than relying on the shebang:
   docker compose -f docker-compose.yaml --profile certbot --profile postgresql exec certbot sh /update-cert.sh
   ```
3. **Enable HTTPS.** Let's Encrypt's actual output filenames (`fullchain.pem`, `privkey.pem`) don't match this compose file's defaults (`dify.crt`/`dify.key`, meant for a manually-supplied cert) — override them:
   ```bash
   sed -i "s|^NGINX_HTTPS_ENABLED=.*|NGINX_HTTPS_ENABLED=true|" .env
   sed -i "s|^NGINX_SSL_CERT_FILENAME=.*|NGINX_SSL_CERT_FILENAME=fullchain.pem|" .env
   sed -i "s|^NGINX_SSL_CERT_KEY_FILENAME=.*|NGINX_SSL_CERT_KEY_FILENAME=privkey.pem|" .env
   docker compose -f docker-compose.yaml -f docker-compose.override.yaml restart --no-deps nginx
   ```
4. Visit `https://<yourname>.duckdns.org/apps` — real padlock, no warnings.

## 8. If a dataset import fails with a filesystem permission error

`opendal.exceptions.PermissionDenied` writing to `upload_files/...` means `docker/volumes/app/storage/` is owned by the wrong user — the container runs as `dify` (uid 1001), and this directory needs to match:
```bash
sudo chown -R 1001:1001 volumes/app/storage
```
This specific issue was a leftover from the old migration-based approach; following this guide from a fresh VM shouldn't hit it, but it's worth knowing the fix if it ever shows up (e.g. if you do end up copying files over by hand for some other reason).

## 9. Cost control — do this regardless

- Pause billing when idle: `az vm deallocate --resource-group <RESOURCE_GROUP> --name <VM_NAME>`
- Resume: `az vm start --resource-group <RESOURCE_GROUP> --name <VM_NAME>`
- **Set a reminder for day ~28** of the trial: `az group delete --name <RESOURCE_GROUP> --yes` — deletes everything before the $200 credit runs out and billing switches to your card.
