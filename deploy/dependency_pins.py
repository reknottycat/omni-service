"""Reviewed upstream identities; changing these requires rebuilding and testing bundles."""

TD_REV = "1b9efe84d6e9a2e2f6eadeeea32b0726d2b8c25d"
TD_SHA = "e7d319970e62bfb48dbccc0a6b0d9a14361a0df04866332f5b6e1db1ca1a28c2"
TD_VERSION = "1.8.71"
TD_SOURCE_URL = "https://codeload.github.com/tdlib/td/tar.gz/" + TD_REV
EXPORTS = ("td_create_client_id", "td_send", "td_receive", "td_execute")
