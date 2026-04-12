#!/usr/bin/env python3
"""Touch screen debugger - run this to see raw touch events"""
try:
    import evdev
except ImportError:
    print("evdev not installed: pip install evdev"); exit(1)

devices = [evdev.InputDevice(p) for p in evdev.list_devices()]

print("\n=== INPUT DEVICES ===")
for d in devices:
    caps = d.capabilities()
    has_abs = evdev.ecodes.EV_ABS in caps
    print(f"  {d.path:20s}  {d.name}")
    if has_abs:
        abs_codes = [code for code, _ in caps[evdev.ecodes.EV_ABS]]
        names = [evdev.ecodes.ABS[c] for c in abs_codes if c in evdev.ecodes.ABS]
        print(f"    └─ ABS axes: {names}")

print("\n=== LISTENING FOR TOUCH EVENTS (touch the screen, Ctrl+C to stop) ===")
print("    Looking for any device with ABS_X/ABS_Y or ABS_MT_POSITION_X/Y\n")

# Find touch device - check both regular and multitouch
touch_dev = None
for d in devices:
    caps = d.capabilities()
    if evdev.ecodes.EV_ABS not in caps: continue
    abs_codes = [code for code, _ in caps[evdev.ecodes.EV_ABS]]
    # match regular touch OR multitouch
    if d.path=="/dev/input/event7":
        if (evdev.ecodes.ABS_X in abs_codes or
            evdev.ecodes.ABS_MT_POSITION_X in abs_codes):
            touch_dev = d
            print(f"Found touch device: {d.name} ({d.path})")
            break

if not touch_dev:
    print("No touch device found!"); exit(1)

for event in touch_dev.read_loop():
    if event.type in (evdev.ecodes.EV_ABS, evdev.ecodes.EV_KEY):
        try:
            name = evdev.ecodes.bytype[event.type][event.code]
        except: name = str(event.code)
        print(f"  type={event.type} code={name:30s} value={event.value}")

