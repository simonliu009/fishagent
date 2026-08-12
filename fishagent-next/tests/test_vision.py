import io
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

from fishagent.application.agent_service import FishAgentSystem
from fishagent.infrastructure.vision import FreshFrameVisionAdapter, HttpSnapshotCameraGateway, validate_frame


def png_bytes(width: int = 8, height: int = 6) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (15, 118, 110)).save(buffer, format="PNG")
    return buffer.getvalue()


class FrameHandler(BaseHTTPRequestHandler):
    frame = png_bytes()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(self.frame)))
        self.end_headers()
        self.wfile.write(self.frame)

    def log_message(self, *_args):
        return


class VisionBoundaryTests(unittest.TestCase):
    def test_validate_frame_records_dimensions_and_hash(self) -> None:
        frame = validate_frame(png_bytes(12, 10), "upload://test")
        self.assertEqual((frame.width, frame.height), (12, 10))
        self.assertEqual(frame.content_type, "image/png")
        self.assertEqual(len(frame.sha256), 64)

    def test_http_snapshot_gateway_fetches_and_validates(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), FrameHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            camera = type("Camera", (), {"source_url": "http://127.0.0.1:%s/frame" % server.server_port})()
            frame = HttpSnapshotCameraGateway().capture(camera)
            self.assertEqual(frame.width, 8)
            self.assertEqual(frame.height, 6)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_vision_adapter_does_not_invent_unavailable_finding(self) -> None:
        system = FishAgentSystem()
        system.initialize_demo()
        observation = FreshFrameVisionAdapter().analyze(system.store.cameras["camera-b01"])
        self.assertEqual(observation.status, "UNAVAILABLE")
        self.assertIsNone(observation.frame_id)
