"""
Two models, deliberately in increasing order of sophistication:

1. PooledMLP -- a fast baseline. Mean+std pools the (T, D) landmark sequence
   into a single (2D,) vector and runs it through an MLP. No temporal
   modeling at all, trains in seconds even on CPU. Get this working FIRST to
   have a guaranteed working demo, then move to the sequence model.

2. BiGRUAttention -- a small BiGRU over the frame sequence with additive
   attention pooling (instead of just taking the last hidden state, since the
   most distinctive frame of a sign isn't always the last one). This is the
   "real" model for the submission; still small enough to train on CPU in a
   reasonable time for a ~90-class / ~15-examples-per-class dataset.
"""
import torch
import torch.nn as nn


class PooledMLP(nn.Module):
    def __init__(self, feature_dim: int, n_classes: int, hidden: int = 256, dropout: float = 0.3):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(feature_dim * 2, hidden),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden, hidden),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(hidden, n_classes),
        )

    def forward(self, x):  # x: (B, T, D)
        mean = x.mean(dim=1)
        std = x.std(dim=1)
        pooled = torch.cat([mean, std], dim=1)
        return self.net(pooled)


class BiGRUAttention(nn.Module):
    def __init__(self, feature_dim: int, n_classes: int, hidden: int = 128,
                 num_layers: int = 2, dropout: float = 0.3):
        super().__init__()
        self.gru = nn.GRU(
            feature_dim, hidden, num_layers=num_layers, batch_first=True,
            bidirectional=True, dropout=dropout if num_layers > 1 else 0.0,
        )
        gru_out_dim = hidden * 2
        self.attn = nn.Sequential(
            nn.Linear(gru_out_dim, gru_out_dim // 2),
            nn.Tanh(),
            nn.Linear(gru_out_dim // 2, 1),
        )
        self.classifier = nn.Sequential(
            nn.Dropout(dropout),
            nn.Linear(gru_out_dim, n_classes),
        )

    def forward(self, x):  # x: (B, T, D)
        out, _ = self.gru(x)  # (B, T, 2H)
        attn_scores = self.attn(out)  # (B, T, 1)
        attn_weights = torch.softmax(attn_scores, dim=1)
        pooled = (out * attn_weights).sum(dim=1)  # (B, 2H)
        return self.classifier(pooled)
