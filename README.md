# Digest

Digest is a self-hosted ebook library and discovery service designed for modern
browsers, Kindle browsers, Kobo browsers, OPDS clients, and Kobo device sync.

It scans an existing ebook library, groups duplicate formats, extracts embedded
metadata, downloads improved metadata and covers, and organises approved books
into canonical author/title folders. The modern interface provides library,
author, series, shelf, reading-state, discovery, download, and metadata-review
views; a separate low-complexity interface supports older e-readers.

## Highlights

- EPUB, KEPUB, MOBI, and AZW3 library scanning and duplicate grouping
- Hardcover, Google Books, Open Library, ISBNdb, and NYT metadata/discovery
- Manual and guarded automatic metadata matching with custom cover uploads
- Personal and shared shelves, including a live **All Books** shelf
- Kobo private-device sync, reading progress, collections, and covers
- Authenticated OPDS catalogue and Send-to-Kindle delivery
- Optional acquisition through Shelfmark or Prowlarr plus SABnzbd
- Responsive modern UI and dedicated Kindle/Kobo browser templates
- PostgreSQL persistence, Alembic migrations, durable background jobs, and
  Traefik-compatible HTTPS routing

## Documentation

- [INSTALL.md](INSTALL.md) — Docker Compose installation, environment variables,
  upgrades, backups, and release images
- [SETUP.md](SETUP.md) — first-run Digest configuration and Shelfmark, Prowlarr,
  and SABnzbd integration

## Container image

Published releases are available from `ghcr.io/shiggsy365/digest`. Pin a numbered
version for predictable deployments or use `latest` to follow stable releases.

## Licence

No licence has yet been selected. All rights are reserved until a licence file is
added to the repository.
