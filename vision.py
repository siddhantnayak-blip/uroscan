"""UroScan strip reader (simple version).

Steps:
 1. Find the coloured pads in the photo (OpenCV contours).
 2. Fit a straight line through them (the strip may be tilted) and work out where all 10 pads are.
 3. For each pad, take the average colour of its centre after comparing it with the white
    paper around it (this cancels out yellow room light or shadows).
 4. Find the closest colours on the reference chart (K-nearest neighbours) using Euclidean
    distance in LAB colour space -> the level, plus an approximate value (e.g. about 180 mg/dL).
"""
import cv2
import numpy as np

from reference import CHART, PAD_ORDER

WHITE = np.array([250.0, 250.0, 248.0])     # colour of the white strip/paper in normal light


class ScanError(Exception):
    """Raised with a friendly message when the photo can't be read."""


def to_lab(rgb):
    """Convert one RGB colour (0-255) to LAB, a colour space where distance matches what our eyes see."""
    pixel = np.array([[rgb]], dtype=np.float32) / 255.0
    return cv2.cvtColor(pixel, cv2.COLOR_RGB2Lab)[0, 0]


def nearest_level(analyte, rgb):
    """KNN: find the 2 chart colours closest to this pad (Euclidean distance in LAB).
    The closest one gives the level (e.g. "250 mg/dL"). If the pad colour lies between two
    neighbouring levels, the value is estimated in between them (weighted by distance)."""
    pad_lab = to_lab(rgb)
    distances = [float(np.linalg.norm(pad_lab - to_lab(chart_rgb))) for _, _, chart_rgb in CHART[analyte]]
    order = np.argsort(distances)
    first, second = int(order[0]), int(order[1])
    label, value, _ = CHART[analyte][first]
    approx = value
    if abs(first - second) == 1:                                   # colour is between two neighbouring levels
        d1, d2 = distances[first], distances[second]
        other_value = CHART[analyte][second][1]
        approx = value + d1 / (d1 + d2) * (other_value - value)   # closer to the nearer level
    return first, label, value, round(float(approx), 3)


def find_pad_boxes(img):
    """Return (x, y, size) of every coloured square in the image."""
    # compare each pixel with the local white background so shadows don't matter
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32)
    k = max(img.shape[:2]) // 12 | 1
    local_white = cv2.dilate(cv2.GaussianBlur(rgb, (0, 0), 3), np.ones((k, k), np.uint8))
    local_white = cv2.GaussianBlur(local_white, (0, 0), k / 3)
    norm = np.clip(rgb / np.maximum(local_white, 1) * WHITE, 0, 255).astype(np.uint8)

    lab = cv2.cvtColor(norm.astype(np.float32) / 255, cv2.COLOR_RGB2Lab)
    colourfulness = np.hypot(lab[..., 1], lab[..., 2])
    mask = ((colourfulness > 14) | (lab[..., 0] < 75)).astype(np.uint8) * 255      # coloured or dark pixels
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    longest_side = max(img.shape[:2])
    boxes = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        square_enough = 0.6 < w / h < 1.6
        right_size = 0.015 * longest_side < w < 0.1 * longest_side
        filled = cv2.contourArea(c) > 0.6 * w * h
        if square_enough and right_size and filled:
            boxes.append((x + w / 2, y + h / 2, (w + h) / 2))
    return boxes, norm


def analyse(image_bytes):
    img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ScanError("That file isn't a photo we can read. Please upload a JPG or PNG.")
    scale = 1000 / max(img.shape[:2])
    img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    boxes, norm = find_pad_boxes(img)
    if len(boxes) < 3:
        raise ScanError("Couldn't find the pads. Put the strip flat on white paper, tip on the LEFT, "
                        "in good light, and fill most of the photo.")

    # keep only the squares that lie on one straight line (that's the strip)
    size = float(np.median([b[2] for b in boxes]))
    best_line = []
    for a in boxes:
        for b in boxes:
            if b[0] <= a[0]:
                continue
            slope = (b[1] - a[1]) / (b[0] - a[0])
            on_line = [p for p in boxes if abs(p[1] - (a[1] + slope * (p[0] - a[0]))) < 0.4 * size]
            if len(on_line) > len(best_line):
                best_line = on_line
    pads = sorted(best_line)
    if len(pads) < 3:
        raise ScanError("Couldn't find the strip. Keep it straight and make sure all 10 pads are visible.")

    xs = np.array([p[0] for p in pads])
    ys = np.array([p[1] for p in pads])
    size = float(np.median([p[2] for p in pads]))
    slope, intercept = np.polyfit(xs, ys, 1)                  # straight line y = m*x + c through the pads
    gaps = np.diff(xs)
    gaps = gaps[(gaps > 1.1 * size) & (gaps < 1.7 * size)]    # distance between neighbouring pads
    step = float(np.median(gaps)) if len(gaps) else 1.3 * size
    first_x = xs[0]                                           # pad 1 (Glucose) is at the tip, on the left

    results = []
    annotated = img.copy()
    for i, analyte in enumerate(PAD_ORDER):
        cx = first_x + i * step
        cy = slope * cx + intercept
        r = int(0.3 * size)                                   # use only the centre of the pad
        patch = norm[max(int(cy) - r, 0):int(cy) + r, max(int(cx) - r, 0):int(cx) + r]
        if patch.size == 0 or cx + size / 2 > img.shape[1]:
            raise ScanError("Part of the strip is outside the photo. Make sure all 10 pads are visible.")
        rgb = np.median(patch.reshape(-1, 3), axis=0)         # average colour of the pad
        level_index, label, value, approx = nearest_level(analyte, rgb)
        results.append(dict(analyte=analyte, level_index=level_index, level_label=label,
                            value=value, approx=approx, rgb=[int(c) for c in rgb]))
        h = int(size / 2)
        cv2.rectangle(annotated, (int(cx) - h, int(cy) - h), (int(cx) + h, int(cy) + h), (40, 40, 220), 2)
        cv2.putText(annotated, str(i + 1), (int(cx) - 8, int(cy) - h - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (40, 40, 220), 2)

    ok, jpg = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return dict(results=results, image=jpg.tobytes())
