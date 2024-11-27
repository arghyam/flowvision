from transformers import Qwen2VLForConditionalGeneration, AutoTokenizer, AutoProcessor
from qwen_vl_utils import process_vision_info
import torch
from service.vision.base_vision_service import BaseVisionService
import base64
from conf.config import Config
import logging


class QwenVisionService(BaseVisionService):

  def __init__(self) -> None:
    config = Config()
    api_logger_name = config.find("logs.api_logger.name")
    base_logger = logging.getLogger(api_logger_name)
    base_logger.info("Loading Qwen2-VL model...")
    model_name = config.find("vision_model")
    self.gpu_type = config.find("gpu_type")
    if self.gpu_type == "cuda":
      device_map = "auto"
    else:
      device_map = {"": "mps"}

    self.model = Qwen2VLForConditionalGeneration.from_pretrained(
      model_name, torch_dtype=torch.bfloat16, device_map=device_map, offload_buffers=True
    )
    # self.tokenizer = AutoTokenizer.from_pretrained(model_name)
    self.processor = AutoProcessor.from_pretrained(model_name, trust_remote_code=True, device_map=device_map)


  def extract(self, image=None, image_bytes: bytes | None = None, download_url: str | None = None):

    try:
      content_messages = []
      content_messages.append({"type": "text", "text": "Extract the meter readings from the image"})

      if download_url:
          content_messages.append({"type": "image", "image_url": {"url": download_url}})
      elif image:
          content_messages.append({"type": "image", "image": image})
      elif image_bytes:
          base64_image = base64.b64encode(image_bytes).decode('utf-8')
          url = f"data:image/png;base64,{base64_image}"
          content_messages.append({"type": "image", "image": url})

      messages = []
      messages.append({"role": "system", "content": [{"type": "text", "text": self.system_context()}]})
      messages.append({"role": "user", "content": content_messages})

      # Preparation for inference
      text = self.processor.apply_chat_template(
          messages, tokenize=False, add_generation_prompt=True
      )
      image_inputs, video_inputs = process_vision_info(messages)
      inputs = self.processor(
          text=[text],
          images=image_inputs,
          videos=video_inputs,
          padding=True,
          return_tensors="pt",
      )
      inputs = inputs.to(self.gpu_type)

      # Inference: Generation of the output
      generated_ids = self.model.generate(**inputs, max_new_tokens=500)
      generated_ids_trimmed = [
          out_ids[len(in_ids):] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
      ]
      output_text = self.processor.batch_decode(
          generated_ids_trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False
      )
      result = output_text[0].replace('\\n', '\n')

    except Exception as e:
      raise e

    return result
