"""Simple network checks run from inside the controller container."""
import socket
import subprocess


def ping(ip, timeout=2):
    try:
        return subprocess.run(["ping", "-c", "1", "-W", str(timeout), ip],
                              capture_output=True, timeout=timeout + 3).returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def tcp(ip, port, timeout=2):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False
