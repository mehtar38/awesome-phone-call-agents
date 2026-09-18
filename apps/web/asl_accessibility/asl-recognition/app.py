"""
FastAPI server: wraps infer.predict() behind an HTTP endpoint AND serves the
minimal camera UI (static/index.html) from the same process, so there's only
one thing to run for the whole demo.

This is the integration seam: your model work (Colab) and the UI work can now
proceed independently. The UI only needs to know one contract -- POST a
video, get back predicted words -- regardless of what changes on the model
side later (baseline -> gru, vocab changes, retraining, etc.).

Run this wherever the trained checkpoint + label_map.json live:

    pip install fastapi uvicorn python-multipart
    uvicorn app:app --host 0.0.0.0 --port 8000

Then open http://localhost:8000/ for the camera UI, or call the API directly:
    POST /predict   (multipart/form-data, field name "video", a video file)
    -> {"predictions": [{"word": "doctor", "confidence": 0.83}, ...]}

If you're running this inside Colab for the demo (simplest option under time
pressure), expose it with a tunnel so it's reachable from a real browser
(Colab's own environment has no camera), e.g.:
    !npx localtunnel --port 8000
or ngrok, if your team already has it set up. getUserMedia (camera access)
requires either localhost or https -- a plain http tunnel URL will NOT let
the browser grant camera access, so make sure whatever tunnel you use serves
https (localtunnel and ngrok both do by default).
"""
import json
import os
import re
import shutil
import tempfile
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import cv2
import numpy as np
from fastapi import Body, FastAPI, File, HTTPException, Path as PathParam, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import db
from gemini_client import extract_card_fields, parse_booking_transcript
from infer import classify_features, predict
from segmenter import SignSegmenter
from utils_landmarks import Landmarkers

# Where the completed booking request is forwarded -- the clinic/interpreter
# matching workflow (calling clinics, checking interpreter availability,
# confirming and calendaring) lives entirely outside this app; set this to
# that workflow's intake endpoint.
BOOKING_WORKFLOW_URL = os.environ.get("BOOKING_WORKFLOW_URL", "")

# Must match the workflow's JSON schema exactly -- see submit_booking_endpoint().
WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

app = FastAPI(title="ASL-to-text inference + booking app")

# Wide open for hackathon speed. Tighten this if it ever runs anywhere but a demo.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    db.init_db()

# Change to "baseline" if that's the checkpoint you actually have saved --
# whatever you trained and downloaded, this must match its filename.
MODEL_NAME = "gru"
CHECKPOINT = f"model_{MODEL_NAME}_best.pt"
LABEL_MAP = "label_map.json"


@app.get("/health")
def health():
    return {
        "status": "ok" if Path(CHECKPOINT).exists() else "missing_checkpoint",
        "model": MODEL_NAME,
        "checkpoint": CHECKPOINT,
    }


@app.post("/predict")
async def predict_endpoint(video: UploadFile = File(...), topk: int = 3):
    if not Path(CHECKPOINT).exists():
        raise HTTPException(500, f"checkpoint not found: {CHECKPOINT} (wrong MODEL_NAME, or file not uploaded here?)")
    if not Path(LABEL_MAP).exists():
        raise HTTPException(500, f"label map not found: {LABEL_MAP}")

    suffix = Path(video.filename or "").suffix or ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        shutil.copyfileobj(video.file, tmp)
        tmp_path = tmp.name

    try:
        results = predict(tmp_path, model_name=MODEL_NAME, checkpoint=CHECKPOINT,
                           label_map_path=LABEL_MAP, topk=topk)
    except Exception as e:
        raise HTTPException(500, f"inference failed: {e}")
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return {"predictions": [{"word": w, "confidence": p} for w, p in results]}


@app.websocket("/ws/stream")
async def stream_endpoint(websocket: WebSocket):
    """The real UI's data path: the browser sends one JPEG frame at a time,
    continuously, for as long as the camera is on. This holds a Landmarkers
    + SignSegmenter per connection, and pushes a message back the instant it
    has something to say -- a status change (signing started/stopped, purely
    for the UI's live visual indicator) or a classified word.

    Protocol:
      client -> server: binary WebSocket messages, each one JPEG-encoded frame
      server -> client: JSON messages,
        {"type": "status", "signing": true|false}
        {"type": "word", "word": "...", "confidence": 0.0-1.0}
        {"type": "error", "message": "..."}
    """
    await websocket.accept()

    if not Path(CHECKPOINT).exists() or not Path(LABEL_MAP).exists():
        await websocket.send_json({"type": "error", "message": f"missing {CHECKPOINT} or {LABEL_MAP} on the server"})
        await websocket.close()
        return

    landmarkers = Landmarkers()
    segmenter = SignSegmenter()
    was_active = False

    try:
        while True:
            data = await websocket.receive_bytes()
            frame = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                continue  # corrupt/partial frame -- skip it, don't kill the connection over one bad frame

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            feat = landmarkers.process_frame(rgb)
            segment = segmenter.add_frame(feat)

            is_active = segmenter.is_active()
            if is_active != was_active:
                await websocket.send_json({"type": "status", "signing": is_active})
                was_active = is_active

            if segment is not None:
                try:
                    results = classify_features(segment, model_name=MODEL_NAME,
                                                 checkpoint=CHECKPOINT, label_map_path=LABEL_MAP, topk=1)
                except Exception as e:
                    await websocket.send_json({"type": "error", "message": f"inference failed: {e}"})
                    continue
                if results:
                    word, confidence = results[0]
                    await websocket.send_json({"type": "word", "word": word, "confidence": confidence})
    except WebSocketDisconnect:
        pass
    finally:
        landmarkers.close()


@app.post("/extract-card")
async def extract_card_endpoint(image: UploadFile = File(...)):
    """One ID or insurance card photo in, best-effort structured fields out.
    Called twice by the frontend (once per card) during profile setup. Never
    guesses a field that isn't visible on that specific card -- see the
    prompt in gemini_client.py -- so a mostly-empty result back is expected
    and normal, not a bug; the verify screen right after this is where the
    user fills in whatever didn't come through."""
    if not os.environ.get("GEMINI_API_KEY"):
        raise HTTPException(500, "GEMINI_API_KEY is not set on the server")
    image_bytes = await image.read()
    mime_type = image.content_type or "image/jpeg"
    try:
        fields = extract_card_fields(image_bytes, mime_type=mime_type)
    except Exception as e:
        raise HTTPException(500, f"card extraction failed: {e}")
    return fields


@app.post("/parse-booking")
async def parse_booking_endpoint(payload: dict = Body(...)):
    """Confirmed sign/typed transcript in, structured availability out. Today's
    real date is injected server-side (never trust a client-supplied date for
    this) so "Tuesday" resolves to an actual calendar date the confirm screen
    can show the user for a sanity check before anything is booked."""
    if not os.environ.get("GEMINI_API_KEY"):
        raise HTTPException(500, "GEMINI_API_KEY is not set on the server")
    transcript = (payload or {}).get("transcript", "").strip()
    if not transcript:
        raise HTTPException(400, "transcript is required")
    today = date.today()
    try:
        parsed = parse_booking_transcript(
            transcript, today_iso=today.isoformat(), today_weekday=today.strftime("%A"),
        )
    except Exception as e:
        raise HTTPException(500, f"booking parse failed: {e}")
    return parsed


@app.get("/profile/{user_id}")
def get_profile_endpoint(user_id: str = PathParam(...)):
    """Returns null fields (not a 404) for an unknown user_id -- the frontend
    treats 'no profile yet' as the normal first-visit case, not an error."""
    profile = db.get_profile(user_id)
    return {"exists": profile is not None, "profile": profile}


@app.post("/profile/{user_id}")
def save_profile_endpoint(user_id: str = PathParam(...), profile: dict = Body(...)):
    """Upsert -- called once after profile+family verification, and again any
    time the user chooses to edit their saved profile later. Fields not
    provided are left as null/empty; the frontend always sends the full
    profile object it's currently holding, never a partial patch."""
    db.save_profile(user_id, profile)
    return {"status": "saved"}


def _format_phone_e164(raw: str, *, who: str) -> str:
    """'(555) 123-4567' / '555-123-4567' / '15551234567' -> '+15551234567'.
    Raises with a message naming which field was bad, not just that
    something was -- the frontend surfaces this string straight to the user."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError(f"{who} phone number needs to be a 10-digit US number")
    return f"+1{digits}"


def _format_time_12h(hhmm: str) -> str:
    """'14:00' -> '2PM', '16:30' -> '4:30PM' -- the 24h HH:MM values our own
    <input type=time"> and Gemini both produce, reshaped into the workflow's
    '2PM' / '4:30PM' pattern."""
    try:
        h, m = hhmm.split(":")
        h, m = int(h), int(m)
    except (ValueError, AttributeError):
        raise ValueError(f"'{hhmm}' is not a valid time")
    period = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    minute_part = "" if m == 0 else f":{m:02d}"
    return f"{h12}{minute_part}{period}"


def _weekday_name(date_str: str) -> str:
    """Always recompute the weekday from the actual date rather than trust
    whatever display label the frontend or Gemini attached to it -- that
    label is decorative UI text (e.g. "Tuesday, Sep 15"), not validated data."""
    try:
        return WEEKDAY_NAMES[date.fromisoformat(date_str).weekday()]
    except ValueError:
        raise ValueError(f"'{date_str}' is not a valid date (expected YYYY-MM-DD)")


@app.post("/submit-booking")
def submit_booking_endpoint(payload: dict = Body(...)):
    """Takes the confirm screen's payload (our own internal field names),
    reshapes it into the exact JSON schema the booking workflow expects, and
    forwards it there over HTTP. Nothing is persisted locally -- there's no
    "past bookings" screen in this app, so a local copy would just be dead
    data; the workflow is the system of record from here on."""
    if not BOOKING_WORKFLOW_URL:
        raise HTTPException(500, "BOOKING_WORKFLOW_URL is not set on the server")

    required = ["user_id", "name", "phone", "dob", "zipcode", "insurance_name",
                "insurance_id", "clinic_type", "availability", "has_interpreter"]
    missing = [f for f in required if payload.get(f) in (None, "", [])]
    if missing:
        raise HTTPException(400, f"missing required field(s): {', '.join(missing)}")

    try:
        dob = date.fromisoformat(payload["dob"])
    except ValueError:
        raise HTTPException(400, f"'{payload['dob']}' is not a valid date of birth (expected YYYY-MM-DD)")
    today = date.today()
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))

    try:
        workflow_payload = {
            "name": payload["name"],
            "date_of_birth": payload["dob"],
            "age": age,
            "phone_number": _format_phone_e164(payload["phone"], who="Your"),  # -> "Your phone number ..."
            "zipcode": payload["zipcode"],
            "appointment_type": payload["clinic_type"],
            "insurance": {
                "provider_name": payload["insurance_name"],
                "policy_number": payload["insurance_id"],
            },
            "availability": [
                {
                    "day": _weekday_name(a["date"]),
                    "date": a["date"],
                    "time": _format_time_12h(a["time"]),
                }
                for a in payload["availability"]
            ],
            "has_interpreter": bool(payload["has_interpreter"]),
        }
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))

    if not workflow_payload["has_interpreter"]:
        try:
            workflow_payload["family_members"] = [
                {
                    "name": m["name"],
                    "relation": m["relation"],
                    "phone_number": _format_phone_e164(m["phone"], who=f"{m.get('name') or 'Family member'}'s"),
                }
                for m in (payload.get("family") or [])
            ]
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e))

    # BOOKING_WORKFLOW_URL must point at POST /runs specifically (not just the
    # host:port) -- that's the one endpoint on the workflow's server that
    # accepts this JSON body. It returns 202 with a run_id THE WORKFLOW
    # generates; we never send one ourselves, only get one back.
    body = json.dumps(workflow_payload).encode("utf-8")
    req = urllib.request.Request(
        BOOKING_WORKFLOW_URL, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            reason = json.loads(raw).get("detail", raw)
        except (json.JSONDecodeError, AttributeError):
            reason = raw
        if e.code == 409:
            # The workflow runs one booking at a time by design (overlapping
            # runs would place overlapping real phone calls) -- this is an
            # expected, retryable state, not a bug.
            raise HTTPException(409, f"A booking is already being processed by the workflow -- try again shortly. ({reason})")
        raise HTTPException(502, f"booking workflow rejected the request ({e.code}): {reason}"[:400])
    except urllib.error.URLError as e:
        raise HTTPException(502, f"could not reach the booking workflow at {BOOKING_WORKFLOW_URL}: {e.reason}")

    try:
        run = json.loads(resp_body)
    except json.JSONDecodeError:
        return {"status": "sent", "workflow_response": resp_body[:2000]}

    # run_id/plan flow straight back to the confirm screen's result: the plan
    # text is the actual informed-consent disclosure (per the workflow's own
    # docs, submitting IS the consent -- there's no second confirm step), so
    # it needs to reach the user, not just get logged here.
    return {
        "status": "sent",
        "run_id": run.get("run_id"),
        "run_status": run.get("status"),
        "mock_mode": run.get("mock_mode"),
        "plan": run.get("plan"),
    }


# Serves static/index.html at GET / (and any other file under static/).
# Mounted last so it doesn't shadow the routes above.
app.mount("/", StaticFiles(directory="static", html=True), name="static")