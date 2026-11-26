"""Read box labels / packing lists with the Claude API (outbound call only)."""
import base64
import json
import math
import os

PROMPT = (
    "This image is either one carton label or a packing list for a textiles warehouse. "
    "Extract the items using the record_items tool. Rules: for a carton label return exactly one item, "
    "set cartons to null, and set pcs_per_carton from the quantity printed on the label. "
    "For a packing list return one item per line, where cartons is the number of cartons on that line. "
    "Use null for anything you cannot read and never guess. Text may be rotated or upside down. "
    "If a line clearly matches one of these existing items, use its exact main name: "
)

NULLABLE_STR = {"type": ["string", "null"]}
TOOL = {
    "name": "record_items",
    "description": "Record the stock items read from the image.",
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["label", "packing_list"]},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "colour": NULLABLE_STR,
                        "size": NULLABLE_STR,
                        "lot": NULLABLE_STR,
                        "pcs_per_carton": {"type": ["integer", "null"]},
                        "cartons": {"type": ["integer", "null"]},
                    },
                    "required": ["name"],
                },
            },
        },
        "required": ["kind", "items"],
    },
}


def extract(img: bytes, known: list, key: str = "") -> dict:
    key = key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("No API key saved. Add one under Settings.")
    import anthropic

    # Time out before the app's 60s limit so the user gets a clear message, and don't retry silently.
    client = anthropic.Anthropic(api_key=key, timeout=45.0, max_retries=0)
    resp = client.messages.create(
        model=os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5"),
        max_tokens=4096,
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "record_items"},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": _media(img),
                                             "data": base64.b64encode(img).decode()}},
                {"type": "text", "text": PROMPT + "; ".join(known[:150])},
            ],
        }],
    )
    if getattr(resp, "stop_reason", "") == "max_tokens":  # never hand back half a packing list as if it were all of it
        raise RuntimeError("The list was too long to read in one go. Photograph it in two or more parts.")
    for b in resp.content:
        if b.type == "tool_use":
            out = b.input if isinstance(b.input, dict) else {}
            raw = out.get("items") or []
            if isinstance(raw, str):  # the model sometimes hands the list over as JSON text instead of a list
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raw = []
            if isinstance(raw, dict):
                raw = [raw]
            items = []
            for it in raw if isinstance(raw, list) else []:
                if not isinstance(it, dict):
                    continue
                try:
                    f = float(it.get("cartons") or 0)
                except (TypeError, ValueError):
                    f = 0.0
                if not math.isfinite(f) or abs(f) > 100000:  # 'inf', '1e999': not a carton count, so not a usable line
                    continue
                items.append(dict(it, cartons=max(int(f), 0)))  # whole cartons, decimals rounded down
            return {"kind": out.get("kind", "label"), "items": items}
    raise RuntimeError("Nothing could be read from that photo. Try a closer, sharper photo.")


def _media(b):
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"
