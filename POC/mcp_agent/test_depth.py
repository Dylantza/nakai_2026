"""
Test Depth Pro on a local image.

Resizes large images for faster inference, clamps depth to ignore sky,
and saves the depth map + side-by-side composite.

Usage:
    python test_depth.py path/to/photo.jpg
    python test_depth.py path/to/photo.jpg --max-size 800
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import torch
from PIL import Image

MAX_INPUT_SIZE = 1536  # longest edge — keeps inference under ~5s on CPU


def run_depth(image_path: str, max_size: int = MAX_INPUT_SIZE) -> None:
    path = Path(image_path)
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    import depth_pro

    # Pick the best available device
    if torch.backends.mps.is_available():
        device = torch.device("mps")
    elif torch.cuda.is_available():
        device = torch.device("cuda")
    else:
        device = torch.device("cpu")
    print(f"Using device: {device}")

    print("Loading Depth Pro model...")
    t0 = time.time()
    model, transform = depth_pro.create_model_and_transforms()
    model.eval()
    model.to(device)
    print(f"Model loaded in {time.time() - t0:.1f}s")

    # Load and resize if needed
    image, _, f_px = depth_pro.load_rgb(str(path))
    h_orig, w_orig = image.shape[:2]
    print(f"Original size: {w_orig}x{h_orig}")

    if max(h_orig, w_orig) > max_size:
        scale = max_size / max(h_orig, w_orig)
        new_w, new_h = int(w_orig * scale), int(h_orig * scale)
        image = np.array(Image.fromarray(image).resize((new_w, new_h), Image.LANCZOS))
        if f_px is not None:
            f_px = f_px * scale
        print(f"Resized to: {new_w}x{new_h}")

    # Run inference
    print("Running depth estimation...")
    t0 = time.time()
    image_tensor = transform(image).to(device)
    with torch.no_grad():
        prediction = model.infer(image_tensor, f_px=f_px)
    elapsed = time.time() - t0
    print(f"Inference done in {elapsed:.2f}s")

    depth = prediction["depth"].cpu().numpy()
    focal = prediction["focallength_px"].item()
    print(f"Raw depth range: {depth.min():.2f}m — {depth.max():.2f}m")
    print(f"Estimated focal length: {focal:.1f}px")

    # Clamp depth to useful range (ignore sky / infinity)
    # Use 95th percentile as max to avoid sky blowout
    d_min = float(np.percentile(depth, 1))
    d_max = float(np.percentile(depth, 95))
    print(f"Clamped range (1st-95th pct): {d_min:.2f}m — {d_max:.2f}m")

    depth_clamped = np.clip(depth, d_min, d_max)

    # Normalize to 0-255
    if d_max - d_min > 0:
        depth_norm = ((depth_clamped - d_min) / (d_max - d_min) * 255).astype(np.uint8)
    else:
        depth_norm = np.zeros_like(depth_clamped, dtype=np.uint8)

    # Invert so close = bright/warm, far = dark/cool in INFERNO
    depth_norm = 255 - depth_norm

    # Colorize
    depth_colored = cv2.applyColorMap(depth_norm, cv2.COLORMAP_INFERNO)

    # Build side-by-side composite
    rgb_bgr = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    h, w = depth_colored.shape[:2]
    rgb_resized = cv2.resize(rgb_bgr, (w, h))
    composite = np.hstack([rgb_resized, depth_colored])

    # Save outputs
    out_dir = path.parent
    stem = path.stem

    depth_path = out_dir / f"{stem}_depth.png"
    composite_path = out_dir / f"{stem}_composite.png"

    cv2.imwrite(str(depth_path), depth_colored)
    cv2.imwrite(str(composite_path), composite)

    print(f"\nSaved:")
    print(f"  Depth map:  {depth_path}")
    print(f"  Composite:  {composite_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test Depth Pro on a single image")
    parser.add_argument("image", help="Path to an image file (jpg, png)")
    parser.add_argument("--max-size", type=int, default=MAX_INPUT_SIZE,
                        help=f"Max longest edge in pixels (default: {MAX_INPUT_SIZE})")
    args = parser.parse_args()
    run_depth(args.image, max_size=args.max_size)
