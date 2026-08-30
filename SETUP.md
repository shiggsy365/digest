# Configuring Digest and acquisition services

## Digest first-run setup

1. Open `https://your-digest-host/setup` and create the first administrator.
2. Open **Administration → Metadata and SMTP**.
3. Choose the default metadata language and provider order.
4. Add the API keys you use. Hardcover tokens may be entered with or without the
   `Bearer` prefix.
5. Run a library scan and review uncertain books under **Metadata review**.
6. Create personal or shared shelves. **All Books** is always available and can
   be selected as the automatic Kobo sync shelf.

For Kobo device sync, select an automatic sync shelf under **Settings**, generate
the private Kobo endpoint, and place it in `.kobo/Kobo/Kobo eReader.conf` as the
`api_endpoint` value under `[OneStoreServices]`. Treat the endpoint as a password.

## Shelfmark

Shelfmark provides direct-download searches. In `.env`, configure its library
mount and optional provider mirrors. Start it with the Compose stack, then open
the loopback port through an SSH tunnel if its own UI needs configuration:

```sh
ssh -L 8084:127.0.0.1:8084 user@your-server
```

In Digest administration, enable Shelfmark and use the internal URL:

```text
http://digest-shelfmark:8084
```

Shelfmark's `/books` and `/downloads` mounts must point at the same host library
directory mounted as `/library` in Digest.

## Prowlarr

Prowlarr searches configured Usenet indexers. Access its UI through a tunnel:

```sh
ssh -L 9696:127.0.0.1:9696 user@your-server
```

Add legal ebook-capable Usenet indexers and copy the API key from **Settings →
General**. In Digest administration, enable Usenet and configure:

```text
Prowlarr URL: http://digest-prowlarr:9696
Prowlarr API key: your API key
```

Digest requests category `7020`, accepts Usenet results only, and rejects torrent
and magnet releases.

## SABnzbd

SABnzbd downloads NZBs selected from Prowlarr. Access its UI through a tunnel:

```sh
ssh -L 8080:127.0.0.1:8080 user@your-server
```

1. Add a Usenet server and test the connection.
2. Create a category such as `books`.
3. Set its completed folder to `/downloads`.
4. Ensure `/downloads` maps to the same host library directory used by Digest.
5. Add the Digest container/network address to SABnzbd's host whitelist when
   SABnzbd rejects API calls by hostname.
6. Copy the API key from **Config → General**.

Configure Digest with:

```text
SABnzbd URL: http://digest-sabnzbd:8080
SABnzbd API key: your API key
SABnzbd category: books
```

## Testing acquisition

Search Discover for a book not already in the library and request it. The
Downloads page searches enabled providers. Unambiguous high-confidence EPUB or
KEPUB releases may be selected automatically; other matches above 80% remain for
manual selection. Digest monitors the provider, scans the completed file,
organises it, and changes the request to **Available**.

If nothing appears, inspect:

```sh
docker compose logs --tail=200 digest-worker digest-shelfmark digest-prowlarr digest-sabnzbd
```

Confirm that internal URLs resolve from `digest-worker`, API keys are correct,
Prowlarr has a working ebook indexer, SABnzbd uses the shared completed folder,
and every service is attached to the same Docker network.
