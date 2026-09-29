import http.client
import json
import threading
import time
import unittest
from pathlib import Path
import sys


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from server import (  # noqa: E402
    LocalBeamServer,
    TransferHub,
    format_bytes,
    http_origin,
    parse_size,
    validate_public_host,
)


class LocalBeamTests(unittest.TestCase):
    def setUp(self):
        self.hub = TransferHub(max_file_size=16 * 1024 * 1024, link_ttl_seconds=600)
        self.server = LocalBeamServer(("127.0.0.1", 0), self.hub)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request_json(self, method, path, payload=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"} if body is not None else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = json.loads(response.read())
        status = response.status
        connection.close()
        return status, result

    def create_transfer(self, name="hello.txt", data=b"hello localbeam"):
        status, result = self.request_json(
            "POST",
            "/api/transfers",
            {"fileName": name, "size": len(data), "contentType": "text/plain"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(len(result["id"]), 8)
        self.assertEqual(result["receivePath"], f"/r/{result['id']}")
        return result

    def test_short_receive_route_loads_the_app(self):
        transfer = self.create_transfer(data=b"x")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", transfer["receivePath"])
        response = connection.getresponse()
        body = response.read()
        self.assertEqual(response.status, 200)
        self.assertIn(b"LocalBeam", body)
        connection.close()

    def test_config_advertises_a_named_share_origin(self):
        status, result = self.request_json("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertTrue(result["publicHost"])
        self.assertEqual(result["shareOrigin"], http_origin("auto", self.port))

    def test_end_to_end_stream_is_identical(self):
        # Larger than the server's bounded 8 MB queue, so this also exercises
        # streaming backpressure rather than only a one-chunk transfer.
        data = (b"LocalBeam payload\x00" * (10 * 1024 * 1024 // 18)) + b"end"
        transfer = self.create_transfer(data=data)
        downloaded = {}

        def receive():
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            connection.request("GET", f"/api/transfers/{transfer['id']}/download")
            response = connection.getresponse()
            downloaded["status"] = response.status
            downloaded["body"] = response.read()
            connection.close()

        receiver = threading.Thread(target=receive)
        receiver.start()
        deadline = time.time() + 3
        while time.time() < deadline:
            status, current = self.request_json("GET", f"/api/transfers/{transfer['id']}")
            if status == 200 and current["state"] == "receiver-ready":
                break
            time.sleep(0.02)
        else:
            self.fail("Receiver did not become ready")

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(
            "PUT",
            f"/api/transfers/{transfer['id']}/upload?key={transfer['uploadKey']}",
            body=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 200)
        connection.close()
        receiver.join(timeout=5)
        self.assertEqual(downloaded["status"], 200)
        self.assertEqual(downloaded["body"], data)

        status, current = self.request_json("GET", f"/api/transfers/{transfer['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(current["state"], "completed")
        self.assertEqual(current["bytesSent"], len(data))

    def test_oversized_file_is_rejected(self):
        self.hub.max_file_size = 4
        status, result = self.request_json(
            "POST",
            "/api/transfers",
            {"fileName": "too-big.bin", "size": 5, "contentType": "application/octet-stream"},
        )
        self.assertEqual(status, 413)
        self.assertIn("current limit", result["error"])

    def test_unsafe_content_type_is_not_reflected(self):
        status, result = self.request_json(
            "POST",
            "/api/transfers",
            {"fileName": "safe.txt", "size": 1, "contentType": "text/plain\r\nX-Bad: yes"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(result["contentType"], "application/octet-stream")

    def test_link_is_single_use(self):
        transfer = self.create_transfer(data=b"x")
        first = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        first.request("GET", f"/api/transfers/{transfer['id']}/download")
        first_response = first.getresponse()
        self.assertEqual(first_response.status, 200)

        second = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        second.request("GET", f"/api/transfers/{transfer['id']}/download")
        second_response = second.getresponse()
        second_response.read()
        self.assertEqual(second_response.status, 409)
        second.close()

        self.hub.fail(self.hub.get(transfer["id"]), "test cleanup", "cancelled")
        first.close()

    def test_unopened_link_expires(self):
        expiring_hub = TransferHub(max_file_size=100, link_ttl_seconds=0)
        transfer = expiring_hub.create("soon.txt", 1, "text/plain")
        self.assertEqual(expiring_hub.get(transfer.transfer_id).state, "expired")

    def test_helpers(self):
        self.assertEqual(parse_size("2GB"), 2 * 1024**3)
        self.assertEqual(parse_size("512 mb"), 512 * 1024**2)
        self.assertEqual(format_bytes(2 * 1024**3), "2 GB")
        self.assertEqual(validate_public_host("OFFICE-PC"), "OFFICE-PC")
        self.assertEqual(validate_public_host("localbeam.local"), "localbeam.local")
        with self.assertRaises(ValueError):
            parse_size("lots")
        with self.assertRaises(ValueError):
            validate_public_host("https://bad.example")


if __name__ == "__main__":
    unittest.main()
