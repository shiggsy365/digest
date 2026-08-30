import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from starlette.requests import Request

from digest.db import Base
from digest.discovery import (
    GENRES,
    author_bibliography,
    build_discovery,
    find_library_book,
    hardcover_where,
    nyt_weekly_lists,
    nyt_weeks,
    refresh_openlibrary_discovery,
    search_discovery_books,
)
from digest.main import (
    api_bestseller_weeks,
    api_hardcover_new_releases,
    api_hardcover_trending,
    book_detail,
    discover,
    discovery_author,
    discovery_book_detail,
)
from digest.models import AppSetting, Book, DiscoveryItem, ReadingState, ReviewState, Role, User
from digest.security import hash_password


def test_discovery_uses_personal_taste_series_progress_and_household_activity() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        reader = User(username="reader", password_hash=hash_password("long-password-1"))
        friend = User(username="friend", password_hash=hash_password("long-password-2"))
        liked = Book(
            title="First",
            primary_author="Good Author",
            series="A Series",
            series_number=1,
            publication_date="2024-01-01",
            review_state=ReviewState.READY,
        )
        sequel = Book(
            title="Second",
            primary_author="Good Author",
            series="A Series",
            series_number=2,
            publication_date="2025-01-01",
            review_state=ReviewState.READY,
        )
        related = Book(
            title="Standalone",
            primary_author="Good Author",
            publication_date="2026-01-01",
            review_state=ReviewState.READY,
        )
        popular = Book(
            title="Household Favourite",
            primary_author="Other Author",
            review_state=ReviewState.READY,
        )
        db.add_all([reader, friend, liked, sequel, related, popular])
        db.flush()
        db.add_all(
            [
                ReadingState(
                    user_id=reader.id,
                    book_id=liked.id,
                    state="finished",
                    rating=5,
                    favourite=True,
                ),
                ReadingState(
                    user_id=friend.id,
                    book_id=popular.id,
                    state="finished",
                    rating=5,
                    favourite=True,
                ),
            ]
        )
        db.commit()

        results = build_discovery(db, reader.id)

        assert results.recommended[0] == sequel
        assert results.recommendation_reasons[sequel.id] == "More from A Series"
        assert sequel in results.continue_series
        assert results.popular[0] == liked
        assert results.new_releases[0] == related


def test_discovery_book_matches_library_by_isbn_then_title_and_author() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        isbn_match = Book(
            title="A Different Edition Title",
            primary_author="An Author",
            isbns_json='["978-1-234-56789-7"]',
            review_state=ReviewState.READY,
        )
        text_match = Book(
            title="The Bee Speaker",
            primary_author="Jane Writer",
            review_state=ReviewState.READY,
        )
        db.add_all([isbn_match, text_match])
        db.commit()

        assert (
            find_library_book(
                db, title="Unrelated", author="Nobody", isbn="9781234567897"
            )
            == isbn_match
        )
        assert (
            find_library_book(db, title="The Bee-Speaker", author="JANE WRITER")
            == text_match
        )
        assert find_library_book(db, title="The Bee Speaker", author="Someone Else") is None


def test_discovery_book_detail_shows_available_or_request_actions() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(
            username="reader",
            password_hash=hash_password("a-long-test-password"),
            role=Role.USER,
            kindle_email="reader@kindle.com",
        )
        book = Book(
            title="Found Book",
            primary_author="Known Author",
            isbns_json='["9781234567897"]',
            review_state=ReviewState.READY,
        )
        db.add_all([user, book])
        db.commit()
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/discover/book",
                "headers": [],
                "query_string": b"",
                "session": {"user_id": user.id},
            }
        )

        available = discovery_book_detail(
            request,
            db,
            source="hardcover",
            title="Another Provider Title",
            author="Known Author",
            isbn="9781234567897",
            description="A useful description.",
        ).body.decode()
        missing = discovery_book_detail(
            request,
            db,
            source="nytimes",
            title="Missing Book",
            author="Other Author",
        ).body.decode()

        assert "Available" in available
        assert "Send EPUB to Kindle" in available
        assert f"/books/{book.id}" in available
        assert "A useful description." in available
        assert "Request book" in missing
        assert "Request download" in missing
        assert "Send EPUB to Kindle" not in missing


def test_discovery_search_uses_openlibrary_without_age_filtering() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "octavia butler"
        return httpx.Response(
            200,
            json={
                "docs": [
                    {
                        "key": "/works/recent",
                        "title": "Recent Book",
                        "author_name": ["Octavia Butler"],
                        "isbn": ["9781234567897"],
                        "language": ["eng"],
                        "first_publish_year": 2024,
                        "cover_i": 42,
                    },
                    {
                        "key": "/works/old",
                        "title": "Old Book",
                        "author_name": ["Old Author"],
                        "language": ["eng"],
                        "first_publish_year": 1800,
                    },
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = search_discovery_books("octavia butler", client=client)

    assert [item["title"] for item in results] == ["Recent Book", "Old Book"]
    assert results[0]["source"] == "openlibrary"
    assert results[0]["isbn"] == "9781234567897"


def test_discovery_search_collapses_punctuation_and_uses_default_language() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "JB Turner"
        return httpx.Response(
            200,
            json={
                "docs": [
                    {
                        "key": "/works/english",
                        "title": "English Book",
                        "author_name": ["J.B. Turner"],
                        "language": ["eng"],
                    },
                    {
                        "key": "/works/french",
                        "title": "French Book",
                        "author_name": ["J. B. Turner"],
                        "language": ["fre"],
                    },
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = search_discovery_books("J. B. Turner", language="en", client=client)

    assert [item["title"] for item in results] == ["English Book"]


def test_discovery_search_accepts_unknown_hardcover_language() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.hardcover.app":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "search": {
                            "results": {
                                "hits": [
                                    {
                                        "document": {
                                            "id": 1,
                                            "title": "The Castle",
                                            "author_names": ["Franz Kafka"],
                                        }
                                    },
                                    {
                                        "document": {
                                            "id": 2,
                                            "title": "Le Château",
                                            "author_names": ["Franz Kafka"],
                                            "language": "fr",
                                        }
                                    },
                                ]
                            }
                        }
                    }
                },
            )
        raise AssertionError("Open Library should not be called for unknown-language Hardcover hits")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = search_discovery_books(
            "the castle",
            hardcover_api_key="secret",
            language="en",
            client=client,
        )

    assert [item["title"] for item in results] == ["The Castle"]
    assert results[0]["source"] == "hardcover"


def test_author_bibliography_uses_exact_author_without_age_filtering() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["author"] == "Ursula Le Guin"
        assert "q" not in request.url.params
        return httpx.Response(
            200,
            json={
                "docs": [
                    {
                        "key": "/works/earthsea",
                        "title": "A Wizard of Earthsea",
                        "author_name": ["Ursula Le Guin"],
                        "language": ["eng"],
                        "first_publish_year": 1968,
                    },
                    {
                        "key": "/works/other",
                        "title": "Another Book",
                        "author_name": ["Another Author"],
                        "language": ["eng"],
                        "first_publish_year": 2024,
                    },
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = author_bibliography("Ursula Le Guin", client=client)

    assert [item["title"] for item in results] == ["A Wizard of Earthsea"]
    assert results[0]["published_year"] == "1968"


def test_author_bibliography_returns_up_to_100_and_ignores_initial_punctuation() -> None:
    def document(index: int, author: str, language: str = "eng") -> dict:
        return {
            "key": f"/works/{index}",
            "title": f"Book {index}",
            "author_name": [author],
            "language": [language],
            "first_publish_year": 2000,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["author"] == "JB Turner"
        assert request.url.params["language"] == "eng"
        page = int(request.url.params["page"])
        assert page == 1
        docs = [document(index, "J. B. Turner") for index in range(100)]
        return httpx.Response(200, json={"docs": docs, "num_found": 102})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = author_bibliography("J. B. Turner", language="en", client=client)

    assert len(results) == 100
    assert results[-1]["title"] == "Book 99"


def test_hardcover_author_bibliography_keeps_results_without_language() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "search": {
                        "results": {
                            "found": 2,
                            "hits": [
                                {
                                    "document": {
                                        "id": 1,
                                        "title": "Children of Time",
                                        "author_names": ["Adrian Tchaikovsky"],
                                    }
                                },
                                {
                                    "document": {
                                        "id": 2,
                                        "title": "Unrelated",
                                        "author_names": ["Another Author"],
                                    }
                                },
                            ],
                        }
                    }
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = author_bibliography(
            "Adrian Tchaikovsky", hardcover_api_key="secret", language="en", client=client
        )

    assert [item["title"] for item in results] == ["Children of Time"]


def test_author_page_marks_library_books_available(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(
            username="reader",
            password_hash=hash_password("a-long-test-password"),
            role=Role.USER,
        )
        book = Book(
            title="Known Work",
            primary_author="Known Author",
            review_state=ReviewState.READY,
        )
        db.add_all([user, book])
        db.commit()
        monkeypatch.setattr(
            "digest.main.author_bibliography",
            lambda author, **kwargs: [
                {
                    "source": "openlibrary",
                    "source_id": "/works/known",
                    "title": "Known Work",
                    "authors": [author],
                    "author": author,
                    "isbn": "",
                    "cover_url": "https://example.test/cover.jpg",
                    "description": "",
                    "published_year": "1960",
                    "genres": [],
                }
            ],
        )
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/discover/author",
                "headers": [],
                "query_string": b"author=Known+Author",
                "session": {"user_id": user.id},
            }
        )

        html = discovery_author(request, db, author="Known Author").body.decode()

        assert "More by Known Author" in html
        assert "Available" in html
        assert "/discover/book?" in html


def test_discover_route_renders_ereader_view_and_preserves_section_navigation() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(
            username="reader",
            password_hash=hash_password("a-long-test-password"),
            role=Role.USER,
        )
        first = Book(
            title="First",
            primary_author="Author",
            publication_date="2025",
            review_state=ReviewState.READY,
        )
        second = Book(
            title="Second",
            primary_author="Author",
            publication_date="2026",
            review_state=ReviewState.READY,
        )
        db.add_all([user, first, second])
        db.commit()
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/discover",
                "headers": [(b"user-agent", b"Kobo Libra Colour")],
                "query_string": b"",
                "session": {"user_id": user.id},
            }
        )

        html = discover(request, db).body.decode()

        assert "Discover" in html and "New Releases" in html
        assert "navigation=/discover%3Fview%3Dnew" in html

        detail_request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": f"/books/{second.id}",
                "headers": [],
                "query_string": b"",
                "session": {"user_id": user.id},
            }
        )
        detail = book_detail(
            second.id,
            detail_request,
            db,
            return_to="/discover",
            navigation="/discover?view=new",
        ).body.decode()
        assert "Next book" in detail and first.id in detail


def test_openlibrary_refresh_replaces_cache_only_after_all_feeds_succeed() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    def success(request: httpx.Request) -> httpx.Response:
        if "/trending/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "works": [
                        {
                            "key": "/works/TRENDING",
                            "title": "Trending Book",
                            "author_name": ["Trend Author"],
                            "cover_i": 12,
                            "first_publish_year": 2026,
                        }
                    ]
                },
            )
        slug = request.url.path.removeprefix("/subjects/").removesuffix(".json")
        return httpx.Response(
            200,
            json={
                "works": [
                    {
                        "key": f"/works/{slug}",
                        "title": f"{slug} book",
                        "authors": [{"name": "Genre Author"}],
                        "cover_id": 34,
                        "first_publish_year": 2025,
                    }
                ]
            },
        )

    with Session(engine, expire_on_commit=False) as db:
        client = httpx.Client(transport=httpx.MockTransport(success))
        assert refresh_openlibrary_discovery(db, client) == len(GENRES) + 1
        assert db.query(DiscoveryItem).count() == len(GENRES) + 1
        trending = db.query(DiscoveryItem).filter_by(kind="trending").one()
        assert trending.title == "Trending Book"
        assert trending.cover_url.endswith("/12-L.jpg")

        failing = httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(503))
        )
        with pytest.raises(httpx.HTTPStatusError):
            refresh_openlibrary_discovery(db, failing)

        assert db.query(DiscoveryItem).count() == len(GENRES) + 1


def test_bookstack_hardcover_date_rules_and_routes_are_preserved(monkeypatch) -> None:
    assert set(hardcover_where()) == {"release_date"}
    assert set(hardcover_where()["release_date"]) == {"_lte", "_gte"}
    assert hardcover_where()["release_date"]["_gte"].startswith("2006-")
    assert set(hardcover_where(90)["release_date"]) == {"_lte", "_gte"}

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="reader", password_hash=hash_password("a-long-password"))
        db.add_all(
            [user, AppSetting(key="hardcover_api_key", value="Bearer secret", secret=True)]
        )
        db.commit()
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/discovery/hardcover-trending",
                "headers": [],
                "query_string": b"",
                "session": {"user_id": user.id},
            }
        )
        calls = []
        monkeypatch.setattr(
            "digest.main.hardcover_books",
            lambda api_key, **kwargs: calls.append((api_key, kwargs)) or [],
        )

        result = api_hardcover_trending(request, db, period="3m", genre="Fantasy")
        assert result == {"title": "Trending - Past 3 Months - Fantasy", "books": []}
        assert calls[-1] == ("Bearer secret", {"days": 90, "genre": "Fantasy"})

        api_hardcover_new_releases(request, db, genre="Mystery")
        assert calls[-1] == (
            "Bearer secret",
            {"days": 120, "genre": "Mystery", "new_releases": True},
        )
        with pytest.raises(HTTPException) as exc:
            api_hardcover_trending(request, db, period="weekly")
        assert exc.value.status_code == 400


def test_configured_hardcover_replaces_openlibrary_as_default_trending(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="reader", password_hash=hash_password("a-long-password"))
        db.add_all(
            [
                user,
                AppSetting(key="hardcover_api_key", value="secret", secret=True),
                DiscoveryItem(
                    provider="openlibrary",
                    kind="trending",
                    category="",
                    source_id="/works/OLD",
                    title="Open Library Result",
                    authors_json="[]",
                    source_url="https://openlibrary.org/works/OLD",
                    rank=1,
                ),
            ]
        )
        db.commit()
        monkeypatch.setattr(
            "digest.main.hardcover_books",
            lambda *args, **kwargs: [
                {
                    "source": "hardcover",
                    "source_id": "1",
                    "title": "Hardcover Result",
                    "authors": ["Author"],
                    "cover_url": "",
                    "published_year": 2026,
                    "rank": None,
                }
            ],
        )
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/discover",
                "headers": [],
                "query_string": b"",
                "session": {"user_id": user.id},
            }
        )

        html = discover(request, db).body.decode()

        assert "Trending - Now" in html and "Hardcover Result" in html
        assert "Open Library Result" not in html


def test_bookstack_nyt_active_list_and_week_history_rules(monkeypatch) -> None:
    response = {
        "results": [
            {
                "list_name_encoded": "active",
                "display_name": "Active",
                "updated": "WEEKLY",
                "newest_published_date": "2026-08-23",
                "oldest_published_date": "2020-01-01",
            },
            {
                "list_name_encoded": "recent",
                "display_name": "Recent",
                "updated": "WEEKLY",
                "newest_published_date": "2026-08-09",
                "oldest_published_date": "2020-01-01",
            },
            {
                "list_name_encoded": "stale",
                "display_name": "Stale",
                "updated": "WEEKLY",
                "newest_published_date": "2026-08-02",
            },
            {
                "list_name_encoded": "monthly",
                "display_name": "Monthly",
                "updated": "MONTHLY",
                "newest_published_date": "2026-08-23",
            },
        ]
    }
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    )
    lists = nyt_weekly_lists("key", client)
    assert [item["slug"] for item in lists] == ["active", "recent"]
    assert len(nyt_weeks(lists[0])) == 26

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(username="reader", password_hash=hash_password("a-long-password"))
        db.add_all([user, AppSetting(key="nytimes_api_key", value="secret", secret=True)])
        db.commit()
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/discovery/bestseller-weeks",
                "headers": [],
                "query_string": b"",
                "session": {"user_id": user.id},
            }
        )
        monkeypatch.setattr("digest.main.nyt_weekly_lists", lambda api_key: lists)
        result = api_bestseller_weeks(request, db, slug="active")
        assert result["title"] == "Active" and len(result["weeks"]) == 26
