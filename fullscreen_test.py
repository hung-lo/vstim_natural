#!/usr/bin/env python3

"""
fullscreen_test.py

Diagnostic script to test whether pygame can control the behavior Pi's
physical display.

Run over SSH with:

    DISPLAY=:0 SDL_VIDEODRIVER=x11 python3 fullscreen_test.py

Expected behavior:
    1. Screen turns gray for 2 sec
    2. Screen turns white for 3 sec
    3. Screen turns black for 3 sec
    4. Screen turns red for 3 sec
    5. Screen turns green for 3 sec
    6. Screen turns blue for 3 sec
    7. Screen shows alternating black/white photodiode square
    8. Script exits

Press ESC to quit early.
"""

import os
import sys
import time

# Force local behavior Pi display, not SSH X11-forwarded display.
# These must be set before importing/initializing pygame display.
os.environ.setdefault("DISPLAY", ":0")
os.environ.setdefault("SDL_VIDEODRIVER", "x11")

import pygame


FULLSCREEN = True
WINDOW_SIZE = (800, 600)

PHOTODIODE_SIZE_PX = 250
PHOTODIODE_MARGIN_PX = 0


def draw_photodiode_patch(screen, color):
    w, h = screen.get_size()
    rect = pygame.Rect(
        w - PHOTODIODE_SIZE_PX - PHOTODIODE_MARGIN_PX,
        PHOTODIODE_MARGIN_PX,
        PHOTODIODE_SIZE_PX,
        PHOTODIODE_SIZE_PX,
    )
    pygame.draw.rect(screen, color, rect)


def check_for_escape():
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            return True
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            return True
    return False


def show_color(screen, color, seconds, label):
    print(f"Showing {label}: {color} for {seconds} sec")
    screen.fill(color)
    pygame.display.flip()

    t0 = time.time()
    while time.time() - t0 < seconds:
        if check_for_escape():
            return False
        time.sleep(0.01)
    return True


def main():
    print("Environment:")
    print(f"  DISPLAY={os.environ.get('DISPLAY')}")
    print(f"  SDL_VIDEODRIVER={os.environ.get('SDL_VIDEODRIVER')}")
    print(f"  XAUTHORITY={os.environ.get('XAUTHORITY')}")

    pygame.init()

    print("Pygame:")
    print(f"  version={pygame.version.ver}")
    print(f"  display driver={pygame.display.get_driver()}")

    try:
        print(f"  num displays={pygame.display.get_num_displays()}")
        print(f"  desktop sizes={pygame.display.get_desktop_sizes()}")
    except Exception as exc:
        print(f"  display query failed: {exc}")

    if FULLSCREEN:
        screen = pygame.display.set_mode(
            (0, 0),
            pygame.FULLSCREEN | pygame.DOUBLEBUF,
        )
    else:
        screen = pygame.display.set_mode(WINDOW_SIZE)

    pygame.display.set_caption("VSTIM fullscreen display test")
    pygame.mouse.set_visible(False)

    print(f"  created screen size={screen.get_size()}")
    print("Starting visual test. Press ESC to quit early.")

    sequence = [
        ((128, 128, 128), 2.0, "gray"),
        ((255, 255, 255), 3.0, "white"),
        ((0, 0, 0), 3.0, "black"),
        ((255, 0, 0), 3.0, "red"),
        ((0, 255, 0), 3.0, "green"),
        ((0, 0, 255), 3.0, "blue"),
    ]

    try:
        for color, seconds, label in sequence:
            if not show_color(screen, color, seconds, label):
                print("Quit requested.")
                return

        print("Showing photodiode patch blink test.")
        t0 = time.time()
        blink_period = 0.5
        while time.time() - t0 < 8.0:
            elapsed = time.time() - t0
            patch_on = int(elapsed / blink_period) % 2 == 0

            screen.fill((128, 128, 128))
            if patch_on:
                draw_photodiode_patch(screen, (255, 255, 255))
            else:
                draw_photodiode_patch(screen, (0, 0, 0))

            pygame.display.flip()

            if check_for_escape():
                print("Quit requested.")
                return

            time.sleep(0.01)

    finally:
        pygame.mouse.set_visible(True)
        pygame.quit()
        print("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise