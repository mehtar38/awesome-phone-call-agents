"""
PyTorch Dataset over cached landmark features (data/features.csv from
extract_landmarks.py). Handles: label encoding, fixed-length resampling,
train-time augmentation, and stratified train/val split (important with
very few examples per class -- a plain random split can leave some rare
words with zero validation examples).
"""
import json

import numpy as np
import pandas as pd
import torch
from torch.utils.data import Dataset

from utils_landmarks import FEATURE_DIM, mirror_features, sample_or_pad

SEQ_LEN = 32  # fixed number of frames per clip after resampling


class ASLDataset(Dataset):
    def __init__(self, features_csv: str, label_to_idx: dict, rows: pd.DataFrame = None,
                 augment: bool = False, mirror_prob: float = 0.0, noise_std: float = 0.01):
        self.df = rows if rows is not None else pd.read_csv(features_csv)
        self.label_to_idx = label_to_idx
        self.augment = augment
        self.mirror_prob = mirror_prob
        self.noise_std = noise_std

    def __len__(self):
        return len(self.df)

    def __getitem__(self, i):
        row = self.df.iloc[i]
        feats = np.load(row["npy_path"])  # (T, 227)
        feats = sample_or_pad(feats, SEQ_LEN)

        if self.augment:
            if self.mirror_prob > 0 and np.random.rand() < self.mirror_prob:
                feats = mirror_features(feats)
            if self.noise_std > 0:
                feats = feats + np.random.normal(0, self.noise_std, feats.shape).astype(np.float32)

        label = self.label_to_idx[row["word"]]
        return torch.from_numpy(feats).float(), label


def build_label_map(features_csv: str) -> dict:
    df = pd.read_csv(features_csv)
    words = sorted(df["word"].unique())
    return {w: i for i, w in enumerate(words)}


def stratified_split(features_csv: str, val_frac: float = 0.2, seed: int = 42):
    """Per-class split so every class with >=2 examples gets at least one
    validation example. With small per-class counts this matters a lot more
    than it would on a large balanced dataset."""
    df = pd.read_csv(features_csv)
    rng = np.random.RandomState(seed)
    train_rows, val_rows = [], []
    for word, group in df.groupby("word"):
        group = group.sample(frac=1, random_state=seed)
        n_val = max(1, int(round(len(group) * val_frac))) if len(group) > 1 else 0
        val_rows.append(group.iloc[:n_val])
        train_rows.append(group.iloc[n_val:])
    return pd.concat(train_rows).reset_index(drop=True), pd.concat(val_rows).reset_index(drop=True)


def save_label_map(label_to_idx: dict, path: str):
    with open(path, "w") as f:
        json.dump(label_to_idx, f, indent=2)


def load_label_map(path: str) -> dict:
    with open(path) as f:
        return json.load(f)
