"""The network helpers the agoda module needs: the Accept-Language its
sessions send and the country its searches are made from. Requests go out
from your own connection (or your AGODA_PROXY), so every session announces
the plain English list a desktop Chrome in the US sends, and every search
is made as a visitor from the US, like the hosted API's."""

ACCEPT_LANGUAGE = "en-US,en;q=0.9"
DIRECT_COUNTRY = "us"


def site_accept_language(site=None):
    return ACCEPT_LANGUAGE


def accept_language(proxy=None, country=None):
    return ACCEPT_LANGUAGE


def exit_country(proxy=None):
    """The country a proxy exit is pinned to. Not read from proxy URLs here,
    so searches use DIRECT_COUNTRY."""
    return None
