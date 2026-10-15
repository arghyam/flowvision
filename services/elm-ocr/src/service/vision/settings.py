"""Reader settings. Built from conf/config.yaml plus environment overrides by conf.config.load_settings()."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ReaderSettings:
    openai_api_key: str
    model: str = "gpt-6-sol"
    reasoning_effort: str | None = "high"      # None for models without reasoning
    image_detail: str = "high"
    reads: int = 1                              # with 2+, every read must agree exactly (costs more)
    min_confidence: float = 0.90                # per-read confidence below this -> NEEDS_REVIEW
    clear_read_confidence: float = 0.95         # accept without a meter type or previous reading at/above this
    max_daily_increase: float = 300.0           # units/day above the previous reading -> NEEDS_REVIEW
    openai_base_url: str = "https://api.openai.com/v1"
    request_timeout: float = 90.0               # longest wait for one model answer (also capped by the deadline)
    max_attempts: int = 2                       # model calls per read: 1 retry, only for errors that were not billed
    max_image_bytes: int = 20 * 1024 * 1024
    max_image_side: int = 2048                  # longer side is downscaled to this
    max_image_pixels: int = 16_000_000          # PNG/WebP over this are refused before decoding (memory protection)
    max_jpeg_pixels: int = 250_000_000          # JPEGs decode at reduced size, so larger phone photos are fine
    timeout_cost_usd: float = 0.03             # counted against the budget when OpenAI times out (it may still bill)
    meter_types_file: str | None = None         # optional JSON file adding/overriding meter types
    price_input_per_m: float = 2.0              # USD per 1M tokens, for cost estimates only
    price_output_per_m: float = 10.0
