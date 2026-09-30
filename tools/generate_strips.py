"""
UroScan synthetic urine-strip dataset generator.
Usage: python uroscan_dataset.py --n 200 --out dataset --seed 42
Creates:
  dataset/images/strip_0001.jpg ...   simulated phone photos
  dataset/labels.csv                  true level + value for every pad
  dataset/reference_chart.png         the colour chart (like the one on a strip bottle)
  dataset/reference_colors.csv        chart colours -> seed data for the REFERENCE_COLOR table
"""
import argparse, csv, json, os, random
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from reference import CHART  # single source of truth for colours

# probability of the "normal" level being chosen, so data looks like real patients (mostly normal)
NORMAL_IDX = {"Glucose":0,"Bilirubin":0,"Ketones":0,"Blood":0,"Protein":0,"Nitrite":0,"Leukocytes":0,
              "Urobilinogen":0,"Specific Gravity":None,"pH":None}
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONTB = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def pick_levels(rng):
    out = {}
    for name, levels in CHART.items():
        ni = NORMAL_IDX[name]
        if ni is None or rng.random() > 0.65:
            i = rng.randrange(len(levels))
        else:
            i = ni
        out[name] = i
    return out


def render(levels, rng, W=1600, H=1000):
    nrng = np.random.default_rng(rng.randrange(1 << 30))
    S = 2
    # background: table / fabric / dark desk
    bg = rng.choice([(120,108,96),(90,95,105),(160,150,135),(60,60,62),(185,180,172)])
    a = np.zeros((H*S, W*S, 3), np.float32) + np.array(bg, np.float32)
    a += nrng.normal(0, 6, (H*S, W*S, 1))
    a += (np.sin(np.linspace(0, rng.uniform(20, 80), H*S))[:, None, None] * rng.uniform(0, 6))
    img = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).convert("RGBA")

    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = W*S//2 + rng.randint(-60, 60)*S, H*S//2 + rng.randint(-40, 40)*S
    card = (cx-620*S, cy-250*S, cx+620*S, cy+250*S)
    d.rounded_rectangle((card[0]+14*S, card[1]+18*S, card[2]+14*S, card[3]+18*S), 16*S, fill=(0,0,0,90))
    layer = layer.filter(ImageFilter.GaussianBlur(10*S))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle(card, 16*S, fill=(246, 245, 240, 255))
    sx0, sy0, sx1, sy1 = cx-550*S, cy-45*S, cx+550*S, cy+45*S
    d.rectangle((sx0+6*S, sy0+8*S, sx1+6*S, sy1+8*S), fill=(0,0,0,40))
    d.rectangle((sx0, sy0, sx1, sy1), fill=(250,250,248,255), outline=(215,215,210,255), width=2*S)
    pad, gap, x = 64*S, 20*S, sx0 + 26*S
    rgbs = {}
    for name, levels_list in CHART.items():
        base = levels_list[levels[name]][2]
        # reaction variation; occasionally halfway between two levels (real strips do this)
        i = levels[name]
        if rng.random() < 0.2 and i + 1 < len(levels_list):
            t = rng.uniform(0.2, 0.45)
            base = tuple(b + t*(c - b) for b, c in zip(base, levels_list[i+1][2]))
        col = tuple(int(np.clip(c + nrng.normal(0, 4), 0, 255)) for c in base)
        rgbs[name] = col
        y = (sy0 + sy1)//2 - pad//2
        d.rectangle((x, y, x+pad, y+pad), fill=col + (255,))
        x += pad + gap
    img = Image.alpha_composite(img, layer).convert("RGB")
    a = np.asarray(img).astype(np.float32)
    tex = nrng.normal(0, 5, a.shape[:2])
    a += tex[..., None] * (a.mean(axis=2, keepdims=True) < 240)
    img = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).resize((W, H), Image.LANCZOS)

    # hand-held rotation + mild perspective
    ang = rng.uniform(-8, 8)
    img = img.rotate(ang, resample=Image.BICUBIC, fillcolor=bg)
    k = rng.uniform(0, 0.00012)
    img = img.transform((W, H), Image.PERSPECTIVE, (1, rng.uniform(-0.03, 0.03), 0, 0, 1, 0, k*rng.choice([-1,1]), 0),
                        resample=Image.BICUBIC, fillcolor=bg)

    a = np.asarray(img).astype(np.float32)
    yy, xx = np.mgrid[0:H, 0:W]
    light = rng.choice(["warm", "cool", "neutral", "dim"])
    gain = {"warm": (1.07, 1.0, 0.88), "cool": (0.92, 1.0, 1.08), "neutral": (1, 1, 1), "dim": (0.8, 0.8, 0.8)}[light]
    a *= np.array(gain)
    fall = rng.uniform(0, 0.3)
    dirx = rng.choice([-1, 1])
    a *= (1 - fall/2 + fall * ((xx / W) if dirx > 0 else (1 - xx / W)))[..., None]
    r2 = ((xx - W/2)/(W/2))**2 + ((yy - H/2)/(H/2))**2
    a *= (1 - rng.uniform(0.1, 0.3) * r2)[..., None]
    glare = rng.random() < 0.5
    if glare:
        gx, gy = rng.uniform(350, 1250), rng.uniform(420, 560)
        a += (np.exp(-(((xx-gx)/rng.uniform(60, 130))**2 + ((yy-gy)/rng.uniform(40, 70))**2)) * rng.uniform(30, 80))[..., None]
    shadow = rng.random() < 0.3
    if shadow:  # soft shadow of a phone/hand across part of the image
        sx = rng.uniform(0, W)
        a *= (1 - 0.3 / (1 + np.exp(-(xx - sx) / 40)))[..., None]
    a += nrng.normal(0, rng.uniform(2, 6), a.shape)
    blur = rng.uniform(0.4, 1.8)
    img = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(blur))
    meta = dict(rotation=round(ang, 2), light=light, glare=glare, shadow=shadow, blur=round(blur, 2))
    return img, rgbs, meta


def reference_chart(path):
    rows = list(CHART.items())
    W, rowh = 1500, 90
    img = Image.new("RGB", (W, 120 + rowh*len(rows)), (250, 250, 247))
    d = ImageDraw.Draw(img)
    d.text((30, 30), "UroScan reference colour chart (simulated 10-parameter strip)", font=ImageFont.truetype(FONTB, 32), fill=(20, 90, 50))
    f, fs = ImageFont.truetype(FONTB, 20), ImageFont.truetype(FONT, 14)
    for r, (name, levels) in enumerate(rows):
        y = 100 + r*rowh
        d.text((30, y+25), name, font=f, fill=(30, 30, 30))
        for i, (lab, _, rgb) in enumerate(levels):
            x = 280 + i*170
            d.rectangle((x, y+5, x+60, y+55), fill=rgb, outline=(150, 150, 150))
            d.text((x, y+60), lab, font=fs, fill=(60, 60, 60))
    img.save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--out", default="dataset")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    os.makedirs(f"{args.out}/images", exist_ok=True)
    reference_chart(f"{args.out}/reference_chart.png")
    with open(f"{args.out}/reference_colors.csv", "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(["analyte", "level_order", "level_label", "numeric_value", "r", "g", "b"])
        for name, levels in CHART.items():
            for i, (lab, val, rgb) in enumerate(levels):
                w.writerow([name, i, lab, val, *rgb])
    names = list(CHART)
    with open(f"{args.out}/labels.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["image", "split", "rotation", "light", "glare", "shadow", "blur"] +
                   [f"{n}|level" for n in names] + [f"{n}|value" for n in names])
        for k in range(1, args.n + 1):
            lv = pick_levels(rng)
            img, rgbs, meta = render(lv, rng)
            fn = f"strip_{k:04d}.jpg"
            img.save(f"{args.out}/images/{fn}", quality=rng.randint(75, 92))
            split = "test" if k % 5 == 0 else "train"
            w.writerow([fn, split, meta["rotation"], meta["light"], meta["glare"], meta["shadow"], meta["blur"]] +
                       [CHART[n][lv[n]][0] for n in names] + [CHART[n][lv[n]][1] for n in names])
            if k % 25 == 0:
                print(f"{k}/{args.n}")
    print("done")


if __name__ == "__main__":
    main()
