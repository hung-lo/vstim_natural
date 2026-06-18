#!/usr/bin/env python3
"""
fullscreen_test.py

Diagnostic script for testing whether pygame can control the behavior Pi screen.

For the headless behavior Pi:
    cd /home/pi/vstim_natural
    source .venv/bin/activate
    unset DISPLAY
    unset SDL_VIDEODRIVER
    python3 fullscreen_test.py

If needed:
    SDL_VIDEODRIVER=RPI python3 fullscreen_test.py

Expected visible behavior:
    gray -> white -> black -> red -> green -> blue
    then a blinking photodiode square in the top-right corner.
"""

import os
import sys
import time


# ============================================================
# Display environment settings
# ============================================================

# For headless Pi / direct framebuffer use:
#   DISPLAY_TARGET = None
#   SDL_VIDEODRIVER_TARGET = None
#
# For forcing the Raspberry Pi SDL backend:
#   DISPLAY_TARGET = None
#   SDL_VIDEODRIVER_TARGET = "RPI"
#
# For desktop/X11 systems:
#   DISPLAY_TARGET = ":0"
#   SDL_VIDEODRIVER_TARGET = None
DISPLAY_TARGET = None
SDL_VIDEODRIVER_TARGET = None
XAUTHORITY_TARGET = None


# ============================================================
# Visual settings
# ============================================================

FULLSCREEN = True
WINDOW_SIZE = (800, 600)

PHOTODIODE_SIZE_PX = 250
PHOTODIODE_MARGIN_PX = 0

COLOR_HOLD_SEC = 4.0
BLINK_TEST_SEC = 8.0
BLINK_PERIOD_SEC = 0.5


def configure_display_environment():
    """
    Configure pygame/SDL display environment.

    For the headless behavior Pi, we intentionally remove DISPLAY so pygame
    does not try to use SSH X11 forwarding or a nonexistent X display.
    """
    if DISPLAY_TARGET is None:
        os.environ.pop("DISPLAY", None)
    else:
        os.environ["DISPLAY"] = DISPLAY_TARGET

    if SDL_VIDEODRIVER_TARGET is None:
        os.environ.pop("SDL_VIDEODRIVER", None)
    else:
        os.environ["SDL_VIDEODRIVER"] = SDL_VIDEODRIVER_TARGET

    if XAUTHORITY_TARGET is None:
        os.environ.pop("XAUTHORITY", None)
    else:
        os.environ["XAUTHORITY"] = XAUTHORITY_TARGET

    print("Display environment:")
    print(f"  DISPLAY={os.environ.get('DISPLAY')}")
    print(f"  SDL_VIDEODRIVER={os.environ.get('SDL_VIDEODRIVER')}")
    print(f"  XAUTHORITY={os.environ.get('XAUTHORITY')}")


def check_for_escape(pygame):
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            return True
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            return True
    return False


def draw_photodiode_patch(pygame, screen, color):
    width, _ = screen.get_size()
    rect = pygame.Rect(
        width - PHOTODIODE_SIZE_PX - PHOTODIODE_MARGIN_PX,
        PHOTODIODE_MARGIN_PX,
        PHOTODIODE_SIZE_PX,
        PHOTODIODE_SIZE_PX,
    )
    pygame.draw.rect(screen, color, rect)


def flip_screen(pygame):
    pygame.display.flip()
    pygame.display.update()


def show_color(pygame, screen, color, seconds, label):
    print(f"Showing {label}: {color} for {seconds:.1f} sec")
    screen.fill(color)
    flip_screen(pygame)

    t0 = time.time()
    while time.time() - t0 < seconds:
        if check_for_escape(pygame):
            return False
        time.sleep(0.01)
    return True


def main():
    configure_display_environment()

    import pygame

    pygame.init()

    print("Pygame:")
    print(f"  version={pygame.version.ver}")

    try:
        print(f"  display driver={pygame.display.get_driver()}")
    except Exception as exc:
        print(f"  display driver query failed: {exc}")

    try:
        print(f"  num displays={pygame.display.get_num_displays()}")
    except Exception as exc:
        print(f"  num displays query failed: {exc}")

    try:
        print(f"  desktop sizes={pygame.display.get_desktop_sizes()}")
    except Exception as exc:
        print(f"  desktop sizes query failed: {exc}")

    flags = pygame.DOUBLEBUF
    if FULLSCREEN:
        flags |= pygame.FULLSCREEN
        screen = pygame.display.set_mode((0, 0), flags)
    else:
        screen = pygame.display.set_mode(WINDOW_SIZE, flags)

    pygame.display.set_caption("VSTIM fullscreen test")
    pygame.mouse.set_visible(False)

    print(f"  created screen size={screen.get_size()}")
    print("Starting fullscreen visual test. Press ESC to quit early.")

    sequence = [
        ((128, 128, 128), COLOR_HOLD_SEC, "gray"),
        ((255, 255, 255), COLOR_HOLD_SEC, "white"),
        ((0, 0, 0), COLOR_HOLD_SEC, "black"),
        ((255, 0, 0), COLOR_HOLD_SEC, "red"),
        ((0, 255, 0), COLOR_HOLD_SEC, "green"),
        ((0, 0, 255), COLOR_HOLD_SEC, "blue"),
    ]

    try:
        for color, seconds, label in sequence:
            keep_going = show_color(pygame, screen, color, seconds, label)
            if not keep_going:
                print("Quit requested.")
                return

        print("Showing photodiode patch blink test.")
        t0 = time.time()

        while time.time() - t0 < BLINK_TEST_SEC:
            elapsed = time.time() - t0
            patch_on = int(elapsed / BLINK_PERIOD_SEC) % 2 == 0

            screen.fill((128, 128, 128))
            if patch_on:
                draw_photodiode_patch(pygame, screen, (255, 255, 255))
            else:
                draw_photodiode_patch(pygame, screen, (0, 0, 0))

            flip_screen(pygame)

            if check_for_escape(pygame):
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
