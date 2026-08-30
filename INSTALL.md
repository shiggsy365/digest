# Installing Digest

## Requirements

- Docker Engine with the Compose plugin
- A writable directory containing your ebook library
- A Linux host with enough space for PostgreSQL, covers, and downloads
- Optional: Traefik and a DNS hostname for public HTTPS access

## 1. Download the release files

Download `compose.yaml` and `.env.example` from the latest GitHub release, then
place them in a dedicated directory such as `/opt/docker/apps/digest`.

```sh
mkdir -p /opt/docker/apps/digest
cd /opt/docker/apps/digest
cp .env.example .env
```

The Compose file pulls the prebuilt `ghcr.io/shiggsy365/digest` image. Building the
application on the deployment host is not required.

## 2. Configure `.env`

At minimum, replace these values:

```dotenv
DIGEST_SECRET_KEY=generate-a-random-value-of-at-least-32-characters
DIGEST_DB_PASSWORD=generate-a-separate-long-random-password
DIGEST_LIBRARY_PATH=/mnt/books/library
PUID=1000
PGID=1000
TZ=Europe/London
```

The UID and GID must have read/write access to the library and download folders.
Generate secrets with `openssl rand -hex 32`.

For private loopback access:

```dotenv
DIGEST_BIND_ADDRESS=127.0.0.1
DIGEST_PORT=8000
DIGEST_PUBLIC_URL=http://127.0.0.1:8000
DIGEST_TRAEFIK_ENABLED=false
```

For Traefik HTTPS access:

```dotenv
DIGEST_PUBLIC_URL=https://digest.example.com
DIGEST_TRAEFIK_ENABLED=true
DIGEST_HOSTNAME=digest.example.com
DOCKER_NETWORK=proxy
DOCKER_NETWORK_EXTERNAL=true
```

Create the external Docker network first if it does not already exist. Digest's
port can remain bound to `127.0.0.1`; Traefik reaches it over the Docker network.

## 3. Start Digest

```sh
docker compose pull
docker compose up -d
docker compose ps
docker compose logs --tail=100 digest-migrate digest-web digest-worker
```

The one-shot migration service must finish successfully before the web and
worker services start. Open `/setup` and create the first administrator.

## Upgrading

Pin `DIGEST_VERSION` in `.env` when predictable upgrades are preferred:

```dotenv
DIGEST_VERSION=1.0.0
```

Then upgrade with:

```sh
docker compose pull digest-migrate digest-web digest-worker
docker compose up -d --force-recreate digest-migrate digest-web digest-worker
```

Database migrations run automatically. Back up before upgrading.

## Backup and restore

Back up both the library directory and PostgreSQL data. Create a database dump:

```sh
docker compose exec -T digest-database pg_dump -U digest -d digest -Fc > digest.dump
```

Restore into a prepared database with:

```sh
docker compose stop digest-web digest-worker
docker compose exec -T digest-database pg_restore -U digest -d digest --clean --if-exists < digest.dump
docker compose up -d digest-web digest-worker
```

Do not run `docker compose down -v` unless all Digest database and application
volume data may be discarded.
