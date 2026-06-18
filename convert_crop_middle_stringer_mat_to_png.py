from pathlib import Path

import numpy as np
import scipy.io as sio
from PIL import Image


MAT_PATH = Path("images_natimg2800_all.mat")
OUT_DIR = Path("stringer_natimg2800_center_crop_png")

# Options: "left", "center", "right"
CROP_REGION = "center"

OUT_DIR.mkdir(parents=True, exist_ok=True)

mat = sio.loadmat(MAT_PATH)
imgs = mat["imgs"]  # expected shape: 68 x 270 x 2800

height, width, n_images = imgs.shape

if width != 270:
    raise ValueError(f"Expected width 270, got {width}")

third = width // 3

crop_slices = {
    "left": slice(0, third),
    "center": slice(third, 2 * third),
    "right": slice(2 * third, 3 * third),
}

x_slice = crop_slices[CROP_REGION]

for i in range(n_images):
    im = imgs[:, x_slice, i]

    if im.dtype != np.uint8:
        im = im.astype(np.float32)
        im = im - im.min()
        if im.max() > 0:
            im = im / im.max()
        im = (im * 255).astype(np.uint8)

    out_path = OUT_DIR / f"natimg_{CROP_REGION}_{i:04d}.png"
    Image.fromarray(im, mode="L").save(out_path)

print(f"Saved {n_images} {CROP_REGION}-crop images to {OUT_DIR.resolve()}")