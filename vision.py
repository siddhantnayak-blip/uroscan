"""UroScan computer-vision pipeline.

photo -> quality check -> find white card -> straighten it -> find the 10 pads
      -> local white balance -> CIELAB colour -> KNN (CIEDE2000) against the
      reference chart -> level + approximate value + confidence
"""
import cv2
import numpy as np

from reference import CHART, PAD_ORDER

REF_WHITE = np.array([250.0, 250.0, 248.0])   # colour of the strip's white plastic under neutral light
PAD_SPACING_RATIO = 84 / 64                   # centre-to-centre distance / pad width (strip geometry)


class ScanError(Exception):
    """Raised with a user-friendly message when the photo can't be read."""


# ---------------------------------------------------------------- colour maths
def rgb_to_lab(rgb):
    arr = np.asarray(rgb, np.float32).reshape(-1, 1, 3) / 255.0
    return cv2.cvtColor(arr, cv2.COLOR_RGB2Lab).reshape(-1, 3)


def ciede2000(lab1, lab2):
    """CIEDE2000 colour difference between two CIELAB colours (vectorised over lab2)."""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2[:, 0], lab2[:, 1], lab2[:, 2]
    C1, C2 = np.hypot(a1, b1), np.hypot(a2, b2)
    Cb = (C1 + C2) / 2
    G = 0.5 * (1 - np.sqrt(Cb**7 / (Cb**7 + 25**7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dLp, dCp = L2 - L1, C2p - C1p
    dh = h2p - h1p
    dh = np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh))
    dh = np.where(C1p * C2p == 0, 0, dh)
    dHp = 2 * np.sqrt(C1p * C2p) * np.sin(np.radians(dh / 2))
    Lbp, Cbp = (L1 + L2) / 2, (C1p + C2p) / 2
    hs = h1p + h2p
    hbp = np.where(C1p * C2p == 0, hs,
                   np.where(np.abs(h1p - h2p) <= 180, hs / 2,
                            np.where(hs < 360, (hs + 360) / 2, (hs - 360) / 2)))
    T = (1 - 0.17 * np.cos(np.radians(hbp - 30)) + 0.24 * np.cos(np.radians(2 * hbp))
         + 0.32 * np.cos(np.radians(3 * hbp + 6)) - 0.20 * np.cos(np.radians(4 * hbp - 63)))
    dtheta = 30 * np.exp(-(((hbp - 275) / 25) ** 2))
    Rc = 2 * np.sqrt(Cbp**7 / (Cbp**7 + 25**7))
    Sl = 1 + (0.015 * (Lbp - 50) ** 2) / np.sqrt(20 + (Lbp - 50) ** 2)
    Sc, Sh = 1 + 0.045 * Cbp, 1 + 0.015 * Cbp * T
    Rt = -np.sin(np.radians(2 * dtheta)) * Rc
    return np.sqrt((dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2 + Rt * (dCp / Sc) * (dHp / Sh))


# Pre-compute LAB for every reference level
REF_LAB = {name: rgb_to_lab([c for _, _, c in levels]) for name, levels in CHART.items()}


def match_colour(analyte, rgb, k=2):
    """KNN: nearest reference levels by CIEDE2000; returns level, approx value, confidence."""
    levels = CHART[analyte]
    lab = rgb_to_lab([rgb])[0]
    d = ciede2000(lab, REF_LAB[analyte])
    order = np.argsort(d)[:k]
    i, j = int(order[0]), int(order[1])
    d1, d2 = float(d[i]), float(d[j])
    label, value, _ = levels[i]
    approx = value
    if abs(i - j) == 1 and d1 + d2 > 0:            # colour sits between two neighbouring levels
        t = d1 / (d1 + d2)
        approx = value + t * (levels[j][1] - value)
    conf = (d2 - d1) / (d2 + d1 + 1e-6)             # 0 = halfway, 1 = exact match
    conf = 0.5 + 0.5 * conf
    if d1 > 12:                                      # far from every chart colour
        conf *= 12 / d1
    return dict(level_index=i, level_label=label, numeric_value=value,
                approx_value=round(float(approx), 4), confidence=round(float(min(conf, 1.0)), 3),
                delta_e=round(d1, 2))


# ---------------------------------------------------------------- geometry
def _flat_field(img, k):
    """Estimate the local white (max filter) and divide it out -> removes shadows, vignette, colour cast."""
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32)
    k = int(k) | 1
    smooth = cv2.GaussianBlur(rgb, (0, 0), 3)          # remove sensor noise before taking the max
    white = cv2.dilate(smooth, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    white = cv2.GaussianBlur(white, (0, 0), k / 3)
    return np.clip(rgb / np.maximum(white, 1) * REF_WHITE, 0, 255), white


def _blobs(norm, dim):
    lab = cv2.cvtColor(norm.astype(np.float32) / 255, cv2.COLOR_RGB2Lab)
    chroma = np.hypot(lab[..., 1], lab[..., 2])
    mask = ((chroma > 14) | (lab[..., 0] < 75)).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if not (0.015 * dim < w < 0.1 * dim and 0.015 * dim < h < 0.1 * dim):
            continue
        if not (0.6 < w / h < 1.6) or area / (w * h) < 0.6:
            continue
        out.append((float(cents[i][0]), float(cents[i][1]), float((w + h) / 2)))
    return out


def _best_line(blobs):
    """RANSAC-style: the line through two blobs that has the most similar-sized blobs on it."""
    best = []
    for i in range(len(blobs)):
        for j in range(i + 1, len(blobs)):
            p, q = np.array(blobs[i][:2]), np.array(blobs[j][:2])
            d = q - p; L = np.linalg.norm(d)
            if L == 0:
                continue
            nrm = np.array([-d[1], d[0]]) / L
            size = (blobs[i][2] + blobs[j][2]) / 2
            inl = [b for b in blobs if abs(np.dot(np.array(b[:2]) - p, nrm)) < 0.4 * size
                   and 0.6 * size < b[2] < 1.5 * size]
            if len(inl) > len(best):
                best = inl
    return best


def _find_pads(img):
    """Returns (straightened image, flat-fielded RGB, pad centres, pad size, spacing) or None."""
    dim = max(img.shape[:2])
    norm, _ = _flat_field(img, 0.08 * dim)
    line = _best_line(_blobs(norm, dim))
    if len(line) < 3:
        return None
    pts = np.array([b[:2] for b in line], np.float32)
    vx, vy, _, _ = cv2.fitLine(pts, cv2.DIST_L2, 0, 0.01, 0.01).ravel()
    if vx < 0:
        vx, vy = -vx, -vy
    angle = np.degrees(np.arctan2(vy, vx))
    if abs(angle) > 60:                      # strip photographed vertically: tip at the top
        img = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
        return _find_pads_horizontal(img)
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    img = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return _find_pads_horizontal(img)


def _find_pads_horizontal(img):
    dim = max(img.shape[:2])
    norm, _ = _flat_field(img, 0.08 * dim)
    blobs = _best_line(_blobs(norm, dim))
    if len(blobs) < 3:
        return None
    blobs.sort()
    size = float(np.median([b[2] for b in blobs]))
    cy = float(np.median([b[1] for b in blobs]))
    xs = np.array([b[0] for b in blobs])
    diffs = np.diff(xs)
    cand = diffs[(diffs > 1.1 * size) & (diffs < 1.7 * size)]
    s = float(np.median(cand)) if len(cand) else PAD_SPACING_RATIO * size
    x0 = xs[0]
    for _ in range(3):                          # least-squares refine of the pad grid
        k = np.round((xs - x0) / s)
        keep = (k >= 0) & (k <= 9)
        if keep.sum() >= 2 and len(set(k[keep])) >= 2:
            A = np.vstack([np.ones(keep.sum()), k[keep]]).T
            x0, s = np.linalg.lstsq(A, xs[keep], rcond=None)[0]
    centres = [(float(x0 + i * s), cy) for i in range(10)]
    return img, norm, centres, size, float(s)


def _patch(rgb, cx, cy, half_w, half_h):
    H, W = rgb.shape[:2]
    x0, x1 = int(max(cx - half_w, 0)), int(min(cx + half_w, W))
    y0, y1 = int(max(cy - half_h, 0)), int(min(cy + half_h, H))
    return rgb[y0:y1, x0:x1].reshape(-1, 3)


# ---------------------------------------------------------------- main entry
def analyse(image_bytes):
    data = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ScanError("That file isn't an image we can read. Please upload a JPG or PNG photo.")
    scale = 1200 / max(img.shape[:2])
    if scale < 1:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    found = _find_pads(img)
    if found is None:
        raise ScanError("Couldn't find the coloured pads. Put the strip flat on a plain white card or paper, "
                        "tip on the LEFT, fill most of the frame and avoid strong shadows.")
    card, norm, centres, size, spacing = found
    H, W = card.shape[:2]
    if centres[-1][0] + size / 2 > W or centres[0][0] - size / 2 < 0:
        raise ScanError("The strip seems cut off. Make sure all 10 pads are inside the photo.")
    quality = {}
    gray = cv2.cvtColor(card, cv2.COLOR_BGR2GRAY)
    quality["sharpness"] = round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 1)
    x0, x1 = int(max(centres[0][0] - size, 0)), int(min(centres[-1][0] + size, W))
    y0, y1 = int(max(centres[0][1] - 2 * size, 0)), int(min(centres[0][1] + 2 * size, H))
    quality["brightness"] = round(float(np.percentile(gray[y0:y1, x0:x1], 90)), 1)
    if quality["brightness"] < 70:
        raise ScanError("The photo is too dark. Move to better light and try again.")

    hsv = cv2.cvtColor(card, cv2.COLOR_BGR2HSV)
    raw = cv2.cvtColor(card, cv2.COLOR_BGR2RGB).astype(np.float32)
    results, glare_pads = [], []
    for idx, (name, (cx, cy)) in enumerate(zip(PAD_ORDER, centres), start=1):
        px = _patch(norm, cx, cy, 0.3 * size, 0.3 * size)                # inner 60 % of the pad
        v = _patch(hsv, cx, cy, 0.3 * size, 0.3 * size)
        glare = (v[:, 2] > 245) & (v[:, 1] < 40)
        if glare.mean() > 0.15:
            glare_pads.append(idx)
        if glare.mean() > 0.05:                      # drop the brightest (glare) pixels
            bright = px.sum(axis=1)
            px = px[bright <= np.percentile(bright, 80)]
        corrected = np.median(px, axis=0)
        m = match_colour(name, corrected)
        m.update(analyte=name, pad=idx,
                 raw_rgb=[int(c) for c in np.median(_patch(raw, cx, cy, 0.3 * size, 0.3 * size), axis=0)],
                 rgb=[int(c) for c in corrected])
        results.append(m)
    quality["glare_pads"] = glare_pads
    warnings = []
    if quality["sharpness"] < 8:
        warnings.append("Photo looks blurry; results may be less accurate.")
    if glare_pads:
        warnings.append(f"Glare detected on pad(s) {', '.join(map(str, glare_pads))}; tilt the phone slightly next time.")
    low = [r["analyte"] for r in results if r["confidence"] < 0.6]
    if low:
        warnings.append("Low confidence for: " + ", ".join(low) + ". Please check these by eye.")
    quality["warnings"] = warnings

    # annotated image for the report
    ann = card.copy()
    for r, (cx, cy) in zip(results, centres):
        h = size / 2
        cv2.rectangle(ann, (int(cx - h), int(cy - h)), (int(cx + h), int(cy + h)), (40, 40, 220), 2)
        cv2.rectangle(ann, (int(cx - .3 * size), int(cy - .3 * size)), (int(cx + .3 * size), int(cy + .3 * size)),
                      (255, 255, 255), 1)
        cv2.putText(ann, str(r["pad"]), (int(cx - 8), int(cy - h - 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (40, 40, 220), 2)
    ok, ann_jpg = cv2.imencode(".jpg", ann, [cv2.IMWRITE_JPEG_QUALITY, 82])
    ok2, orig_jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return dict(results=results, quality=quality,
                annotated=ann_jpg.tobytes(), image=orig_jpg.tobytes())
