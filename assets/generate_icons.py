"""Export the desktop, Hook, and web icons. Run python assets/generate_icons.py."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

ASSETS = Path(__file__).resolve().parent
BASE = 512
SCALE = 3
SIZE = BASE * SCALE
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def points(vertices):
    return [(x * SCALE, y * SCALE) for x, y in vertices]


def fill_gradient(image, mask, top, bottom):
    ramp = Image.linear_gradient("L").resize((1, SIZE), Image.Resampling.BILINEAR)
    color = ImageOps.colorize(ramp, top, bottom).resize((SIZE, SIZE)).convert("RGBA")
    color.putalpha(mask)
    image.alpha_composite(color)


def face(image, vertices, top, bottom):
    mask = Image.new("L", (SIZE, SIZE))
    ImageDraw.Draw(mask).polygon(points(vertices), fill=255)
    fill_gradient(image, mask, top, bottom)


def make_icon(hook=False):
    image = Image.new("RGBA", (SIZE, SIZE))
    mask = Image.new("L", image.size)
    tile = (10 * SCALE, 10 * SCALE, 501 * SCALE, 501 * SCALE)
    radius = 114 * SCALE
    ImageDraw.Draw(mask).rounded_rectangle(tile, radius=radius, fill=255)
    fill_gradient(image, mask, "#172737", "#0a121c")
    ImageDraw.Draw(image).rounded_rectangle(tile, radius=radius, outline="#36505f", width=3 * SCALE)

    if hook:
        # A terminal prompt keeps the lightweight Hook recognizable in Explorer.
        face(
            image, [(133, 141), (262, 239), (262, 269), (133, 367),
                    (101, 324), (193, 254), (101, 184)],
            "#fff8e5", "#ddcba2",
        )
        face(
            image, [(276, 307), (398, 307), (410, 318), (410, 357),
                    (276, 357)],
            "#95edd6", "#4bbca9",
        )
    else:
        # Three architectural forms define a shared space at the center.
        face(
            image, [(256, 82), (420, 177), (349, 218), (256, 164),
                    (163, 218), (92, 177)],
            "#fff8e5", "#ddcba2",
        )
        face(
            image, [(92, 204), (163, 245), (163, 318), (242, 364),
                    (242, 446), (92, 359)],
            "#a3f2de", "#52c6ae",
        )
        face(
            image, [(420, 204), (349, 245), (349, 318), (270, 364),
                    (270, 446), (420, 359)],
            "#a9d9e6", "#5b9caf",
        )
        face(
            image, [(256, 227), (311, 259), (256, 291), (201, 259)],
            "#fff8e5", "#e4d4af",
        )

    return image.resize((BASE, BASE), Image.Resampling.LANCZOS)


def save(image, stem):
    image.save(ASSETS / f"{stem}.png", optimize=True)
    image.save(ASSETS / f"{stem}.ico", sizes=[(size, size) for size in ICO_SIZES])


if __name__ == "__main__":
    main = make_icon()
    save(main, "agentbridge")
    save(make_icon(hook=True), "agentbridge_hook")
    main.resize((128, 128), Image.Resampling.LANCZOS).save(
        ASSETS.parent / "web" / "assets" / "agentbridge.png", optimize=True,
    )
