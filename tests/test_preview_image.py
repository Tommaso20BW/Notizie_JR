import unittest

from preview_image import PreviewImageResolver, normalize_image_url


class FakeResponse:
    def __init__(self, text, url="https://example.com/news"):
        self.text = text
        self.url = url
        self.headers = {"Content-Type": "text/html; charset=utf-8"}

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def get(self, url, timeout):
        self.calls += 1
        return self.response


class PreviewImageTests(unittest.TestCase):
    def test_open_graph_image_is_resolved(self):
        session = FakeSession(
            FakeResponse('<meta property="og:image" content="/images/cover.jpg">')
        )
        resolver = PreviewImageResolver(session)

        image_url = resolver.resolve("https://example.com/news")

        self.assertEqual(image_url, "https://example.com/images/cover.jpg")
        self.assertEqual(session.calls, 1)

    def test_direct_image_does_not_download_page(self):
        session = FakeSession(FakeResponse(""))
        resolver = PreviewImageResolver(session)

        image_url = resolver.resolve(
            "https://example.com/news",
            "https://cdn.example.com/photo.jpg",
        )

        self.assertEqual(image_url, "https://cdn.example.com/photo.jpg")
        self.assertEqual(session.calls, 0)

    def test_invalid_schemes_are_rejected(self):
        self.assertEqual(normalize_image_url("javascript:alert(1)"), "")


if __name__ == "__main__":
    unittest.main()
