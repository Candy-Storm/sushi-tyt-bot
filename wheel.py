"""Анімація «Колеса фортуни».

Для кожного призу створюється свій GIF: колесо крутиться, плавно гальмує
і зупиняється стрілкою саме на тому секторі, який випав. Розмір секторів
пропорційний ймовірностям із prizes.py.

GIF-файли створюються один раз і кешуються в assets/wheel/. Якщо змінити
призи чи ймовірності, імена файлів зміняться, і колесо перемалюється саме.
Pillow потрібен лише для малювання; готові GIF працюють і без нього.
"""
from __future__ import annotations

import hashlib
import logging
import math
import random
from pathlib import Path

from prizes import PRIZES, Prize

log = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
FONT_PATH = ASSETS_DIR / "DejaVuSans-Bold.ttf"
WHEEL_DIR = ASSETS_DIR / "wheel"

# --- параметри анімації -------------------------------------------------------
FRAMES = 64          # кадрів під час обертання
FRAME_MS = 30        # тривалість одного кадра, мс
HOLD_MS = 4000       # скільки тримається останній кадр (колесо вже зупинилось)
SPIN_SECONDS = FRAMES * FRAME_MS / 1000
TURNS = 3            # повних обертів до зупинки

# --- геометрія (малюємо у 2x і зменшуємо для гладких країв) ---------------------
SCALE = 2
W, H = 720, 760              # розмір полотна у 2x (підсумковий GIF: 360x380)
CX, CY, R = 360, 410, 330    # центр і радіус колеса
RING = 18                    # товщина зовнішнього обідка

BG = (31, 36, 48)
RING_COLOR = (44, 49, 66)
BULB_COLOR = (255, 224, 130)
POINTER_FILL = (255, 210, 63)
POINTER_EDGE = (20, 24, 33)

# Підпис і колір кожного сектора (білий текст читається на всіх кольорах).
STYLE: dict[str, tuple[str, tuple[int, int, int]]] = {
    "roll": ("РОЛ", (231, 76, 60)),
    "discount15": ("-15%", (230, 126, 34)),
    "discount10": ("-10%", (39, 174, 96)),
    "drink": ("НАПІЙ", (41, 128, 185)),
    "try_tomorrow": ("ЗАВТРА", (142, 68, 173)),
    "spin_again": ("ЩЕ РАЗ", (22, 160, 133)),
}
_FALLBACK_COLORS = [
    (192, 57, 43), (211, 84, 0), (39, 174, 96), (41, 128, 185),
    (125, 60, 152), (233, 30, 99), (0, 150, 136), (121, 85, 72),
]


def _style(prize: Prize, index: int) -> tuple[str, tuple[int, int, int]]:
    """Підпис і колір сектора: власний підпис призу, інакше стиль за замовчуванням."""
    default_label, default_color = STYLE.get(prize.key, (None, None))
    label = prize.label or default_label or prize.key.upper()[:8]
    color = default_color or _FALLBACK_COLORS[index % len(_FALLBACK_COLORS)]
    return label, color


def _signature(prizes=None) -> str:
    """Короткий відбиток списку призів: змінилися призи чи шанси — змінилися файли.

    Підпис додається, лише якщо він відрізняється від стандартного, тому GIF для
    початкових призів лишаються чинними.
    """
    prizes = PRIZES if prizes is None else prizes
    parts = []
    for p in prizes:
        part = f"{p.key}:{p.weight}"
        if p.label and p.label != STYLE.get(p.key, (None,))[0]:
            part += f":{p.label}"
        parts.append(part)
    return hashlib.md5("|".join(parts).encode()).hexdigest()[:8]


def gif_path(prize_key: str, prizes=None) -> Path:
    return WHEEL_DIR / f"wheel_{_signature(prizes)}_{prize_key}.gif"


def _sectors(prizes=None) -> list[tuple[float, float, float]]:
    """(початок, кінець, центр) кожного сектора в градусах за годинниковою від верху."""
    prizes = PRIZES if prizes is None else prizes
    total = sum(p.weight for p in prizes)
    result, angle = [], 0.0
    for prize in prizes:
        width = 360.0 * prize.weight / total
        result.append((angle, angle + width, angle + width / 2))
        angle += width
    return result


def _point(angle_cw: float, radius: float) -> tuple[float, float]:
    """Точка на колі: кут від верху за годинниковою стрілкою."""
    rad = math.radians(angle_cw)
    return CX + radius * math.sin(rad), CY - radius * math.cos(rad)


def _fit_font(text: str, max_len: float, size: int = 46):
    from PIL import ImageFont

    while size > 18:
        font = ImageFont.truetype(str(FONT_PATH), size)
        if font.getlength(text) <= max_len:
            return font
        size -= 2
    return ImageFont.truetype(str(FONT_PATH), size)


def _draw_label(layer, angle_cw: float, text: str, r_end: float, r_start: float) -> None:
    """Текст уздовж радіуса сектора, кінець напису біля обідка."""
    from PIL import Image, ImageDraw

    font = _fit_font(text, r_end - r_start)
    length = font.getlength(text)
    pad = 6
    txt = Image.new("RGBA", (int(length) + pad * 2, int(font.size * 1.5)), (0, 0, 0, 0))
    d = ImageDraw.Draw(txt)
    mid = txt.height / 2
    d.text((pad + 2, mid + 2), text, font=font, fill=(0, 0, 0, 90), anchor="lm")
    d.text((pad, mid), text, font=font, fill=(255, 255, 255, 255), anchor="lm")

    # Текст читається зліва направо (бік «схід»); повертаємо його на напрямок сектора.
    rotated = txt.rotate(90 - angle_cw, expand=True, resample=Image.BICUBIC)
    dist = r_end - length / 2
    x, y = _point(angle_cw, dist)
    layer.alpha_composite(rotated, (int(x - rotated.width / 2), int(y - rotated.height / 2)))


def _render_base(prizes=None):
    """Колесо без стрілки (RGBA), яке потім обертається."""
    from PIL import Image, ImageDraw

    prizes = list(PRIZES if prizes is None else prizes)
    sectors = _sectors(prizes)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # Обідок із «лампочками»
    d.ellipse((CX - R, CY - R, CX + R, CY + R), fill=RING_COLOR)
    for i in range(24):
        bx, by = _point(i * 15, R - RING / 2)
        d.ellipse((bx - 5, by - 5, bx + 5, by + 5), fill=BULB_COLOR)

    # Сектори
    inner = R - RING
    box = (CX - inner, CY - inner, CX + inner, CY + inner)
    for index, (prize, (start, end, _)) in enumerate(zip(prizes, sectors)):
        _, color = _style(prize, index)
        d.pieslice(box, start - 90, end - 90, fill=color)
    for start, _, _ in sectors:
        d.line([(CX, CY), _point(start, inner)], fill=(255, 255, 255), width=5)

    # Підписи
    for index, (prize, (_, _, center)) in enumerate(zip(prizes, sectors)):
        label, _ = _style(prize, index)
        _draw_label(layer, center, label, r_end=inner - 18, r_start=70)

    # Центр
    d.ellipse((CX - 46, CY - 46, CX + 46, CY + 46), fill=(255, 255, 255))
    d.ellipse((CX - 30, CY - 30, CX + 30, CY + 30), fill=RING_COLOR)
    return layer


def _compose(base, theta: float):
    """Один кадр: колесо, повернуте за годинниковою на theta°, і нерухома стрілка."""
    from PIL import Image, ImageDraw

    frame = Image.new("RGBA", (W, H), BG + (255,))
    frame.alpha_composite(base.rotate(-theta, resample=Image.BICUBIC, center=(CX, CY)))
    d = ImageDraw.Draw(frame)
    d.polygon([(CX - 34, 6), (CX + 34, 6), (CX, 104)], fill=POINTER_FILL, outline=POINTER_EDGE, width=6)
    return frame.convert("RGB").resize((W // SCALE, H // SCALE), Image.LANCZOS)


def generate(prize_key: str, path: Path, base=None, prizes=None) -> None:
    """Малює GIF, що закінчується на секторі призу prize_key."""
    from PIL import Image

    prizes = list(PRIZES if prizes is None else prizes)
    keys = [p.key for p in prizes]
    if prize_key not in keys:
        raise KeyError(prize_key)
    if not FONT_PATH.exists():
        raise FileNotFoundError(f"Не знайдено шрифт {FONT_PATH}")

    base = base or _render_base(prizes)
    start, end, center = _sectors(prizes)[keys.index(prize_key)]
    # Невеликий зсув усередині сектора, щоб стрілка не завжди показувала в центр.
    offset = random.Random(prize_key).uniform(-0.3, 0.3) * (end - start)
    final = 360 * TURNS - (center + offset)

    frames = []
    for i in range(FRAMES):
        t = i / (FRAMES - 1)
        ease = 1 - (1 - t) ** 2          # швидкий старт, плавне гальмування
        frames.append(_compose(base, final * ease))

    palette = frames[-1].quantize(colors=64, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    paletted = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f in frames]
    durations = [FRAME_MS] * FRAMES
    durations[-1] = HOLD_MS

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    paletted[0].save(
        tmp, format="GIF", save_all=True, append_images=paletted[1:],
        duration=durations, loop=0, disposal=1,
    )
    tmp.replace(path)


def _cleanup_old(prizes) -> int:
    """Видаляє GIF, намальовані для попередніх налаштувань призів (щоб не займали місце)."""
    keep = f"wheel_{_signature(prizes)}_"
    removed = 0
    for path in WHEEL_DIR.glob("wheel_*.gif"):
        if not path.name.startswith(keep):
            try:
                path.unlink()
                removed += 1
            except OSError:
                log.warning("Не вдалося видалити старий GIF %s", path.name, exc_info=True)
    return removed


def ensure_wheels(prizes=None) -> int:
    """Створює відсутні GIF для призів і прибирає застарілі. Повертає, скільки створено.

    prizes — знімок списку призів на момент виклику (за замовчуванням поточний).
    Функція довга, тому її запускають у окремому потоці.
    """
    prizes = list(PRIZES if prizes is None else prizes)
    missing = [p.key for p in prizes if not gif_path(p.key, prizes).exists()]
    if missing:
        base = _render_base(prizes)
        for key in missing:
            generate(key, gif_path(key, prizes), base=base, prizes=prizes)
            log.info("Створено анімацію колеса: %s", gif_path(key, prizes).name)
    _cleanup_old(prizes)
    return len(missing)
