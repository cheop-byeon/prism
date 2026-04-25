"""
Embedding server for jiebi/RFC-DRAlign-QN (PEFT on Mistral-7B-v0.1).

Uses EOS token </s> (token ID 2) as the text representation, extracted
from the last hidden state at the explicit EOS position — not via
attention_mask sum, because Mistral has no dedicated pad token and we
must append EOS manually before padding.

Compatible with HuggingFace InferenceClient.feature_extraction(), so
Dify can use it as an "Inference Endpoints" model with task_type=feature-extraction.

Usage:
    pip install fastapi uvicorn torch transformers peft accelerate bitsandbytes
    python embed_server.py [--quantize] [--port 8001]

Flags:
    --quantize   CUDA: 4-bit NF4 via bitsandbytes (~3.5 GB VRAM).
                 MPS/CPU: float16 (~14 GB RAM, half of float32).
    --port N     Port to listen on (default: 8001).

Then in Dify > Settings > Model Provider > HuggingFace Hub:
    Endpoint Type: Inference Endpoints
    Endpoint URL: http://localhost:8001
    Task Type: Feature Extraction
"""

from __future__ import annotations

import argparse
import logging
from contextlib import asynccontextmanager
from typing import Union

import torch
import uvicorn
from fastapi import FastAPI
from peft import PeftModel
from pydantic import BaseModel
from transformers import AutoModel, AutoTokenizer

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BASE_MODEL_ID = "mistralai/Mistral-7B-v0.1"
PEFT_MODEL_ID = "jiebi/RFC-DRAlign-QN"

tokenizer: AutoTokenizer
model: PeftModel
device: str


def _detect_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _load_model(quantize: bool) -> None:
    global tokenizer, model, device

    device = _detect_device()
    log.info("Device: %s", device)

    log.info("Loading tokenizer from %s", BASE_MODEL_ID)
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL_ID)

    # Mistral has no dedicated pad token.  Use <unk> (ID 0) so pad IDs are
    # never confused with EOS (ID 2).  EOS positions are tracked explicitly
    # anyway, so this only affects what value fills the padding slots.
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token_id = tokenizer.unk_token_id or 0

    model_kwargs: dict = {}

    if quantize and device == "cuda":
        # True 4-bit NF4 quantization: ~3.5 GB VRAM instead of ~14 GB.
        # bitsandbytes places the model on CUDA automatically; do NOT call
        # .to(device) afterwards or it will raise a device-map conflict.
        from transformers import BitsAndBytesConfig

        model_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
        )
        log.info("4-bit NF4 quantization enabled (bitsandbytes)")
    elif quantize:
        # bitsandbytes 4-bit requires CUDA; use float16 as a best-effort
        # memory reduction on MPS/CPU (halves RAM vs float32).
        log.warning(
            "4-bit bitsandbytes quantization requires CUDA (detected: %s). "
            "Falling back to float16 — RAM ~14 GB instead of ~28 GB.",
            device,
        )
        model_kwargs["torch_dtype"] = torch.float16
    else:
        model_kwargs["torch_dtype"] = torch.float16 if device == "cuda" else torch.float32

    log.info("Loading base model from %s", BASE_MODEL_ID)
    base = AutoModel.from_pretrained(BASE_MODEL_ID, **model_kwargs)

    log.info("Loading PEFT adapter from %s", PEFT_MODEL_ID)
    model = PeftModel.from_pretrained(base, PEFT_MODEL_ID)

    # bitsandbytes already placed the model on CUDA; moving it again would
    # conflict with device-map bookkeeping.
    if not (quantize and device == "cuda"):
        model.to(device)

    model.eval()
    log.info("Model ready on %s", device)


def _make_app(quantize: bool) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _load_model(quantize)
        yield

    return FastAPI(lifespan=lifespan)


class EmbedRequest(BaseModel):
    # HuggingFace InferenceClient sends {"inputs": str | list[str]}
    inputs: Union[str, list[str]]


def _embed(texts: list[str]) -> list[list[float]]:
    """
    Tokenize each text, append EOS explicitly, record its position, then
    batch with right-padding.  Extract last_hidden_state[:, eos_pos, :] for
    each sequence.
    """
    eos_id = tokenizer.eos_token_id  # 2 for Mistral

    input_ids_list: list[torch.Tensor] = []
    attention_masks_list: list[torch.Tensor] = []
    eos_positions: list[int] = []

    for text in texts:
        enc = tokenizer(text, add_special_tokens=True, return_tensors="pt")
        ids = enc["input_ids"][0]       # (seq_len,)
        mask = enc["attention_mask"][0]

        # Append EOS unless the tokenizer already added it (guard against
        # configs where add_eos_token=True to avoid double-appending).
        if ids[-1] != eos_id:
            ids = torch.cat([ids, ids.new_tensor([eos_id])])
            mask = torch.cat([mask, mask.new_ones(1)])

        # Record EOS position before padding is added — always correct.
        eos_positions.append(ids.shape[0] - 1)
        input_ids_list.append(ids)
        attention_masks_list.append(mask)

    # Right-pad the batch to the longest sequence.
    max_len = max(t.shape[0] for t in input_ids_list)
    pad_id = tokenizer.pad_token_id

    padded_ids = torch.full((len(texts), max_len), pad_id, dtype=torch.long)
    padded_masks = torch.zeros(len(texts), max_len, dtype=torch.long)

    for i, (ids, mask) in enumerate(zip(input_ids_list, attention_masks_list)):
        n = ids.shape[0]
        padded_ids[i, :n] = ids
        padded_masks[i, :n] = mask

    padded_ids = padded_ids.to(device)
    padded_masks = padded_masks.to(device)

    with torch.no_grad():
        output = model(input_ids=padded_ids, attention_mask=padded_masks)

    hidden = output.last_hidden_state  # (batch, max_len, hidden_dim)
    embeddings: list[list[float]] = []
    for i, eos_pos in enumerate(eos_positions):
        emb = hidden[i, eos_pos, :].cpu().float().tolist()
        embeddings.append(emb)

    return embeddings


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EOS-token embedding server")
    parser.add_argument(
        "--quantize",
        action="store_true",
        help="4-bit NF4 on CUDA, float16 fallback on MPS/CPU",
    )
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    app = _make_app(quantize=args.quantize)

    @app.post("/")
    def embed(request: EmbedRequest) -> list[list[float]]:
        texts = [request.inputs] if isinstance(request.inputs, str) else request.inputs
        return _embed(texts)

    uvicorn.run(app, host="0.0.0.0", port=args.port)
