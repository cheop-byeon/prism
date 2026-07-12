"""OpenAI-compatible embedding shim for the RFC-DRAlign model.

Forwards /v1/embeddings to a local llama-server, appending "</s>" to every
input first. Why: the model was trained with tokenizer add_eos_token=True
PLUS a manual EOS append, so inputs ended with a DOUBLE EOS and last-token
pooling learned to read the second one. llama-server contributes one EOS
(via --override-kv tokenizer.ggml.add_eos_token=bool:true); the literal
"</s>" appended here becomes the first (parse_special tokenization).

Validated: with the double EOS, llama.cpp q8_0 embeddings match the fp16
PyTorch reference at cosine 0.989-0.9998; with a single EOS they diverge
to ~0.86.

Usage: python3 dev/rfc-dralign-embed-shim.py [--port 8090] [--upstream http://127.0.0.1:8089]
"""

import argparse
import json
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "http://127.0.0.1:8089"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        if self.path.rstrip("/") not in ("/v1/embeddings", "/embeddings"):
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        texts = body.get("input", [])
        if isinstance(texts, str):
            texts = [texts]
        body["input"] = [t + "</s>" for t in texts]

        req = urllib.request.Request(
            f"{UPSTREAM}/v1/embeddings",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=600) as res:
                payload = res.read()
                self.send_response(200)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            self.send_response(exc.code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        pass  # quiet


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--upstream", default=UPSTREAM)
    args = parser.parse_args()
    UPSTREAM = args.upstream
    print(f"embed shim on :{args.port} -> {UPSTREAM} (appending </s> to inputs)")
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
