"""
Embedding generator for the Grida Library — Google Gemini Embedding 2
via an OpenAI-compatible `/embeddings` endpoint (OpenRouter in dev, the
Vercel AI Gateway in prod; same model id either way).

Produces two single-modality vectors (never fused):
  - embed_image(image, mimetype) -> image vector       (gemini_embedding_2__image)
  - embed_text(text)             -> text  vector       (gemini_embedding_2__text)

Both are returned as 1536-d, L2-normalized lists. The model returns 3072-d
by default; we take the first 1536 (Matryoshka truncation) and re-normalize.
This post-processing MUST match the editor's query embedder so stored and
query vectors are comparable (the cross-modal floor depends on it).

A legacy Amazon Titan image embedding (1024-d) is kept behind
`titan_embed_image()` for the transition window only — the worker can
dual-write it so the old `similar()` stays live until the grida-repo cutover
migration repoints retrieval. Remove it (and boto3) after cutover + soak.

Verified request shapes (OpenAI-compatible):
  text  -> {"model": M, "input": "some text"}
  image -> {"model": M, "input": [{"content": [
              {"type": "image_url", "image_url": {"url": "data:<mime>;base64,..."}}]}]}
"""
import os
import json
import math
import requests
from dotenv import load_dotenv
from embedding_transform import b64

load_dotenv()

# --- Gemini Embedding 2 (OpenAI-compatible) ---
EMBEDDINGS_URL = os.getenv(
    "EMBEDDINGS_URL", "https://openrouter.ai/api/v1/embeddings")
EMBEDDINGS_API_KEY = os.getenv("EMBEDDINGS_API_KEY")
EMBEDDING_MODEL_ID = os.getenv("EMBEDDING_MODEL_ID", "google/gemini-embedding-2")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1536"))

# --- legacy Titan (transition dual-write only) ---
TITAN_MODEL_ID = "amazon.titan-embed-image-v1"


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
            "Authorization": f"Bearer {EMBEDDINGS_API_KEY}",
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
        [{"content": [{"type": "image_url", "image_url": {"url": data_url}}]}])


def embed_text(text: str) -> list:
    """Text embedding (1536-d, L2-normalized)."""
    return _embed(text)


def titan_embed_image(image: str | bytes, mimetype: str) -> list:
    """LEGACY: Amazon Titan Multimodal Embeddings G1 (1024-d) via Bedrock.
    Transition dual-write only — drop after the grida-repo cutover + soak."""
    import boto3  # lazy: only needed when DUAL_WRITE_TITAN is on
    body = {
        "inputImage": b64(image, mimetype),
        "embeddingConfig": {"outputEmbeddingLength": 1024},
    }
    bedrock = boto3.client(
        service_name="bedrock-runtime",
        region_name=os.getenv("AWS_REGION", "us-east-1"),
    )
    response = bedrock.invoke_model(
        body=json.dumps(body),
        modelId=TITAN_MODEL_ID,
        accept="application/json",
        contentType="application/json",
    )
    response_body = json.loads(response.get("body").read())
    if response_body.get("message") is not None:
        raise EmbedError(
            f"Embeddings generation error: {response_body['message']}")
    return response_body["embedding"]
