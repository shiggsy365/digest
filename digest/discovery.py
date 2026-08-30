import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import Book, DiscoveryItem, ReadingState, ReviewState, now
from .providers import (
    HardcoverProvider,
    OpenLibraryProvider,
    language_matches,
    normalise_author,
)

GENRES = {
    "fantasy": "Fantasy",
    "science_fiction": "Science Fiction",
    "mystery_and_detective_stories": "Mystery",
    "romance": "Romance",
    "thriller": "Thriller",
    "historical_fiction": "Historical Fiction",
}
DISCOVERY_MAX_AGE_YEARS = 20
HARDCOVER_TRENDING_PERIODS = {
    "now": ("Now", 30),
    "3m": ("Past 3 Months", 90),
    "12m": ("Past 12 Months", 365),
    "all": ("All Time", None),
}
HARDCOVER_FALLBACK_GENRES = [
    "Fantasy",
    "Science Fiction",
    "Romance",
    "Mystery",
    "Thriller",
    "Horror",
    "Historical Fiction",
    "Young Adult",
    "Biography",
    "History",
    "Nonfiction",
]
NYT_FALLBACK_LISTS = {
    "hardcover-fiction": "Hardcover Fiction",
    "hardcover-nonfiction": "Hardcover Nonfiction",
    "combined-print-and-e-book-fiction": "Combined Fiction",
    "combined-print-and-e-book-nonfiction": "Combined Nonfiction",
    "young-adult-hardcover": "Young Adult Hardcover",
    "childrens-middle-grade-hardcover": "Children Middle Grade",
}
HARDCOVER_BOOKS_QUERY = """
query DigestHardcoverBooks($where: books_bool_exp!, $order: [books_order_by!], $limit: Int!) {
  books(where: $where, order_by: $order, limit: $limit) {
    id title description release_date release_year users_count ratings_count cached_image
    image { url }
    contributions(limit: 5) { author { name } }
    default_ebook_edition { isbn_13 isbn_10 }
    default_physical_edition { isbn_13 isbn_10 }
    taggings(limit: 20) { tag { tag tag_category { category } } }
  }
}
"""
HARDCOVER_GENRES_QUERY = """
query DigestHardcoverGenres {
  books(order_by: [{users_count: desc}], limit: 250) {
    taggings(limit: 20) { tag { tag tag_category { category } } }
  }
}
"""


@dataclass
class DiscoveryResults:
    recommended: list[Book] = field(default_factory=list)
    recommendation_reasons: dict[str, str] = field(default_factory=dict)
    continue_series: list[Book] = field(default_factory=list)
    popular: list[Book] = field(default_factory=list)
    new_releases: list[Book] = field(default_factory=list)
    trending: list[DiscoveryItem] = field(default_factory=list)
    genre_items: list[DiscoveryItem] = field(default_factory=list)
    genres: dict[str, str] = field(default_factory=lambda: GENRES.copy())
    selected_genre: str = "fantasy"
    external_updated_at: object | None = None


def _ready_books(db: Session) -> list[Book]:
    return list(
        db.scalars(
            select(Book)
            .where(Book.review_state == ReviewState.READY)
            .order_by(Book.title)
        )
    )


def find_library_book(
    db: Session,
    *,
    title: str,
    author: str = "",
    isbn: str = "",
    include_review: bool = False,
) -> Book | None:
    """Find a conservative library match for a discovery result."""

    def isbn_key(value: object) -> str:
        return re.sub(r"[^0-9X]", "", str(value or "").upper())

    wanted_isbn = isbn_key(isbn)
    books = list(db.scalars(select(Book).order_by(Book.title))) if include_review else _ready_books(db)
    if wanted_isbn:
        for book in books:
            try:
                book_isbns = json.loads(book.isbns_json or "[]")
            except (TypeError, ValueError):
                book_isbns = []
            if wanted_isbn in {isbn_key(value) for value in book_isbns}:
                return book

    def text_key(value: object) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()

    wanted_title = text_key(title)
    wanted_author = text_key(author)
    if not wanted_title or not wanted_author:
        return None
    wanted_title_words = set(wanted_title.split())
    return next(
        (
            book
            for book in books
            if text_key(book.primary_author) == wanted_author
            and (
                text_key(book.title) == wanted_title
                or (
                    len(wanted_title_words) >= 2
                    and wanted_title_words.issubset(set(text_key(book.title).split()))
                )
            )
        ),
        None,
    )


def search_discovery_books(
    query: str,
    *,
    hardcover_api_key: str = "",
    language: str = "en",
    client: httpx.Client | None = None,
) -> list[dict]:
    query = query.strip()
    if not query:
        return []
    owns_client = client is None
    client = client or httpx.Client(
        timeout=15,
        follow_redirects=True,
        headers={"User-Agent": "Digest/0.1"},
    )
    try:
        provider = HardcoverProvider(client, hardcover_api_key) if hardcover_api_key else None
        candidates = provider.search(normalise_author(query), "", []) if provider else []
        qualified = [
            candidate
            for candidate in candidates
            if language_matches(candidate.language, language, allow_unknown=True)
        ]
        if not qualified:
            candidates = OpenLibraryProvider(client).search(normalise_author(query), "", [])
            qualified = [
                candidate
                for candidate in candidates
                if language_matches(candidate.language, language, allow_unknown=False)
            ]
    finally:
        if owns_client:
            client.close()
    return _discovery_candidates(qualified)


def author_bibliography(
    author: str,
    *,
    hardcover_api_key: str = "",
    language: str = "en",
    client: httpx.Client | None = None,
) -> list[dict]:
    author = author.strip()
    if not author:
        return []
    owns_client = client is None
    client = client or httpx.Client(
        timeout=15,
        follow_redirects=True,
        headers={"User-Agent": "Digest/0.1"},
    )
    try:
        provider = (
            HardcoverProvider(client, hardcover_api_key)
            if hardcover_api_key
            else OpenLibraryProvider(client)
        )
        candidates = provider.bibliography(normalise_author(author), language=language)
    finally:
        if owns_client:
            client.close()
    def author_key(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.casefold())

    wanted = author_key(author)
    candidates = [
        candidate
        for candidate in candidates
        if wanted
        in {author_key(value) for value in candidate.authors}
        and language_matches(
            candidate.language,
            language,
            allow_unknown=isinstance(provider, HardcoverProvider),
        )
    ]
    return _discovery_candidates(candidates)


def _discovery_candidates(candidates) -> list[dict]:
    results: list[dict] = []
    for candidate in candidates:
        published = candidate.publication_date or ""
        results.append(
            {
                "source": candidate.source,
                "source_id": candidate.source_id,
                "title": candidate.title,
                "authors": candidate.authors,
                "author": candidate.authors[0] if candidate.authors else "",
                "isbn": candidate.isbns[0] if candidate.isbns else "",
                "cover_url": candidate.cover_url or "",
                "description": candidate.description or "",
                "published_year": published,
                "genres": [],
            }
        )
    return results


def _popularity(states: list[ReadingState]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for state in states:
        score = 0.0
        if state.favourite:
            score += 4
        if state.rating:
            score += state.rating / 2
        if state.state == "finished":
            score += 2
        elif state.state == "reading":
            score += 1
        elif state.state == "abandoned":
            score -= 2
        scores[state.book_id] = scores.get(state.book_id, 0) + score
    return scores


def build_discovery(
    db: Session, user_id: int, limit: int = 12, genre: str = "fantasy"
) -> DiscoveryResults:
    books = _ready_books(db)
    states = list(db.scalars(select(ReadingState)))
    personal = {state.book_id: state for state in states if state.user_id == user_id}
    popularity = _popularity(states)

    positive_books = [
        book
        for book in books
        if (state := personal.get(book.id))
        and (state.favourite or (state.rating or 0) >= 4 or state.state == "finished")
    ]
    author_affinity: dict[str, float] = {}
    series_affinity: dict[str, float] = {}
    for book in positive_books:
        state = personal[book.id]
        weight = 2 + (2 if state.favourite else 0) + max((state.rating or 0) - 3, 0)
        author_affinity[book.primary_author.casefold()] = (
            author_affinity.get(book.primary_author.casefold(), 0) + weight
        )
        if book.series:
            series_affinity[book.series.casefold()] = (
                series_affinity.get(book.series.casefold(), 0) + weight + 1
            )

    candidates: list[tuple[float, str, Book]] = []
    for book in books:
        state = personal.get(book.id)
        if state and state.state in {"reading", "finished", "abandoned", "want-to-read"}:
            continue
        author_score = author_affinity.get(book.primary_author.casefold(), 0)
        series_score = series_affinity.get(book.series.casefold(), 0) if book.series else 0
        score = author_score + series_score * 1.25 + popularity.get(book.id, 0) * 0.2
        if score <= 0:
            continue
        if series_score >= author_score and book.series:
            reason = f"More from {book.series}"
        else:
            reason = f"Because you enjoy {book.primary_author}"
        candidates.append((score, reason, book))
    candidates.sort(key=lambda item: (-item[0], item[2].title.casefold()))

    interacted_series = {
        book.series.casefold()
        for book in books
        if book.series
        and (state := personal.get(book.id))
        and state.state in {"reading", "finished"}
    }
    series_books = [
        book
        for book in books
        if book.series
        and book.series.casefold() in interacted_series
        and (
            book.id not in personal
            or personal[book.id].state in {"unread", "want-to-read"}
        )
    ]
    series_books.sort(
        key=lambda book: (
            book.series.casefold() if book.series else "",
            book.series_number is None,
            book.series_number or 0,
            book.title.casefold(),
        )
    )

    popular = sorted(
        books,
        key=lambda book: (
            -popularity.get(book.id, 0),
            book.title.casefold(),
        ),
    )
    popular = [book for book in popular if popularity.get(book.id, 0) > 0]
    new_releases = sorted(
        (book for book in books if book.publication_date),
        key=lambda book: (book.publication_date or "", book.title.casefold()),
        reverse=True,
    )

    recommended = [item[2] for item in candidates[:limit]]
    selected_genre = genre if genre in GENRES else "fantasy"
    oldest_year = str(datetime.now(UTC).year - DISCOVERY_MAX_AGE_YEARS)
    trending = list(
        db.scalars(
            select(DiscoveryItem)
            .where(
                DiscoveryItem.kind == "trending",
                DiscoveryItem.publication_date >= oldest_year,
            )
            .order_by(DiscoveryItem.rank)
            .limit(limit)
        )
    )
    genre_items = list(
        db.scalars(
            select(DiscoveryItem)
            .where(
                DiscoveryItem.kind == "genre",
                DiscoveryItem.category == selected_genre,
                DiscoveryItem.publication_date >= oldest_year,
            )
            .order_by(DiscoveryItem.rank)
            .limit(limit)
        )
    )
    updated = max(
        (item.fetched_at for item in [*trending, *genre_items]),
        default=None,
    )
    return DiscoveryResults(
        recommended=recommended,
        recommendation_reasons={item[2].id: item[1] for item in candidates[:limit]},
        continue_series=series_books[:limit],
        popular=popular[:limit],
        new_releases=new_releases[:limit],
        trending=trending,
        genre_items=genre_items,
        selected_genre=selected_genre,
        external_updated_at=updated,
    )


def _openlibrary_item(data: dict, kind: str, category: str, rank: int) -> DiscoveryItem:
    source_id = str(data.get("key") or "").strip()
    title = str(data.get("title") or "").strip()
    if not source_id or not title:
        raise ValueError("Open Library discovery item has no work ID or title")
    authors = data.get("author_name")
    if not isinstance(authors, list):
        authors = [item.get("name", "") for item in data.get("authors", [])]
    cover_id = data.get("cover_i") or data.get("cover_id")
    year = data.get("first_publish_year") or data.get("first_publish_date")
    match = re.match(r"\d{4}", str(year or ""))
    if not match or int(match.group()) < datetime.now(UTC).year - DISCOVERY_MAX_AGE_YEARS:
        raise ValueError("Open Library discovery item is outside the publication window")
    return DiscoveryItem(
        provider="openlibrary",
        kind=kind,
        category=category,
        source_id=source_id,
        title=title,
        authors_json=json.dumps([str(item) for item in authors if item]),
        publication_date=str(year) if year else None,
        cover_url=f"https://covers.openlibrary.org/b/id/{cover_id}-L.jpg" if cover_id else None,
        source_url=f"https://openlibrary.org{source_id}",
        rank=rank,
        fetched_at=now(),
    )


def refresh_openlibrary_discovery(
    db: Session, client: httpx.Client | None = None, limit: int = 24
) -> int:
    owns_client = client is None
    client = client or httpx.Client(
        timeout=20,
        follow_redirects=True,
        headers={"User-Agent": "Digest/0.1 (self-hosted ebook discovery)"},
    )
    try:
        response = client.get(
            "https://openlibrary.org/trending/daily.json", params={"limit": limit}
        )
        response.raise_for_status()
        items = []
        for rank, item in enumerate(response.json().get("works", []), start=1):
            try:
                items.append(_openlibrary_item(item, "trending", "", rank))
            except ValueError:
                continue
        for slug in GENRES:
            response = client.get(
                f"https://openlibrary.org/subjects/{slug}.json", params={"limit": limit}
            )
            response.raise_for_status()
            for rank, item in enumerate(response.json().get("works", []), start=1):
                try:
                    items.append(_openlibrary_item(item, "genre", slug, rank))
                except ValueError:
                    continue
        if not items:
            raise ValueError("Open Library returned no discovery items")
        db.execute(delete(DiscoveryItem).where(DiscoveryItem.provider == "openlibrary"))
        db.add_all(items)
        db.commit()
        return len(items)
    finally:
        if owns_client:
            client.close()


def _hardcover_headers(api_key: str) -> dict[str, str]:
    token = re.sub(r"^Bearer\s+", "", api_key, flags=re.IGNORECASE).strip()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Digest/0.1",
    }


def _hardcover_graphql(
    api_key: str,
    query: str,
    variables: dict | None = None,
    client: httpx.Client | None = None,
) -> dict:
    owns_client = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=False)
    try:
        response = client.post(
            "https://api.hardcover.app/v1/graphql",
            headers=_hardcover_headers(api_key),
            json={"query": query, "variables": variables or {}},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errors"):
            raise ValueError(payload["errors"][0].get("message", "Hardcover GraphQL error"))
        return payload.get("data") or {}
    finally:
        if owns_client:
            client.close()


def _hardcover_genres(book: dict) -> list[str]:
    values: list[str] = []
    for tagging in book.get("taggings") or []:
        tag = tagging.get("tag") or {}
        if (tag.get("tag_category") or {}).get("category") != "Genre":
            continue
        name = str(tag.get("tag") or "").strip()
        if name and name.casefold() not in {value.casefold() for value in values}:
            values.append(name)
    return values


def normalize_hardcover_books(books: list[dict]) -> list[dict]:
    results: list[dict] = []
    seen: set[str] = set()
    for book in books:
        authors = [
            str((item.get("author") or {}).get("name") or "").strip()
            for item in book.get("contributions") or []
        ]
        authors = [item for item in authors if item]
        isbn = ""
        for edition_name in ("default_ebook_edition", "default_physical_edition"):
            edition = book.get(edition_name) or {}
            isbn = edition.get("isbn_13") or edition.get("isbn_10") or isbn
        title = str(book.get("title") or "").strip()
        identity = str(book.get("id") or isbn or f"{title}|{authors[:1]}")
        if not title or identity in seen:
            continue
        image = book.get("cached_image") or book.get("image") or {}
        cover_url = image.get("url", "") if isinstance(image, dict) else str(image or "")
        results.append(
            {
                "source": "hardcover",
                "source_id": str(book.get("id") or ""),
                "title": title,
                "authors": authors,
                "author": authors[0] if authors else "",
                "isbn": isbn,
                "cover_url": cover_url,
                "description": str(book.get("description") or ""),
                "published_year": book.get("release_year")
                or str(book.get("release_date") or "")[:4],
                "genres": _hardcover_genres(book),
                "users_count": book.get("users_count") or 0,
                "ratings_count": book.get("ratings_count") or 0,
            }
        )
        seen.add(identity)
    return results[:40]


def hardcover_where(days: int | None = None) -> dict:
    today = datetime.now(UTC).date()
    oldest = date(today.year - DISCOVERY_MAX_AGE_YEARS, today.month, today.day)
    release_date = {"_lte": today.isoformat(), "_gte": oldest.isoformat()}
    if days:
        release_date["_gte"] = (today - timedelta(days=days)).isoformat()
    return {"release_date": release_date}


def hardcover_books(
    api_key: str,
    *,
    days: int | None,
    genre: str,
    new_releases: bool = False,
    client: httpx.Client | None = None,
) -> list[dict]:
    where = hardcover_where(days)
    if genre:
        where["taggings"] = {
            "tag": {
                "tag": {"_eq": genre},
                "tag_category": {"category": {"_eq": "Genre"}},
            }
        }
    order = (
        [{"release_date": "desc"}, {"users_count": "desc"}]
        if new_releases
        else [{"users_count": "desc"}, {"ratings_count": "desc"}]
    )
    data = _hardcover_graphql(
        api_key,
        HARDCOVER_BOOKS_QUERY,
        {"where": where, "order": order, "limit": 40},
        client,
    )
    return normalize_hardcover_books(data.get("books") or [])


def hardcover_genres(api_key: str, client: httpx.Client | None = None) -> list[str]:
    data = _hardcover_graphql(api_key, HARDCOVER_GENRES_QUERY, client=client)
    counts: dict[str, int] = {}
    for book in data.get("books") or []:
        for genre in _hardcover_genres(book):
            counts[genre] = counts.get(genre, 0) + 1
    return [
        genre
        for genre, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))
    ][:50] or HARDCOVER_FALLBACK_GENRES


def normalize_nyt_books(books: list[dict]) -> list[dict]:
    results: list[dict] = []
    seen: set[str] = set()
    for book in books:
        isbn = book.get("primary_isbn13") or book.get("primary_isbn10") or ""
        title = str(book.get("title") or "").strip()
        author = str(book.get("author") or "").strip()
        identity = str(isbn or f"{title.casefold()}|{author.casefold()}")
        if not title or identity in seen:
            continue
        results.append(
            {
                "source": "nytimes",
                "source_id": isbn or title,
                "title": title,
                "authors": [author] if author else [],
                "author": author,
                "isbn": isbn,
                "cover_url": book.get("book_image") or "",
                "description": book.get("description") or "",
                "published_year": "",
                "rank": book.get("rank"),
                "weeks_on_list": book.get("weeks_on_list"),
            }
        )
        seen.add(identity)
    return sorted(results, key=lambda item: item.get("rank") or 9999)


def nyt_weekly_lists(api_key: str, client: httpx.Client | None = None) -> list[dict]:
    owns_client = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=False)
    try:
        response = client.get(
            "https://api.nytimes.com/svc/books/v3/lists/names.json",
            params={"api-key": api_key},
            headers={"User-Agent": "Digest/0.1"},
        )
        response.raise_for_status()
        weekly = []
        for item in response.json().get("results", []):
            newest = item.get("newest_published_date") or ""
            slug = item.get("list_name_encoded") or ""
            if item.get("updated") == "WEEKLY" and newest and slug:
                weekly.append(
                    {
                        "slug": slug,
                        "title": item.get("display_name") or item.get("list_name") or slug,
                        "newest_published_date": newest,
                        "oldest_published_date": item.get("oldest_published_date") or "",
                    }
                )
        if not weekly:
            return []
        latest = max(date.fromisoformat(item["newest_published_date"]) for item in weekly)
        active_since = latest - timedelta(days=14)
        return [
            item
            for item in weekly
            if date.fromisoformat(item["newest_published_date"]) >= active_since
        ]
    finally:
        if owns_client:
            client.close()


def nyt_weeks(list_info: dict) -> list[dict]:
    if list_info.get("newest_published_date"):
        newest = date.fromisoformat(list_info["newest_published_date"])
        oldest = date.fromisoformat(list_info.get("oldest_published_date") or newest.isoformat())
    else:
        today = datetime.now(UTC).date()
        newest = today - timedelta(days=(today.weekday() + 1) % 7)
        oldest = newest - timedelta(days=7 * 25)
    weeks = []
    current = newest
    while current >= oldest and len(weeks) < 26:
        weeks.append({"date": current.isoformat(), "title": current.strftime("%d %b %Y")})
        current -= timedelta(days=7)
    return weeks


def nyt_bestsellers(
    api_key: str,
    slug: str,
    published_date: str,
    client: httpx.Client | None = None,
) -> list[dict]:
    owns_client = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=False)
    try:
        response = client.get(
            f"https://api.nytimes.com/svc/books/v3/lists/{published_date}/{slug}.json",
            params={"api-key": api_key},
            headers={"User-Agent": "Digest/0.1"},
        )
        response.raise_for_status()
        results = response.json().get("results") or {}
        books = results.get("books", []) if isinstance(results, dict) else []
        return normalize_nyt_books(books)
    finally:
        if owns_client:
            client.close()
