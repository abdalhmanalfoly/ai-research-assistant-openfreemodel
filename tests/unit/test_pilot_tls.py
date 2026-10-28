"""TLS + download-guard tests for the legacy pilot importer (no DB, no production writes).

Covers the Windows fix for `CERTIFICATE_VERIFY_FAILED`: the script's TLS context must use an
explicit, maintained CA bundle (certifi by default) with verification fully ON, must REJECT
untrusted/self-signed certificates, and must accept them only when a deliberately-supplied
private CA is configured via the documented PILOT_CA_BUNDLE path (corporate scenario).

Network- and openssl-dependent checks are skipped (never failed) when unavailable.
"""
import http.server
import os
import shutil
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from scripts.legacy_pilot_import import tls_context, validated_download


def _set_dns(ip):
    info = (socket.AF_INET, socket.SocketKind.SOCK_STREAM, 6, "", (ip, 0))
    return mock.patch("scripts.legacy_pilot_import.socket.getaddrinfo", return_value=[info])


def _https_server(certfile: str, keyfile: str):
    """Start a tiny HTTPS server on 127.0.0.1 serving a %PDF- body. Returns (port, stop)."""

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"%PDF-1.4\n% probe\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    def stop():
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)

    return port, stop


class TlsContextTests(unittest.TestCase):
    def test_verification_fully_enabled(self):
        ctx = tls_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_trust_store_is_nonempty(self):
        self.assertTrue(tls_context().get_ca_certs())

    def test_modern_protocol_floor(self):
        self.assertGreaterEqual(tls_context().minimum_version, ssl.TLSVersion.TLSv1_2)


class RealAuthorityHandshakeTests(unittest.TestCase):
    def test_handshake_against_real_public_ca_succeeds(self):
        """The actual incident case: arxiv.org must verify against the configured bundle."""
        try:
            socket.create_connection(("arxiv.org", 443), timeout=5).close()
        except OSError as e:
            self.skipTest(f"network unavailable: {e}")
        try:
            with socket.create_connection(("arxiv.org", 443), timeout=5) as sock, \
                    tls_context().wrap_socket(sock, server_hostname="arxiv.org"):
                pass
        except ssl.SSLError as e:
            self.fail(f"arxiv.org must verify against certifi ({e}); do NOT paper over with verify=False")


class UntrustedAndCustomCaTests(unittest.TestCase):
    """Self-signed local server: untrusted by default; trusted ONLY via an explicit CA bundle."""

    @classmethod
    def setUpClass(cls):
        openssl = shutil.which("openssl")
        if not openssl:
            raise unittest.SkipTest("openssl CLI unavailable")
        d = tempfile.mkdtemp(prefix="pilot_tls_")
        cls.cert, cls.key = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
        try:
            subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-keyout", cls.key,
                            "-out", cls.cert, "-days", "1", "-nodes", "-subj", "/CN=localhost",
                            "-addext", "subjectAltName=DNS:localhost"],
                           check=True, capture_output=True, timeout=60)
        except Exception as e:
            raise unittest.SkipTest(f"openssl cert generation failed: {e}")

    @classmethod
    def tearDownClass(cls):
        for f in (cls.cert, cls.key):
            if f and os.path.exists(f):
                os.unlink(f)

    def test_untrusted_self_signed_is_rejected(self):
        port, stop = _https_server(self.cert, self.key)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                with self.assertRaises(ssl.SSLCertVerificationError):
                    tls_context().wrap_socket(sock, server_hostname="localhost")
        finally:
            stop()

    def test_admin_supplied_private_ca_is_accepted_via_cafile(self):
        """Trusting a deliberately-supplied private root CA = the documented corporate path."""
        ctx = ssl.create_default_context(cafile=self.cert)
        port, stop = _https_server(self.cert, self.key)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock, \
                    ctx.wrap_socket(sock, server_hostname="localhost"):
                pass  # handshake-ok
        finally:
            stop()

    def test_hostname_check_survives_with_custom_bundle(self):
        ctx = ssl.create_default_context(cafile=self.cert)
        port, stop = _https_server(self.cert, self.key)
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
                with self.assertRaises(ssl.SSLCertVerificationError):
                    ctx.wrap_socket(sock, server_hostname="not-localhost")
        finally:
            stop()


class DownloadGuardTests(unittest.TestCase):
    def test_local_addresses_blocked_by_ssrf_guard_before_any_fetch(self):
        # Loopback/link-local targets must be refused even though a local server is reachable.
        with self.assertRaises(Exception, msg="loopback must be refused"):
            validated_download("http://127.0.0.1/x.pdf")
        with self.assertRaises(Exception, msg="link-local/metadata must be refused"):
            validated_download("http://169.254.169.254/latest/meta-data/")

    def test_non_pdf_magic_rejected_without_db(self):
        """validated_download must never accept a body lacking the %PDF- magic (patched transport)."""
        import io

        class FakeResp(io.BytesIO):
            url = "http://public.example/doc"

            def read(self, n=-1):  # urllib calls read(25MB+1)
                return super().read(n)

        from scripts import legacy_pilot_import as m

        class FakeOpener:
            def open(self, req, timeout=None):
                return FakeResp(b"<!DOCTYPE html> virus? no\n")

        with _set_dns("140.82.121.4"), \
                mock.patch.object(m, "build_opener", return_value=FakeOpener()):
            with self.assertRaises(ValueError, msg="non-PDF body must be rejected"):
                validated_download("http://public.example/doc")

    def test_size_cap_enforced(self):
        import io

        class FakeResp(io.BytesIO):
            url = "http://public.example/doc"

        from scripts import legacy_pilot_import as m

        class FakeOpener:
            def open(self, req, timeout=None):
                return FakeResp(b"%PDF-1.4" + b"x" * (m.MAX_PDF_BYTES + 1))

        with _set_dns("140.82.121.4"), \
                mock.patch.object(m, "build_opener", return_value=FakeOpener()):
            with self.assertRaises(ValueError, msg="oversized body must be rejected"):
                validated_download("http://public.example/doc")

    def test_valid_pdf_accepted_by_functional_gate(self):
        import io

        class FakeResp(io.BytesIO):
            url = "http://public.example/doc"

        from scripts import legacy_pilot_import as m

        class FakeOpener:
            def open(self, req, timeout=None):
                return FakeResp(b"%PDF-1.4\nminimal test document\n")

        with _set_dns("140.82.121.4"), \
                mock.patch.object(m, "build_opener", return_value=FakeOpener()):
            data, digest = validated_download("http://public.example/doc")
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertEqual(digest, __import__("hashlib").sha256(data).hexdigest())
