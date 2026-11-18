"""Generate irb/docs/wiring.svg (MAPQuest IRB rig wiring).

    python irb/docs/make_wiring.py

Pins and addresses follow irb/firmware/irb_fw/config.h; edit both together.
"""
from pathlib import Path

OUT = Path(__file__).resolve().parent / "wiring.svg"
W, H = 1500, 1060

C = {"3v3": "#d32f2f", "gnd": "#222222", "sda": "#1e63c6", "scl": "#c99700",
     "gpio": "#ef6c00", "tube": "#a8adb3", "box": "#37474f", "fill": "#f7f9fb",
     "sub": "#eef3f8", "note": "#5f6b7a", "warn": "#b26b00"}
out = []


def add(s):
    out.append(s)


def text(x, y, s, size=13, anchor="start", weight="normal", color="#1b1f24", italic=False):
    st = ' font-style="italic"' if italic else ""
    add(f'<text x="{x}" y="{y}" font-size="{size}" text-anchor="{anchor}" '
        f'font-weight="{weight}" fill="{color}"{st}>{s}</text>')


def box(x, y, w, h, fill=None, stroke=None, dash=False, r=8, sw=2):
    d = ' stroke-dasharray="6 4"' if dash else ""
    add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill or C["fill"]}" '
        f'stroke="{stroke or C["box"]}" stroke-width="{sw}"{d}/>')


def wire(points, color, width=3, dash=False):
    d = ' stroke-dasharray="7 5"' if dash else ""
    pts = " ".join(f"{x},{y}" for x, y in points)
    add(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{width}" '
        f'stroke-linejoin="round" stroke-linecap="round"{d}/>')


def dot(x, y, color):
    add(f'<circle cx="{x}" cy="{y}" r="5" fill="{color}"/>')


def pin(x, y, label, side, color):
    """A pin stub on a box edge; side 'L' = label inside to the right, 'R' = inside to the left."""
    add(f'<circle cx="{x}" cy="{y}" r="4" fill="white" stroke="{color}" stroke-width="2"/>')
    if side == "L":
        text(x + 10, y + 4, label, 12)
    else:
        text(x - 10, y + 4, label, 12, anchor="end")


def resistor_h(x1, x2, y, color):
    n, amp = 6, 7
    step = (x2 - x1) / (n * 2)
    pts = [(x1, y)]
    for i in range(n * 2):
        pts.append((x1 + step * (i + 0.5), y + (amp if i % 2 == 0 else -amp)))
    pts.append((x2, y))
    wire(pts, color, 2.5)


def resistor_v(x, y1, y2, color):
    n, amp = 6, 7
    step = (y2 - y1) / (n * 2)
    pts = [(x, y1)]
    for i in range(n * 2):
        pts.append((x + (amp if i % 2 == 0 else -amp), y1 + step * (i + 0.5)))
    pts.append((x, y2))
    wire(pts, color, 2.5)


# ---------------------------------------------------------------- frame
add(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
    f'font-family="Segoe UI, Helvetica, Arial, sans-serif">')
add(f'<rect width="{W}" height="{H}" fill="white"/>')
text(30, 38, "MAPQuest IRB rig: wiring", 24, weight="bold")
text(30, 60, "Firmware irb_fw 0.2.1 · irb/DESIGN.md §3 · flashed to a bare board only: "
     "this wiring has not yet been verified on hardware", 13, color=C["warn"])

# legend
lx = 990
for i, (k, name) in enumerate((("3v3", "3V3"), ("gnd", "GND"), ("sda", "SDA"), ("scl", "SCL"),
                               ("gpio", "GPIO25 / gate"), ("tube", "air tube"))):
    x = lx + (i % 3) * 165
    y = 26 + (i // 3) * 24
    wire([(x, y), (x + 34, y)], C[k], 8 if k == "tube" else 3)
    text(x + 42, y + 5, name, 12)

# ---------------------------------------------------------------- laptop + ESP32
box(30, 130, 150, 90)
text(105, 162, "Laptop", 15, "middle", "bold")
text(105, 182, "irb_session.py", 12, "middle")
text(105, 200, "(irb/host)", 11, "middle", color=C["note"])
wire([(180, 175), (230, 175)], "#7a7a7a", 6)
text(105, 240, "USB cable to the board:", 11, "middle", color=C["note"])
text(105, 254, "921600 baud serial", 11, "middle", color=C["note"])
text(105, 268, "and 5 V power", 11, "middle", color=C["note"])

box(230, 100, 200, 330)
text(330, 128, "ESP32-WROOM-32D", 15, "middle", "bold")
text(330, 146, "dev board", 12, "middle")
text(330, 162, "Arduino: ESP32 Dev Module", 11, "middle", color=C["note"])
text(330, 176, "USB-UART: CP210x", 11, "middle", color=C["note"])
pin(230, 175, "USB", "L", "#7a7a7a")
ESP = {"3v3": 210, "gnd": 240, "sda": 290, "scl": 320, "gpio": 395}
pin(430, ESP["3v3"], "3V3", "R", C["3v3"])
pin(430, ESP["gnd"], "GND", "R", C["gnd"])
pin(430, ESP["sda"], "GPIO21 · SDA", "R", C["sda"])
pin(430, ESP["scl"], "GPIO22 · SCL", "R", C["scl"])
pin(430, ESP["gpio"], "GPIO25 · valve", "R", C["gpio"])
text(330, 360, "avoid GPIO 0, 2, 5, 12, 15", 10, "middle", color=C["note"])
text(330, 373, "(boot strapping)", 10, "middle", color=C["note"])

# ---------------------------------------------------------------- buses
BX = {"3v3": 500, "gnd": 522, "sda": 544, "scl": 566}
MUX_X, MUX_W = 650, 210
MUXL = {"3v3": 210, "gnd": 240, "sda": 290, "scl": 320}       # aligned with ESP pins
MPR_Y0 = 600
MPRL = {"3v3": 640, "gnd": 670, "sda": 700, "scl": 730}
TOP = {"3v3": 82, "gnd": 98}

# ESP -> mux straight across, through the bus junctions
for k in ("3v3", "gnd", "sda", "scl"):
    wire([(434, ESP[k]), (MUX_X - 4, ESP[k])], C[k])
# vertical buses
wire([(BX["3v3"], TOP["3v3"]), (BX["3v3"], MPRL["3v3"])], C["3v3"])
wire([(BX["gnd"], TOP["gnd"]), (BX["gnd"], 1010)], C["gnd"])
wire([(BX["sda"], ESP["sda"]), (BX["sda"], MPRL["sda"])], C["sda"])
wire([(BX["scl"], ESP["scl"]), (BX["scl"], MPRL["scl"])], C["scl"])
for k in ("3v3", "gnd", "sda", "scl"):
    dot(BX[k], ESP[k], C[k])
    wire([(BX[k], MPRL[k]), (MUX_X - 4, MPRL[k])], C[k])
dot(BX["3v3"], MPRL["3v3"], C["3v3"])
dot(BX["gnd"], MPRL["gnd"], C["gnd"])
text(576, 566, "main I2C", 10, color=C["note"])
text(576, 579, "bus, 400 kHz", 10, color=C["note"])

# ---------------------------------------------------------------- TCA9548A
box(MUX_X, 120, MUX_W, 430)
text(MUX_X + MUX_W / 2, 146, "TCA9548A I2C mux", 15, "middle", "bold")
text(MUX_X + MUX_W / 2, 164, "0x70 (A0–A2 open)", 12, "middle")
for k, lab in (("3v3", "VIN"), ("gnd", "GND"), ("sda", "SDA"), ("scl", "SCL")):
    pin(MUX_X, MUXL[k], lab, "L", C[k])
MUXR = {"sd0": 260, "sc0": 290, "sd1": 470, "sc1": 500}
pin(MUX_X + MUX_W, MUXR["sd0"], "SD0", "R", C["sda"])
pin(MUX_X + MUX_W, MUXR["sc0"], "SC0", "R", C["scl"])
pin(MUX_X + MUX_W, MUXR["sd1"], "SD1", "R", C["sda"])
pin(MUX_X + MUX_W, MUXR["sc1"], "SC1", "R", C["scl"])
text(MUX_X + MUX_W - 14, 238, "ch0 → distal", 11, "end", color=C["note"])
text(MUX_X + MUX_W - 14, 448, "ch1 → proximal", 11, "end", color=C["note"])
text(MUX_X + 14, 372, "RST: leave open", 11, color=C["note"])
text(MUX_X + 14, 388, "ch2–7: unused", 11, color=C["note"])
text(MUX_X + 14, 404, "switches SDA/SCL only,", 11, color=C["note"])
text(MUX_X + 14, 420, "not power", 11, color=C["note"])

# ---------------------------------------------------------------- MPRLS
box(MUX_X, MPR_Y0, MUX_W, 160)
text(MUX_X + MUX_W / 2, MPR_Y0 + 24, "Adafruit MPRLS (3965)", 14, "middle", "bold")
text(MUX_X + MUX_W / 2, MPR_Y0 + 41, "0x18 · main bus", 12, "middle")
for k, lab in (("3v3", "VIN"), ("gnd", "GND"), ("sda", "SDA"), ("scl", "SCL")):
    pin(MUX_X, MPRL[k], lab, "L", C[k])
text(MUX_X + MUX_W - 12, MPR_Y0 + 120, "3Vo, RST, EOC:", 10, "end", color=C["note"])
text(MUX_X + MUX_W - 12, MPR_Y0 + 133, "not connected", 10, "end", color=C["note"])
PORT_Y = 690
add(f'<rect x="{MUX_X + MUX_W}" y="{PORT_Y - 9}" width="16" height="18" fill="{C["tube"]}"/>')
text(MUX_X + MUX_W - 10, PORT_Y + 4, "port", 11, "end", color=C["note"])

# ---------------------------------------------------------------- bands
BAND_X, BAND_W = 1010, 460
PWR_X = {"3v3": 950, "gnd": 972}
wire([(BX["3v3"], TOP["3v3"]), (PWR_X["3v3"], TOP["3v3"]), (PWR_X["3v3"], 420)], C["3v3"])
wire([(BX["gnd"], TOP["gnd"]), (PWR_X["gnd"], TOP["gnd"]), (PWR_X["gnd"], 450)], C["gnd"])
text(735, TOP["3v3"] - 6, "band power from the 3V3 rail (the mux does not pass power)", 11,
     "middle", color=C["note"])


def band(y0, name, where, ch, sd, sc):
    pins = {"3v3": y0 + 60, "gnd": y0 + 90, "sda": y0 + 120, "scl": y0 + 150}
    box(BAND_X, y0, BAND_W, 200, fill="white", dash=True, stroke="#78909c", sw=1.5)
    text(BAND_X + 12, y0 + 22, f"{name} band", 15, weight="bold")
    text(BAND_X + 12, y0 + 40, f"{where} · mux {ch} · one 4-wire (Qwiic) cable", 11, color=C["note"])
    for k, lab in (("3v3", "3V3"), ("gnd", "GND"), ("sda", "SDA"), ("scl", "SCL")):
        pin(BAND_X, pins[k], lab, "L", C[k])
    # power drops
    wire([(PWR_X["3v3"], pins["3v3"]), (BAND_X - 4, pins["3v3"])], C["3v3"])
    dot(PWR_X["3v3"], pins["3v3"], C["3v3"])
    wire([(PWR_X["gnd"], pins["gnd"]), (BAND_X - 4, pins["gnd"])], C["gnd"])
    dot(PWR_X["gnd"], pins["gnd"], C["gnd"])
    # mux channel -> band (route to the band's SDA/SCL rows)
    wire([(MUX_X + MUX_W + 4, sd), (915, sd), (915, pins["sda"]), (BAND_X - 4, pins["sda"])], C["sda"])
    wire([(MUX_X + MUX_W + 4, sc), (930, sc), (930, pins["scl"]), (BAND_X - 4, pins["scl"])], C["scl"])
    # chain: connector -> MAX30101 -> BMI270
    mx, bx_, cw = 1095, 1285, 170
    for k in ("3v3", "gnd", "sda", "scl"):
        wire([(BAND_X + 50, pins[k]), (mx, pins[k])], C[k], 2.5)
        wire([(mx + cw, pins[k]), (bx_, pins[k])], C[k], 2.5)
    box(mx, y0 + 46, cw, 140, fill=C["sub"])
    text(mx + cw / 2, y0 + 68, "MAX30101", 14, "middle", "bold")
    text(mx + cw / 2, y0 + 85, "SparkFun SEN-16474", 11, "middle")
    text(mx + cw / 2, y0 + 100, "0x57 · red + IR", 11, "middle")
    text(mx + cw / 2, y0 + 172, "INT: not connected", 10, "middle", color=C["note"])
    box(bx_, y0 + 46, cw, 140, fill=C["sub"])
    text(bx_ + cw / 2, y0 + 68, "BMI270", 14, "middle", "bold")
    text(bx_ + cw / 2, y0 + 85, "6-axis IMU breakout", 11, "middle")
    text(bx_ + cw / 2, y0 + 100, "0x68 (SDO low)", 11, "middle")
    text(bx_ + cw / 2, y0 + 172, "INT1/INT2: not connected", 10, "middle", color=C["note"])


band(130, "Distal", "forearm, below the cuff", "ch0", MUXR["sd0"], MUXR["sc0"])
band(370, "Proximal", "upper arm, above the cuff", "ch1", MUXR["sd1"], MUXR["sc1"])
text(BAND_X + BAND_W / 2, 588, "identical bands: the mux channel decides which is which", 11,
     "middle", color=C["note"])
text(BAND_X + BAND_W / 2, 602, "long proximal cable: drop I2C to 100 kHz if errors", 11,
     "middle", color=C["note"])

# ---------------------------------------------------------------- valve driver
GY = 905              # gate row
GND_Y = 1010
MX = 710                                                   # MOSFET gate plate x
SX = MX + 50                                               # drain / source lead x
wire([(434, ESP["gpio"]), (465, ESP["gpio"]), (465, GY), (480, GY)], C["gpio"])
resistor_h(480, 545, GY, C["gpio"])
text(512, GY - 16, "220 Ω", 12, "middle")
wire([(545, GY), (MX, GY)], C["gpio"])
dot(590, GY, C["gpio"])
resistor_v(590, GY + 15, GY + 75, C["gnd"])
wire([(590, GY), (590, GY + 15)], C["gpio"], 2.5)
wire([(590, GY + 75), (590, GND_Y)], C["gnd"])
text(603, GY + 50, "10 kΩ pull-down", 12)
text(603, GY + 65, "(valve stays open in reset)", 10, color=C["note"])
# MOSFET symbol
wire([(MX, GY - 22), (MX, GY + 22)], "#222", 3)            # gate plate
wire([(MX + 10, GY - 28), (MX + 10, GY + 28)], "#222", 3)  # channel
wire([(MX + 10, GY - 24), (SX, GY - 24), (SX, 870)], "#222", 2.5)   # drain
wire([(MX + 10, GY + 24), (SX, GY + 24), (SX, GND_Y)], C["gnd"], 2.5)  # source
add(f'<polygon points="{MX + 12},{GY + 24} {MX + 24},{GY + 18} {MX + 24},{GY + 30}" fill="#222"/>')
text(SX + 12, GY + 50, "N-ch logic-level MOSFET", 12, weight="bold")
text(SX + 12, GY + 66, "(part TBD) · D top, S bottom", 11, color=C["note"])
wire([(BX["gnd"], GND_Y), (SX, GND_Y)], C["gnd"])
dot(BX["gnd"], GND_Y, C["gnd"])
dot(590, GND_Y, C["gnd"])
text(530, GND_Y + 22, "common ground: tie the valve supply's − here", 11, color=C["note"])

# valve + flyback diode
VX, VY, VW, VH = 900, 820, 150, 100
NEG_Y, POS_Y = 870, 838
wire([(SX, NEG_Y), (VX - 4, NEG_Y)], "#222", 2.5)
dot(SX, NEG_Y, "#222")
dot(845, NEG_Y, "#222")
wire([(805, POS_Y), (VX - 4, POS_Y)], C["3v3"], 2.5)
wire([(805, POS_Y), (805, 780)], C["3v3"], 2.5, dash=True)
text(805, 772, "V_valve ≈ 3 V: 3V3 rail or own supply (TBD, §3.3)", 11, "middle", color=C["warn"])
dot(845, POS_Y, C["3v3"])
# diode, cathode toward + (top)
wire([(845, POS_Y), (845, 845)], "#222", 2)
wire([(845, 862), (845, NEG_Y)], "#222", 2)
add(f'<polygon points="836,862 854,862 845,846" fill="#222"/>')
wire([(835, 846), (855, 846)], "#222", 2.5)
text(812, 814, "flyback diode", 11, color=C["warn"])
text(812, 828, "(confirm fitted)", 10, color=C["warn"])
box(VX, VY, VW, VH)
pin(VX, POS_Y, "+", "L", C["3v3"])
pin(VX, NEG_Y, "−", "L", "#222")
text(VX + VW / 2, VY + 22, "Valve", 14, "middle", "bold")
text(VX + VW / 2, VY + 40, "Olimex AIR-E-VALVE-3V", 10, "middle")
text(VX + VW / 2, VY + 56, "NORMALLY OPEN", 11, "middle", "bold", C["warn"])
text(VX + VW / 2 + 14, VY + 72, "HIGH = closed", 10, "middle", color=C["note"])
text(VX + VW / 2 + 14, VY + 86, "~45 Ω coil", 10, "middle", color=C["note"])

# ---------------------------------------------------------------- pneumatics
MAN_X = 1150
TUBE = C["tube"]
wire([(MUX_X + MUX_W + 16, PORT_Y), (MAN_X, PORT_Y)], TUBE, 8)
wire([(MAN_X, 650), (MAN_X, 950)], TUBE, 8)
wire([(VX + VW, 870), (MAN_X, 870)], TUBE, 8)
wire([(VX + VW / 2, VY + VH), (VX + VW / 2, VY + VH + 40)], TUBE, 8)
add(f'<polygon points="{VX + VW / 2 - 10},{VY + VH + 40} {VX + VW / 2 + 10},{VY + VH + 40} '
    f'{VX + VW / 2},{VY + VH + 54}" fill="{TUBE}"/>')
text(VX + VW / 2 + 16, VY + VH + 48, "vent to room", 11, color=C["note"])
wire([(MAN_X, 650), (1220, 650)], TUBE, 8)
wire([(MAN_X, 950), (1220, 950)], TUBE, 8)
for y in (PORT_Y, 870):
    add(f'<circle cx="{MAN_X}" cy="{y}" r="7" fill="{TUBE}"/>')
box(1220, 615, 250, 75, fill="#fff7ed", stroke="#8d6e63")
text(1345, 645, "Upper-arm cuff", 14, "middle", "bold")
text(1345, 664, "size by arm circumference (AHA)", 11, "middle", color=C["note"])
box(1220, 915, 250, 75, fill="#fff7ed", stroke="#8d6e63")
text(1345, 943, "Hand bulb + release screw", 14, "middle", "bold")
text(1345, 962, "inflation · mechanical dump", 11, "middle", color=C["note"])
text(MAN_X + 14, 790, "cuff hose", 11, color=C["note"])
text(MAN_X + 14, 804, "manifold / tees", 11, color=C["note"])

# ---------------------------------------------------------------- notes
box(30, 470, 400, 400, fill="#fafafa", stroke="#cfd8dc", sw=1.2)
text(46, 496, "Before powering", 14, weight="bold")
notes = [
    "1. Flyback diode across the valve coil",
    "   (e.g. 1N5819 / 1N4148), cathode to +.",
    "2. 10 kΩ gate pull-down fitted: it keeps",
    "   the valve OPEN (cuff vented) while the",
    "   ESP32 resets or is unpowered.",
    "3. Decide the valve supply. If it shares",
    "   3V3, check the I2C lines for dips while",
    "   VALVE TEST clicks; else use its own",
    "   supply with a common ground.",
    "4. MOSFET must switch fully at 3.3 V gate",
    "   drive (logic-level, low Vgs(th)).",
    "5. Pull-ups: mux and MPRLS breakouts have",
    "   their own; each band adds its own on",
    "   its channel only.",
    "6. Keep the bulb release screw in the line.",
    "7. Then run the phase 1 checklist",
    "   (irb/README.md): SCAN should show 0x18",
    "   on the main bus, 0x57 + 0x68 on ch0/ch1.",
]
for i, n in enumerate(notes):
    text(46, 520 + i * 19, n, 12)

add("</svg>")
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text("\n".join(out), encoding="utf-8")
print("wrote", OUT)
