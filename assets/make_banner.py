"""Generate a promotional banner for Agent Viz (Douyin 9:16 vertical, 1080x1920)."""
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import math

W, H = 1080, 1920
BG_TOP = (10, 14, 32)
BG_BOT = (16, 32, 58)
ACCENT = (34, 211, 238)      # cyan
PURPLE = (167, 139, 250)     # violet
GREEN = (52, 211, 153)       # emerald
ORANGE = (251, 191, 36)      # amber
RED = (239, 68, 68)
WHITE = (235, 240, 250)
DIM = (148, 163, 184)
GRID = (30, 41, 66)

FONT_YAHEI = "C:/Windows/Fonts/msyh.ttc"
FONT_YAHEI_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
FONT_SIMHEI = "C:/Windows/Fonts/simhei.ttf"


def font(path, size):
    return ImageFont.truetype(path, size)


def vertical_gradient(size, top, bottom):
    w, h = size
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        t = y / h
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        for x in range(w):
            px[x, y] = (r, g, b)
    return img


def draw_grid(draw, w, h, step=54, color=GRID):
    for x in range(0, w, step):
        draw.line([(x, 0), (x, h)], fill=color, width=1)
    for y in range(0, h, step):
        draw.line([(0, y), (w, y)], fill=color, width=1)


def draw_glow(draw, cx, cy, r, color, alpha_max=90):
    """Draw a radial glow using concentric ellipses."""
    for i in range(r, 0, -3):
        a = int(alpha_max * (1 - i / r))
        draw.ellipse([cx - i, cy - i, cx + i, cy + i], outline=None,
                     fill=(color[0], color[1], color[2], a) if False else color)


def rounded_rect(draw, box, radius, fill, outline=None, width=1):
    x0, y0, x1, y1 = box
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def main():
    img = vertical_gradient((W, H), BG_TOP, BG_BOT)
    draw = ImageDraw.Draw(img)

    # Grid
    draw_grid(draw, W, H)

    # Glows
    glow_img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow_img)
    for (cx, cy, r, c) in [(150, 300, 220, ACCENT), (950, 800, 260, PURPLE), (300, 1500, 240, GREEN)]:
        for i in range(r, 0, -4):
            a = int(70 * (1 - i / r))
            gd.ellipse([cx - i, cy - i, cx + i, cy + i], fill=(c[0], c[1], c[2], a))
    glow_img = glow_img.filter(ImageFilter.GaussianBlur(30))
    img = Image.alpha_composite(img.convert("RGBA"), glow_img)
    draw = ImageDraw.Draw(img)

    # ── Header ──────────────────────────────────────────────
    f_logo = font(FONT_SIMHEI, 40)
    f_title = font(FONT_YAHEI_BOLD, 74)
    f_sub = font(FONT_YAHEI, 34)
    f_h1 = font(FONT_YAHEI_BOLD, 46)
    f_body = font(FONT_YAHEI, 30)
    f_small = font(FONT_YAHEI, 24)
    f_btn = font(FONT_SIMHEI, 36)

    # Logo pill
    rounded_rect(draw, (60, 70, 300, 140), 35, fill=(20, 30, 55), outline=ACCENT, width=2)
    draw.text((110, 88), "⚡ Agent Viz", font=f_logo, fill=ACCENT)

    # Title
    draw.text((60, 200), "多 Agent 协作", font=f_title, fill=WHITE)
    draw.text((60, 295), "可视化面板", font=f_title, fill=ACCENT)
    draw.text((60, 420), "Hermes Dashboard 插件 · 实时监控你的 Agent 军团", font=f_sub, fill=DIM)

    # ── Feature cards ───────────────────────────────────────
    cards = [
        ("01", "任务看板", "Kanban 状态一目了然\n就绪 / 运行中 / 阻塞 / 完成", ACCENT, (60, 560)),
        ("02", "依赖拓扑图", "SVG 任务依赖关系\n采集 → 清洗 → 建模 全链路", PURPLE, (60, 900)),
        ("03", "消息流时间线", "子代理动作实时滚动\n思考 → 工具 → 结果（带耗时）", GREEN, (60, 1240)),
    ]
    for num, title, desc, color, (x, y) in cards:
        rounded_rect(draw, (x, y, x + 960, y + 300), 24, fill=(15, 25, 48), outline=(38, 52, 84), width=2)
        # number badge
        rounded_rect(draw, (x + 30, y + 30, x + 100, y + 100), 35, fill=color)
        draw.text((x + 42, y + 38), num, font=f_h1, fill=(10, 14, 32))
        # title
        draw.text((x + 130, y + 30), title, font=f_h1, fill=color)
        # desc lines
        lines = desc.split("\n")
        for i, ln in enumerate(lines):
            draw.text((x + 130, y + 110 + i * 46), ln, font=f_body, fill=WHITE if i == 0 else DIM)

    # ── Mini topology mock ──────────────────────────────────
    mock_y = 1600
    draw.text((60, mock_y - 30), "实时拓扑示例", font=f_h1, fill=WHITE)
    nodes = [(180, 1730, "采集", ACCENT), (520, 1730, "清洗", PURPLE), (860, 1730, "建模", GREEN)]
    for i, (nx, ny, label, c) in enumerate(nodes):
        rounded_rect(draw, (nx - 70, ny - 34, nx + 70, ny + 34), 16, fill=(15, 25, 48), outline=c, width=3)
        draw.text((nx - 34, ny - 16), label, font=f_btn, fill=c)
        if i < 2:
            nx2 = nodes[i + 1][0]
            draw.line([(nx + 70, ny), (nx2 - 70, ny)], fill=DIM, width=4)
            mx = (nx + nx2) / 2
            draw.polygon([(mx + 12, ny), (mx - 6, ny - 12), (mx - 6, ny + 12)], fill=DIM)

    # ── Footer ──────────────────────────────────────────────
    rounded_rect(draw, (60, 1808, 1020, 1868), 30, fill=ACCENT)
    draw.text((150, 1820), "🚀 开源 · 免费 · 自托管 · 每 10s 自动刷新", font=f_btn, fill=(10, 14, 32))
    draw.text((60, 1888), "github.com/8888-gif/agent-viz", font=f_small, fill=DIM)

    img = img.convert("RGB")
    img.save("C:/Users/Administrator/Desktop/agent-viz/assets/banner.png", "PNG")
    print("saved banner.png", img.size)


if __name__ == "__main__":
    main()
