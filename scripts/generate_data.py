#!/usr/bin/env python3
"""Generate the retail demo's sample data into data/*.json. Deterministic.

Every product carries what the room planner needs to reason with: real
dimensions in inches, a `kind` (desk, chair, printer, ...), colour and style,
and the links between things that must match — which cartridges fit which
printer, how long an HDMI cable is, how big a screen or whiteboard is.

    python scripts/generate_data.py
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import DATA_DIR  # noqa: E402

rng = random.Random(20261008)

COLORS = {
    "Black": "#222222", "White": "#F2F2F2", "Gray": "#8A8D91", "Espresso": "#4B3621",
    "Walnut": "#773F1A", "Natural Oak": "#C8A165", "Navy": "#1F3A5F", "Blue": "#2E6DB4",
    "Red": "#CC0000", "Green": "#3A7D44", "Silver": "#BFC4C9", "Brown": "#7A4E2D",
    "Charcoal": "#36454F", "Teal": "#1A7F7A", "Clear": "#DDE6EA",
    "Yellow": "#F2C94C", "Brass": "#B5893A", "Beige": "#D8C9A8", "Ivory": "#F6F0DC",
    "Pastel Blue": "#BFD8EE", "Orange": "#E07B39", "Cyan": "#00A3E0", "Magenta": "#E6007E",
    "Pink": "#F4A7B9", "Purple": "#7B3FA0", "Assorted": "#9B59B6",
}

BRANDS = {
    "furniture": ["Bancroft", "Halden", "Kinsley Home", "Ardent Office", "Novus Works"],
    "seating": ["Ergonova", "Halden", "Ardent Office", "Posture Lab"],
    "tech": ["TechWave", "ClearView", "PrintPro", "Lumina"],
    "supplies": ["Inkwell", "Marker Max", "OfficeEssentials", "Brightline"],
}

products: list[dict] = []
_seq = {"n": 1000}


def _sku(prefix: str) -> str:
    _seq["n"] += 1
    return f"{prefix}-{_seq['n']}"


def price_jitter(base: float) -> float:
    """Retail-looking prices: $124.99, not $124.36."""
    if base < 20:
        return round(base, 2)
    return round(base * rng.uniform(0.92, 1.08)) - 0.01


def add(base: dict, colors: list[str], prefix: str) -> list[dict]:
    """One base item -> one SKU per colour. Variants share `base_id`."""
    base_id = _sku(prefix)
    out = []
    for i, color in enumerate(colors):
        doc = {
            "type": "product",
            "sku": f"{base_id}-{color.upper().replace(' ', '')[:4]}",
            "base_id": base_id,
            "color": color,
            "color_hex": COLORS[color],
            "rating": round(rng.uniform(3.8, 4.9), 1),
            "reviews": rng.randint(12, 2400),
            "stock": rng.choice([0, 4, 12, 25, 60, 140, 300]) if i else rng.choice(
                [12, 25, 60, 140, 300]),
            **base,
        }
        doc["price"] = price_jitter(base["price"])
        doc["base_name"] = base["name"]
        doc["name"] = f"{base['name']}, {color}"
        out.append(doc)
    products.extend(out)
    return out


# --------------------------------------------------------------------------- #
# The catalogue: about 40 products, each in the colours real photos exist for.
# `photo` is the search phrase used to find a matching photo; {color} is filled
# in per variant (chosen by hand).
# --------------------------------------------------------------------------- #
ALL_ROOMS = ["home office", "private office", "open office", "conference room",
             "bedroom office"]
DESK_FINISHES = ["Walnut", "White", "Black", "Natural Oak"]
CHAIR_COLORS = ["Black", "Gray", "White", "Blue"]

# Desks
add({"category": "Furniture", "subcategory": "Desks", "kind": "desk",
     "name": 'Writing Desk 48"W x 24"D', "brand": "Kinsley Home", "style": "modern",
     "material": "engineered wood, steel legs", "dims": {"w": 48, "d": 24, "h": 30},
     "seats": 1, "price": 169, "features": ["cable grommet", "levelling feet"],
     "room_tags": ["home office", "bedroom office"], "photo": "{color} wooden writing desk"},
    DESK_FINISHES, "DSK")
add({"category": "Furniture", "subcategory": "Desks", "kind": "desk",
     "name": 'Executive Desk with Drawers 60"W x 30"D', "brand": "Bancroft", "style": "classic",
     "material": "wood veneer", "dims": {"w": 60, "d": 30, "h": 30}, "seats": 1, "price": 349,
     "features": ["3 storage drawers", "cable grommet"],
     "room_tags": ["private office", "home office"], "photo": "{color} executive office desk"},
    ["Walnut", "White", "Black"], "DSK")
for w, d, price in [(48, 30, 349), (60, 30, 449)]:
    add({"category": "Furniture", "subcategory": "Standing Desks", "kind": "desk",
         "name": f'Electric Standing Desk {w}"W x {d}"D', "brand": "Novus Works",
         "style": "modern", "material": "steel frame, laminate top",
         "dims": {"w": w, "d": d, "h": 29}, "seats": 1, "price": price, "standing": True,
         "features": ["dual motor", "height 25\"-50\"", "4 memory presets"],
         "room_tags": ["home office", "private office", "open office", "bedroom office"],
         "photo": "{color} standing desk"}, ["Black", "White", "Walnut"], "DSK")
add({"category": "Furniture", "subcategory": "L-Shaped Desks", "kind": "desk",
     "name": 'L-Shaped Corner Desk 60"x60"', "brand": "Halden", "style": "modern",
     "material": "engineered wood", "dims": {"w": 60, "d": 60, "h": 30}, "seats": 1,
     "price": 459, "l_shaped": True, "features": ["reversible return", "cable tray"],
     "room_tags": ["home office", "private office"], "photo": "{color} l-shaped corner desk"},
    ["Black", "White", "Walnut"], "DSK")
add({"category": "Furniture", "subcategory": "Benching Workstations", "kind": "desk",
     "name": '4-Person Benching Workstation 120"x60"', "brand": "Novus Works",
     "style": "modern", "material": "steel frame, laminate tops",
     "dims": {"w": 120, "d": 60, "h": 30}, "seats": 4, "price": 1199, "benching": True,
     "features": ["privacy screens", "shared power rail"], "room_tags": ["open office"],
     "photo": "{color} open plan office desks"}, ["White", "Gray"], "DSK")

# Chairs
CHAIR_USE = {"Mesh Task Chair": "task", "Ergonomic Mesh Chair with Lumbar Support": "task",
             "Executive Leather Chair": "executive", "Guest Chair with Arms": "guest"}
for label, price, feats, style, rooms, photo in [
    ("Mesh Task Chair", 129, ["breathable mesh back", "adjustable height", "swivel"],
     "modern", ["home office", "open office", "bedroom office"], "{color} mesh office chair"),
    ("Ergonomic Mesh Chair with Lumbar Support", 299,
     ["adjustable lumbar", "4D armrests", "seat slide", "headrest"], "modern",
     ["home office", "private office", "open office", "bedroom office"],
     "{color} ergonomic office chair"),
    ("Executive Leather Chair", 249, ["high back", "padded arms", "tilt lock"], "classic",
     ["private office"], "{color} leather executive office chair"),
    ("Guest Chair with Arms", 119, ["padded seat", "stackable 2-high"], "classic",
     ["private office", "conference room"], "{color} office guest chair"),
]:
    add({"category": "Furniture", "subcategory": "Office Chairs", "kind": "chair",
         "name": label, "brand": rng.choice(BRANDS["seating"]), "style": style,
         "material": "mesh" if "Mesh" in label else ("leather" if "Leather" in label
                                                      else "fabric"),
         "dims": {"w": 26, "d": 26, "h": 42}, "price": price, "pack_qty": 1,
         "use": CHAIR_USE[label], "features": feats, "room_tags": rooms, "photo": photo},
        ["Black", "Gray", "White"] if "Leather" not in label else ["Black", "Brown", "White"],
        "CHR")

# Conference tables
for seats, w, d, price, shape, photo in [
        (4, 48, 48, 399, "Round", "{color} round meeting table"),
        (8, 96, 42, 949, "Rectangular", "{color} conference room table"),
        (10, 120, 48, 1299, "Boat-Shaped", "{color} boardroom table")]:
    add({"category": "Furniture", "subcategory": "Conference Tables",
         "kind": "conference_table", "name": f"{shape} Conference Table, seats {seats}",
         "shape": shape.split("-")[0].lower(),
         "brand": "Bancroft", "style": "modern", "material": "laminate top, steel base",
         "dims": {"w": w, "d": d, "h": 30}, "seats": seats, "price": price,
         "features": ["power/data grommet"],
         "room_tags": ["conference room"] + (["private office"] if seats <= 4 else []),
         "photo": photo}, ["Walnut", "White", "Gray"] if seats > 4 else ["Walnut", "White"],
        "CNF")

# Storage
add({"category": "Furniture", "subcategory": "Storage", "kind": "storage",
     "name": "2-Drawer Vertical File Cabinet", "brand": "Ardent Office", "style": "modern",
     "material": "steel", "dims": {"w": 15, "d": 22, "h": 28}, "price": 139,
     "features": ["locking", "full-extension drawers"],
     "room_tags": ["home office", "private office", "open office"],
     "photo": "{color} filing cabinet"}, ["Black", "White", "Gray"], "STO")
add({"category": "Furniture", "subcategory": "Storage", "kind": "storage",
     "name": "5-Shelf Bookcase", "brand": "Kinsley Home", "style": "classic",
     "material": "engineered wood", "dims": {"w": 30, "d": 12, "h": 72}, "price": 149,
     "features": ["adjustable shelves", "anti-tip kit"],
     "room_tags": ["home office", "private office", "bedroom office"],
     "photo": "{color} bookshelf"}, ["Walnut", "White", "Black"], "STO")

# Printers and the cartridges that fit them
PRINTERS = [
    ("Wireless All-in-One Inkjet Printer", "inkjet", 129, 5, ["print", "scan", "copy"],
     ["home office", "bedroom office"], "{color} inkjet printer"),
    ("Monochrome Laser Printer", "laser", 199, 10, ["40 ppm", "auto duplex", "Ethernet"],
     ["private office", "home office"], "{color} laser printer"),
    ("Color Laser Multifunction Printer", "laser", 499, 20,
     ["print, scan, copy, fax", "50-sheet ADF", "touchscreen"], ["open office", "private office"],
     "office multifunction printer"),
]
for label, tech, price, users, feats, rooms, photo in PRINTERS:
    p = add({"category": "Technology", "subcategory": "Printers", "kind": "printer",
             "name": label, "brand": "PrintPro", "style": "modern", "material": "plastic",
             "printer_type": tech, "users_supported": users,
             "dims": {"w": 17, "d": 15, "h": 12 if users < 20 else 20}, "price": price,
             "features": feats, "room_tags": rooms, "photo": photo},
            ["White", "Black"] if users < 20 else ["White"], "PRN")
    family = p[0]["base_id"]
    for p_ in p:
        p_["cartridge_family"] = family
    ink = "Ink" if tech == "inkjet" else "Toner"
    cartridges = ([("Black", 1.0)] + ([("Tri-Color", 1.15)] if tech == "inkjet" else [])
                  + ([("Cyan/Magenta/Yellow 3-Pack", 2.4)] if users >= 20 else []))
    for color_name, mult in cartridges:
        price_c = (29 if tech == "inkjet" else 79) * mult
        yield_ = 300 if tech == "inkjet" else 2500
        add({"category": "Ink & Toner", "subcategory": f"{ink} Cartridges",
             "kind": "cartridge", "name": f"{color_name} {ink} Cartridge for {label}",
             "brand": "PrintPro", "style": "standard", "material": ink.lower(),
             "dims": {"w": 5, "d": 3, "h": 4}, "price": round(price_c, 2),
             "page_yield": yield_, "compatible_printers": [family],
             "role": "black" if color_name == "Black" else "color",
             "cart_style": "ink" if tech == "inkjet" else "toner",
             "compact_toner": users >= 20,
             "pack_qty": 3 if "3-Pack" in color_name else 1,
             "features": [f"~{yield_:,} page yield", "genuine"], "room_tags": rooms,
             "photo": "printer ink cartridges" if tech == "inkjet" else "toner cartridge"},
            ["Black"], "CRT")

# Displays, cables, power
for size, price in [(43, 279), (55, 429), (65, 599), (75, 899)]:
    add({"category": "Technology", "subcategory": "TVs & Displays", "kind": "tv",
         "name": f'{size}" 4K UHD Smart Display', "brand": "ClearView", "style": "modern",
         "material": "aluminum", "screen_in": size,
         "dims": {"w": round(size * 0.87), "d": 3, "h": round(size * 0.49)}, "price": price,
         "features": ["4K HDR", "3 HDMI inputs", "screen mirroring", "VESA mount"],
         "room_tags": ["conference room", "open office", "private office", "home office"],
         "photo": "flat screen tv"}, ["Black"], "TV")
add({"category": "Technology", "subcategory": "Monitors", "kind": "monitor",
     "name": '27" QHD Monitor', "brand": "Lumina", "style": "modern", "material": "plastic",
     "screen_in": 27, "dims": {"w": 24, "d": 8, "h": 16}, "price": 229,
     "features": ["HDMI + DisplayPort", "height adjustable stand"],
     "room_tags": ["home office", "private office", "open office", "bedroom office"],
     "photo": "{color} computer monitor on desk"}, ["Black", "White"], "MON")
for ft, price in [(6, 12.99), (15, 24.99)]:
    add({"category": "Technology", "subcategory": "Cables", "kind": "hdmi_cable",
         "name": f"High-Speed HDMI 2.1 Cable, {ft} ft", "brand": "TechWave",
         "style": "standard", "material": "braided nylon", "length_ft": ft,
         "dims": {"w": 6, "d": 6, "h": 2}, "price": price,
         "features": ["4K@120Hz / 8K", "gold-plated connectors"],
         "room_tags": ["conference room", "open office", "private office", "home office"],
         "photo": "{color} hdmi cable"}, ["Black", "White"], "CBL")
add({"category": "Technology", "subcategory": "Power & Cable Management",
     "kind": "power_strip", "name": "8-Outlet Surge Protector with USB-C",
     "brand": "TechWave", "style": "standard", "material": "plastic",
     "dims": {"w": 12, "d": 3, "h": 2}, "price": 29.99,
     "features": ["2,000 J surge rating", "6 ft cord", "2 USB-C"], "room_tags": ALL_ROOMS,
     "photo": "{color} power strip"}, ["White", "Black"], "PWR")

# Whiteboards and the things that go with them
for w_ft, h_ft, price, mobile in [(4, 3, 79.99, False), (6, 4, 159.99, False),
                                  (6, 4, 349.99, True)]:
    add({"category": "Office Supplies", "subcategory": "Whiteboards", "kind": "whiteboard",
         "name": f"{'Mobile Double-Sided ' if mobile else 'Magnetic Dry-Erase '}Whiteboard "
                 f"{w_ft}' x {h_ft}'", "brand": "Marker Max", "style": "standard",
         "material": "porcelain steel", "board_w_ft": w_ft, "board_h_ft": h_ft,
         "mobile": mobile,
         "dims": {"w": w_ft * 12, "d": 24 if mobile else 1,
                  "h": h_ft * 12 + (30 if mobile else 0)},
         "price": price, "features": ["aluminum frame", "marker tray"] + (
             ["locking casters"] if mobile else []),
         "room_tags": ["conference room", "open office", "private office", "home office"],
         "photo": "mobile whiteboard" if mobile else "office whiteboard"}, ["Silver"], "WBD")
add({"category": "Office Supplies", "subcategory": "Markers", "kind": "dry_erase_marker",
     "name": "Low-Odor Dry-Erase Markers, 8-Pack", "brand": "Marker Max", "style": "standard",
     "material": "ink", "pack_qty": 8, "dims": {"w": 6, "d": 4, "h": 1}, "price": 11.99,
     "features": ["low odor", "assorted colors"], "room_tags": ALL_ROOMS,
     "photo": "dry erase markers"}, ["Black"], "MKR")
add({"category": "Office Supplies", "subcategory": "Markers", "kind": "permanent_marker",
     "name": "Permanent Markers, Fine Point, 12-Pack", "brand": "Marker Max",
     "style": "standard", "material": "ink", "pack_qty": 12, "dims": {"w": 6, "d": 3, "h": 1},
     "price": 13.99, "features": ["quick-drying", "fade resistant"], "room_tags": ALL_ROOMS,
     "photo": "permanent markers"}, ["Black"], "MKR")
add({"category": "Office Supplies", "subcategory": "Erasers", "kind": "whiteboard_eraser",
     "name": "Magnetic Whiteboard Eraser", "brand": "OfficeEssentials", "style": "standard",
     "material": "felt", "dims": {"w": 5, "d": 2, "h": 2}, "price": 5.49,
     "features": ["non-abrasive felt"], "room_tags": ALL_ROOMS,
     "photo": "whiteboard eraser"}, ["Gray"], "ERS")

# Paper and everyday supplies
for label, reams, price, photo in [
        ('Multipurpose Copy Paper, 8.5" x 11", 1 Ream', 1, 8.99, "ream of printer paper"),
        ('Multipurpose Copy Paper, 8.5" x 11", 10-Ream Case', 10, 54.99, "box of paper")]:
    add({"category": "Office Supplies", "subcategory": "Paper", "kind": "paper",
         "name": label, "brand": "Brightline", "style": "standard", "material": "paper",
         "reams": reams, "paper_use": "copy", "sheet": "letter",
         "dims": {"w": 12, "d": 9, "h": 2 * reams}, "price": price,
         "features": ["92 bright", "jam-free"], "room_tags": ALL_ROOMS, "photo": photo},
        ["White"], "PPR")
for label, kind, price, colors, photo in [
        ("Sticky Notes 3\" x 3\", 12 Pads", "sticky_notes", 11.99, ["Yellow"], "sticky notes"),
        ("Ballpoint Pens, Medium, 12-Pack", "pens", 7.99, ["Blue", "Black"], "{color} pens"),
        ("Full-Strip Desk Stapler", "stapler", 14.99, ["Black", "Red"], "{color} stapler"),
        ("Desk Organizer, 5 Compartments", "organizer", 24.99, ["Black", "White"],
         "{color} desk organizer")]:
    add({"category": "Office Supplies", "subcategory": "Desk Accessories", "kind": kind,
         "name": label, "brand": "OfficeEssentials", "style": "standard",
         "material": "plastic", "dims": {"w": 10, "d": 6, "h": 4}, "price": price,
         "features": ["everyday essential"], "room_tags": ALL_ROOMS, "photo": photo},
        colors, "SUP")

# Bins, lamps, rugs, mats
add({"category": "Breakroom & Facilities", "subcategory": "Trash & Recycling",
     "kind": "wastebasket", "name": "Office Wastebasket, 7 gal", "brand": "OfficeEssentials",
     "style": "standard", "material": "plastic", "gallons": 7,
     "dims": {"w": 12, "d": 12, "h": 14}, "price": 16.99, "features": ["easy-clean"],
     "room_tags": ALL_ROOMS, "photo": "{color} trash can"}, ["Red", "Blue", "Ivory", "Black",
                                                            "White", "Gray"], "BIN")
add({"category": "Breakroom & Facilities", "subcategory": "Trash & Recycling",
     "kind": "recycling_bin", "name": "Recycling Bin, 7 gal", "brand": "OfficeEssentials",
     "style": "standard", "material": "recycled plastic", "gallons": 7,
     "dims": {"w": 12, "d": 12, "h": 14}, "price": 17.99, "features": ["recycled plastic"],
     "room_tags": ALL_ROOMS, "photo": "{color} recycling bin"}, ["Blue", "Green"], "BIN")
add({"category": "Furniture", "subcategory": "Lighting", "kind": "lamp",
     "name": "LED Desk Lamp with USB Charging", "brand": "Kinsley Home", "style": "modern",
     "material": "metal", "dims": {"w": 8, "d": 8, "h": 18}, "price": 39.99,
     "features": ["dimmable", "3 colour temperatures"],
     "room_tags": ["home office", "bedroom office", "private office"],
     "photo": "{color} desk lamp"}, ["Black", "White", "Brass"], "LMP")
add({"category": "Furniture", "subcategory": "Lighting", "kind": "lamp",
     "name": "Arc Floor Lamp", "brand": "Kinsley Home", "style": "modern",
     "material": "metal", "dims": {"w": 14, "d": 14, "h": 70}, "price": 89.99,
     "features": ["dimmable", "marble base"],
     "room_tags": ["home office", "bedroom office", "private office"],
     "photo": "{color} floor lamp"}, ["Black", "Brass"], "LMP")
add({"category": "Furniture", "subcategory": "Rugs & Mats", "kind": "rug",
     "name": "Low-Pile Area Rug 5' x 7'", "brand": "Kinsley Home", "style": "modern",
     "material": "polypropylene", "dims": {"w": 60, "d": 84, "h": 1}, "price": 89.99,
     "features": ["chair-caster friendly"], "room_tags": ["home office", "bedroom office"],
     "photo": "{color} area rug"}, ["Gray", "Blue", "Beige"], "RUG")
add({"category": "Furniture", "subcategory": "Rugs & Mats", "kind": "chair_mat",
     "name": 'Polycarbonate Chair Mat 36" x 48"', "brand": "OfficeEssentials",
     "style": "standard", "material": "polycarbonate", "dims": {"w": 36, "d": 48, "h": 0},
     "price": 49.99, "features": ["for carpet and hard floors"],
     "room_tags": ["home office", "private office", "open office", "bedroom office"],
     "photo": "office chair on floor mat"}, ["Clear"], "MAT")

# More meeting tables: every shape in a few sizes. Added last so the SKUs and
# prices of everything above stay as they were.
for seats, w, d, price, shape, label, colors in [
        (4, 42, 42, 329, "round", 'Round Meeting Table 42"', ["White", "Walnut"]),
        (6, 60, 60, 599, "round", 'Round Conference Table 60"', ["Walnut", "White"]),
        (4, 42, 42, 379, "square", 'Square Meeting Table 42"', ["White", "Walnut"]),
        (8, 60, 60, 849, "square", 'Square Conference Table 60"', ["Walnut", "Gray"]),
        (6, 72, 36, 649, "rectangular", 'Rectangular Conference Table 72"', ["White", "Walnut"]),
        (12, 144, 48, 1799, "rectangular", 'Rectangular Conference Table 144"',
         ["Walnut", "Gray"])]:
    add({"category": "Furniture", "subcategory": "Conference Tables",
         "kind": "conference_table", "name": f"{label}, seats {seats}", "shape": shape,
         "brand": "Bancroft", "style": "modern", "material": "laminate top, steel base",
         "dims": {"w": w, "d": d, "h": 30}, "seats": seats, "price": price,
         "features": ["power/data grommet"] if seats > 4 else ["compact footprint"],
         "room_tags": ["conference room"] + (["private office"] if seats <= 4 else []),
         "photo": f"{{color}} {shape} meeting table"}, colors, "CNF")

# Boardrooms, huddle rooms, executive suites and video calls. Added last, again,
# so nothing above changes.
for seats, w, d, price in [(16, 192, 54, 2899), (22, 240, 60, 3999)]:
    add({"category": "Furniture", "subcategory": "Conference Tables",
         "kind": "conference_table", "name": f"Boardroom Table {w // 12}', seats {seats}",
         "shape": "rectangular", "brand": "Bancroft", "style": "classic",
         "material": "wood veneer top, steel base", "dims": {"w": w, "d": d, "h": 30},
         "seats": seats, "price": price,
         "features": ["power/data modules", "modular sections", "cable trough"],
         "room_tags": ["boardroom"], "photo": "{color} boardroom table"},
        ["Walnut", "Gray"], "BRD")
add({"category": "Furniture", "subcategory": "Conference Tables", "kind": "conference_table",
     "name": 'Huddle High-Top Table 72", seats 4', "shape": "rectangular", "high_top": True,
     "brand": "Novus Works", "style": "modern", "material": "laminate top, steel legs",
     "dims": {"w": 72, "d": 36, "h": 42}, "seats": 4, "price": 549,
     "features": ["counter height", "power module"], "room_tags": ["huddle room"],
     "photo": "{color} high top table"}, ["White", "Walnut"], "HUD")
add({"category": "Furniture", "subcategory": "Office Chairs", "kind": "chair",
     "name": "Counter-Height Stool", "brand": "Ardent Office", "style": "modern",
     "material": "molded seat, steel frame", "dims": {"w": 18, "d": 18, "h": 40},
     "price": 119, "use": "stool", "features": ["footrest", "26\" seat height"],
     "room_tags": ["huddle room"],
     "photo": "{color} bar stool"}, ["Black", "Gray"], "STL")
add({"category": "Furniture", "subcategory": "L-Shaped Desks", "kind": "desk",
     "name": 'U-Shaped Executive Desk 72"', "brand": "Bancroft", "style": "classic",
     "material": "wood veneer", "dims": {"w": 72, "d": 84, "h": 30}, "seats": 1,
     "price": 1499, "u_shaped": True,
     "features": ["bridge and hutch-ready return", "locking drawers", "cable management"],
     "room_tags": ["executive suite"], "photo": "{color} executive desk"},
    ["Walnut", "Black"], "DSK")
add({"category": "Furniture", "subcategory": "Tables", "kind": "side_table",
     "name": 'Round Side Table 24"', "brand": "Kinsley Home", "style": "modern",
     "material": "wood veneer", "dims": {"w": 24, "d": 24, "h": 22}, "price": 129,
     "features": ["fits beside guest chairs"], "room_tags": ["executive suite",
                                                         "private office"],
     "photo": "{color} side table"}, ["Walnut", "White"], "TBL")
add({"category": "Furniture", "subcategory": "Storage", "kind": "storage",
     "name": 'Credenza with Doors 72"', "brand": "Bancroft", "style": "classic",
     "material": "wood veneer", "dims": {"w": 72, "d": 20, "h": 30}, "price": 699,
     "features": ["adjustable shelves", "cable cut-outs", "catering-ready top"],
     "room_tags": ["boardroom", "executive suite"], "photo": "{color} credenza"},
    ["Walnut", "White", "Gray"], "STO")
for name, kind, price, dims, feats, rooms, photo in [
        ("4K Conference Camera", "camera", 599, {"w": 10, "d": 4, "h": 3},
         ["120° field of view", "auto-framing", "mounts above a display"],
         ["boardroom", "conference room", "private office", "executive suite"],
         "conference camera"),
        ("Video Bar (camera, speakers, mics)", "video_bar", 899, {"w": 30, "d": 4, "h": 3},
         ["all-in-one for small rooms", "auto-framing", "noise suppression"],
         ["huddle room", "conference room"], "video conference"),
        ("Conference Speakerphone", "speaker", 249, {"w": 9, "d": 9, "h": 2},
         ["360° microphones", "covers 10 seats", "USB and Bluetooth"],
         ["boardroom", "conference room", "huddle room", "private office",
          "executive suite"], "conference speakerphone"),
        ("Soundbar for Meeting Rooms", "speaker", 349, {"w": 36, "d": 4, "h": 3},
         ["clear voice for large rooms", "mounts under a display"],
         ["boardroom", "conference room"], "soundbar")]:
    add({"category": "Technology", "subcategory": "Video Conferencing", "kind": kind,
         "name": name, "brand": "ClearView", "style": "modern", "material": "aluminum",
         "dims": dims, "price": price, "features": feats, "room_tags": rooms,
         "photo": photo}, ["Black"], "AV")
add({"category": "Breakroom & Facilities", "subcategory": "Trash & Recycling",
     "kind": "wastebasket", "name": "Small Desk-Side Wastebasket, 3 gal",
     "brand": "OfficeEssentials", "style": "modern", "material": "steel mesh",
     "dims": {"w": 10, "d": 10, "h": 12}, "price": 12.99, "gallons": 3,
     "features": ["fits under a desk or table"],
     "room_tags": ["boardroom", "conference room", "huddle room", "executive suite",
                   "private office", "home office", "bedroom office"],
     "photo": "{color} small trash can"}, ["Black", "Gray"], "BIN")


# More choice in every aisle (round 4). Each product lists every colour it
# might come in; only the colours a reviewed photo was found for are kept.
WOODS = ["Walnut", "Natural Oak", "White", "Black", "Espresso", "Gray"]
SEATS = ["Black", "Gray", "White", "Navy", "Brown", "Teal"]
for name, w, d, price, feats, rooms, style in [
        ('Compact Desk 40"W x 20"D', 40, 20, 129, ["fits small spaces", "cable grommet"],
         ["home office", "bedroom office"], "modern"),
        ('Trestle Desk 60"W x 30"D', 60, 30, 289, ["solid wood top", "steel trestle legs"],
         ["home office", "private office"], "industrial"),
        ('Glass-Top Desk 55"W x 24"D', 55, 24, 249, ["tempered glass top", "steel frame"],
         ["home office", "private office"], "modern")]:
    add({"category": "Furniture", "subcategory": "Desks", "kind": "desk", "name": name,
         "brand": rng.choice(BRANDS["furniture"]), "style": style,
         "material": "glass, steel" if "Glass" in name else "wood, steel",
         "dims": {"w": w, "d": d, "h": 30}, "seats": 1, "price": price, "features": feats,
         "room_tags": rooms, "photo": "{color} desk"}, WOODS, "DSK")
for name, use, price, feats, rooms, dims in [
        ("Conference Room Chair", "meeting", 179, ["mid back", "swivel and tilt", "casters"],
         ["conference room", "boardroom", "huddle room"], {"w": 25, "d": 25, "h": 38}),
        ("Stacking Chair", "stacking", 69, ["stacks 10 high", "polypropylene shell"],
         ["conference room", "huddle room"], {"w": 21, "d": 21, "h": 32}),
        ("Drafting Stool with Footring", "drafting", 159,
         ["seat height 22\"-32\"", "foot ring", "swivel"], ["open office", "huddle room"],
         {"w": 24, "d": 24, "h": 48}),
        ("Lounge Armchair", "lounge", 399, ["upholstered", "solid wood legs"],
         ["executive suite", "private office", "home office"], {"w": 30, "d": 32, "h": 32}),
        ("Swivel Desk Chair with Arms", "task", 99, ["padded seat", "adjustable height"],
         ["home office", "bedroom office", "open office"], {"w": 25, "d": 25, "h": 40})]:
    add({"category": "Furniture", "subcategory": "Office Chairs", "kind": "chair",
         "name": name, "brand": rng.choice(BRANDS["seating"]), "style": "modern",
         "material": "fabric", "dims": dims, "price": price, "pack_qty": 1, "use": use,
         "features": feats, "room_tags": rooms, "photo": "{color} chair"}, SEATS, "CHR")
for name, dims, price, feats, rooms in [
        ("3-Drawer Mobile Pedestal", {"w": 16, "d": 20, "h": 24}, 159,
         ["locking", "fits under a desk", "casters"], ["home office", "open office"]),
        ('Lateral File Cabinet, 2 Drawers 36"', {"w": 36, "d": 18, "h": 28}, 349,
         ["letter and legal hanging files", "locking"], ["open office", "private office"]),
        ("Cube Storage Organizer, 3 x 3", {"w": 44, "d": 12, "h": 44}, 119,
         ["9 cubes", "fabric bins fit"], ["home office", "bedroom office"]),
        ('Steel Storage Cabinet 72"', {"w": 36, "d": 18, "h": 72}, 449,
         ["4 adjustable shelves", "locking doors"], ["open office", "private office"])]:
    add({"category": "Furniture", "subcategory": "Storage", "kind": "storage", "name": name,
         "brand": rng.choice(BRANDS["furniture"]), "style": "modern",
         "material": "steel" if "Steel" in name or "File" in name else "engineered wood",
         "dims": dims, "price": price, "features": feats, "room_tags": rooms,
         "photo": "{color} storage"}, ["White", "Black", "Gray", "Natural Oak", "Walnut"],
        "STO")
add({"category": "Furniture", "subcategory": "Lighting", "kind": "lamp",
     "name": "Swing-Arm Architect Desk Lamp", "brand": "Lumina", "style": "industrial",
     "material": "metal", "dims": {"w": 8, "d": 8, "h": 28}, "price": 54.99,
     "features": ["clamp or base mount", "fully adjustable arm"],
     "room_tags": ["home office", "bedroom office", "private office", "open office"],
     "photo": "{color} lamp"}, ["Black", "White", "Silver", "Red", "Green"], "LMP")
add({"category": "Furniture", "subcategory": "Rugs & Mats", "kind": "rug",
     "name": "Round Area Rug 6'", "brand": "Kinsley Home", "style": "modern",
     "material": "polypropylene", "round": True, "dims": {"w": 72, "d": 72, "h": 1},
     "price": 109.99, "features": ["non-slip backing"],
     "room_tags": ["home office", "bedroom office"], "photo": "{color} rug"},
    ["Gray", "Blue", "Beige", "Green"], "RUG")
for size, price, extra in [(24, 149, {}), (34, 449, {"ultrawide": True, "curved": True})]:
    add({"category": "Technology", "subcategory": "Monitors", "kind": "monitor",
         "name": f'{size}" {"Ultrawide Curved " if extra else "Full HD "}Monitor',
         "brand": "Lumina", "style": "modern", "material": "plastic", "screen_in": size,
         "dims": {"w": 32 if extra else 21, "d": 9, "h": 18 if extra else 15},
         "price": price, **extra,
         "features": ["HDMI + DisplayPort", "USB-C 65 W"] if extra else ["HDMI", "VESA mount"],
         "room_tags": ["home office", "private office", "open office", "bedroom office"],
         "photo": "monitor"}, ["Black", "White", "Silver"], "MON")
for name, kind, sub, price, feats, colors in [
        ("Wireless Keyboard and Mouse Set", "keyboard_mouse", "Keyboards & Mice", 49.99,
         ["quiet keys", "2.4 GHz receiver"], ["Black", "White", "Gray"]),
        ("Over-Ear Headphones for Calls", "headset", "Headsets", 79.99,
         ["cushioned ear cups", "3.5 mm and USB-C cable"], ["Black", "White"]),
        ("1080p HD Webcam", "webcam", "Webcams", 59.99, ["1080p 30 fps", "privacy shutter"],
         ["Black"]),
        ("Aluminum Laptop Stand", "laptop_stand", "Desk Accessories", 39.99,
         ["raises screen to eye level", "foldable"], ["Silver", "Black", "Gray"]),
        ("Dual Monitor Arm", "monitor_arm", "Monitor Mounts", 119.99,
         ["holds two 17\"-32\" screens", "gas springs", "clamp mount"], ["Black", "Silver",
                                                                           "White"])]:
    add({"category": "Technology", "subcategory": sub, "kind": kind, "name": name,
         "brand": rng.choice(BRANDS["tech"]), "style": "modern", "material": "aluminum",
         "dims": {"w": 12, "d": 8, "h": 4}, "price": price, "features": feats,
         "room_tags": ["home office", "private office", "open office", "bedroom office"],
         "photo": name}, colors, "ACC")
for name, kind, price, feats, colors in [
        ("Spiral Notebooks, College Ruled, 5-Pack", "notebook", 12.99,
         ["100 sheets each", "perforated pages"], ["Blue", "Black", "Red", "Green"]),
        ("Manila File Folders, Letter, 100-Pack", "folders", 14.99, ["1/3-cut tabs"],
         ["Beige"]),
        ('Office Scissors 8"', "scissors", 6.99, ["stainless steel blades"],
         ["Black", "Red", "Blue", "Orange"]),
        ("Desktop Tape Dispenser", "tape_dispenser", 9.99, ["weighted base"],
         ["Black", "Gray", "Silver"]),
        ("Desktop Calculator, 12 Digit", "calculator", 15.99, ["solar and battery"],
         ["Black", "White", "Gray"]),
        ("Binder Clips, Assorted, 60-Pack", "clips", 7.49, ["sizes from 3/4\" to 2\""],
         ["Black"]),
        ("Hole Punch, 3-Hole", "hole_punch", 19.99, ["punches 20 sheets"], ["Black", "Silver",
                                                                            "Gray"]),
        ("Letter Trays, Stackable, 3-Pack", "letter_tray", 21.99, ["stacks 3 high"],
         ["Black", "Silver", "White"])]:
    add({"category": "Office Supplies", "subcategory": "Desk Accessories", "kind": kind,
         "name": name, "brand": rng.choice(BRANDS["supplies"]), "style": "standard",
         "material": "plastic", "dims": {"w": 8, "d": 6, "h": 3}, "price": price,
         "features": feats, "room_tags": ALL_ROOMS, "photo": name}, colors, "SUP")
add({"category": "Breakroom & Facilities", "subcategory": "Coat Racks",
     "kind": "coat_rack", "name": "Standing Coat Rack", "brand": "Kinsley Home",
     "style": "classic", "material": "wood", "dims": {"w": 20, "d": 20, "h": 70},
     "price": 59.99, "features": ["8 hooks", "umbrella ring"],
     "room_tags": ["private office", "executive suite", "home office"],
     "photo": "coat rack"}, ["Walnut", "Black", "Natural Oak", "White"], "FAC")
add({"category": "Breakroom & Facilities", "subcategory": "Decor", "kind": "plant",
     "name": "Artificial Potted Plant 4'", "brand": "Kinsley Home", "style": "modern",
     "material": "silk, ceramic pot", "dims": {"w": 18, "d": 18, "h": 48}, "price": 79.99,
     "features": ["no watering", "weighted pot"],
     "room_tags": ["private office", "executive suite", "home office", "open office",
                   "conference room", "boardroom"], "photo": "potted plant"},
    ["Green"], "FAC")
add({"category": "Breakroom & Facilities", "subcategory": "Breakroom",
     "kind": "coffee_maker", "name": "12-Cup Coffee Maker", "brand": "Brightline",
     "style": "modern", "material": "stainless steel", "dims": {"w": 9, "d": 10, "h": 14},
     "price": 69.99, "features": ["programmable", "keep-warm plate"],
     "room_tags": ["open office", "home office"], "photo": "coffee maker"},
    ["Black", "Silver", "White", "Red"], "FAC")

# Boardroom, manager's office and meeting-room supplies (round 5).
for name, pack, price, rooms in [
        ("Dry-Erase Markers, Chisel Tip, 12-Pack Assorted", 12, 17.99,
         ["boardroom", "conference room"]),
        ("Dry-Erase Markers, Fine Tip, 4-Pack", 4, 6.49,
         ["huddle room", "private office", "executive suite", "home office"])]:
    add({"category": "Office Supplies", "subcategory": "Markers", "kind": "dry_erase_marker",
         "name": name, "brand": "Marker Max", "style": "standard", "material": "ink",
         "pack_qty": pack, "dims": {"w": 6, "d": 4, "h": 1}, "price": price,
         "features": ["low odor", "erases cleanly"], "room_tags": rooms,
         "photo": "dry erase markers"}, ["Black"], "MKR")
add({"category": "Office Supplies", "subcategory": "Erasers", "kind": "whiteboard_eraser",
     "name": "Large Magnetic Whiteboard Eraser", "brand": "OfficeEssentials",
     "style": "standard", "material": "felt", "dims": {"w": 7, "d": 3, "h": 2}, "price": 8.99,
     "features": ["wide felt pad", "sticks to the board"],
     "room_tags": ["boardroom", "conference room", "huddle room"],
     "photo": "whiteboard eraser"}, ["Gray"], "ERS")
add({"category": "Office Supplies", "subcategory": "Erasers", "kind": "whiteboard_cleaner",
     "name": "Whiteboard Cleaning Spray, 8 oz", "brand": "OfficeEssentials",
     "style": "standard", "material": "cleaning solution", "dims": {"w": 3, "d": 3, "h": 8},
     "price": 7.49, "features": ["removes ghosting", "streak-free"],
     "room_tags": ["boardroom", "conference room", "huddle room"],
     "photo": "whiteboard cleaner"}, ["White"], "ERS")
for name, use, price, colors, feats, rooms in [
        ("Executive Mesh Chair, High-Back with Headrest", "executive", 389, ["Black", "Gray"],
         ["adjustable headrest", "synchro-tilt", "4D armrests"],
         ["boardroom", "private office", "executive suite"]),
        ("Executive Conference Chair, Mid-Back Leather", "executive", 329,
         ["Black", "Brown", "Espresso"], ["padded arms", "tilt and swivel", "casters"],
         ["boardroom", "conference room"]),
        ("Big & Tall Executive Chair", "executive", 449, ["Black"],
         ["supports 400 lb", "wide seat", "high back"], ["executive suite", "private office"]),
        ("Leather Guest Chair with Arms", "guest", 189, ["Black", "Brown"],
         ["padded leather seat", "sled base"], ["private office", "executive suite"])]:
    add({"category": "Furniture", "subcategory": "Office Chairs", "kind": "chair",
         "name": name, "brand": rng.choice(BRANDS["seating"]), "style": "classic",
         "material": "mesh" if "Mesh" in name else "leather",
         "dims": {"w": 27, "d": 27, "h": 46 if "High" in name or "Big" in name else 41},
         "price": price, "pack_qty": 1, "use": use, "features": feats, "room_tags": rooms,
         "photo": "{color} office chair"}, colors, "CHR")
add({"category": "Office Supplies", "subcategory": "Whiteboards", "kind": "whiteboard",
     "name": "Mobile Glass Whiteboard 6' x 4'", "brand": "Marker Max", "style": "modern",
     "material": "tempered glass", "board_w_ft": 6, "board_h_ft": 4, "mobile": True,
     "dims": {"w": 72, "d": 24, "h": 78}, "price": 599.99,
     "features": ["ghost-free glass", "locking casters", "double-sided"],
     "room_tags": ["boardroom", "conference room"], "photo": "glass whiteboard"},
    ["White"], "WBD")
add({"category": "Furniture", "subcategory": "Storage", "kind": "storage",
     "name": "Executive Bookcase, 5-Shelf", "brand": "Bancroft", "style": "classic",
     "material": "wood veneer", "dims": {"w": 36, "d": 14, "h": 72}, "price": 399,
     "features": ["adjustable shelves", "crown moulding"],
     "room_tags": ["private office", "executive suite"], "photo": "{color} bookcase"},
    ["Walnut", "Espresso"], "STO")

# HQ cubicles: compact screens, panel workstations, desk-space savers (round 6).
for size, price, colors, rooms, name in [
        (19.5, 99, ["Black"], ["open office", "home office", "bedroom office"],
         '19.5" HD+ Compact Monitor'),
        (22, 129, ["Black", "White"], ["open office", "private office", "home office"],
         '22" Full HD Monitor, Height-Adjustable')]:
    add({"category": "Technology", "subcategory": "Monitors", "kind": "monitor",
         "name": name, "brand": "Lumina", "style": "modern", "material": "plastic",
         "screen_in": size, "compact": True,
         "dims": {"w": round(size * 0.9), "d": 7, "h": round(size * 0.65)}, "price": price,
         "features": ["HDMI + VGA", "VESA mount", "low blue light"] + (
             ["height and tilt adjust"] if size >= 22 else []),
         "room_tags": rooms, "photo": "computer monitor"}, colors, "MON")
for w, d, price in [(60, 30, 549), (48, 24, 449)]:
    add({"category": "Furniture", "subcategory": "Cubicle Workstations", "kind": "desk",
         "name": f'Cubicle Workstation {w}"x{d}" with Privacy Panel', "brand": "Novus Works",
         "style": "modern", "material": "laminate top, fabric panels",
         "dims": {"w": w, "d": d, "h": 30}, "seats": 1, "price": price, "panel": True,
         "features": ["53\" fabric panels", "pinnable surface", "cable raceway"],
         "room_tags": ["open office"], "photo": "cubicle"}, ["Gray", "White"], "DSK")
add({"category": "Office Supplies", "subcategory": "Desk Accessories", "kind": "monitor_riser",
     "name": "Monitor Riser with Drawer", "brand": "OfficeEssentials", "style": "modern",
     "material": "steel mesh", "dims": {"w": 22, "d": 9, "h": 4}, "price": 29.99,
     "features": ["raises screen 4\"", "storage drawer"],
     "room_tags": ["open office", "home office", "bedroom office"], "photo": "monitor riser"},
    ["Black", "White"], "SUP")
add({"category": "Furniture", "subcategory": "Lighting", "kind": "lamp",
     "name": "Clamp-On LED Desk Task Light", "brand": "Lumina", "style": "modern",
     "material": "aluminum", "dims": {"w": 6, "d": 6, "h": 20}, "price": 34.99,
     "features": ["clamps to desk or panel", "dimmable"],
     "room_tags": ["open office"], "photo": "clamp lamp"}, ["Black", "White"], "LMP")

# Ink & toner: high-yield, multipacks, single colours, drums, compatibles and a
# supertank printer with its ink bottles (round 7).
_fam = {p["base_name"]: p["base_id"] for p in products if p["kind"] == "printer"}
INKJET, MONO, COLOR = (_fam["Wireless All-in-One Inkjet Printer"],
                       _fam["Monochrome Laser Printer"],
                       _fam["Color Laser Multifunction Printer"])
tank = add({"category": "Technology", "subcategory": "Printers", "kind": "printer",
            "name": "Supertank Inkjet All-in-One Printer", "brand": "PrintPro",
            "style": "modern", "material": "plastic", "printer_type": "inkjet",
            "users_supported": 5, "ink_tank": True, "dims": {"w": 18, "d": 14, "h": 9},
            "price": 279, "features": ["refillable ink tanks", "up to 7,500 pages per set",
                                       "print, scan, copy", "Wi-Fi"],
            "room_tags": ["home office", "bedroom office", "open office"],
            "photo": "ink tank printer"}, ["White", "Black"], "PRN")
for p_ in tank:
    p_["cartridge_family"] = p_["base_id"]
TANK = tank[0]["base_id"]
for name, fam, style, role, price, yield_, extra, colors, sub in [
        ("Black Ink Cartridge XL, High Yield", INKJET, "ink", "black", 42.99, 600,
         {"xl": True}, ["Black"], "Ink Cartridges"),
        ("Tri-Color Ink Cartridge XL, High Yield", INKJET, "ink", "color", 49.99, 450,
         {"xl": True}, ["Black"], "Ink Cartridges"),
        ("Black + Tri-Color Ink Combo Pack", INKJET, "ink", "bundle", 54.99, 300,
         {"pack_qty": 2, "ink_colors": ["Black", "Cyan"]}, ["Black"], "Ink Cartridges"),
        ("Black Ink Cartridge Twin Pack", INKJET, "ink", "bundle", 49.99, 300,
         {"pack_qty": 2, "ink_colors": ["Black", "Black"]}, ["Black"], "Ink Cartridges"),
        ("High-Yield Black Toner Cartridge", MONO, "toner", "black", 129.99, 6000,
         {"xl": True}, ["Black"], "Toner Cartridges"),
        ("Black Toner Cartridge 2-Pack", MONO, "toner", "bundle", 144.99, 2500,
         {"pack_qty": 2, "ink_colors": ["Black", "Black"]}, ["Black"], "Toner Cartridges"),
        ("Remanufactured Black Toner Cartridge (Compatible)", MONO, "toner", "black", 44.99,
         2500, {"compatible": True}, ["Black"], "Toner Cartridges"),
        ("Drum Unit", MONO, "drum", "drum", 99.99, 12000, {}, ["Black"], "Toner Cartridges"),
        ("Toner Cartridge", COLOR, "toner", "single", 89.99, 2300, {},
         ["Cyan", "Magenta", "Yellow"], "Toner Cartridges"),
        ("High-Yield Black Toner Cartridge", COLOR, "toner", "black", 139.99, 7000,
         {"xl": True}, ["Black"], "Toner Cartridges"),
        ("CMYK Toner Value Pack, 4 Cartridges", COLOR, "toner", "bundle", 289.99, 2300,
         {"pack_qty": 4, "ink_colors": ["Black", "Cyan", "Magenta", "Yellow"]}, ["Black"],
         "Toner Cartridges"),
        ("Imaging Drum Unit", COLOR, "drum", "drum", 179.99, 30000, {}, ["Black"],
         "Toner Cartridges"),
        ("Ink Bottle, 70 ml", TANK, "bottle", "single", 13.99, 6000, {},
         ["Black", "Cyan", "Magenta", "Yellow"], "Ink Bottles"),
        ("Ink Bottle Multipack, CMYK", TANK, "bottle", "bundle", 49.99, 6000,
         {"pack_qty": 4, "ink_colors": ["Black", "Cyan", "Magenta", "Yellow"]}, ["Black"],
         "Ink Bottles")]:
    printer = next(p for p in products if p["base_id"] == fam)["base_name"]
    add({"category": "Ink & Toner", "subcategory": sub, "kind": "cartridge",
         "name": f"{name} for {printer}", "brand": "PrintPro" if not extra.get("compatible")
         else "Inkwell", "style": "standard",
         "material": "ink" if style in ("ink", "bottle") else "toner",
         "dims": {"w": 5, "d": 3, "h": 4}, "price": price, "page_yield": yield_,
         "compatible_printers": [fam], "cart_style": style, "role": role,
         "compact_toner": fam == COLOR,
         "pack_qty": extra.get("pack_qty", 1), **extra,
         "features": [f"~{yield_:,} page yield"] + (["high yield"] if extra.get("xl") else [])
         + (["remanufactured, compatible"] if extra.get("compatible") else ["genuine"]),
         "room_tags": ["home office", "private office", "open office"],
         "photo": "printer ink"}, colors, "CRT")

# Paper and supplies (round 8): more grades and sizes, and everyday desk items.
for name, reams, price, extra, colors, feats in [
        ('Premium Bright Copy Paper, 8.5" x 11", 1 Ream', 1, 11.99,
         {"paper_use": "copy", "grade": "premium", "label": "#24527a"}, ["White"],
         ["98 bright", "24 lb", "crisp colour printing"]),
        ('Premium Bright Copy Paper, 8.5" x 11", 10-Ream Case', 10, 79.99,
         {"paper_use": "copy", "grade": "premium", "label": "#24527a"}, ["White"],
         ["98 bright", "24 lb", "5,000 sheets"]),
        ('Recycled Copy Paper, 30% Post-Consumer, 10-Ream Case', 10, 59.99,
         {"paper_use": "copy", "recycled": True, "label": "#3a9d4a"}, ["White"],
         ["30% recycled", "FSC certified", "5,000 sheets"]),
        ('Legal Copy Paper, 8.5" x 14", 1 Ream', 1, 12.99,
         {"paper_use": "specialty", "sheet": "legal", "label": "#7b3fa0"}, ["White"],
         ["legal size", "92 bright"]),
        ('Ledger Copy Paper, 11" x 17", 1 Ream', 1, 19.99,
         {"paper_use": "specialty", "sheet": "ledger", "label": "#e67e22"}, ["White"],
         ["tabloid size", "for spreadsheets and plans"]),
        ('Cardstock, 110 lb, 8.5" x 11", 250 Sheets', 1, 18.99,
         {"paper_use": "specialty", "cardstock": True, "label": "#5b4b8a"},
         ["White", "Ivory"], ["heavyweight", "name tents and covers"]),
        ('Colored Copy Paper, Pastel Assortment, 1 Ream', 1, 13.99,
         {"paper_use": "specialty", "assorted": True, "label": "#e6007e"}, ["Pastel Blue"],
         ["5 pastel colours", "flyers and handouts"]),
        ('Glossy Photo Paper, 4" x 6", 100 Sheets', 1, 14.99,
         {"paper_use": "specialty", "sheet": "photo", "label": "#00a3e0"}, ["White"],
         ["instant-dry gloss", "for inkjet printers"])]:
    add({"category": "Office Supplies", "subcategory": "Paper", "kind": "paper",
         "name": name, "brand": "Brightline", "style": "standard", "material": "paper",
         "reams": reams, "sheet": extra.pop("sheet", "letter"), **extra,
         "dims": {"w": 12, "d": 9, "h": 2 * reams}, "price": price, "features": feats,
         "room_tags": ALL_ROOMS, "photo": "paper"}, colors, "PPR")
for name, kind, price, colors, feats, extra in [
        ('3-Ring Binder, 1"', "binder", 4.99, ["Black", "Blue", "Red", "White"],
         ["clear view cover", "holds 225 sheets"], {"ring_in": 1}),
        ('3-Ring Binder, 2"', "binder", 8.49, ["Black", "Blue", "White"],
         ["clear view cover", "holds 475 sheets"], {"ring_in": 2}),
        ("Highlighters, Chisel Tip, 6-Pack", "highlighter", 6.99, ["Yellow"],
         ["assorted fluorescent", "smear-safe"], {"pack_qty": 6}),
        ("Legal Pads, 8.5\" x 11.75\", 12-Pack", "legal_pad", 15.99, ["Yellow", "White"],
         ["wide ruled", "perforated"], {}),
        ("#10 Business Envelopes, 500 Box", "envelopes", 24.99, ["White"],
         ["self-seal", "security tint"], {}),
        ("Index Cards, 3\" x 5\", Ruled, 300 Pack", "index_cards", 5.49,
         ["White", "Assorted"], ["ruled one side"], {})]:
    add({"category": "Office Supplies", "subcategory": "Binders & Filing"
         if kind in ("binder",) else "Notebooks & Pads" if kind in ("legal_pad",
                                                                    "index_cards")
         else "Desk Accessories", "kind": kind, "name": name,
         "brand": rng.choice(BRANDS["supplies"]), "style": "standard", "material": "paper",
         "dims": {"w": 8, "d": 6, "h": 3}, "price": price, "features": feats, **extra,
         "room_tags": ALL_ROOMS, "photo": name}, colors, "SUP")

# Colours added to earlier products once a real photo of that colour turned up.
# Built from their own seeded random draws, so nothing above changes.
EXTRA_COLOURS: dict[str, list[str]] = {
    "DSK-1005": ["Gray"],
    "CHR-1007": ["Teal"],
    "CHR-1008": ["Navy", "Blue", "Yellow"],
    "CHR-1010": ["Orange"],
    "CHR-1072": ["Natural Oak"],
    "CNF-1013": ["Natural Oak"],
    "CNF-1052": ["Natural Oak"],
    "STL-1059": ["Espresso", "Natural Oak", "White"],
    "STO-1062": ["Natural Oak"],
    "TBL-1061": ["Espresso"],
    "CHR-1009": ["Beige"],
    "CNF-1012": ["Natural Oak", "Black"],
    "CNF-1055": ["Espresso", "White"],
    "STO-1014": ["Beige"],
    "PRN-1016": ["Silver"],
    "BRD-1056": ["Natural Oak", "White"],
    "BRD-1057": ["Black", "White"],
    "CNF-1054": ["Natural Oak"],
    "LMP-1046": ["Yellow"],
}
for base_id, extra in EXTRA_COLOURS.items():
    lead = next(p for p in products if p["base_id"] == base_id)
    for color in extra:
        r = random.Random(f"{base_id}-{color}")
        doc = dict(lead, color=color, color_hex=COLORS[color],
                   sku=f"{base_id}-{color.upper().replace(' ', '')[:4]}",
                   name=f"{lead['base_name']}, {color}", rating=round(r.uniform(3.8, 4.9), 1),
                   reviews=r.randint(12, 2400), stock=r.choice([4, 12, 25, 60, 140]))
        products.append(doc)

# Value and premium twins. Lines the planner picks by size (the 75" display, the
# table for 12) or that had a single product offered no lower- or higher-priced
# choice, so Value, Recommended and Premium came out the same and fitting a tight
# budget had nothing to swap. Each twin copies its original's fit — size, seats,
# use, room tags, colours — at another price, rated below every colour of the
# original so the best-rated (Recommended) pick stays the same. Built last, with
# their own seeds: every earlier SKU, price and rating is unchanged.
TWINS = ['Compact Desk 40"W x 20"D', "4K Conference Camera", "Video Bar (camera, speakers, mics)",
         "Conference Speakerphone", "Soundbar for Meeting Rooms",
         "Ergonomic Mesh Chair with Lumbar Support", "Guest Chair with Arms",
         "Conference Room Chair", "Executive Mesh Chair, High-Back with Headrest",
         "Leather Guest Chair with Arms", 'Polycarbonate Chair Mat 36" x 48"',
         'Rectangular Conference Table 72", seats 6', "Rectangular Conference Table, seats 8",
         "Boat-Shaped Conference Table, seats 10", 'Rectangular Conference Table 144", seats 12',
         "Boardroom Table 16', seats 16", "Boardroom Table 20', seats 22",
         'Electric Standing Desk 48"W x 30"D', 'L-Shaped Corner Desk 60"x60"',
         'Cubicle Workstation 48"x24" with Privacy Panel', "Arc Floor Lamp",
         "Swing-Arm Architect Desk Lamp", "Clamp-On LED Desk Task Light",
         "Low-Pile Area Rug 5' x 7'", 'Round Side Table 24"', "2-Drawer Vertical File Cabinet",
         "5-Shelf Bookcase", 'Credenza with Doors 72"', "Executive Bookcase, 5-Shelf",
         '43" 4K UHD Smart Display', '55" 4K UHD Smart Display', '65" 4K UHD Smart Display',
         '75" 4K UHD Smart Display', "Magnetic Dry-Erase Whiteboard 4' x 3'",
         "Magnetic Dry-Erase Whiteboard 6' x 4'", "8-Outlet Surge Protector with USB-C",
         "Desk Organizer, 5 Compartments"]
TWIN_TIERS = [("Basics", "OfficeIQ Basics", 0.62, 0.3), ("Signature", "Atelier", 1.65, 0.1)]
_OWN = ("sku", "base_id", "color", "color_hex", "rating", "reviews", "stock", "price",
        "base_name", "name", "brand", "image", "image_credit")
for name in TWINS:
    family = [p for p in products if p["base_name"] == name]
    lead, floor = family[0], min(p["rating"] for p in family)
    for word, brand, factor, below in TWIN_TIERS:
        base_id = _sku(lead["base_id"].split("-")[0])
        for i, src in enumerate(family):
            r = random.Random(f"{name}-{word}-{src['color']}")
            price = lead["price"] * factor * r.uniform(0.97, 1.03)
            products.append({
                **{k: v for k, v in src.items() if k not in _OWN},
                "sku": f"{base_id}-{src['color'].upper().replace(' ', '')[:4]}",
                "base_id": base_id, "color": src["color"], "color_hex": src["color_hex"],
                "rating": round(max(3.5, floor - below - r.choice([0, 0.1])), 1),
                "reviews": r.randint(12, 900), "stock": r.choice([12, 25, 60, 140]),
                "price": round(price, 2) if price < 20 else round(price) - 0.01,
                "base_name": f"{word} {name}", "name": f"{word} {name}, {src['color']}",
                "brand": brand, "tier_twin": word.lower(),
            })
all_products = list(products)       # every colour, photo or not

# What each item is *for*, in the words shoppers use — so a search by meaning
# ("my back hurts", "small bedroom", "presenting to clients") finds the right
# thing even when it shares no keyword with the product name.
USE_FOR = [
    (lambda p: "Lumbar" in p["name"],
     "Supports your lower back through long workdays; helps with back pain and posture."),
    (lambda p: "Mesh Task" in p["name"],
     "Breathable, comfortable everyday seating for focused work at a desk."),
    (lambda p: "Executive Leather" in p["name"], "A polished look for a manager's private office."),
    (lambda p: "Guest Chair" in p["name"], "Seating for visitors and clients across the desk."),
    (lambda p: "Conference Room Chair" in p["name"],
     "Comfortable seating for meetings around a conference table."),
    (lambda p: "Stacking" in p["name"], "Extra seats for events and training that store away."),
    (lambda p: "Drafting" in p["name"], "Tall seating for standing-height counters and benches."),
    (lambda p: p.get("standing"),
     "Switch between sitting and standing to stay active and ease back strain."),
    (lambda p: p.get("l_shaped"), "Makes use of a room corner; room for two monitors."),
    (lambda p: p.get("benching"), "Open-plan team seating with shared power."),
    (lambda p: p["kind"] == "desk" and p["dims"]["w"] <= 48,
     "Fits small spaces like a bedroom corner or apartment."),
    (lambda p: p["kind"] == "desk" and p["dims"]["w"] >= 60,
     "Plenty of surface for dual monitors and paperwork."),
    (lambda p: p["kind"] == "conference_table", "Team meetings, client presentations and planning."),
    (lambda p: p.get("high_top"), "Stand-up or stool-height meetings in a small huddle room."),
    (lambda p: "Boardroom" in p["name"], "Board meetings and large client presentations."),
    (lambda p: p.get("u_shaped"), "An executive workspace that wraps around you, with room "
     "for guests across the desk."),
    (lambda p: p["kind"] == "side_table", "A small table beside guest chairs for drinks and "
     "papers."),
    (lambda p: "Credenza" in p["name"], "Storage at the back of a boardroom, doubles as a "
     "catering station."),
    (lambda p: p["kind"] in ("camera", "video_bar", "speaker"),
     "Video calls and hybrid meetings: Zoom, Teams and Meet."),
    (lambda p: "Stool" in p["name"], "Seating for high-top tables and counters."),
    (lambda p: p.get("use") == "executive",
     "Executive seating for a boardroom or a manager's office."),
    (lambda p: p["kind"] == "whiteboard_cleaner",
     "Keeps the whiteboard clean and free of ghosting."),
    (lambda p: p["kind"] in ("dry_erase_marker", "whiteboard_eraser"),
     "Whiteboard supplies for meetings and brainstorming."),
    (lambda p: p["kind"] == "tv", "Presenting, video calls and dashboards in meeting rooms."),
    (lambda p: p["kind"] == "monitor", "A second screen for productivity at a desk."),
    (lambda p: p.get("recycled"), "Recycled paper for a greener office."),
    (lambda p: p.get("grade") == "premium", "Brighter, heavier paper for client-ready prints."),
    (lambda p: p["kind"] in ("binder", "folders", "letter_tray"),
     "Keeps documents organized and easy to find."),
    (lambda p: p["kind"] in ("notebook", "legal_pad", "index_cards", "highlighter"),
     "Note-taking for meetings and everyday work."),
    (lambda p: p.get("ink_tank"), "Very low cost per page: refill the tanks from bottles."),
    (lambda p: p.get("xl"), "High yield: more pages per cartridge, lower cost per page."),
    (lambda p: p.get("cart_style") == "drum", "Replaces the imaging drum when prints fade."),
    (lambda p: p.get("compatible"), "A lower-cost compatible alternative to genuine toner."),
    (lambda p: p["kind"] == "printer" and p.get("printer_type") == "inkjet",
     "Home printing of documents, homework and photos."),
    (lambda p: p["kind"] == "printer" and p.get("printer_type") == "laser",
     "Fast, low-cost printing for a busy office team."),
    (lambda p: p["kind"] == "lamp", "Reduces eye strain with focused task lighting."),
    (lambda p: p.get("compact"), "A compact screen for a small cubicle desk at HQ."),
    (lambda p: p.get("panel"), "A cubicle desk with privacy panels for open-plan offices."),
    (lambda p: p["kind"] in ("monitor_arm", "monitor_riser"),
     "Lifts screens to eye level and frees desk space."),
    (lambda p: p["kind"] == "whiteboard", "Brainstorming, planning and teaching on the wall."),
    (lambda p: p["kind"] == "chair_mat", "Lets a desk chair roll on carpet and protects floors."),
    (lambda p: p["kind"] == "rug", "Warms up a home office and dampens noise."),
    (lambda p: p["kind"] == "storage", "Keeps files and supplies organized and out of sight."),
    (lambda p: p["kind"] in ("power_strip", "cable_mgmt"),
     "Powers and tidies desk cables safely."),
    (lambda p: p["kind"] in ("wastebasket", "recycling_bin"), "Keeps the workspace tidy."),
]


def use_for(p: dict) -> str:
    return " ".join(text for test, text in USE_FOR if test(p))


def search_text(p: dict) -> str:
    bits = [p["name"], p["subcategory"], p["kind"].replace("_", " "), use_for(p),
            p.get("style", ""),
            p.get("material", ""), " ".join(p.get("features", [])),
            "for " + ", ".join(p.get("room_tags", []))]
    d = p.get("dims")
    if d:
        bits.append(f'{d["w"]} x {d["d"]} inches')
    return ". ".join(b for b in bits if b)


# Product images are 3D renders drawn from each product's shape and colour
# (retail/render.py), so everything the renderer has a model for is sold.
from retail.render import MODELS  # noqa: E402

products[:] = [p for p in products if p["kind"] in MODELS]
for p in products:
    p["image"], p["image_credit"] = None, None

_variants: dict[str, list[dict]] = {}
for p in products:
    _variants.setdefault(p["base_id"], []).append(
        {"color": p["color"], "hex": p["color_hex"], "sku": p["sku"]})
for p in products:
    p["colors_available"] = _variants[p["base_id"]]

for p in products:
    p["description"] = (
        f"{p['name']} by {p['brand']}. "
        + (f"Measures {p['dims']['w']}\"W x {p['dims']['d']}\"D x {p['dims']['h']}\"H. "
           if p.get("dims") else "")
        + "Features: " + ", ".join(p.get("features", [])) + ". "
        + (use_for(p) + " " if use_for(p) else "")
        + "Ideal for " + ", ".join(p.get("room_tags", [])) + ".")
    p["search_text"] = search_text(p)

# --------------------------------------------------------------------------- #
# Categories, offers, room rules, knowledge base, customers
# --------------------------------------------------------------------------- #
categories: dict[str, set[str]] = {}
for p in products:
    categories.setdefault(p["category"], set()).add(p["subcategory"])
category_docs = [{"type": "category", "category": c, "subcategories": sorted(s),
                  "order": i} for i, (c, s) in enumerate(sorted(categories.items()))]

offers = [
    {"offer_id": "DESK_CHAIR_15", "name": "Desk + Chair: save 15%",
     "kind": "pair", "a": {"kind": "desk"}, "b": {"kind": "chair"}, "discount_pct": 15,
     "applies_to": "both", "description": "Buy any desk with any office chair and save 15% on both."},
    {"offer_id": "PRINTER_INK_10", "name": "Printer + 2 cartridges: save 10%",
     "kind": "printer_ink", "min_cartridges": 2, "discount_pct": 10, "applies_to": "all",
     "description": "Buy a printer with two compatible cartridges and save 10% on the set."},
    {"offer_id": "CHAIRS_3PLUS", "name": "3+ chairs: save 10% (10+: 15%)",
     "kind": "qty_tier", "match": {"kind": "chair"}, "tiers": [[10, 15], [3, 10]],
     "description": "Furnishing a team? 10% off 3 or more chairs, 15% off 10 or more."},
    {"offer_id": "WHITEBOARD_KIT", "name": "Whiteboard kit: 20% off markers & eraser",
     "kind": "kit", "anchor": {"kind": "whiteboard"},
     "items": [{"kind": "dry_erase_marker"}, {"kind": "whiteboard_eraser"}], "discount_pct": 20,
     "description": "Buy a whiteboard and get 20% off dry-erase markers and an eraser."},
    {"offer_id": "TV_HDMI_FREE", "name": "Free HDMI cable with any display",
     "kind": "free_item", "anchor": {"kind": "tv"}, "item": {"kind": "hdmi_cable"},
     "max_value": 25.0, "description": "Get one HDMI cable (up to $25) free with any TV display."},
    {"offer_id": "HOME_OFFICE_STARTER", "name": "Home Office Starter: $50 off",
     "kind": "set_amount", "set": [{"kind": "desk"}, {"kind": "chair"}, {"kind": "lamp"}],
     "amount_off": 50.0, "description": "Desk + chair + desk lamp together: $50 off."},
    {"offer_id": "CONFERENCE_READY", "name": "Conference Room Ready: save 12%",
     "kind": "set_pct", "set": [{"kind": "conference_table"}, {"kind": "chair", "qty": 6},
                                {"kind": "tv"}], "discount_pct": 12,
     "description": "Conference table + 6 chairs + a display: save 12% on all of it."},
    {"offer_id": "PAPER_BULK_10", "name": "Paper by the case: 10% off 5+ cases",
     "kind": "qty_tier", "match": {"kind": "paper", "min_reams": 5}, "tiers": [[5, 10]],
     "description": "Stock up: 10% off when you buy 5 or more cases of paper."},
    {"offer_id": "SUPPLIES_100", "name": "Spend $100 on supplies, save 15%",
     "kind": "spend_category", "category": "Office Supplies", "min_spend": 100.0,
     "discount_pct": 15, "description": "15% off office supplies when they total $100 or more."},
    {"offer_id": "VIDEO_KIT_10", "name": "Video call kit: camera + speakerphone, save 10%",
     "kind": "kit", "anchor": {"kind": "camera"}, "items": [{"kind": "speaker"}],
     "discount_pct": 10, "description": "Buy a conference camera with a speakerphone or "
     "soundbar and save 10% on the audio."},
    {"offer_id": "FREE_DELIVERY_500", "name": "Free delivery over $500",
     "kind": "free_delivery", "min_subtotal": 500.0,
     "description": "Orders of $500 or more ship free."},
]
for o in offers:
    o.update({"type": "offer", "active": True})

room_rules = [{
    "type": "room_rule", "rule_id": "DEFAULT",
    "chair_clearance_in": 36, "walkway_in": 36, "min_walkway_in": 30,
    "wall_margin_in": 6, "table_to_wall_in": 48, "guest_chair_space_in": 30, "door_in": 36,
    "people_per_printer": 10, "people_per_bin": 4, "reams_per_person_month": 1.5,
    "paper_months": 2, "tv_viewing_ratio": 2.6, "whiteboard_wall_fraction": 0.6,
    "room_types": {
        "boardroom": {"sqft": [400, 600], "capacity": [12, 24], "table_to_wall_in": 48,
                      "table_width_ft": [4, 5], "table_length_ft": [12, 20],
                      "display_wall": "narrow", "av": ["tv", "camera", "soundbar",
                                                       "speakerphone"]},
        "conference room": {"sqft": [250, 350], "capacity": [8, 14], "table_to_wall_in": 36,
                            "table_width_ft": [3, 4], "table_length_ft": [8, 12],
                            "display_wall": "narrow", "av": ["tv", "camera",
                                                             "speakerphone"]},
        "huddle room": {"sqft": [100, 150], "capacity": [2, 5], "table_to_wall_in": 36,
                        "display_wall": "narrow", "av": ["tv", "video_bar"]},
        "executive suite": {"sqft": [150, 200], "capacity": [1, 3], "av": []},
        "private office": {"sqft": [120, 200], "capacity": [1, 3], "av": []},
        "home office": {"sqft": [70, 150], "capacity": [1, 2], "av": []},
        "bedroom office": {"sqft": [80, 160], "capacity": [1, 2], "av": []},
        "open office": {"sqft_per_person": [60, 110], "av": []},
    },
    "notes": "Clearances follow common office-planning guidance: 36\" behind a desk "
             "chair, 36\" main walkways (30\" minimum), 48\" from a meeting table to "
             "the wall. Screen size: the farthest seat's distance / 2.6.",
}]

kb = [
    ("KB-01", "Delivery times and costs", "shipping",
     "Most in-stock items ship next business day. Furniture arrives in 3-5 business days. "
     "Delivery is free on orders of $500 or more; otherwise a flat $29.99 applies.",
     ["how long does delivery take", "do you charge for shipping", "when will my desk arrive"]),
    ("KB-02", "Returns policy", "returns",
     "Return most items within 30 days for a full refund. Furniture can be returned within "
     "14 days in original packaging. Opened ink and toner cannot be returned.",
     ["can I return a desk", "what is your return policy", "how do returns work"]),
    ("KB-03", "Assembly service", "services",
     "Professional assembly is available for desks, chairs and storage for a flat $79 per "
     "item, or $199 for a room of up to 5 items. Book it at checkout.",
     ["do you assemble furniture", "can someone build my desk", "assembly cost"]),
    ("KB-04", "Warranty", "support",
     "Office chairs carry a 5-year warranty, desks 3 years, electronics 1 year from the "
     "manufacturer. Ergonomic chairs carry a 10-year mechanism warranty.",
     ["what warranty do chairs have", "is my desk under warranty"]),
    ("KB-05", "Bulk and business orders", "business",
     "Business accounts get volume pricing on orders of 10 or more chairs or desks, net-30 "
     "invoicing and a dedicated account manager. Orders over $10,000 require approval.",
     ["do you offer bulk discounts", "business account", "net 30 terms"]),
    ("KB-06", "Choosing a desk size", "guides",
     "Allow at least 36 inches behind the desk for the chair. A 48\" desk suits a laptop and "
     "one monitor; 60\" fits two monitors; L-shaped desks suit corners and dual workflows.",
     ["what size desk do I need", "how big should my desk be"]),
    ("KB-07", "Choosing a TV size for a meeting room", "guides",
     "A good rule: screen diagonal of about one inch for every 2.6 inches of distance from "
     "the farthest seat. For a 16-foot room, a 75\" display works well.",
     ["what size TV for a conference room", "how big a screen do I need"]),
    ("KB-08", "Ergonomic setup", "guides",
     "Set chair height so feet rest flat, elbows at 90 degrees, and the top of the monitor at "
     "or just below eye level, about an arm's length away.",
     ["how should I set up my chair", "ergonomic desk setup"]),
    ("KB-09", "Ink and toner recycling", "sustainability",
     "Bring empty ink and toner cartridges to any store or mail them free to recycle them.",
     ["recycle ink cartridges", "what do I do with empty toner"]),
    ("KB-10", "Price match", "pricing",
     "We match a lower advertised price from major retailers on identical in-stock items "
     "at the time of purchase.",
     ["do you price match", "found it cheaper elsewhere"]),
    ("KB-11", "Payment methods", "payments",
     "We accept major credit cards, business accounts on net-30 terms and gift cards. In "
     "this demo store, use test card 4242 4242 4242 4242 with any future expiry and CVC.",
     ["what payment methods do you accept", "can I pay by invoice"]),
    ("KB-12", "Tracking an order", "orders",
     "You'll get an email with tracking as soon as your order ships. Ask the assistant "
     "'where is my order' with your order number for status.",
     ["where is my order", "track my order"]),
    ("KB-13", "Free room planning consultation", "services",
     "Our design team plans offices of any size for free: send your room dimensions or a "
     "photo and we'll suggest a layout and a shopping list.",
     ["can you help me plan my office", "office design service"]),
    ("KB-14", "Setting up a home office in a bedroom", "guides",
     "In a bedroom, put the desk near a window for daylight, keep 36\" of chair space, use a "
     "compact desk (40-48\") and a chair mat to protect carpet.",
     ["how to set up an office in my bedroom", "home office in a small room"]),
    ("KB-15", "Bundle offers", "pricing",
     "Bundles apply automatically in your cart: desk + chair saves 15%, printer + 2 "
     "cartridges saves 10%, 3+ chairs save 10% (15% for 10+), and more.",
     ["what bundles do you have", "do you have any deals"]),
]
kb.append(
    ("KB-16", "Meeting room sizes", "guides",
     "Typical sizes: a huddle room is 100-150 sq ft for 2-5 people, with the table pushed "
     "against a wall display. A conference room is 250-350 sq ft for 8-14, with a table "
     "3-4 ft wide and 8-12 ft long and at least 36 inches behind seated people. A boardroom "
     "is 400-600+ sq ft for 12-24, with a table 4-5 ft wide and up to 20 ft long, 48 inches "
     "of walking space round it and a credenza at the back. An executive suite is 150-200 "
     "sq ft: an L- or U-shaped desk facing the door, two guest chairs and a side table. "
     "Rooms for video calls need a display, a camera above it and a speakerphone or soundbar.",
     ["how big should a boardroom be", "what size is a huddle room",
      "how much space for a conference room", "video conference room setup"]))
kb_docs = [{"type": "kb_article", "article_id": a, "title": t, "category": c, "answer": ans,
            "question_variants": qv} for a, t, c, ans, qv in kb]

customers = [
    {"customer_id": "RET-1001", "name": "Rachel Kim", "company": "Northwind Capital",
     "segment": "business", "role": "Facilities Director",
     "email": "rachel.kim@northwind.example.com", "city": "Boston, MA",
     "persona": "Setting up a new 30x40 ft HQ floor for 16 people plus a conference room."},
    {"customer_id": "RET-1002", "name": "Leo Martins", "company": "Brightwave Labs",
     "segment": "business", "role": "Office Manager", "email": "leo@brightwave.example.com",
     "city": "Austin, TX", "persona": "Startup moving into a 20x24 ft office for 6 people."},
    {"customer_id": "RET-1003", "name": "Priyank Desai", "company": None, "segment": "home",
     "role": "Remote software engineer", "email": "priyank.desai@example.com",
     "city": "Seattle, WA", "persona": "Turning a 12x10 ft spare bedroom into a home office."},
    {"customer_id": "RET-1004", "name": "James O'Neill", "company": None, "segment": "home",
     "role": "Teacher", "email": "james.oneill@example.com", "city": "Chicago, IL",
     "persona": "Small 9x8 ft home office corner for lesson planning and printing."},
]
for c in customers:
    c["type"] = "customer"

memories = {
    "RET-1001": [
        ("preference", "Corporate standard colour for furniture is white with gray chairs.", 0.95),
        ("preference", "Budget for the new HQ floor is about $25,000.", 0.9),
        ("fact", "Needs a conference room that seats 10 with a large display.", 0.85),
        ("preference", "Prefers ergonomic mesh chairs for all staff.", 0.9),
    ],
    "RET-1002": [
        ("preference", "Likes an industrial look: black and walnut.", 0.9),
        ("preference", "Wants electric standing desks for the whole team.", 0.95),
        ("fact", "Team prints a lot of color marketing material.", 0.8),
    ],
    "RET-1003": [
        ("preference", "Prefers walnut wood finishes and a black chair.", 0.9),
        ("fact", "Uses two monitors and needs a desk at least 55 inches wide.", 0.9),
        ("preference", "Budget around $1,500 for the home office.", 0.85),
        ("fact", "The spare bedroom has carpet, so a chair mat is needed.", 0.8),
        ("fact", "Has lower back pain from sitting at the desk all day.", 0.9),
    ],
    "RET-1004": [
        ("preference", "Prefers white furniture to keep the small room bright.", 0.9),
        ("fact", "Prints worksheets weekly, so an inkjet all-in-one is enough.", 0.85),
        ("preference", "Keep the total under $600.", 0.9),
        ("preference", "Can't justify more than $400 for any single item.", 0.85),
        ("fact", "The corner is small, so the desk must be no wider than 40 inches.", 0.85),
    ],
}


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    out = {"products": products, "categories": category_docs, "offers": offers,
           "room_rules": room_rules, "kb_articles": kb_docs, "customers": customers,
           "memories": memories}
    for name, docs in out.items():
        (DATA_DIR / f"{name}.json").write_text(json.dumps(docs, indent=1))
    (DATA_DIR / "products_all.json").write_text(json.dumps(all_products, indent=1))
    kinds: dict[str, int] = {}
    for p in products:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    print(f"products: {len(products)} SKUs from {len({p['base_id'] for p in products})} "
          f"base items")
    print("by kind:", ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
    print(f"categories: {len(category_docs)}, offers: {len(offers)}, kb: {len(kb_docs)}, "
          f"customers: {len(customers)}, memories: {sum(map(len, memories.values()))}")


if __name__ == "__main__":
    main()
