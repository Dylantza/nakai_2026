from pypylon import pylon
import cv2

def start_camera():
    # Connect to the first available Basler camera
    camera = pylon.InstantCamera(pylon.TlFactory.GetInstance().CreateFirstDevice())
    camera.Open()

    print(f"Connected to: {camera.GetDeviceInfo().GetModelName()}")

    camera.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)

    # Converter to turn Basler frames into OpenCV-compatible BGR format
    converter = pylon.ImageFormatConverter()
    converter.OutputPixelFormat = pylon.PixelType_BGR8packed
    converter.OutputBitAlignment = pylon.OutputBitAlignment_MsbAligned

    print("Feed opened. Press 'q' to close the window.")

    while camera.IsGrabbing():
        grab_result = camera.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)

        if grab_result.GrabSucceeded():
            image = converter.Convert(grab_result)
            frame = image.GetArray()
            cv2.imshow('Basler Camera Feed', frame)

            if cv2.waitKey(1) & 0xFF == ord('q'):
                grab_result.Release()
                break
        else:
            print(f"Grab failed: {grab_result.ErrorDescription}")

        grab_result.Release()

    camera.StopGrabbing()
    camera.Close()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    start_camera()
