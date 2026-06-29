"""
Embedding generator for the Grida Library — Google Gemini Embedding 2
via the Vercel AI Gateway (`/v1/embeddings`, OpenAI-compatible).

Produces two single-modality vectors (never fused):
  - embed_image(image, mimetype) -> image vector       (gemini_embedding_2__image)
  - embed_text(text)             -> text  vector       (gemini_embedding_2__text)

Both are returned as 1536-d, L2-normalized lists. The model returns 3072-d
by default; we take the first 1536 (Matryoshka truncation) and re-normalize.
This post-processing MUST match the editor's query embedder so stored and
query vectors are comparable (the cross-modal floor depends on it).

Live-verified request shapes (Vercel AI Gateway):
  text  -> {"model": M, "input": "some text"}
  image -> {"model": M, "input": [
              {"type": "image_url", "image_url": {"url": "data:<mime>;base64,..."}}]}
"""
import os
import math
import requests
from dotenv import load_dotenv
from embedding_transform import b64

load_dotenv()

# --- Gemini Embedding 2 via the Vercel AI Gateway ---
EMBEDDINGS_URL = os.getenv(
    "EMBEDDINGS_URL", "https://ai-gateway.vercel.sh/v1/embeddings")
AI_GATEWAY_API_KEY = os.getenv("AI_GATEWAY_API_KEY")
EMBEDDING_MODEL_ID = os.getenv("EMBEDDING_MODEL_ID", "google/gemini-embedding-2")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1536"))


class EmbedError(Exception):
    "Error returned by the embedding provider."

    def __init__(self, message):
        self.message = message
        super().__init__(message)


def _to_library_vector(v: list) -> list:
    """Truncate to EMBEDDING_DIM (MRL) and L2-normalize."""
    if len(v) < EMBEDDING_DIM:
        raise EmbedError(
            f"embedding dim {len(v)} < required {EMBEDDING_DIM}")
    s = v[:EMBEDDING_DIM]
    n = math.sqrt(sum(x * x for x in s))
    return s if n == 0 else [x / n for x in s]


def _embed(input_payload) -> list:
    resp = requests.post(
        EMBEDDINGS_URL,
        headers={
            "Authorization": f"Bearer {AI_GATEWAY_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"model": EMBEDDING_MODEL_ID, "input": input_payload},
        timeout=60,
    )
    body = resp.json()
    if not resp.ok or body.get("error"):
        raise EmbedError(
            f"Embeddings generation error: {body.get('error') or body}")
    return _to_library_vector(body["data"][0]["embedding"])


def embed_image(image: str | bytes, mimetype: str) -> list:
    """Image embedding (1536-d, L2-normalized). SVG is rasterized to PNG
    by `b64` (cairosvg) before embedding."""
    encoded = b64(image, mimetype)
    out_mime = "image/png" if mimetype == "image/svg+xml" else mimetype
    data_url = f"data:{out_mime};base64,{encoded}"
    return _embed(
        [{"type": "image_url", "image_url": {"url": data_url}}])


def embed_text(text: str) -> list:
    """Text embedding (1536-d, L2-normalized)."""
    return _embed(text)
