"""
Run the trained model on a single video file, or (classify_features, used by
app.py's live streaming endpoint) on an already-extracted landmark sequence.
This is the function the rest of the call-agent pipeline calls per ASL sign.

Usage:
    python infer.py --video path/to/clip.mp4 --model gru
    python infer.py --video path/to/clip.mp4 --model gru --topk 5
"""
import argparse
from functools import lru_cache

import cv2
import numpy as np
import torch

from dataset import load_label_map
from models import BiGRUAttention, PooledMLP
from utils_landmarks import FEATURE_DIM, Landmarkers, sample_or_pad

SEQ_LEN = 32


def extract_features_from_video(path: str) -> np.ndarray:
    cap = cv2.VideoCapture(path)
    feats = []
    with Landmarkers() as landmarkers:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            feats.append(landmarkers.process_frame(rgb))
    cap.release()
    if not feats:
        raise ValueError(f"No frames could be read from {path}")
    return np.stack(feats)


def load_model(model_name: str, n_classes: int, checkpoint: str, device):
    model = PooledMLP(FEATURE_DIM, n_classes) if model_name == "baseline" else BiGRUAttention(FEATURE_DIM, n_classes)
    model.load_state_dict(torch.load(checkpoint, map_location=device))
    model.to(device)
    model.eval()
    return model


@lru_cache(maxsize=4)
def _load_cached(model_name: str, checkpoint: str, label_map_path: str):
    """Cached so repeated calls (e.g. one per detected sign in a live stream)
    don't reload weights from disk every time -- loaded once, reused."""
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    label_to_idx = load_label_map(label_map_path)
    idx_to_label = {v: k for k, v in label_to_idx.items()}
    model = load_model(model_name, len(label_to_idx), checkpoint, device)
    return model, idx_to_label, device


def classify_features(feats: np.ndarray, model_name: str = "gru", checkpoint: str = None,
                       label_map_path: str = "label_map.json", topk: int = 3):
    """Run the trained model on an already-extracted (T, FEATURE_DIM) landmark
    sequence -- what app.py's live streaming endpoint calls, since it already
    has per-frame features from Landmarkers and doesn't need to re-extract
    from a video file the way predict() below does."""
    checkpoint = checkpoint or f"model_{model_name}_best.pt"
    model, idx_to_label, device = _load_cached(model_name, checkpoint, label_map_path)

    feats = sample_or_pad(feats, SEQ_LEN)
    x = torch.from_numpy(feats).float().unsqueeze(0).to(device)  # (1, T, D)

    with torch.no_grad():
        logits = model(x)
        probs = torch.softmax(logits, dim=1).squeeze(0)

    top_probs, top_idx = probs.topk(min(topk, len(idx_to_label)))
    return [(idx_to_label[i.item()], p.item()) for p, i in zip(top_probs, top_idx)]


def predict(video_path: str, model_name: str = "gru", checkpoint: str = None,
            label_map_path: str = "label_map.json", topk: int = 3):
    feats = extract_features_from_video(video_path)
    return classify_features(feats, model_name, checkpoint, label_map_path, topk)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--model", choices=["baseline", "gru"], default="gru")
    ap.add_argument("--checkpoint", default=None)
    ap.add_argument("--topk", type=int, default=3)
    args = ap.parse_args()

    results = predict(args.video, args.model, args.checkpoint, topk=args.topk)
    print("\nTop predictions:")
    for word, prob in results:
        print(f"  {word:15s} {prob:.3f}")
