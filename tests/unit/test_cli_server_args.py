"""Tests the cli serve function with port and host arguments set with some edge cases tested.
"""
import pytest
from unittest import mock
from symdex.cli import serve

@pytest.fixture(autouse=True)
def mock_environment():
    """Mocks the entire application environment state needed by symdex/cli."""
    with mock.patch("symdex.cli._maybe_print_update_notice"), mock.patch(
        "symdex.mcp.server.mcp"
    ) as mock_mcp:
        yield mock_mcp


def test_serve_happy_path(mock_environment):
    """Test case: Successful call with no arguments (relying on defaults)."""
    # Arrange: We expect the function call to proceed without asserting an error,
    # which means our mock setup is sufficient for a 'pass' state.
    # We capture the function execution and ensure it doesn't raise an unhandled exception.
    serve(port=None, host=None, state_dir=None)
    mock_environment.run.assert_called_once_with()


def test_serve_explicit_params_success(mock_environment):
    """Test case: Successful call with valid explicit port and host."""
    test_host = "127.0.0.1"
    test_port = 8080
    
    serve(host=test_host, port=test_port, state_dir=None)
    
    mock_environment.run.assert_called_once_with(
        transport="streamable-http", host=test_host, port=test_port
    )


@pytest.mark.parametrize("invalid_host, msg", [
    ("127.0.0.1.5", "Invalid host"),  
    ("127.0..1", "Invalid host"),
    ("127.0.a.1", "Invalid host"),
    ("Hello world", "Invalid host"),
    ("....", "Invalid host")
])
def test_serve_explicit_params_failure(invalid_host, msg):
    """Test case: Should fail validation for invalid hosts."""
    with pytest.raises(AssertionError) as excinfo:
        serve(host=invalid_host, port=8080, state_dir=None)
    assert msg in str(excinfo.value)


def test_serve_unknown_ip_format():
    """Test case: Fails on host string that looks like an IP but isn't valid (e.g., text)."""
    # Test non-IP string format
    with pytest.raises(AssertionError):
        serve(host="not-an-ip", port=8080, state_dir=None)


@pytest.mark.parametrize("invalid_port", [
    0,                  # Port too low
    65537,               # Port too high
    "hello",               # not a port
    11.6               # float 
])
def test_serve_port_validation(invalid_port):
    """Test case: Should fail validation if the port is out of range."""
    # Test using parameterized invalid ports
    with pytest.raises(AssertionError):
        serve(port=invalid_port, host="localhost", state_dir=None)


def test_serve_valid_edge_case_ports():
    """Test case: Should succeed for min and max valid ports."""
    # 1. Min Port Test (Should pass silently)
    with mock.patch("symdex.mcp.server.mcp") as mock_mcp:
        serve(port=1, host="localhost", state_dir=None)
        mock_mcp.run.assert_called_once()

    # 2. Max Port Test (Should pass silently)
    with mock.patch("symdex.mcp.server.mcp") as mock_mcp:
        serve(port=65535, host="localhost", state_dir=None)
        mock_mcp.run.assert_called_once()


def test_serve_mixed_and_missing_params(mock_environment):
    """Test case: Valid combination of missing and present arguments."""
    # Test 1: Host provided, port missing (Should pass validation if host is valid)
    serve(host="localhost", port=None, state_dir=None)
    mock_environment.run.assert_called_once_with(transport="streamable-http", host="localhost")
    mock_environment.run.reset_mock()

    # Test 2: Port provided, host missing (Should pass validation if port is valid)
    serve(port=3000, host=None, state_dir=None)
    mock_environment.run.assert_called_once_with(transport="streamable-http", port=3000)
