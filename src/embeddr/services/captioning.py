import torch
from PIL import Image
from transformers import (
    BlipProcessor,
    BlipForConditionalGeneration,
    AutoModelForCausalLM,
)
import logging
import threading
import os

logger = logging.getLogger(__name__)

_model = None
_processor = None
_current_model_name = None
_lock = threading.RLock()
_device = "cuda" if torch.cuda.is_available() else "cpu"

DEFAULT_MODEL = "vikhyatk/moondream2"

CAPTIONING_MODELS = [
    {
        "id": "vikhyatk/moondream2",
        "name": "Moondream2",
        "description": "Efficient VLM capable of captioning and VQA",
        "capabilities": ["caption", "query"],
        "options": {
            "caption": [
                {
                    "name": "length",
                    "label": "Caption Length",
                    "type": "select",
                    "options": [
                        {"label": "Normal", "value": "normal"},
                        {"label": "Short", "value": "short"},
                    ],
                    "default": "normal",
                }
            ],
            "query": [
                {
                    "name": "prompt",
                    "label": "Query / Prompt",
                    "type": "textarea",
                    "required": True,
                    "placeholder": "e.g. Describe the main subject in detail...",
                    "description": "Enter a question or instruction for the model.",
                }
            ],
        },
    },
    {
        "id": "Salesforce/blip-image-captioning-base",
        "name": "BLIP Base",
        "description": "Standard image captioning model",
        "capabilities": ["caption"],
        "options": {"caption": []},
    },
]


def get_captioning_models():
    return CAPTIONING_MODELS


def get_loaded_model_name():
    return _current_model_name


def load_model(model_name: str = DEFAULT_MODEL):
    global _model, _processor, _current_model_name

    with _lock:
        if _model is not None and _current_model_name == model_name:
            return _model, _processor

        if _model is not None:
            unload_model()

        logger.info(f"Loading captioning model: {model_name}")
        try:
            if "moondream" in model_name:
                # Moondream2
                model = AutoModelForCausalLM.from_pretrained(
                    model_name, trust_remote_code=True, revision="2025-01-09"
                ).to(_device)
                processor = None
            else:
                # BLIP
                processor = BlipProcessor.from_pretrained(model_name)
                model = BlipForConditionalGeneration.from_pretrained(model_name).to(
                    _device
                )

            _model = model
            _processor = processor
            _current_model_name = model_name
            return _model, _processor
        except Exception as e:
            logger.error(f"Failed to load model {model_name}: {e}")
            raise


def unload_model():
    global _model, _processor, _current_model_name
    with _lock:
        if _model is not None:
            del _model
            del _processor
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            _model = None
            _processor = None
            _current_model_name = None


def generate_caption(image_path: str, model_name: str = DEFAULT_MODEL, **kwargs) -> str:
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Image not found: {image_path}")

    model, processor = load_model(model_name)

    # Extract common parameters
    prompt = kwargs.get("prompt")
    length = kwargs.get("length", "normal")

    try:
        image = Image.open(image_path).convert("RGB")

        if "moondream" in model_name:
            # Moondream2 specific generation
            if prompt:
                print(f"[Captioning] Using prompt for Moondream2: {prompt}")
                # Query mode
                result = model.query(image, prompt)
                return result["answer"].strip()
            else:
                # Caption mode
                result = model.caption(image, length=length)
                return result["caption"].strip()
        else:
            # BLIP generation
            # BLIP conditional generation if prompt is provided (though standard BLIP is usually just captioning)
            # For now, we'll stick to standard captioning for BLIP unless we switch to VQA model
            inputs = processor(image, return_tensors="pt").to(_device)
            out = model.generate(**inputs)
            caption = processor.decode(out[0], skip_special_tokens=True)
            return caption

    except Exception as e:
        logger.error(f"Error generating caption for {image_path}: {e}")
        raise
