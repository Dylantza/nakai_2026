import sys
import math
import pygame
import serial

SERIAL_PORT = '/dev/cu.usbmodem178421801'
BAUD_RATE = 115200

SPEED_MIN = 1
SPEED_MAX = 10
speed = 5

# Motor mix output (-1.0 to 1.0)
left_mix = 0.0
right_mix = 0.0

# colors
BG       = (30, 30, 36)
PANEL    = (42, 42, 50)
WHITE    = (220, 220, 230)
DIM      = (100, 100, 115)
GREEN    = (80, 220, 120)
YELLOW   = (240, 200, 60)
CYAN     = (80, 200, 230)
GRAY     = (90, 90, 100)
RED      = (220, 70, 70)
BAR_BG   = (55, 55, 65)
BAR_FILL = (80, 220, 120)

WIDTH, HEIGHT = 480, 420
FPS = 30

try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
except serial.SerialException as e:
    print(f"Could not connect: {e}")
    sys.exit(1)

pygame.init()
pygame.joystick.init()

joystick = None
if pygame.joystick.get_count() > 0:
    joystick = pygame.joystick.Joystick(0)
    joystick.init()
    print(f"Controller connected: {joystick.get_name()}")
else:
    print("No controller found — keyboard only.")

# Joystick tuning
STICK_DEADZONE = 0.12

screen = pygame.display.set_mode((WIDTH, HEIGHT))
pygame.display.set_caption("Teensy Controller — 360°")
font_big = pygame.font.SysFont("Menlo", 22, bold=True)
font = pygame.font.SysFont("Menlo", 16)
font_sm = pygame.font.SysFont("Menlo", 13)
clock = pygame.time.Clock()


def arcade_mix(throttle, turn):
    """Arcade-drive: convert (throttle, turn) to (left, right) motor values.
    Both inputs and outputs are in -1.0 .. 1.0 range."""
    left  = throttle + turn
    right = throttle - turn
    # scale both down proportionally so neither exceeds 1.0
    maximum = max(abs(left), abs(right), 1.0)
    return left / maximum, right / maximum


def send_motor(l, r):
    """Send analog motor command over serial. l, r in -1.0..1.0"""
    l_pct = int(round(l * 100))
    r_pct = int(round(r * 100))
    ser.write(f"M{l_pct},{r_pct}\n".encode())


def draw():
    screen.fill(BG)

    # title bar
    pygame.draw.rect(screen, PANEL, (0, 0, WIDTH, 50))
    title = font_big.render("TEENSY CONTROLLER — 360°", True, WHITE)
    screen.blit(title, (WIDTH // 2 - title.get_width() // 2, 14))

    # --- stick visualizer ---
    cx, cy = WIDTH - 80, 130
    radius = 50
    pygame.draw.circle(screen, BAR_BG, (cx, cy), radius, 2)
    pygame.draw.circle(screen, GRAY, (cx, cy), 3)
    # dot showing current direction
    dot_x = cx + int(right_mix * radius * 0.5 - left_mix * radius * 0.5)
    # use motor mix to approximate position: forward = both positive
    fwd = (left_mix + right_mix) / 2.0
    turn = (right_mix - left_mix) / 2.0
    dot_x = cx + int(turn * radius * 0.9)
    dot_y = cy - int(fwd * radius * 0.9)
    if left_mix == 0 and right_mix == 0:
        dot_color = GRAY
    else:
        dot_color = GREEN
    pygame.draw.circle(screen, dot_color, (dot_x, dot_y), 7)

    stick_label = font_sm.render("DIRECTION", True, DIM)
    screen.blit(stick_label, (cx - stick_label.get_width() // 2, cy + radius + 8))

    # --- motor bars ---
    motor_label = font.render("MOTORS", True, DIM)
    screen.blit(motor_label, (30, 70))

    for i, (name, val) in enumerate([("L", left_mix), ("R", right_mix)]):
        y = 100 + i * 30
        lbl = font_sm.render(name, True, WHITE)
        screen.blit(lbl, (30, y + 2))

        bar_x, bar_w, bar_h = 55, 200, 18
        pygame.draw.rect(screen, BAR_BG, (bar_x, y, bar_w, bar_h), border_radius=3)

        mid = bar_x + bar_w // 2
        fill_w = int((bar_w // 2) * abs(val))
        if val >= 0:
            pygame.draw.rect(screen, GREEN, (mid, y, fill_w, bar_h), border_radius=3)
        else:
            pygame.draw.rect(screen, YELLOW, (mid - fill_w, y, fill_w, bar_h), border_radius=3)

        # center tick
        pygame.draw.line(screen, DIM, (mid, y), (mid, y + bar_h), 1)

        pct_text = font_sm.render(f"{int(val * 100):+d}%", True, WHITE)
        screen.blit(pct_text, (bar_x + bar_w + 8, y + 2))

    # speed bar
    speed_label = font.render("SPEED", True, DIM)
    screen.blit(speed_label, (30, 175))

    bar_x, bar_y, bar_w, bar_h = 30, 202, 226, 20
    pygame.draw.rect(screen, BAR_BG, (bar_x, bar_y, bar_w, bar_h), border_radius=4)
    fill_w = int(bar_w * speed / SPEED_MAX)
    pygame.draw.rect(screen, BAR_FILL, (bar_x, bar_y, fill_w, bar_h), border_radius=4)

    speed_text = font.render(f"{speed}/{SPEED_MAX}", True, WHITE)
    screen.blit(speed_text, (bar_x + bar_w + 12, bar_y))

    # key hints
    hints = [
        ("[W] Forward", "[A] Left",  "[+] Speed Up"),
        ("[S] Reverse", "[D] Right", "[-] Speed Down"),
        ("[X] E-Stop",  "",          "[ESC] Quit"),
    ]
    y = 250
    for row in hints:
        for i, text in enumerate(row):
            if text:
                label = font_sm.render(text, True, DIM)
                screen.blit(label, (30 + i * 155, y))
        y += 24

    # controller hints
    if joystick:
        y += 6
        ctrl_label = font_sm.render("CONTROLLER", True, DIM)
        screen.blit(ctrl_label, (30, y))
        y += 18
        ctrl_hints = "Left Stick: 360° Move  |  LB/RB: Speed  |  B: E-Stop"
        ctrl_text = font_sm.render(ctrl_hints, True, DIM)
        screen.blit(ctrl_text, (30, y))

    pygame.display.flip()


def apply_deadzone(value, deadzone):
    if abs(value) < deadzone:
        return 0.0
    # remap so output starts at 0 right after the deadzone
    sign = 1.0 if value > 0 else -1.0
    return sign * (abs(value) - deadzone) / (1.0 - deadzone)


running = True
prev_l = 0.0
prev_r = 0.0

# keyboard state for analog-like mixing
key_throttle = 0.0
key_turn = 0.0

try:
    draw()
    while running:
        clock.tick(FPS)

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_x:
                    left_mix = right_mix = 0.0
                    send_motor(0, 0)
                    draw()
                elif event.key in (pygame.K_PLUS, pygame.K_EQUALS) and speed < SPEED_MAX:
                    speed += 1
                    ser.write(b"+\n")
                    draw()
                elif event.key == pygame.K_MINUS and speed > SPEED_MIN:
                    speed -= 1
                    ser.write(b"-\n")
                    draw()
            # Controller buttons
            elif event.type == pygame.JOYBUTTONDOWN and joystick:
                if event.button == 1:  # B = e-stop
                    left_mix = right_mix = 0.0
                    send_motor(0, 0)
                    draw()
                elif event.button == 5 and speed < SPEED_MAX:  # RB
                    speed += 1
                    ser.write(b"+\n")
                    draw()
                elif event.button == 4 and speed > SPEED_MIN:  # LB
                    speed -= 1
                    ser.write(b"-\n")
                    draw()
            elif event.type == pygame.JOYDEVICEADDED:
                if joystick is None:
                    joystick = pygame.joystick.Joystick(event.device_index)
                    joystick.init()
                    print(f"Controller connected: {joystick.get_name()}")
            elif event.type == pygame.JOYDEVICEREMOVED:
                if joystick and event.instance_id == joystick.get_instance_id():
                    joystick = None
                    print("Controller disconnected.")

        # --- build throttle + turn from inputs ---
        throttle = 0.0
        turn = 0.0

        # keyboard: WASD gives full digital values
        keys = pygame.key.get_pressed()
        if keys[pygame.K_w]:
            throttle = 1.0
        elif keys[pygame.K_s]:
            throttle = -1.0
        if keys[pygame.K_a]:
            turn = -1.0
        elif keys[pygame.K_d]:
            turn = 1.0

        # controller stick overrides if there's input
        if joystick:
            sx = apply_deadzone(joystick.get_axis(0), STICK_DEADZONE)
            sy = apply_deadzone(-joystick.get_axis(1), STICK_DEADZONE)  # invert Y

            if abs(sx) > 0 or abs(sy) > 0:
                throttle = sy
                turn = sx

        left_mix, right_mix = arcade_mix(throttle, turn)

        # only send when values change (avoid flooding serial)
        if abs(left_mix - prev_l) > 0.02 or abs(right_mix - prev_r) > 0.02:
            send_motor(left_mix, right_mix)
            prev_l = left_mix
            prev_r = right_mix
            draw()

finally:
    ser.write(b"x\n")
    ser.close()
    pygame.quit()
    print("Disconnected. Bye!")
