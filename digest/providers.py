import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import httpx


@dataclass
class Candidate:
    source: str
    source_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    isbns: list[str] = field(default_factory=list)
    language: str | list[str] | None = None
    description: str | None = None
    publication_date: str | None = None
    page_count: int | None = None
    cover_url: str | None = None
    series: str | None = None
    series_number: float | None = None
    confidence: float = 0


def normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def normalise_author(value: str) -> str:
    tokens = re.sub(r"[^A-Za-z0-9]+", " ", value).strip().split()
    result: list[str] = []
    initials = ""
    for token in tokens:
        if len(token) == 1 and token.isalpha():
            initials += token
            continue
        if initials:
            result.append(initials)
            initials = ""
        result.append(token)
    if initials:
        result.append(initials)
    return " ".join(result)


def language_matches(value: object, language: str, *, allow_unknown: bool = True) -> bool:
    if not value:
        return allow_unknown
    aliases = {
        "en": {"en", "eng", "english"},
        "fr": {"fr", "fre", "fra", "french"},
        "de": {"de", "deu", "ger", "german"},
        "es": {"es", "spa", "spanish"},
        "it": {"it", "ita", "italian"},
    }
    wanted = language.casefold().split("-", 1)[0]
    accepted = aliases.get(wanted, {wanted})
    values = value if isinstance(value, list) else [value]
    return any(str(item).casefold().split("-", 1)[0] in accepted for item in values)


def openlibrary_language_code(language: str) -> str:
    return {
        "en": "eng",
        "fr": "fre",
        "de": "ger",
        "es": "spa",
        "it": "ita",
    }.get(language.casefold().split("-", 1)[0], language.casefold().split("-", 1)[0])


def score(candidate: Candidate, title: str, author: str, isbns: list[str]) -> float:
    wanted_isbns = {re.sub(r"\D", "", item) for item in isbns}
    found_isbns = {re.sub(r"\D", "", item) for item in candidate.isbns}
    if wanted_isbns & found_isbns:
        return 1.0
    title_score = SequenceMatcher(None, normalise(title), normalise(candidate.title)).ratio()
    candidate_author = candidate.authors[0] if candidate.authors else ""
    author_score = SequenceMatcher(
        None,
        normalise_author(author).casefold(),
        normalise_author(candidate_author).casefold(),
    ).ratio()
    return round(title_score * 0.7 + author_score * 0.3, 4)


class MetadataProvider:
    name = "base"

    def __init__(self, client: httpx.Client, api_key: str | None = None):
        self.client = client
        self.api_key = api_key

    def search(self, title: str, author: str, isbns: list[str]) -> list[Candidate]:
        raise NotImplementedError


class OpenLibraryProvider(MetadataProvider):
    name = "openlibrary"

    def search(self, title: str, author: str, isbns: list[str]) -> list[Candidate]:
        params = {
            "limit": 10,
            "fields": "key,title,author_name,isbn,language,first_publish_year,cover_i,number_of_pages_median",
        }
        if isbns:
            params["q"] = f"isbn:{isbns[0]}"
        elif title and not author:
            params["q"] = title
        else:
            params.update({"title": title, "author": author})
        data = self.client.get("https://openlibrary.org/search.json", params=params).json()
        return self._candidates(data.get("docs", []))

    def bibliography(
        self, author: str, limit: int = 100, language: str = ""
    ) -> list[Candidate]:
        fields = (
            "key,title,author_name,isbn,language,first_publish_year,cover_i,"
            "number_of_pages_median"
        )
        page = 1
        results: list[Candidate] = []
        seen: set[str] = set()
        while True:
            params = {
                "author": author,
                "limit": min(limit, 100),
                "page": page,
                "fields": fields,
            }
            if language:
                params["language"] = openlibrary_language_code(language)
            response = self.client.get(
                "https://openlibrary.org/search.json",
                params=params,
            )
            response.raise_for_status()
            data = response.json()
            docs = data.get("docs", [])
            for candidate in self._candidates(docs):
                identity = candidate.source_id or f"{candidate.title}|{candidate.authors}"
                if identity not in seen:
                    results.append(candidate)
                    seen.add(identity)
                    if len(results) >= limit:
                        return results
            page_size = min(limit, 100)
            if (
                not docs
                or len(docs) < page_size
                or page * page_size >= int(data.get("num_found", 0))
            ):
                break
            page += 1
        return results

    def _candidates(self, items: list[dict]) -> list[Candidate]:
        results = []
        for item in items:
            cover = item.get("cover_i")
            results.append(
                Candidate(
                    source=self.name,
                    source_id=item.get("key", ""),
                    title=item.get("title", ""),
                    authors=item.get("author_name", []),
                    isbns=item.get("isbn", []),
                    language=item.get("language") or None,
                    publication_date=str(item.get("first_publish_year", "")) or None,
                    page_count=item.get("number_of_pages_median"),
                    cover_url=f"https://covers.openlibrary.org/b/id/{cover}-L.jpg"
                    if cover
                    else None,
                )
            )
        return results


class GoogleBooksProvider(MetadataProvider):
    name = "google_books"

    def search(self, title: str, author: str, isbns: list[str]) -> list[Candidate]:
        query = f"isbn:{isbns[0]}" if isbns else f'intitle:"{title}" inauthor:"{author}"'
        params = {"q": query, "maxResults": 10}
        if self.api_key:
            params["key"] = self.api_key
        data = self.client.get("https://www.googleapis.com/books/v1/volumes", params=params).json()
        results = []
        for item in data.get("items", []):
            info = item.get("volumeInfo", {})
            ids = [value.get("identifier", "") for value in info.get("industryIdentifiers", [])]
            image_links = info.get("imageLinks", {})
            cover = next(
                (
                    image_links.get(size)
                    for size in ("extraLarge", "large", "medium", "small", "thumbnail")
                    if image_links.get(size)
                ),
                None,
            )
            if cover:
                cover = cover.replace("http://", "https://").replace("&edge=curl", "")
                cover = re.sub(r"([?&])zoom=1(?:&|$)", r"\1zoom=0&", cover).rstrip("&?")
            results.append(
                Candidate(
                    source=self.name,
                    source_id=item.get("id", ""),
                    title=info.get("title", ""),
                    authors=info.get("authors", []),
                    isbns=ids,
                    language=info.get("language"),
                    description=info.get("description"),
                    publication_date=info.get("publishedDate"),
                    page_count=info.get("pageCount"),
                    cover_url=cover,
                )
            )
        return results


class HardcoverProvider(MetadataProvider):
    name = "hardcover"

    def search(self, title: str, author: str, isbns: list[str]) -> list[Candidate]:
        query = isbns[0] if isbns else f"{title} {author}"
        return self._search(query, per_page=10, page=1)[0]

    def bibliography(
        self, author: str, limit: int = 100, language: str = ""
    ) -> list[Candidate]:
        page = 1
        results: list[Candidate] = []
        seen: set[str] = set()
        while True:
            # Hardcover currently caps search result pages at 25 even when a
            # larger per_page value is requested.
            candidates, found = self._search(author, per_page=25, page=page)
            for candidate in candidates:
                identity = candidate.source_id or f"{candidate.title}|{candidate.authors}"
                if identity not in seen:
                    results.append(candidate)
                    seen.add(identity)
                    if len(results) >= limit:
                        return results
            if not candidates or (found and page * 25 >= found):
                break
            page += 1
        return results

    def _search(self, query: str, *, per_page: int, page: int) -> tuple[list[Candidate], int]:
        token = re.sub(r"^Bearer\s+", "", self.api_key or "", flags=re.IGNORECASE).strip()
        response = self.client.post(
            "https://api.hardcover.app/v1/graphql",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "query": """query Search($query: String!, $perPage: Int!, $page: Int!) {
                  search(query: $query, query_type: "Book", per_page: $perPage, page: $page) {
                    results
                  }
                }""",
                "variables": {"query": query, "perPage": per_page, "page": page},
            },
        )
        response.raise_for_status()
        payload = response.json().get("data", {}).get("search", {}).get("results", {})
        hits = payload.get("hits", payload if isinstance(payload, list) else [])
        results = []
        for hit in hits:
            item = hit.get("document", hit)
            results.append(
                Candidate(
                    source=self.name,
                    source_id=str(item.get("id", "")),
                    title=item.get("title", ""),
                    authors=[
                        value.get("name", "") if isinstance(value, dict) else str(value)
                        for value in item.get("author_names", item.get("authors", []))
                    ],
                    isbns=[value for value in (item.get("isbn_10"), item.get("isbn_13")) if value],
                    language=item.get("language") or item.get("language_code"),
                    description=item.get("description"),
                    publication_date=item.get("release_date"),
                    page_count=item.get("pages"),
                    cover_url=item.get("image", {}).get("url")
                    if isinstance(item.get("image"), dict)
                    else item.get("image"),
                    series=item.get("series_name"),
                    series_number=item.get("series_position"),
                )
            )
        found = int(payload.get("found", len(hits))) if isinstance(payload, dict) else len(hits)
        return results, found


class IsbnDbProvider(MetadataProvider):
    name = "isbndb"

    def search(self, title: str, author: str, isbns: list[str]) -> list[Candidate]:
        endpoint = (
            f"https://api2.isbndb.com/book/{isbns[0]}"
            if isbns
            else f"https://api2.isbndb.com/books/{title}"
        )
        response = self.client.get(
            endpoint,
            params=None if isbns else {"pageSize": 10, "author": author},
            headers={"Authorization": self.api_key or ""},
        )
        response.raise_for_status()
        payload = response.json()
        books = [payload["book"]] if payload.get("book") else payload.get("books", [])
        return [
            Candidate(
                source=self.name,
                source_id=str(item.get("isbn13") or item.get("isbn") or ""),
                title=item.get("title", ""),
                authors=item.get("authors", []),
                isbns=[value for value in (item.get("isbn"), item.get("isbn13")) if value],
                language=item.get("language"),
                description=item.get("synopsis"),
                publication_date=item.get("date_published"),
                page_count=item.get("pages"),
                cover_url=item.get("image"),
            )
            for item in books
        ]


def available_providers(settings: dict[str, str]) -> dict[str, MetadataProvider]:
    client = httpx.Client(timeout=15, follow_redirects=True, headers={"User-Agent": "Digest/0.1"})
    providers: dict[str, MetadataProvider] = {
        "google_books": GoogleBooksProvider(client, settings.get("google_books_api_key")),
        "openlibrary": OpenLibraryProvider(client),
    }
    if settings.get("hardcover_api_key"):
        providers["hardcover"] = HardcoverProvider(client, settings["hardcover_api_key"])
    if settings.get("isbndb_api_key"):
        providers["isbndb"] = IsbnDbProvider(client, settings["isbndb_api_key"])
    return providers
