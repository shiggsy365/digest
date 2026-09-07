# Digest KOReader Plugin

This plugin connects Digest to KOReader without opening the Digest website for normal reading flows. Library and Discover open through KOReader's OPDS browser, which also lets the Bookshelf plugin show them with its shelf/catalog view.

It also adds support for document links:

- `digest://library`
- `digest://discover`

## Install

Copy `digest.koplugin` to KOReader's `plugins` directory, then restart KOReader.

## Configure

Open `Tools` -> `More tools` -> `Digest`.

Set both:

- `Digest server URL`, for example `https://digest.example`
- `Digest username`, for example `admin`
- `Digest API token`, for example `dgt_...`

Create the token in Digest's token/admin screen and paste it into KOReader. For the OPDS browser, KOReader uses your Digest username with the API token as the password. For the plugin's JSON fallback screens, it uses `Authorization: Bearer ...`. It does not use a browser session or trusted-device login link.

## Bookshelf View

Open `Tools` -> `More tools` -> `Digest` -> `Install OPDS catalogs`. This writes these catalogs into KOReader's OPDS settings:

- `Digest Library` at `/opds`, a hub with Title, Author, Latest, Series, and Discover folders
- `Digest Library - By Title` at `/opds/catalog/title`
- `Digest Library - By Author` at `/opds/catalog/authors-v2`
- `Digest Library - Latest` at `/opds/catalog/latest`
- `Digest Library - By Series` at `/opds/catalog/series`
- `Digest Discover - Search` at `/opds/discover/search`
- `Digest Discover - Trending` at `/opds/discover/trending`
- `Digest Discover - NYT Bestsellers` at `/opds/discover/nyt-bestsellers`
- `Digest Discover - New Releases` at `/opds/discover/new-releases`

Bookshelf v4 can use KOReader's OPDS catalogs as catalog chips, so these entries should be available there after installation.

## Native Fallback Screens

The plugin still includes native menu fallback screens for Library, Discover, search, book detail, description, request-download, and `Add to eReader`. `Add to eReader` downloads the selected book file from Digest into KOReader's download folder and opens it.
