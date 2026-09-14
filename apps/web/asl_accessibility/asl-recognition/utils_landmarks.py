"""
Shared landmark extraction + normalization, used by both extract_landmarks.py
(offline batch processing of the training set) and infer.py (single video /
webcam inference), so training and inference features are guaranteed to match.

Uses mediapipe's Tasks API (HandLandmarker + PoseLandmarker). NOTE: an earlier
version of this file used the legacy `solutions.holistic` API, which is
simpler but was removed from mediapipe entirely as of ~0.10.18+ -- it's gone
for good in current releases (confirmed: Colab's Python 3.12 runtime only has
wheels for mediapipe >=0.10.30, all of which lack `solutions`). The Tasks API
is the only forward-compatible option now, at the cost of needing two small
model files downloaded once via `download_mediapipe_models.py`.

Feature vector layout per frame (225 dims):
  [0:99]    pose landmarks (33 landmarks x xyz)
  [99:162]  left hand landmarks (21 landmarks x xyz)
  [162:225] right hand landmarks (21 landmarks x xyz)
Plus 2 extra flags appended per frame (227 dims total):
  [225]     left_hand_present (0/1)
  [226]     right_hand_present (0/1)

This layout is unchanged from the Holistic-based version, so dataset.py,
models.py, and train.py did not need any changes.

Normalization: translate so the midpoint of the shoulders (pose landmarks 11,
12) is the origin, then scale by shoulder width. This makes the features
roughly invariant to the signer's distance from the camera and position in
frame. Hand landmarks are normalized using the SAME shoulder-based origin/
scale (not their own wrist), so relative hand position/motion relative to the
body is preserved -- this matters for signs where hand position relative to
the body is part of the meaning.
"""
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision

N_POSE, N_HAND = 33, 21
FEATURE_DIM = N_POSE * 3 + N_HAND * 3 + N_HAND * 3 + 2  # 227

LEFT_SHOULDER, RIGHT_SHOULDER = 11, 12

DEFAULT_HAND_MODEL = "models/hand_landmarker.task"
DEFAULT_POSE_MODEL = "models/pose_landmarker.task"


class Landmarkers:
    """Owns one HandLandmarker + one PoseLandmarker in VIDEO mode. Create one
    instance per script run (not per video) and reuse it -- model loading has
    real overhead, and VIDEO mode just needs a strictly-increasing timestamp
    per call, which this tracks internally across however many frames/videos
    you feed it, so there's no need to recreate it between videos."""

    def __init__(self, hand_model_path: str = DEFAULT_HAND_MODEL,
                 pose_model_path: str = DEFAULT_POSE_MODEL):
        self.hand = mp_vision.HandLandmarker.create_from_options(
            mp_vision.HandLandmarkerOptions(
                base_options=mp_python.BaseOptions(model_asset_path=hand_model_path),
                running_mode=mp_vision.RunningMode.VIDEO,
                num_hands=2,
            )
        )
        self.pose = mp_vision.PoseLandmarker.create_from_options(
            mp_vision.PoseLandmarkerOptions(
                base_options=mp_python.BaseOptions(model_asset_path=pose_model_path),
                running_mode=mp_vision.RunningMode.VIDEO,
                num_poses=1,
            )
        )
        self._ts_ms = 0

    def process_frame(self, rgb_frame: np.ndarray) -> np.ndarray:
        """rgb_frame: HxWx3 uint8, RGB order (cv2 reads BGR -- convert first)."""
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
        self._ts_ms += 1  # only needs to strictly increase call over call
        hand_result = self.hand.detect_for_video(mp_image, self._ts_ms)
        pose_result = self.pose.detect_for_video(mp_image, self._ts_ms)
        return extract_frame_features(hand_result, pose_result)

    def close(self):
        self.hand.close()
        self.pose.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _landmarks_to_array(landmarks_list, n_points):
    if not landmarks_list:
        return np.zeros((n_points, 3), dtype=np.float32)
    return np.array([[lm.x, lm.y, lm.z] for lm in landmarks_list], dtype=np.float32)


def extract_frame_features(hand_result, pose_result) -> np.ndarray:
    """Convert one frame's HandLandmarkerResult + PoseLandmarkerResult into a
    normalized flat feature vector."""
    pose_lms = pose_result.pose_landmarks[0] if pose_result.pose_landmarks else None
    pose = _landmarks_to_array(pose_lms, N_POSE)

    left_lms, right_lms = None, None
    for lms, handedness in zip(hand_result.hand_landmarks, hand_result.handedness):
        label = handedness[0].category_name  # "Left" or "Right"
        if label == "Left":
            left_lms = lms
        else:
            right_lms = lms

    left_hand = _landmarks_to_array(left_lms, N_HAND)
    right_hand = _landmarks_to_array(right_lms, N_HAND)
    left_present = float(left_lms is not None)
    right_present = float(right_lms is not None)

    if pose_lms is not None:
        origin = (pose[LEFT_SHOULDER] + pose[RIGHT_SHOULDER]) / 2.0
        scale = np.linalg.norm(pose[LEFT_SHOULDER] - pose[RIGHT_SHOULDER])
        scale = scale if scale > 1e-6 else 1.0
    else:
        origin = np.zeros(3, dtype=np.float32)
        scale = 1.0

    pose_n = (pose - origin) / scale
    left_n = (left_hand - origin) / scale
    right_n = (right_hand - origin) / scale

    # zero out hands that weren't detected (avoids a spurious "origin" reading)
    left_n = left_n if left_present else np.zeros_like(left_n)
    right_n = right_n if right_present else np.zeros_like(right_n)

    feat = np.concatenate([
        pose_n.flatten(), left_n.flatten(), right_n.flatten(),
        [left_present, right_present],
    ]).astype(np.float32)
    return feat


def sample_or_pad(sequence: np.ndarray, target_len: int) -> np.ndarray:
    """Uniformly resample a (T, D) sequence to exactly target_len frames.
    Short sequences are upsampled (repeated indices); long ones downsampled.
    This keeps every training example the same shape without a padding mask,
    which keeps the model code simple -- fine for short isolated-word clips."""
    t = sequence.shape[0]
    if t == 0:
        return np.zeros((target_len, sequence.shape[1] if sequence.ndim > 1 else FEATURE_DIM), dtype=np.float32)
    idx = np.linspace(0, t - 1, target_len).round().astype(int)
    return sequence[idx]


def mirror_features(feat_seq: np.ndarray) -> np.ndarray:
    """Horizontal-flip augmentation: valid because ASL signs are performed
    correctly by either a left- or right-hand-dominant signer -- a mirrored
    right-handed video is what the same sign looks like from a left-handed
    signer. Negates the x-axis and swaps the left/right hand feature blocks.
    Does NOT swap pose left/right landmark indices (approximation -- fine for
    this hackathon's timeline; pose mostly contributes coarse body position
    here, not fine left/right distinctions)."""
    out = feat_seq.copy()
    pose = out[:, 0:99].reshape(-1, N_POSE, 3)
    left = out[:, 99:162].reshape(-1, N_HAND, 3)
    right = out[:, 162:225].reshape(-1, N_HAND, 3)
    flags = out[:, 225:227]

    pose[:, :, 0] *= -1
    left[:, :, 0] *= -1
    right[:, :, 0] *= -1

    new_left, new_right = right, left
    new_flags = flags[:, [1, 0]]

    return np.concatenate([
        pose.reshape(len(out), -1),
        new_left.reshape(len(out), -1),
        new_right.reshape(len(out), -1),
        new_flags,
    ], axis=1).astype(np.float32)
