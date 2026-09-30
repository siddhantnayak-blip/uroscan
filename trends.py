"""Trend analysis over a patient's scan history (simple linear regression)."""
import numpy as np


def analyse_trend(points, normal_max_index=0):
    """points: list of dicts (oldest first) with level_index and status.
    Returns arrow, words, consecutive abnormal count, scans until predicted abnormal."""
    if not points:
        return dict(arrow="–", word="no data", streak=0, forecast=None)
    y = np.array([p["level_index"] for p in points[-6:]], float)
    streak = 0
    for p in reversed(points):
        if p["status"] in ("High", "Low"):
            streak += 1
        else:
            break
    if len(y) < 3:
        return dict(arrow="→", word="not enough scans", streak=streak, forecast=None)
    x = np.arange(len(y))
    slope = float(np.polyfit(x, y, 1)[0])
    if slope > 0.15:
        arrow, word = "↑", "rising"
    elif slope < -0.15:
        arrow, word = "↓", "falling"
    else:
        arrow, word = "→", "stable"
    forecast = None
    last = points[-1]
    if slope > 0.15 and last["status"] == "Normal":
        gap = (normal_max_index + 1) - y[-1]
        forecast = max(1, int(np.ceil(gap / slope)))
    return dict(arrow=arrow, word=word, streak=streak, forecast=forecast, slope=round(slope, 3))
