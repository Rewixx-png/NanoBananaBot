"""Local Real-ESRGAN upscaling with quality CPU models.

anime       -> RealESRGAN_x4plus_anime_6B.pth (quality)
photo       -> RealESRGAN_x2plus.pth (quality, native 2x)
photo_fast  -> realesr-general-x4v3.pth (fast, used for auto-upscale)

Models live in models/upscale/ and are intentionally not committed.
Inference runs in a worker thread so the aiogram event loop stays responsive.
"""

import asyncio
import io
import logging
import os
import threading
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "upscale"
MODEL_FILES = {
    "anime": "RealESRGAN_x4plus_anime_6B.pth",
    "photo": "RealESRGAN_x2plus.pth",
    "photo_fast": "realesr-general-x4v3.pth",
}
MODE_LABELS = {
    "anime": "Аниме",
    "photo": "Реалистичное фото",
}
MAX_INPUT_SIDE = 2048
OUTPUT_SCALE = 2
TILE_SIZE = 512
TILE_OVERLAP = 32
_TORCH_THREADS = max(1, len(os.sched_getaffinity(0)) or os.cpu_count() or 1)
_INFER_LOCK = threading.Lock()
_MODELS: dict = {}


def _infer_tiled(model, tensor, scale: int):
    """Run the model in bounded tiles so large photos do not exhaust RAM."""
    import torch

    _, _, height, width = tensor.shape
    if height <= TILE_SIZE and width <= TILE_SIZE:
        with torch.inference_mode():
            return model(tensor)

    output = torch.zeros(
        (1, tensor.shape[1], height * scale, width * scale),
        dtype=torch.float32,
    )
    for y in range(0, height, TILE_SIZE):
        for x in range(0, width, TILE_SIZE):
            y0 = max(0, y - TILE_OVERLAP)
            y1 = min(height, y + TILE_SIZE + TILE_OVERLAP)
            x0 = max(0, x - TILE_OVERLAP)
            x1 = min(width, x + TILE_SIZE + TILE_OVERLAP)
            patch = tensor[:, :, y0:y1, x0:x1]
            with torch.inference_mode():
                patch_out = model(patch)
            crop_top = (y - y0) * scale
            crop_left = (x - x0) * scale
            valid_h = min(TILE_SIZE, height - y) * scale
            valid_w = min(TILE_SIZE, width - x) * scale
            output[
                :, :,
                y * scale:y * scale + valid_h,
                x * scale:x * scale + valid_w,
            ] = patch_out[
                :, :,
                crop_top:crop_top + valid_h,
                crop_left:crop_left + valid_w,
            ]
    return output


def has_local_models() -> bool:
    return all((MODEL_DIR / name).is_file() for name in MODEL_FILES.values())


def _load_model(mode: str):
    if mode in _MODELS:
        return _MODELS[mode]
    try:
        import torch
        from spandrel import ModelLoader
    except ImportError as error:
        raise RuntimeError(
            "Локальный апскейлер не установлен: нужны torch, torchvision и spandrel."
        ) from error

    path = MODEL_DIR / MODEL_FILES[mode]
    if not path.is_file():
        raise RuntimeError(f"Модель не найдена: {path.name}.")
    torch.set_num_threads(_TORCH_THREADS)
    descriptor = ModelLoader().load_from_file(str(path))
    descriptor.model.eval()
    _MODELS[mode] = descriptor
    logger.info(
        "Local upscaler loaded: %s (%s, %s threads)",
        mode, descriptor.architecture.name, _TORCH_THREADS,
    )
    return descriptor


def _upscale_sync(image_bytes: bytes, mode: str) -> Tuple[Optional[bytes], Optional[str]]:
    if mode not in MODEL_FILES:
        return None, f"Неизвестный режим улучшения: {mode}."

    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
        width, height = image.size
        if width < 8 or height < 8:
            return None, "Фото слишком маленькое."
        if max(width, height) > MAX_INPUT_SIDE:
            return None, (
                f"Фото слишком большое для локального апскейла ({width}x{height}). "
                f"Максимум {MAX_INPUT_SIDE}px по длинной стороне."
            )

        with _INFER_LOCK:
            descriptor = _load_model(mode)
            import torch
            import torch.nn.functional as functional
            scale = int(descriptor.scale)
            tensor = torch.from_numpy(
                np.asarray(image, dtype=np.float32) / 255.0
            ).permute(2, 0, 1).unsqueeze(0)
            output = _infer_tiled(descriptor.model, tensor, scale)
            target = (height * OUTPUT_SCALE, width * OUTPUT_SCALE)
            if output.shape[-2:] != target:
                output = functional.interpolate(
                    output, size=target, mode="bicubic",
                    align_corners=False, antialias=True,
                )
            output = (
                output.squeeze(0).clamp(0, 1).permute(1, 2, 0)
                .mul(255).round().byte().cpu().numpy()
            )

        result = Image.fromarray(output)
        buffer = io.BytesIO()
        result.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue(), None
    except Exception as error:
        logger.exception("Local upscale failed: %s", error)
        return None, "Не удалось улучшить фото локально. Попробуй другое фото."


async def upscale_image(
    image_bytes: bytes, mode: str = "photo"
) -> Tuple[Optional[bytes], Optional[str]]:
    if mode not in MODEL_FILES:
        return None, f"Неизвестный режим улучшения: {mode}."
    return await asyncio.to_thread(_upscale_sync, image_bytes, mode)
