"""
Turns a CONTINUOUS stream of per-frame landmark feature vectors into discrete
"here's one sign" segments, so a user can sign naturally -- at their own
pace, one word after another -- without clicking a button per word.

Be clear about what this is and isn't: this is NOT continuous sign language
recognition (that's a genuinely hard research problem -- coarticulated,
fluid signing, needing continuous sentence-level training data, which this
project's dataset doesn't have -- it's isolated word clips only). What this
IS: continuous capture + automatic segmentation, using the same
hand-presence signal MediaPipe already gives us, so the boundary between
signs is detected instead of manually marked. Each detected segment is then
classified independently by the existing isolated-word model -- same model,
same features, just fed automatically instead of one clip at a time.

Why hand-presence rather than motion: the dataset this model was trained on
is dictionary-style isolated clips -- hands enter frame, perform the sign,
leave/drop out of frame or return to rest. Segmenting on "is a hand visible"
mirrors that structure directly, and it doesn't fail on signs that are mostly
a held handshape with little translation (many fingerspelled letters, for
example) the way a pure motion-magnitude threshold would.

TUNING WARNING: ABSENT_FRAMES_FOR_BOUNDARY below was NOT tuned against a real
camera or a real signer -- this sandbox has neither. It's a reasonable
starting point given a ~7-10fps capture rate (see static/index.html), but
expect to adjust it once you're testing live:
  - Words getting cut in half / one sign split into two -> raise it (the
    signer is pausing mid-sign, e.g. between fingerspelled letters, longer
    than the current threshold allows).
  - Two distinct signs getting merged into one segment -> lower it.
  - MIN_SEGMENT_FRAMES filters out brief false triggers (a hand flickering
    into frame for an instant); MAX_SEGMENT_FRAMES is just a safety net so a
    stuck-open hand can't buffer forever.
"""
import numpy as np

ABSENT_FRAMES_FOR_BOUNDARY = 8   # consecutive no-hand frames => a sign just ended
MIN_SEGMENT_FRAMES = 5           # shorter than this => discard as noise
MAX_SEGMENT_FRAMES = 150         # safety cap regardless of boundary detection
TAIL_TRIM_FRAMES = 2
LEFT_PRESENT_IDX, RIGHT_PRESENT_IDX = 225, 226


def _hands_present(feat: np.ndarray) -> bool:
    return feat[LEFT_PRESENT_IDX] > 0.5 or feat[RIGHT_PRESENT_IDX] > 0.5


class SignSegmenter:
    """Stateful, one instance per active camera session (e.g. per WebSocket
    connection). Feed it one feature vector per frame via add_frame(); it
    returns a stacked (T, FEATURE_DIM) array the instant it decides a
    complete sign just ended, or None on every other frame."""

    def __init__(self):
        self.buffer = []
        self.absent_count = 0

    def add_frame(self, feat: np.ndarray):
        present = _hands_present(feat)

        if present:
            self.absent_count = 0
            self.buffer.append(feat)
        elif self.buffer:
            # a sign is in progress and hands just disappeared/rested --
            # keep buffering briefly in case this is a blip, not a real end
            self.absent_count += 1
            self.buffer.append(feat)
            if self.absent_count >= ABSENT_FRAMES_FOR_BOUNDARY:
                return self._flush()
        # else: idle, no hands, nothing buffered -- nothing to do

        if len(self.buffer) >= MAX_SEGMENT_FRAMES:
            return self._flush()
        return None

    def _flush(self):
        segment = self.buffer
        self.buffer = []
        self.absent_count = 0

        last_present = -1
        for i, f in enumerate(segment):
            if _hands_present(f):
                last_present = i
        segment = segment[: last_present + 1]

        trim = min(TAIL_TRIM_FRAMES, max(0, len(segment) - MIN_SEGMENT_FRAMES))
        if trim > 0:
            segment = segment[:-trim]

        if len(segment) < MIN_SEGMENT_FRAMES:
            return None
        return np.stack(segment)

    def is_active(self) -> bool:
        """True while a sign is currently being buffered -- use this to
        drive a live 'signing detected' indicator in the UI."""
        return len(self.buffer) > 0
