"""
Depth Pro serverless GPU endpoint on Modal.

Accepts a JPEG image, runs Apple Depth Pro on a T4 GPU,
returns a side-by-side RGB + depth map composite as base64.

Setup:
    pip install modal
    modal setup          # one-time auth
    modal deploy depth_api.py  # persistent deployment

Test:
    curl -X POST https://YOUR_WORKSPACE--depth-pro-api-predict.modal.run \
        -F "image=@photo.jpg"
"""

import io
import modal
try:
    from fastapi import Request, File, UploadFile
except ImportError:
    from typing import Any
    Request = Any
    UploadFile = Any
    def File(*args, **kwargs):
        return Any

# ---------------------------------------------------------------------------
# Modal app + container image
# ---------------------------------------------------------------------------

app = modal.App("depth-pro-api")

depth_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("git")
    .pip_install(
        "torch",
        "torchvision",
        "timm",
        "huggingface_hub",
        "Pillow",
        "opencv-python-headless",
        "numpy<2",
        "fastapi[standard]",
        "python-multipart",
    )
    .pip_install("git+https://github.com/apple/ml-depth-pro.git")
)

# ---------------------------------------------------------------------------
# Model class — loaded once, reused across requests
# ---------------------------------------------------------------------------

MAX_SIZE = 1536
WARM_POOL = 1  # keep 1 container warm to avoid cold starts


@app.cls(
    image=depth_image,
    gpu="T4",
    scaledown_window=300,  # keep warm for 5 min after last request
)
@modal.concurrent(max_inputs=4)
class DepthModel:
    @modal.enter()
    def load_model(self):
        """Called once when the container starts — downloads + loads the model."""
        import torch
        import depth_pro
        from huggingface_hub import hf_hub_download

        # Download checkpoint
        hf_hub_download(
            repo_id="apple/DepthPro",
            filename="depth_pro.pt",
            local_dir="./checkpoints",
        )

        self.device = torch.device("cuda")
        self.model, self.transform = depth_pro.create_model_and_transforms()
        self.model.eval()
        self.model.to(self.device)
        print(f"Depth Pro loaded on {self.device}")

    @modal.fastapi_endpoint(method="POST")
    async def predict(self, image: UploadFile = File(...)):
        """Accept a JPEG upload, return depth composite as base64 JSON."""
        import base64
        import time
        import cv2
        import numpy as np
        import torch
        from PIL import Image
        from fastapi.responses import JSONResponse

        # Parse the uploaded image
        if image is None:
            return JSONResponse(
                {"error": "Send a JPEG as 'image' in multipart form data"},
                status_code=400,
            )

        jpeg_bytes = await image.read()
        img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
        img_np = np.array(img)

        # Resize if too large
        h, w = img_np.shape[:2]
        f_px = None
        if max(h, w) > MAX_SIZE:
            scale = MAX_SIZE / max(h, w)
            new_w, new_h = int(w * scale), int(h * scale)
            img_np = np.array(img.resize((new_w, new_h), Image.LANCZOS))

        # Inference
        t0 = time.time()
        image_tensor = self.transform(img_np).to(self.device)
        with torch.no_grad():
            prediction = self.model.infer(image_tensor, f_px=f_px)
        inference_time = time.time() - t0

        depth = prediction["depth"].cpu().numpy()

        # Clamp with percentiles, normalize
        d_min = float(np.percentile(depth, 1))
        d_max = float(np.percentile(depth, 95))
        depth_clamped = np.clip(depth, d_min, d_max)

        if d_max - d_min > 0:
            depth_norm = ((depth_clamped - d_min) / (d_max - d_min) * 255).astype(np.uint8)
        else:
            depth_norm = np.zeros_like(depth_clamped, dtype=np.uint8)

        depth_norm = 255 - depth_norm  # invert: close = bright
        depth_colored = cv2.applyColorMap(depth_norm, cv2.COLORMAP_INFERNO)
        depth_colored_rgb = cv2.cvtColor(depth_colored, cv2.COLOR_BGR2RGB)

        # Side-by-side composite
        h_d, w_d = depth_colored_rgb.shape[:2]
        rgb_resized = cv2.resize(img_np, (w_d, h_d))
        composite = np.hstack([rgb_resized, depth_colored_rgb])

        # Encode to base64 JPEG
        buf = io.BytesIO()
        Image.fromarray(composite).save(buf, format="JPEG", quality=85)
        composite_b64 = base64.b64encode(buf.getvalue()).decode()

        return JSONResponse({
            "composite_b64": composite_b64,
            "depth_min_m": round(d_min, 2),
            "depth_max_m": round(d_max, 2),
            "inference_s": round(inference_time, 3),
        })

    @modal.fastapi_endpoint(method="GET")
    async def health(self):
        return {"status": "ok", "gpu": "T4"}
