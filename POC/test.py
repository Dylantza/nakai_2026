import sys
import time
import pygame
import serial

SERIAL_PORT = '/dev/cu.usbmodem178421801'
BAUD_RATE = 115200

SPEED_MIN = 1
SPEED_MAX = 10
speed = 5
state = 'STOPPED'

DIRECTION_KEYS = {
    pygame.K_w: ('w', 'FORWARD'),
    pygame.K_s: ('s', 'REVERSE'),
    pygame.K_a: ('a', 'LEFT'),
    pygame.K_d: ('d', 'RIGHT'),
}

# --- Impeller control ---
impeller_power = 0       # -100 to 100
IMPELLER_STEP = 10

# --- Brush control ---
brush_on = False

# --- Telemetry state ---
impeller_read_power = 0
impeller_read_pwm = 1500
water_warning = False
water_value = 0
water_warn_time = 0
distance_mm = 0

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
ORANGE   = (240, 150, 50)
BAR_BG   = (55, 55, 65)
BAR_FILL = (80, 220, 120)

WIDTH, HEIGHT = 420, 580
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
STICK_DEADZONE = 0.4

screen = pygame.display.set_mode((WIDTH, HEIGHT))
pygame.display.set_caption("Teensy Controller")
font_big = pygame.font.SysFont("Menlo", 22, bold=True)
font = pygame.font.SysFont("Menlo", 16)
font_sm = pygame.font.SysFont("Menlo", 13)
clock = pygame.time.Clock()

def draw():
    screen.fill(BG)

    # title bar
    pygame.draw.rect(screen, PANEL, (0, 0, WIDTH, 50))
    title = font_big.render("TEENSY CONTROLLER", True, WHITE)
    screen.blit(title, (WIDTH // 2 - title.get_width() // 2, 14))

    # status
    if state == 'STOPPED':
        color, arrow = GRAY, "o"
    elif state == 'FORWARD':
        color, arrow = GREEN, "^"
    elif state == 'REVERSE':
        color, arrow = YELLOW, "v"
    elif state == 'LEFT':
        color, arrow = CYAN, "<"
    elif state == 'RIGHT':
        color, arrow = CYAN, ">"
    else:
        color, arrow = WHITE, "?"

    status_label = font.render("STATUS", True, DIM)
    screen.blit(status_label, (30, 70))
    status_text = font_big.render(f"{arrow}  {state}", True, color)
    screen.blit(status_text, (30, 95))

    # speed bar
    speed_label = font.render("SPEED", True, DIM)
    screen.blit(speed_label, (30, 145))

    bar_x, bar_y, bar_w, bar_h = 30, 172, 260, 20
    pygame.draw.rect(screen, BAR_BG, (bar_x, bar_y, bar_w, bar_h), border_radius=4)
    fill_w = int(bar_w * speed / SPEED_MAX)
    pygame.draw.rect(screen, BAR_FILL, (bar_x, bar_y, fill_w, bar_h), border_radius=4)

    speed_text = font.render(f"{speed}/{SPEED_MAX}", True, WHITE)
    screen.blit(speed_text, (bar_x + bar_w + 12, bar_y))

    # impeller bar
    imp_label = font.render("IMPELLER", True, DIM)
    screen.blit(imp_label, (30, 205))

    imp_bar_y = 232
    imp_bar_w = 260
    imp_bar_h = 20
    center_x = bar_x + imp_bar_w // 2
    pygame.draw.rect(screen, BAR_BG, (bar_x, imp_bar_y, imp_bar_w, imp_bar_h), border_radius=4)
    if impeller_power > 0:
        fw = int((imp_bar_w / 2) * impeller_power / 100)
        pygame.draw.rect(screen, GREEN, (center_x, imp_bar_y, fw, imp_bar_h), border_radius=4)
    elif impeller_power < 0:
        fw = int((imp_bar_w / 2) * abs(impeller_power) / 100)
        pygame.draw.rect(screen, YELLOW, (center_x - fw, imp_bar_y, fw, imp_bar_h), border_radius=4)
    # center line
    pygame.draw.line(screen, WHITE, (center_x, imp_bar_y), (center_x, imp_bar_y + imp_bar_h), 2)

    imp_text = font.render(f"{impeller_power}%", True, WHITE)
    screen.blit(imp_text, (bar_x + imp_bar_w + 12, imp_bar_y))

    # key hints
    hints = [
        ("[W] Forward",  "[A] Left",   "[+] Speed Up"),
        ("[S] Reverse",  "[D] Right",  "[-] Speed Down"),
        ("[Up] Imp+",    "[Down] Imp-", "[0] Imp Stop"),
        ("[B] Brush",    "[X] E-Stop",  "[ESC] Quit"),
    ]
    y = 270
    for row in hints:
        for i, text in enumerate(row):
            if text:
                label = font_sm.render(text, True, DIM)
                screen.blit(label, (30 + i * 140, y))
        y += 24

    # controller hints
    if joystick:
        y += 6
        ctrl_label = font_sm.render("CONTROLLER", True, DIM)
        screen.blit(ctrl_label, (30, y))
        y += 18
        ctrl_hints = "Stick: Move | LB/RB: Speed | A: Imp Off | B: E-Stop"
        ctrl_text = font_sm.render(ctrl_hints, True, DIM)
        screen.blit(ctrl_text, (30, y))

    # --- Telemetry panel ---
    tele_y = 380
    pygame.draw.line(screen, GRAY, (30, tele_y), (WIDTH - 30, tele_y))
    tele_y += 10

    tele_label = font.render("TELEMETRY", True, DIM)
    screen.blit(tele_label, (30, tele_y))
    tele_y += 28

    # Impeller
    imp_color = GREEN if impeller_read_power == 0 else (YELLOW if abs(impeller_read_power) < 50 else RED)
    imp_tele = font.render(f"Impeller  Power: {impeller_read_power}%  PWM: {impeller_read_pwm}", True, imp_color)
    screen.blit(imp_tele, (30, tele_y))
    tele_y += 26

    # Brush
    brush_color = GREEN if brush_on else GRAY
    brush_status = "ON" if brush_on else "OFF"
    brush_text = font.render(f"Brush: {brush_status}", True, brush_color)
    screen.blit(brush_text, (30, tele_y))
    tele_y += 26

    # Distance
    if distance_mm > 0:
        dist_color = GREEN if distance_mm > 500 else (YELLOW if distance_mm > 200 else RED)
        dist_text = font.render(f"Range: {distance_mm} mm", True, dist_color)
    else:
        dist_text = font.render("Range: ---", True, DIM)
    screen.blit(dist_text, (30, tele_y))
    tele_y += 26

    # Water
    if water_warning:
        water_text = font.render(f"WATER WARNING!  ({water_value})", True, ORANGE)
    else:
        water_text = font.render("Water: OK", True, GREEN)
    screen.blit(water_text, (30, tele_y))

    pygame.display.flip()

def read_telemetry():
    global impeller_read_power, impeller_read_pwm, water_warning, water_value, water_warn_time, distance_mm, brush_on
    while ser.in_waiting:
        try:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
        except Exception:
            break
        if not line:
            continue
        if line.startswith("Power:"):
            # "Power: 0% | PWM: 1500"
            try:
                parts = line.split('|')
                impeller_read_power = int(parts[0].split(':')[1].strip().rstrip('%'))
                impeller_read_pwm = int(parts[1].split(':')[1].strip())
            except (IndexError, ValueError):
                pass
        elif line.startswith("Distance:"):
            try:
                distance_mm = int(line.split(':')[1].strip())
            except (IndexError, ValueError):
                pass
        elif line.startswith("Brush Motor:"):
            brush_on = "ON" in line
        elif "WATER WARNING" in line:
            water_warning = True
            water_warn_time = time.time()
            try:
                water_value = int(line.split(':')[1].strip())
            except (IndexError, ValueError):
                pass
    # clear water warning if no new warning for 2 seconds
    if water_warning and time.time() - water_warn_time > 2.0:
        water_warning = False

running = True
prev_state = state

# priority order: W > S > A > D
DIRECTION_PRIORITY = [pygame.K_w, pygame.K_s, pygame.K_a, pygame.K_d]

try:
    draw()
    while running:
        clock.tick(FPS)

        # read sensor data from Teensy
        read_telemetry()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_x:
                    state = 'STOPPED'
                    ser.write(b'x\n')
                    draw()
                elif event.key in (pygame.K_PLUS, pygame.K_EQUALS) and speed < SPEED_MAX:
                    speed += 1
                    ser.write(b'+\n')
                    draw()
                elif event.key == pygame.K_MINUS and speed > SPEED_MIN:
                    speed -= 1
                    ser.write(b'-\n')
                    draw()
                elif event.key == pygame.K_UP:
                    impeller_power = min(impeller_power + IMPELLER_STEP, 100)
                    ser.write(f'{impeller_power}\n'.encode())
                    draw()
                elif event.key == pygame.K_DOWN:
                    impeller_power = max(impeller_power - IMPELLER_STEP, -100)
                    ser.write(f'{impeller_power}\n'.encode())
                    draw()
                elif event.key == pygame.K_0:
                    impeller_power = 0
                    ser.write(b'0\n')
                    draw()
                elif event.key == pygame.K_b:
                    brush_on = not brush_on
                    if brush_on:
                        ser.write(b'brush_on\n')
                    else:
                        ser.write(b'brush_off\n')
                    draw()
            # Xbox controller buttons
            elif event.type == pygame.JOYBUTTONDOWN and joystick:
                # B button (1) = e-stop, RB (5) = speed up, LB (4) = speed down
                if event.button == 1:  # B
                    state = 'STOPPED'
                    ser.write(b'x\n')
                    draw()
                elif event.button == 5 and speed < SPEED_MAX:  # RB
                    speed += 1
                    ser.write(b'+\n')
                    draw()
                elif event.button == 4 and speed > SPEED_MIN:  # LB
                    speed -= 1
                    ser.write(b'-\n')
                    draw()
                elif event.button == 0:  # A = impeller stop
                    impeller_power = 0
                    ser.write(b'0\n')
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

        # poll held keys every frame for smooth, uninterrupted movement
        keys = pygame.key.get_pressed()
        new_state = 'STOPPED'
        cmd = None
        for k in DIRECTION_PRIORITY:
            if keys[k]:
                cmd, new_state = DIRECTION_KEYS[k]
                break

        # if no keyboard direction, check controller left stick / D-pad
        if new_state == 'STOPPED' and joystick:
            lx = joystick.get_axis(0)  # left stick X
            ly = joystick.get_axis(1)  # left stick Y (negative = up)
            # D-pad via hat (if available)
            hat_x, hat_y = 0, 0
            if joystick.get_numhats() > 0:
                hat_x, hat_y = joystick.get_hat(0)

            if ly < -STICK_DEADZONE or hat_y > 0:
                cmd, new_state = 'w', 'FORWARD'
            elif ly > STICK_DEADZONE or hat_y < 0:
                cmd, new_state = 's', 'REVERSE'
            elif lx < -STICK_DEADZONE or hat_x < 0:
                cmd, new_state = 'a', 'LEFT'
            elif lx > STICK_DEADZONE or hat_x > 0:
                cmd, new_state = 'd', 'RIGHT'

        if new_state != prev_state:
            state = new_state
            if cmd:
                ser.write((cmd + '\n').encode())
            else:
                ser.write(b'x\n')
            prev_state = state

        draw()
finally:
    ser.write(b'x\n')
    ser.close()
    pygame.quit()
    print("Disconnected. Bye!")
