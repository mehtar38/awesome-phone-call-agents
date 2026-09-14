"""
All Gemini API calls live here, kept server-side on purpose: an API key must
never sit in browser JS where anyone can read it out of dev tools. Two jobs:

1. extract_card_fields() -- reads an ID or insurance card photo, returns the
   structured profile fields. Replaces an earlier plan to do this client-side
   with Tesseract.js OCR + regex; once a Gemini-backed endpoint existed anyway
   for booking-transcript parsing, reusing it here is both less code and much
   more reliable than parsing raw OCR text with hand-written regexes.

2. parse_booking_transcript() -- reads the confirmed sign/typed transcript,
   resolves relative day references ("Tuesday") against today's real date,
   and returns the structured availability the confirm screen shows back to
   the user for a final check before it's ever sent to the backend.

Uses the current `google-genai` SDK (the package Google actively supports --
the older `google-generativeai` package's support has ended). Model is
gemini-2.5-flash: current, multimodal (needed for the card-image job), and
fast/cheap enough for both of these small, low-latency calls.

NOTE: this sandbox's network is blocked from reaching Google's API, so
neither function has been exercised against a live key here -- the SDK call
shapes below were checked directly against the installed google-genai
package (Client.models.generate_content, types.Part.from_bytes), not
guessed, but a real GEMINI_API_KEY is still needed to confirm end-to-end
behavior. Smoke-test both endpoints before the demo.
"""
import json
import os

from google import genai
from google.genai import types

_client = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))
    return _client


MODEL_NAME = "gemini-2.5-flash"


def _strip_code_fence(text: str) -> str:
    """Gemini often wraps JSON answers in ```json ... ``` even when told not
    to -- strip that before json.loads() instead of fighting the prompt over it."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.lower().startswith("json"):
            text = text[4:]
    return text.strip()


CARD_PROMPT = """This image is a government ID card or a health insurance card.
Extract whichever of the following fields are visible on THIS card. Return
STRICT JSON only, no prose before or after it, matching this schema exactly:

{
  "name": "",
  "age": null,
  "dob": "",
  "address": "",
  "insurance_name": "",
  "insurance_id": ""
}

Rules:
- "dob" must be in YYYY-MM-DD format if a date of birth is visible, else "".
- "age" is an integer only if an age (not DOB) is directly printed on the card, else null.
- Leave any field not visible on this specific card as "" (or null for age) --
  never guess, invent, or infer a value that is not actually printed on the card.
- Output only the JSON object, nothing else.
"""


def extract_card_fields(image_bytes: bytes, mime_type: str = "image/jpeg") -> dict:
    client = _get_client()
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=[
            CARD_PROMPT,
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
        ],
    )
    return json.loads(_strip_code_fence(response.text))


BOOKING_PROMPT_TEMPLATE = """You are parsing a transcript of ASL-to-text (or typed)
input from a deaf/hard-of-hearing user booking a medical appointment.

Today's date is {today_iso} ({today_weekday}).

Transcript: "{transcript}"

The user may reference a day of the week (resolve it to the NEXT occurrence of
that day on or after today -- if they say a day that IS today, use today's
date) and one or more specific clock times (not ranges -- e.g. "3", "4:30",
"2pm" are single timestamps, not time spans). Return STRICT JSON only, no
prose before or after it, matching this schema exactly:

{{
  "availability": [
    {{"day": "Tuesday", "date": "YYYY-MM-DD", "time": "HH:MM"}}
  ],
  "clinic_type_hint": "physical_therapy" | "mental_health" | "general" | "ent" | "dentist" | null,
  "wants_interpreter": true | false | null
}}

- "time" must be 24-hour HH:MM.
- If no day is mentioned at all, use today's date for every timestamp.
- If a field can't be determined from the transcript, use null (or an empty
  list for availability) -- never guess.
- Output only the JSON object, nothing else.
"""


def parse_booking_transcript(transcript: str, today_iso: str, today_weekday: str) -> dict:
    prompt = BOOKING_PROMPT_TEMPLATE.format(
        transcript=transcript, today_iso=today_iso, today_weekday=today_weekday,
    )
    client = _get_client()
    response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
    return json.loads(_strip_code_fence(response.text))
