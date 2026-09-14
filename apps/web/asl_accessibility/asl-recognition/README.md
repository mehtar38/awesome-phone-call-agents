# ASL-to-Text model

First component of the CALL-E hackathon accessibility submission: converts ASL
video into English text (a limited domain vocabulary), so a deaf/HoH caller
can drive the appointment-booking workflow. This repo covers *only* the
ASL-to-text piece -- not the interpreter matching / calendar / call workflow.

## Why this approach

The dataset ([`akasheroor/American-Sign-Language-Dataset`](https://huggingface.co/datasets/akasheroor/American-Sign-Language-Dataset))
is 108,618 isolated word/fingerspelling clips across 2,208 words -- not
continuous sentence signing, and not remotely feasible to fully download (it's
~54GB) or train on in a 2-day window. So:

- **Domain-subset vocabulary, not the full 2,208 words.** `vocab.py` has a
  candidate list (~90 words: alphabet, numbers, days, months, time words, and
  appointment-booking domain words) built for what the call workflow actually
  needs. It has NOT been verified against the live dataset yet -- run
  `check_vocab_coverage.py` first.
- **Selective download, not the full dataset.** `download_subset.py` only
  pulls videos for words in `vocab.py`, capped at `MAX_PER_WORD` clips each.
  Rough budget at the defaults: ~90 words x 15 clips x ~5MB =~ 6-7GB.
- **Landmark-based model, not raw video.** MediaPipe (HandLandmarker +
  PoseLandmarker) extracts hand+pose landmark coordinates per frame; the model
  classifies from that ~225-dim sequence rather than raw pixels. This is
  standard practice for isolated sign recognition (see WLASL/MS-ASL
  literature) and trains far faster, on far less data, than a video CNN --
  important given the per-class sample counts here are small either way.

## Setup

**This was built and tested in a sandbox with no internet access to
huggingface.co, so the download/training steps could NOT be run against the
real dataset here** -- only against synthetic data (see "What's been tested"
below). Run everything below in Colab or on a machine with normal internet
access.

```bash
pip install -r requirements.txt
```

Or open `colab_ASL_to_Text_Training.ipynb` in Colab and run cells top to
bottom -- it's the same steps, with a GPU and no local setup needed.

## Pipeline

1. **`check_vocab_coverage.py`** -- confirms which candidate words exist in
   the dataset, with clip counts. Edit `vocab.py` based on the output.
2. **`download_subset.py`** -- downloads only the needed video files (not the
   full dataset) into `data/videos/<word>/`.
3. **`download_mediapipe_models.py`** -- one-time download (~10-15MB) of the
   HandLandmarker/PoseLandmarker model files (see "Known constraints" below
   for why this step exists).
4. **`extract_landmarks.py`** -- runs MediaPipe over every clip, caches
   normalized landmark features to `data/features/*.npy`. This is the slow
   step; it's a one-time cost, cached so you can iterate on the model without
   re-running it.
5. **`train.py --model baseline`** then **`train.py --model gru`** -- trains
   the pooled-MLP baseline (fast sanity check) and then the BiGRU+attention
   sequence model (the real submission model). Both use a stratified
   train/val split and class-weighted loss since per-word clip counts vary.
6. **`infer.py --video <path>`** -- runs the trained model on a new clip,
   prints top-k predicted words + confidence. This is the function signature
   the rest of the call-agent workflow will eventually call per signed
   utterance.
7. **`app.py`** -- a FastAPI server exposing the model over HTTP AND
   WebSocket, and serving the camera UI (`static/index.html`) at `/`. Run
   `uvicorn app:app --host 0.0.0.0 --port 8000`, open `http://localhost:8000/`,
   click "Start camera" once, then just sign naturally -- no per-word button
   press. See "How live recognition actually works" below for how that's
   possible given the model is trained on isolated word clips, not sentences.

## How live recognition actually works

The model classifies one isolated sign at a time -- that's what the dataset
and training are built on. But the UI can't reasonably ask a user to record
a separate timed clip per word; a real accessibility tool has to let someone
sign continuously, at their own pace. The gap between those two is bridged
by `segmenter.py`:

- The browser streams a continuous sequence of frames to the server over a
  WebSocket (`/ws/stream`), not a single video file.
- The server runs the same MediaPipe landmark extraction on every frame
  (`utils_landmarks.Landmarkers`, unchanged from the batch pipeline).
- `segmenter.SignSegmenter` watches the hand-presence flag already built into
  each frame's features and decides where one sign ends and the next begins:
  a segment closes once hands have been absent (dropped to rest, out of
  frame) for `ABSENT_FRAMES_FOR_BOUNDARY` consecutive frames. This mirrors
  how the training clips themselves are structured (hand enters frame,
  signs, leaves/rests) rather than relying on motion magnitude, which would
  fail on signs that are mostly a held handshape.
- The instant a segment closes, it's classified with the exact same model
  and features as `infer.py` -- `infer.classify_features()` is shared code,
  not a reimplementation.

Be clear about the honest scope of this: it is NOT continuous sign language
recognition in the research sense (fluid, coarticulated sentence signing).
That needs continuous sentence-level training data, which this dataset
doesn't have. What this is: continuous capture with automatic per-sign
segmentation, so the user experience is "just sign," while the underlying
model still only ever sees one isolated sign at a time -- which is what it
was actually trained on.

**This has not been tuned against a real camera or a real signer** (this
sandbox has neither). `segmenter.py`'s docstring flags exactly which
constant to adjust and in which direction if signs get cut in half or two
signs get merged together -- expect to spend real time on this once you're
testing live, it's the one piece that couldn't be verified without a camera.

## What's been tested

The original Holistic-based version of this pipeline was run end-to-end
against synthetic data (video generation -> extraction -> feature caching ->
stratified split -> both models training -> checkpoint save/load ->
inference) and passed. After switching to the Tasks API (see below), the
feature-building logic (`extract_frame_features`, hand left/right assignment,
normalization, the empty-detection case) was re-verified with mocked
landmarker outputs, but **the full pipeline has NOT been re-run end-to-end
against real MediaPipe output** -- this sandbox is blocked from downloading
the Tasks API's model files (storage.googleapis.com), same as it's blocked
from huggingface.co. Everything downstream of `extract_frame_features`
(dataset/model/train/infer) is unchanged from the version that did pass the
full synthetic test.

**Run `smoke_test.py` in Colab/local first** (after `download_mediapipe_models.py`)
to confirm the pipeline actually works end-to-end with real MediaPipe output
before spending time on the real dataset download.

`app.py`'s HTTP layer (`/health`, `/predict`, static UI serving) and its
WebSocket streaming protocol (`/ws/stream`) were both verified with a mocked
model and a mocked `Landmarkers`, since this sandbox can't run the real
model or real MediaPipe end-to-end. The WebSocket test specifically checked:
a scripted present/absent frame sequence produces the correct `status`
messages at the right moments, a segment boundary produces exactly one
`word` message with the mocked model's output, and all of this coexists
correctly with the `/health` and static-file routes on the same app.
`segmenter.SignSegmenter` itself was unit-tested directly (no camera needed
for this part): idle-only input never triggers, a full sign gets correctly
trimmed and flushed, a brief detection blip mid-sign does NOT split it into
two, too-short noise gets discarded, and the max-length safety cap fires
when a boundary never comes. Separately confirmed with a real ffmpeg-encoded
file: OpenCV decodes VP8-in-webm and individual JPEG frames correctly, so
both the old file-upload path and the new frame-streaming path are sound at
the format level.

What's NOT yet verified, because it requires a camera and a real trained
checkpoint (neither available here): the actual segmentation thresholds
against a real signer, and the full loop (browser camera -> WebSocket ->
MediaPipe -> segmenter -> real model -> UI) end to end. Do that first, with
`ABSENT_FRAMES_FOR_BOUNDARY` as the main dial to adjust, before relying on
this for a demo.

## Known constraints / things to revisit if time allows

- **MediaPipe uses the Tasks API** (`HandLandmarker` + `PoseLandmarker` in
  `utils_landmarks.Landmarkers`), not the older `solutions.holistic` API.
  Holistic was simpler (one call, no separate model files) but is gone for
  good in current mediapipe releases -- confirmed Colab's Python 3.12 runtime
  only has wheels for mediapipe >=0.10.30, none of which have `solutions`.
  Run `download_mediapipe_models.py` once before extraction/inference/smoke
  test -- it fetches the two small model files the Tasks API needs.
- **Fixed 32-frame resampling** (`dataset.SEQ_LEN`) rather than variable-length
  sequences with padding/masking -- simpler code, works fine for short
  isolated-word clips, but loses some temporal detail on longer clips.
- **Mirror augmentation is off by default** (`--mirror-prob 0.0`). It's
  implemented (`utils_landmarks.mirror_features`) and is a real accessibility
  win if it works (handedness invariance), but hasn't been validated against
  real data yet -- try `--mirror-prob 0.5` and compare val accuracy once you
  have real training data.
- **Pose landmarks aren't left/right-swapped on mirror**, only hand blocks are
  (see docstring) -- an approximation that's fine for now but worth revisiting
  if pose position turns out to matter a lot for your chosen vocab.
- No face landmarks are used, even though a few ASL signs incorporate facial
  expression -- left out to keep the feature vector small and extraction fast
  given the time budget.
