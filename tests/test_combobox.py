"""Country aliases for a closed combobox menu."""

from autofill.combobox import country_aliases, country_from_phone, match_country_option
from autofill.models import Option


def test_phone_country_rows_match_the_alias_and_the_calling_code():
    options = [
        Option(value="1", label="Canada +1", selector="#ca"),
        Option(value="1", label="United States +1", selector="#us"),
        Option(value="44", label="United Kingdom +44", selector="#uk"),
    ]
    for word in ("United States", "USA", "us", "+1"):
        match = match_country_option(word, options)
        assert match is not None
        assert match.label == "United States +1"
    uk = match_country_option("+44", options)
    assert uk is not None
    assert uk.label == "United Kingdom +44"
    assert country_from_phone("+1 5550100000") == "United States"
    assert country_from_phone("5550100000") == ""
    assert country_from_phone("0044 20 7946 0000") == "United Kingdom"


def test_country_aliases_match_united_states_and_the_uk():
    options = [
        Option(value="CA", label="Canada", selector="#ca"),
        Option(value="US", label="United States", selector="#us"),
        Option(value="GB", label="United Kingdom", selector="#gb"),
    ]
    for word in ("United States", "United States of America", "USA", "U.S.", "us"):
        match = match_country_option(word, options)
        assert match is not None
        assert match.label == "United States"
    uk = match_country_option("GB", options)
    assert uk is not None
    assert uk.label == "United Kingdom"
    assert match_country_option("Yes", options) is None
    assert country_aliases("USA")[0] == "united states of america"
    assert "united states" in country_aliases("U.S.A.")
