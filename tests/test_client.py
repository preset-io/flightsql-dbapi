import pytest

from flightsql.client import create_flight_client


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("insecure", "false"),
        ("disable_server_verification", 0),
    ],
)
def test_create_flight_client_rejects_non_boolean_tls_modes(parameter, value):
    with pytest.raises(TypeError, match=rf"{parameter} must be a bool or None"):
        create_flight_client(**{parameter: value})


def test_create_flight_client_rejects_conflicting_tls_modes():
    with pytest.raises(ValueError, match="cannot both be true"):
        create_flight_client(insecure=True, disable_server_verification=True)
