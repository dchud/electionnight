"""Multilingual embeddings, run on the CPU in small, spaced batches."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import numpy as np

from monitor.parsers.base import Item, ParserOutput, needs_parser_sql
from monitor.settings import Settings

EMBED_DIM = 384
ONNX_THREADS = 4
PASSAGE_PREFIX = "passage: "
QUERY_PREFIX = "query: "


def load_embedding_model(settings: Settings) -> Any:
    """Load the model, registering it with fastembed if it is not listed.

    The Hugging Face repository for this model ships an ONNX export, so no
    torch install is needed.
    """
    from fastembed import TextEmbedding
    from fastembed.common.model_description import ModelSource, PoolingType

    name = settings.nlp.embed_model
    settings.paths.models.mkdir(parents=True, exist_ok=True)
    listed = {m["model"] for m in TextEmbedding.list_supported_models()}
    if name not in listed:
        TextEmbedding.add_custom_model(
            model=name,
            pooling=PoolingType.MEAN,
            normalization=True,
            sources=ModelSource(hf=name),
            dim=EMBED_DIM,
            model_file="onnx/model.onnx",
            additional_files=[],
        )
    return TextEmbedding(
        model_name=name,
        cache_dir=str(settings.paths.models),
        threads=ONNX_THREADS,
    )


def passage_text(item: Item) -> str:
    return f"{PASSAGE_PREFIX}{item.english_title}. {item.english_summary}"


class EmbedParser:
    name = "embed"
    version = "1"
    max_concurrency = 1
    input_query = needs_parser_sql(
        name,
        version,
        "i.item_id IN (SELECT item_id FROM lake.nlp.classify_a)",
    )

    def __init__(self, settings: Settings, model: Any = None) -> None:
        self.settings = settings
        self.batch_size = settings.nlp.embed_batch_size
        self.duty_cycle = settings.nlp.embed_duty_cycle
        self.throttle = 1.0
        self._model = model

    def _embed(self, batch: list[Item]) -> np.ndarray:
        if self._model is None:
            self._model = load_embedding_model(self.settings)
        texts = [passage_text(item) for item in batch]
        vectors = self._model.embed(texts, batch_size=self.batch_size)
        return np.array(list(vectors), dtype=np.float32)

    async def run(self, batch: list[Item]) -> ParserOutput:
        started = time.monotonic()
        vectors = await asyncio.to_thread(self._embed, batch)
        output = ParserOutput()
        for item, vector in zip(batch, vectors, strict=True):
            item.vec = vector
            output.add(
                "nlp.embeddings",
                {
                    "item_id": item.item_id,
                    "model": self.settings.nlp.embed_model,
                    "vec": vector.tolist(),
                    "text_basis": "title+summary",
                    "dim": len(vector),
                    "parser": self.name,
                    "parser_version": self.version,
                },
            )
        elapsed = time.monotonic() - started
        duty = max(0.05, self.duty_cycle * self.throttle)
        await asyncio.sleep(elapsed * (1 / duty - 1))
        return output
