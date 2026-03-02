"""Quick test: grab one frame from Basler and check it."""
from pypylon import pylon
import cv2
import time

camera = pylon.InstantCamera(pylon.TlFactory.GetInstance().CreateFirstDevice())
camera.Open()

try:
    camera.ExposureAuto.SetValue("Continuous")
except Exception as e:
    print(f"Auto exposure failed: {e}")
try:
    camera.GainAuto.SetValue("Continuous")
except Exception as e:
    print(f"Auto gain failed: {e}")

camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)

converter = pylon.ImageFormatConverter()
converter.OutputPixelFormat = pylon.PixelType_BGR8packed
converter.OutputBitAlignment = pylon.OutputBitAlignment_MsbAligned

print("Warming up...")
time.sleep(2)
for _ in range(50):
    g = camera.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
    g.Release()

print("Grabbing frame...")
grab_result = camera.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
print(f"Grab succeeded: {grab_result.GrabSucceeded()}")

image = converter.Convert(grab_result)
frame = image.GetArray()
print(f"Frame shape: {frame.shape}, min: {frame.min()}, max: {frame.max()}")

frame_resized = cv2.resize(frame, (960, 540))
_, jpeg = cv2.imencode(".jpg", frame_resized, [cv2.IMWRITE_JPEG_QUALITY, 80])
print(f"JPEG size: {len(jpeg.tobytes())} bytes")

cv2.imwrite("/tmp/basler_test.jpg", frame_resized)
print("Saved to /tmp/basler_test.jpg")

grab_result.Release()
camera.StopGrabbing()
camera.Close()
