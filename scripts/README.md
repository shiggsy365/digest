# Scripts

## Deploy the Latest VPS Release

From the repository root:

```bash
./scripts/deploy-vps-latest.sh
```

The script finds the latest GitHub release for `shiggsy365/digest`, SSHes to the
VPS, updates `/opt/docker/apps/digest/.env`, pulls the new image, recreates the
Digest migration/web/worker services, and checks `/healthz`.

Deploy a specific version:

```bash
./scripts/deploy-vps-latest.sh --version 1.0.26
```

Useful overrides:

```bash
HOST=ubuntu@example.com REMOTE_DIR=/opt/docker ./scripts/deploy-vps-latest.sh
```
