from pathlib import Path

import numpy as np
import scipy.io as sio
from PIL import Image


# Change these paths as needed
MAT_PATH = Path("images_natimg2800_all.mat")
OUT_DIR = Path("stringer_natimg2800_png")

OUT_DIR.mkdir(parents=True, exist_ok=True)

mat = sio.loadmat(MAT_PATH)

if "imgs" not in mat:
    raise KeyError(f"Could not find variable 'imgs'. Found keys: {list(mat.keys())}")

imgs = mat["imgs"]  # expected shape: height x width x n_images

print(f"Loaded imgs with shape {imgs.shape}, dtype {imgs.dtype}")
print(f"Pixel range: {imgs.min()} to {imgs.max()}")

if imgs.ndim != 3:
    raise ValueError(f"Expected imgs to be 3D, got shape {imgs.shape}")

n_images = imgs.shape[2]

for i in range(n_images):
    im = imgs[:, :, i]

    # The uploaded file is already uint8, so preserve original pixel values.
    if im.dtype != np.uint8:
        im = im.astype(np.float32)
        im = im - im.min()
        if im.max() > 0:
            im = im / im.max()
        im = (im * 255).astype(np.uint8)

    out_path = OUT_DIR / f"natimg_{i:04d}.png"
    Image.fromarray(im, mode="L").save(out_path)

print(f"Saved {n_images} PNGs to: {OUT_DIR.resolve()}")