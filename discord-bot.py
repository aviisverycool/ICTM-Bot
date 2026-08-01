import os
import re
import discord
from discord import app_commands
import aiohttp
from dotenv import load_dotenv
import asyncio
import logging
import urllib.parse
import io
from PIL import Image

# ---------- Setup ----------
logging.basicConfig(level=logging.INFO)
load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
API_BASE = os.getenv("API_BASE_URL")

if not TOKEN or not API_BASE:
    raise ValueError("Missing DISCORD_BOT_TOKEN or API_BASE_URL in .env file.")

user_problems = {}

# ---------- LaTeX Renderer (CodeCogs PNG) ----------
# NOTE: CodeCogs parses the entire query string as the formula, so appending
# extra query params (e.g. &bg=...&fg=...) makes it return HTTP 400 for every
# request. Only the URL-encoded formula may be sent.
#
# Problem text from the API mixes plain English with inline math ($...$,
# \[...\]) and sometimes plaintext math (43^{123}, 1234_{seven}). CodeCogs
# only understands raw LaTeX, so we convert the text into a proper LaTeX
# document fragment: escaped text inside a wrapping \parbox (so long problems
# wrap instead of producing a giant unreadable image), math kept in math mode,
# Unicode math symbols mapped to LaTeX commands, and \large for legibility.

ENV_PATTERN = r"\\begin\{(?P<env>\w+\*?)\}(?P<envbody>.*?)\\end\{(?P=env)\}"
MATH_PATTERN = re.compile(
    r"(?P<envm>" + ENV_PATTERN + r")"
    r"|(?P<inl>\$(?P<inlbody>[^$]*)\$)"
    r"|(?P<paren>\\\((?P<parenbody>.*?)\\\))"
    r"|(?P<disp>\\\[(?P<dispbody>.*?)\\\])",
    re.DOTALL,
)
ENV_MAP = {"align*": "aligned", "align": "aligned", "gather*": "gathered", "gather": "gathered"}
PLAIN_MATH_PATTERN = re.compile(r"([^\s{}_^]+[\^_]\{[^{}]*\})")

UNICODE_TO_LATEX = {
    "\u2261": r"$\equiv$", "\u2260": r"$\ne$", "\u2248": r"$\approx$",
    "\u2265": r"$\ge$", "\u2264": r"$\le$", "\u00d7": r"$\times$",
    "\u00f7": r"$\div$", "\u00b7": r"$\cdot$", "\u00b1": r"$\pm$",
    "\u2212": r"$-$", "\u00b0": r"$^\circ$", "\u221e": r"$\infty$",
    "\u2192": r"$\to$", "\u2190": r"$\gets$", "\u2220": r"$\angle$",
    "\u2206": r"$\Delta$", "\u03c0": r"$\pi$", "\u03b8": r"$\theta$",
    "\u03b1": r"$\alpha$", "\u03b2": r"$\beta$", "\u03b3": r"$\gamma$",
    "\u03b4": r"$\delta$", "\u03bb": r"$\lambda$", "\u03bc": r"$\mu$",
    "\u03c3": r"$\sigma$", "\u03c6": r"$\phi$", "\u03c9": r"$\omega$",
    "\u2208": r"$\in$", "\u2209": r"$\notin$", "\u222a": r"$\cup$",
    "\u2229": r"$\cap$", "\u2282": r"$\subset$", "\u2283": r"$\supset$",
    "\u2211": r"$\sum$", "\u220f": r"$\prod$", "\u222b": r"$\int$",
    "\u2713": r"$\checkmark$", "\u2717": r"$\times$",
    "\u2705": r"$\checkmark$", "\u274c": r"$\times$",
    "\u2714": r"$\checkmark$", "\u2718": r"$\times$",
    "\u00bd": r"$\frac{1}{2}$", "\u2153": r"$\frac{1}{3}$",
    "\u2154": r"$\frac{2}{3}$", "\u00bc": r"$\frac{1}{4}$",
    "\u00be": r"$\frac{3}{4}$", "\u2026": r"$\dots$", "\u22ef": r"$\cdots$",
}

def escape_latex_text(seg):
    """Escape a plain-text segment so it is safe inside LaTeX text mode."""
    seg = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", seg)
    seg = seg.replace("\t", " ")
    seg = re.sub(r"\*\*(.+?)\*\*", lambda m: "\x04" + m.group(1) + "\x05", seg)
    seg = seg.replace("`", "")
    seg = seg.replace("\n\n", "\x01")
    seg = seg.replace("\n", "\x02")
    seg = seg.replace("\\\\", "\x03")
    seg = seg.replace("\\", "\x11")
    seg = seg.replace("%", "\x06")
    seg = seg.replace("#", "\x07")
    seg = seg.replace("&", "\x08")
    seg = seg.replace("_", "\x09")
    seg = seg.replace("{", "\x0a")
    seg = seg.replace("}", "\x0d")
    seg = seg.replace("~", "\x0e")
    seg = seg.replace("^", "\x0f")
    seg = seg.replace("$", "\x10")
    seg = seg.replace("\x11", r"\textbackslash{}")
    seg = seg.replace("\x06", r"\%")
    seg = seg.replace("\x07", r"\#")
    seg = seg.replace("\x08", r"\&")
    seg = seg.replace("\x09", r"\_")
    seg = seg.replace("\x0a", r"\{")
    seg = seg.replace("\x0d", r"\}")
    seg = seg.replace("\x0e", r"\textasciitilde{}")
    seg = seg.replace("\x0f", r"\textasciicircum{}")
    seg = seg.replace("\x10", r"\textdollar{}")
    seg = seg.replace("\x04", r"\textbf{")
    seg = seg.replace("\x05", "}")
    seg = seg.replace("\x01", r"\\[6pt]")
    seg = seg.replace("\x02", " ")
    seg = seg.replace("\x03", r"\\[2pt]")
    for ch, tex in UNICODE_TO_LATEX.items():
        seg = seg.replace(ch, "\x0b" + tex + "\x0c")
    return seg

def clean_math(seg, keep_amp=False):
    """Normalize a raw math segment for use in math mode."""
    seg = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", seg)
    seg = seg.replace("\t", " ")
    seg = seg.replace("\r", "").replace("\n", " ")
    seg = re.sub(r"^\\(?: |qquad|quad)", "", seg)
    seg = re.sub(r"\\hspace\*?\{[^}]*\}$", "", seg)
    seg = re.sub(r"\\(?:qquad|quad)$", "", seg)
    seg = seg.strip()
    saved = []
    seg = re.sub(r"\\text\{[^{}]*\}", lambda m: saved.append(m.group(0)) or f"\x7f{len(saved) - 1}\x7f", seg)
    for ch, tex in UNICODE_TO_LATEX.items():
        if ch in seg:
            seg = seg.replace(ch, tex[1:-1] if tex.startswith("$") else tex)
    seg = re.sub(r"\x7f\d+\x7f", lambda m: saved[int(m.group(0)[1:-1])], seg)
    seg = re.sub(r"(?<!\\)%", r"\%", seg)
    seg = re.sub(r"(?<!\\)#", r"\#", seg)
    if not keep_amp:
        seg = re.sub(r"(?<!\\)&", r"\&", seg)
    seg = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", seg)
    seg = seg.replace("`", "")
    return seg

def text_to_formula_parts(seg):
    """Split a plain-text segment into ('text', ...) and ('math', ...) parts."""
    parts = []
    for i, part in enumerate(PLAIN_MATH_PATTERN.split(seg)):
        if not part:
            continue
        if i % 2 == 1:
            math = clean_math(part)
            if math:
                parts.append(("math", math))
        else:
            escaped = escape_latex_text(part)
            buf = []
            for sub in re.split(r"(\x0b.*?\x0c)", escaped):
                if not sub:
                    continue
                if sub.startswith("\x0b"):
                    body = sub[1:-1]
                    if body.startswith("$") and body.endswith("$"):
                        body = body[1:-1]
                    parts.append(("math", body))
                else:
                    buf.append(sub)
            if buf:
                parts.append(("text", "".join(buf)))
    return parts

def normalize_math_newlines(text):
    """Collapse newlines inside math spans so per-line splitting stays safe."""
    out = []
    pos = 0
    for m in MATH_PATTERN.finditer(text):
        out.append(text[pos:m.start()])
        out.append(re.sub(r"\s*\n+\s*", " ", m.group(0)))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)

OPEN_PUNCT = "([{"
CLOSE_PUNCT = ")],;:!?."

def join_with_spacing(parts):
    """
    Join text/math parts, inserting spaces between adjacent parts unless
    punctuation rules say otherwise. Avoids double spaces.
    """
    pieces = []
    for idx, (k, t) in enumerate(parts):
        # Prepare token: text parts stay as-is, math parts get wrapped
        token = t if k == "text" else f"\\ensuremath{{{t}}}"
        if idx == 0:
            pieces.append(token)
            continue

        # Decide whether to add a space before this token
        add_space = True

        # 1) No space before punctuation that usually follows without a space
        if k == "text" and t and t[0] in CLOSE_PUNCT:
            add_space = False

        # 2) No space after opening punctuation
        prev_k, prev_t = parts[idx - 1]
        if prev_k == "text" and prev_t and prev_t[-1] in OPEN_PUNCT:
            add_space = False

        # 3) Avoid double spaces: if previous token already ends with a space
        #    or current token starts with a space, don't add another
        if pieces[-1].endswith(" ") or t.startswith(" "):
            add_space = False

        if add_space:
            pieces.append(" " + token)
        else:
            pieces.append(token)

    return "".join(pieces)

def line_formula(seg):
    """Convert one source line into a CodeCogs-renderable formula."""
    parts = []
    pos = 0
    for m in MATH_PATTERN.finditer(seg):
        parts.extend(text_to_formula_parts(seg[pos:m.start()]))
        if m.group("envm") is not None:
            env = m.group("env")
            body = clean_math(m.group("envbody"), keep_amp=True)
            mapped = ENV_MAP.get(env, env)
            if mapped != env:
                body = re.sub(r"^\{\d+\}", "", body)
            body = body.replace("&", r"\quad ")
            parts.append(("math", f"\\begin{{{mapped}}} {body} \\end{{{mapped}}}"))
        else:
            body = m.group("inlbody")
            if body is None:
                body = m.group("parenbody")
            if body is None:
                body = m.group("dispbody")
            math = clean_math(body or "")
            if math:
                parts.append(("math", math))
        pos = m.end()
    parts.extend(text_to_formula_parts(seg[pos:]))
    parts = [(k, t.strip()) for k, t in parts if (k, t.strip()) != ("text", "")]
    if not parts:
        return None
    if all(k == "text" for k, _ in parts):
        text = " ".join(t for _, t in parts)
        if len(text) > 100:
            return "\\large\\text{\\parbox{13cm}{" + text + "}}"
        return "\\large\\text{" + text + "}"
    body = join_with_spacing(parts)
    return "\\large\\text{\\parbox{13cm}{" + body + "}}"

def text_to_lines(text):
    """Split text into per-line formulas; None marks a paragraph break."""
    text = normalize_math_newlines(text.replace("\r", "").strip())
    lines = []
    prev_blank = False
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            prev_blank = True
            continue
        if prev_blank:
            lines.append(None)
        prev_blank = False
        f = line_formula(line)
        if f:
            lines.append(f)
    return lines

def choices_line(choices):
    """Format answer choices as a single formula line."""
    parts = [("text", "\\textbf{Choices:}")]
    for letter, value in sorted(choices.items()):
        v = value.replace("$", "").strip()
        parts.append(("math", "\\quad"))
        parts.append(("text", f"({letter}) "))
        if "\\" in v or "$" in v:
            math = clean_math(v)
            if math:
                parts.append(("math", math))
        else:
            parts.append(("text", escape_latex_text(v)))
    # Ensure we always have a space after the choice letter
    # The join_with_spacing will handle the spacing between parts,
    # but we need to make sure the text part for the letter ends with a space.
    # We already have that in the text part f"({letter}) ".
    body = join_with_spacing(parts)
    return "\\large " + body

def problem_formula(comp_name, problem_id, problem_text, choices=None):
    """Build a list of per-line formulas (None = paragraph break)."""
    lines = []
    header = escape_latex_text(f"{comp_name} - Problem {problem_id}")
    lines.append("\\large\\text{\\parbox{13cm}{\\textbf{" + header + "}}}")
    lines.extend(text_to_lines(problem_text))
    if choices:
        lines.append(None)
        lines.append(choices_line(choices))
    return lines

def solution_formula(result_text, solution_text=None):
    """Build a list of per-line formulas (None = paragraph break)."""
    lines = text_to_lines(result_text)
    if solution_text:
        lines.append(None)
        lines.append("\\large\\text{\\parbox{13cm}{\\textbf{Solution:}}}")
        lines.extend(text_to_lines(solution_text))
    return lines

def to_aspect_ratio(img, ratio_w=4, ratio_h=3, bg="white"):
    """Pad an image to the given aspect ratio (w:h), centering content."""
    w, h = img.size
    target_w = max(w, -(-(h * ratio_w) // ratio_h))
    target_w = -(-target_w // ratio_w) * ratio_w
    target_h = target_w * ratio_h // ratio_w
    canvas = Image.new("RGBA", (target_w, target_h), bg)
    canvas.paste(img, ((target_w - w) // 2, (target_h - h) // 2))
    return canvas

async def render_latex(lines, filename="latex.png"):
    """Render per-line LaTeX formulas, stacked into one padded PNG image."""
    pad = 16
    line_gap = 4
    para_gap = 10
    async with aiohttp.ClientSession() as session:
        images = []
        for line in lines:
            if line is None:
                images.append(None)
                continue
            url = "https://latex.codecogs.com/png.image?" + urllib.parse.quote("\\bg{white} " + line)
            try:
                async with session.get(url, timeout=10) as resp:
                    if resp.status != 200:
                        logging.warning(f"CodeCogs returned {resp.status} for: {line[:80]}")
                        return None
                    data = await resp.read()
                    images.append(Image.open(io.BytesIO(data)).convert("RGBA"))
            except Exception as e:
                logging.warning(f"CodeCogs error: {e}")
                return None
        rendered = [i for i in images if i is not None]
        if not rendered:
            return None
        width = max(i.width for i in rendered) + pad * 2
        y = pad
        for img in images:
            if img is None:
                y += para_gap
            else:
                y += img.height + line_gap
        canvas = Image.new("RGBA", (width, y), "white")
        y = pad
        for img in images:
            if img is None:
                y += para_gap
            else:
                canvas.paste(img, ((width - img.width) // 2, y))
                y += img.height + line_gap
        canvas = to_aspect_ratio(canvas)
        buf = io.BytesIO()
        canvas.save(buf, format="PNG")
        buf.seek(0)
        return discord.File(buf, filename=filename)

# ---------- Image Helpers ----------
def resolve_image_urls(image_url):
    """Return candidate absolute URLs for an API-relative image path."""
    if not image_url:
        return []
    if image_url.startswith("http://") or image_url.startswith("https://"):
        return [image_url]
    base_parts = urllib.parse.urlsplit(API_BASE)
    host_root = f"{base_parts.scheme}://{base_parts.netloc}/"
    if image_url.startswith("/api/"):
        return [host_root + image_url[len("/api/"):], host_root + image_url]
    return [host_root + image_url]

async def fetch_image_file(image_url, filename="diagram.png"):
    """Fetch a problem diagram from the first working URL, as a discord.File."""
    async with aiohttp.ClientSession() as session:
        for url in resolve_image_urls(image_url):
            try:
                async with session.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if not data:
                            continue
                        img = Image.open(io.BytesIO(data)).convert("RGBA")
                        buf = io.BytesIO()
                        img.save(buf, format="PNG")
                        buf.seek(0)
                        return discord.File(buf, filename=filename)
            except Exception as e:
                logging.warning(f"Image fetch error for {url}: {e}")
    return None

# ---------- API Helpers ----------
async def api_get(endpoint, params=None):
    async with aiohttp.ClientSession() as session:
        url = f"{API_BASE}{endpoint}"
        try:
            async with session.get(url, params=params, timeout=15) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        except Exception:
            return None

async def api_post(endpoint, data=None):
    async with aiohttp.ClientSession() as session:
        url = f"{API_BASE}{endpoint}"
        try:
            async with session.post(url, json=data, timeout=15) as resp:
                if resp.status in (200, 201):
                    return await resp.json()
                return None
        except Exception:
            return None

# ---------- Discord Client ----------
class BotClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        logging.info("Commands synced!")

client = BotClient()

# ---------- Commands ----------
@client.tree.command(name="practice", description="Get a random math problem")
@app_commands.describe(
    competition="AMC10, AMC12, AIME, NSML, or ICTM",
    topic="Algebra, Geometry, etc. (optional)",
    difficulty="easy, medium, or hard (optional)"
)
async def practice(interaction: discord.Interaction, competition: str, topic: str = None, difficulty: str = None):
    await interaction.response.defer()

    try:
        params = {"competition": competition.upper()}
        if topic: params["topic"] = topic.title()
        if difficulty: params["difficulty"] = difficulty.lower()

        data = await api_get("/problems/random", params=params)
        if not data:
            await interaction.followup.send("❌ No problem found.", ephemeral=True)
            return

        if "problem_id" not in data or "problem_text" not in data:
            await interaction.followup.send("❌ Invalid API response.", ephemeral=True)
            return

        user_problems[interaction.user.id] = data["problem_id"]

        # Build the LaTeX formula (header, problem text, choices)
        formula = problem_formula(
            data.get('competition_name', 'Unknown'),
            data['problem_id'],
            data['problem_text'].strip(),
            data.get('choices'),
        )

        # Render as image
        image_file = await render_latex(formula, "problem.png")
        diagram_file = None
        if data.get("image_url"):
            diagram_file = await fetch_image_file(data["image_url"], "diagram.png")

        if image_file:
            embed = discord.Embed(
                title=f"📐 {data.get('competition_name', 'Unknown')} - Problem {data['problem_id']}",
                color=discord.Color.blue()
            )
            embed.set_image(url="attachment://problem.png")
            embed.set_footer(text="Type /answer YOUR_ANSWER to check.")
            if diagram_file:
                diagram_embed = discord.Embed(
                    title="📊 Diagram",
                    color=discord.Color.blue()
                )
                diagram_embed.set_image(url="attachment://diagram.png")
                await interaction.followup.send(
                    embeds=[embed, diagram_embed],
                    files=[image_file, diagram_file]
                )
            else:
                await interaction.followup.send(embed=embed, file=image_file)
        else:
            # Fallback to raw text
            await interaction.followup.send(f"```latex\n{data['problem_text']}\n```")
            await interaction.followup.send("Type `/answer YOUR_ANSWER` to check.")

    except Exception as e:
        logging.error(f"Practice error: {e}")
        await interaction.followup.send("❌ Something went wrong.", ephemeral=True)


@client.tree.command(name="answer", description="Check your answer")
@app_commands.describe(answer="Your answer (e.g., 42, A, or 3/4)")
async def answer(interaction: discord.Interaction, answer: str):
    await interaction.response.defer()

    try:
        problem_id = user_problems.get(interaction.user.id)
        if not problem_id:
            await interaction.followup.send("No active problem. Use `/practice` first.", ephemeral=True)
            return

        data = await api_post(f"/problems/{problem_id}/check", data={"answer": answer})
        if not data:
            await interaction.followup.send("❌ API error.", ephemeral=True)
            return

        if data.get("correct"):
            result_text = "✅ Correct!"
            color = discord.Color.green()
        else:
            correct = data.get("correct_answer", "unknown")
            result_text = f"❌ Incorrect. Correct answer: `{correct}`"
            color = discord.Color.red()

        solution_text = data.get("solution_text")
        full_text = f"{result_text}"
        if solution_text:
            full_text += f"\n\n**Solution:** {solution_text.strip()}"
        formula = solution_formula(result_text, solution_text.strip() if solution_text else None)

        image_file = await render_latex(formula, "solution.png")
        if image_file:
            embed = discord.Embed(
                title="📊 Answer Result",
                color=color
            )
            embed.set_image(url="attachment://solution.png")
            await interaction.followup.send(embed=embed, file=image_file)
        else:
            await interaction.followup.send(full_text)

    except Exception as e:
        logging.error(f"Answer error: {e}")
        await interaction.followup.send("❌ Something went wrong.", ephemeral=True)


@client.tree.command(name="ping", description="Check if bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")


if __name__ == "__main__":
    logging.info("Starting bot...")
    client.run(TOKEN)