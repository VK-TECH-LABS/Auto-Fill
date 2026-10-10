"""Country aliases for a closed combobox menu."""

from autofill.combobox import country_aliases, match_country_option
from autofill.models import Option


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
