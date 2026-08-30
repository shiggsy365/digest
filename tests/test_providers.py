from digest.providers import (
    GoogleBooksProvider,
    HardcoverProvider,
    IsbnDbProvider,
    available_providers,
)


class Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self.payload


class Client:
    def __init__(self, payload: dict):
        self.payload = payload
        self.request = None

    def post(self, url: str, **kwargs) -> Response:
        self.request = ("POST", url, kwargs)
        return Response(self.payload)

    def get(self, url: str, **kwargs) -> Response:
        self.request = ("GET", url, kwargs)
        return Response(self.payload)


def test_hardcover_result_is_mapped_to_candidate() -> None:
    client = Client(
        {
            "data": {
                "search": {
                    "results": {
                        "hits": [
                            {
                                "document": {
                                    "id": 42,
                                    "title": "The Book",
                                    "author_names": ["An Author"],
                                    "isbn_13": "9781234567890",
                                    "pages": 321,
                                    "image": {"url": "https://example.test/cover.jpg"},
                                }
                            }
                        ]
                    }
                }
            }
        }
    )

    result = HardcoverProvider(client, "secret").search("The Book", "An Author", [])

    assert result[0].source == "hardcover"
    assert result[0].authors == ["An Author"]
    assert result[0].isbns == ["9781234567890"]
    assert client.request[2]["headers"]["Authorization"] == "Bearer secret"


def test_hardcover_accepts_a_token_pasted_with_bearer_prefix() -> None:
    client = Client({"data": {"search": {"results": {"hits": []}}}})

    HardcoverProvider(client, "Bearer secret").search("Book", "Author", [])

    assert client.request[2]["headers"]["Authorization"] == "Bearer secret"


def test_hardcover_bibliography_pages_through_25_result_provider_cap() -> None:
    class PagingClient:
        def __init__(self):
            self.pages = []

        def post(self, url: str, **kwargs) -> Response:
            page = kwargs["json"]["variables"]["page"]
            self.pages.append(page)
            start = (page - 1) * 25
            hits = [
                {
                    "document": {
                        "id": index,
                        "title": f"Book {index}",
                        "author_names": ["J.B. Turner"],
                        "language": "eng",
                    }
                }
                for index in range(start, start + 25)
            ]
            return Response(
                {"data": {"search": {"results": {"hits": hits, "found": 100}}}}
            )

    client = PagingClient()

    results = HardcoverProvider(client, "secret").bibliography("JB Turner")

    assert len(results) == 100
    assert client.pages == [1, 2, 3, 4]


def test_google_books_prefers_the_largest_available_cover() -> None:
    client = Client(
        {
            "items": [
                {
                    "id": "volume-1",
                    "volumeInfo": {
                        "title": "The Book",
                        "authors": ["An Author"],
                        "imageLinks": {
                            "thumbnail": "http://example.test/cover.jpg?zoom=1&edge=curl",
                            "large": "http://example.test/large.jpg?zoom=1&edge=curl",
                        },
                    },
                }
            ]
        }
    )

    result = GoogleBooksProvider(client).search("The Book", "An Author", [])

    assert result[0].cover_url == "https://example.test/large.jpg?zoom=0"


def test_isbndb_exact_isbn_result_is_mapped_to_candidate() -> None:
    client = Client(
        {
            "book": {
                "title": "The Book",
                "authors": ["An Author"],
                "isbn": "1234567890",
                "isbn13": "9781234567890",
                "synopsis": "Description",
                "pages": 222,
            }
        }
    )

    result = IsbnDbProvider(client, "secret").search("Ignored", "Ignored", ["9781234567890"])

    assert result[0].source == "isbndb"
    assert result[0].description == "Description"
    assert client.request[1].endswith("/book/9781234567890")


def test_keyed_providers_are_disabled_until_configured() -> None:
    without_keys = available_providers({})
    with_keys = available_providers({"hardcover_api_key": "h", "isbndb_api_key": "i"})

    assert "hardcover" not in without_keys and "isbndb" not in without_keys
    assert "hardcover" in with_keys and "isbndb" in with_keys
