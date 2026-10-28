"""Unit tests for the legacy pilot importer's security hard limits (no network, no DB writes).

Coverage: the SSRF host guard must refuse every private/reserved target (loopback, RFC1918,
link-local, cloud-metadata, CGNAT, unspecified) while accepting public addresses; and the
pilot's record list must never exceed the 13 manifest entries or lose its deterministic
uuid5 identity derivation.
"""
import socket
import unittest
import uuid
from unittest import mock

from scripts.legacy_pilot_import import MAX_PILOT, PILOT, _reject_private_host


def _set_dns_resolution(ip: str):
    """Patch  getaddrinfo  so the 'resolved' address is ip."""
    info = (socket.AF_INET, socket.SocketKind.SOCK_STREAM, 6, "", (ip, 0))
    return mock.patch("scripts.legacy_pilot_import.socket.getaddrinfo", return_value=[info])


class SsrfGuardTests(unittest.TestCase):
    def test_refuses_loopback_private_linklocal_metadata_cgnat_unspecified(self):
        for ip in ("127.0.0.1", "127.8.8.8", "10.1.2.3", "172.16.0.9", "172.31.255.1", "192.168.4.4",
                   "169.254.169.254", "0.0.0.0", "100.64.0.1", "224.0.0.1", "255.255.255.255"):
            with self.subTest(ip=ip), _set_dns_resolution(ip):
                with self.assertRaises(ValueError):
                    _reject_private_host("attacker.example")

    def test_refuses_ipv6_loopback_and_ula(self):
        for ip in ("::1", "fd00::1", "fe80::1"):
            info = (socket.AF_INET6, socket.SocketKind.SOCK_STREAM, 6, "", (ip, 0, 0, 0))
            with self.subTest(ip=ip), mock.patch("scripts.legacy_pilot_import.socket.getaddrinfo",
                                                 return_value=[info]):
                with self.assertRaises(ValueError):
                    _reject_private_host("attacker.example")

    def test_accepts_public_address(self):
        with _set_dns_resolution("140.82.121.4"):
            self.assertEqual(_reject_private_host("public.example"), "public.example")


class HardLimitTests(unittest.TestCase):
    def test_pilot_list_is_exactly_13_unique_manifest_records(self):
        self.assertEqual(MAX_PILOT, 13)
        self.assertEqual(len(PILOT), 13)
        self.assertEqual(len({p for p, _ in PILOT}), 13)
        self.assertEqual(len({lid for _, lid in PILOT}), 13)

    def test_ids_are_documented_deterministic_derivation(self):
        for pid, lid in PILOT:
            self.assertEqual(pid, str(uuid.uuid5(uuid.NAMESPACE_URL, f"urn:x-arxiv-legacy:{lid}")))

    def test_manifest_sources_are_arxiv_abs_urls(self):
        for _, lid in PILOT:
            self.assertTrue(lid.startswith("http://arxiv.org/abs/"))
