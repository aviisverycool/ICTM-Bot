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

ENV_PATTERN = r"\\begin\{(?P<env>\w+\*?)\}.*?\\end\{(?P=env)\}"
MATH_PATTERN = re.compile(rf"({ENV_PATTERN}|\$([^$]*)\$|\\\[(.*?)\\\])", re.DOTALL)
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
    seg = re.sub(r"\*\*(.+?)\*\*", lambda m: "\x04" + m.group(1) + "\x05", seg)
    seg = seg.replace("`", "")
    seg = seg.replace("\n\n", "\x01")
    seg = seg.replace("\n", "\x02")
    seg = seg.replace("\\\\", "\x03")
    seg = seg.replace("%", r"\%")
    seg = seg.replace("#", r"\#")
    seg = seg.replace("&", r"\&")
    seg = seg.replace("_", r"\_")
    seg = seg.replace("{", r"\{")
    seg = seg.replace("}", r"\}")
    seg = seg.replace("~", r"\textasciitilde{}")
    seg = seg.replace("^", r"\textasciicircum{}")
    seg = seg.replace("$", r"\$")
    seg = seg.replace("\\", r"\textbackslash{}")
    seg = seg.replace("\x04", r"\textbf{")
    seg = seg.replace("\x05", "}")
    seg = seg.replace("\x01", r"\\[6pt]")
    seg = seg.replace("\x02", " ")
    seg = seg.replace("\x03", r"\\[2pt]")
    for ch, tex in UNICODE_TO_LATEX.items():
        seg = seg.replace(ch, tex)
    return seg

def clean_math(seg, keep_amp=False):
    """Normalize a raw math segment for use inside $...$."""
    seg = seg.replace("\r", "").replace("\n", " ")
    seg = re.sub(r"^\\(?: |qquad|quad)", "", seg)
    seg = re.sub(r"\\hspace\*?\{[^}]*\}$", "", seg)
    seg = re.sub(r"\\(?:qquad|quad)$", "", seg)
    seg = seg.strip()
    seg = re.sub(r"(?<!\\)%", r"\%", seg)
    seg = re.sub(r"(?<!\\)#", r"\#", seg)
    if not keep_amp:
        seg = re.sub(r"(?<!\\)&", r"\&", seg)
    seg = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", seg)
    seg = seg.replace("`", "")
    return seg

def text_to_tex(seg):
    """Convert a plain-text segment (possibly with plaintext math) to LaTeX."""
    out = []
    for i, part in enumerate(PLAIN_MATH_PATTERN.split(seg)):
        if not part:
            continue
        if i % 2 == 1:
            math = clean_math(part)
            if math:
                out.append("$" + math + "$")
        else:
            out.append(escape_latex_text(part))
    return "".join(out)

def latex_body(text):
    """Convert text (math delimiters + plaintext math + escaped text) to LaTeX."""
    text = text.replace("\r", "").strip()
    out = []
    pos = 0
    for m in MATH_PATTERN.finditer(text):
        out.append(text_to_tex(text[pos:m.start()]))
        if m.group(1) is not None:
            math = clean_math(m.group(1), keep_amp=True)
            if math:
                out.append(math)
        else:
            math = clean_math(m.group(3) or m.group(4))
            if math:
                out.append("$" + math + "$")
        pos = m.end()
    out.append(text_to_tex(text[pos:]))
    return "".join(out)

def text_to_latex(text):
    """Convert mixed plaintext/LaTeX text into a CodeCogs-renderable formula."""
    return "\\large\\text{\\parbox{13cm}{" + latex_body(text) + "}}"

def choices_to_tex(choices):
    """Format answer choices as a horizontal LaTeX line."""
    parts = []
    for letter, value in sorted(choices.items()):
        v = value.replace("$", "").strip()
        if "\\" in v or "$" in v:
            v = "$" + clean_math(v) + "$"
        else:
            v = escape_latex_text(v)
        parts.append(f"({letter}) {v}")
    return "\\textbf{Choices:} \\qquad " + "\\qquad ".join(parts)

def problem_formula(comp_name, problem_id, problem_text, choices=None):
    body = []
    body.append("\\textbf{" + escape_latex_text(f"{comp_name} - Problem {problem_id}") + "}")
    body.append(latex_body(problem_text))
    if choices:
        body.append(choices_to_tex(choices))
    return "\\large\\text{\\parbox{13cm}{" + "\\\\[8pt]".join(body) + "}}"

def solution_formula(result_text, solution_text=None):
    body = [text_to_tex(result_text)]
    if solution_text:
        body.append(r"\textbf{Solution:}\ " + text_to_tex(solution_text))
    return "\\large\\text{\\parbox{13cm}{" + "\\\\[8pt]".join(body) + "}}"

async def render_latex(formula, filename="latex.png"):
    """Render a LaTeX formula as a PNG image (white background, black text, padded)."""
    formula = "\\bg{white} " + formula
    url = "https://latex.codecogs.com/png.image?" + urllib.parse.quote(formula)
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    img = Image.open(io.BytesIO(data)).convert("RGBA")
                    pad = 20
                    padded = Image.new("RGBA", (img.width + pad * 2, img.height + pad * 2), "white")
                    padded.paste(img, (pad, pad))
                    buf = io.BytesIO()
                    padded.save(buf, format="PNG")
                    buf.seek(0)
                    return discord.File(buf, filename=filename)
                logging.warning(f"CodeCogs returned {resp.status}")
                return None
        except Exception as e:
            logging.warning(f"CodeCogs error: {e}")
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
        if image_file:
            embed = discord.Embed(
                title=f"📐 {data.get('competition_name', 'Unknown')} - Problem {data['problem_id']}",
                color=discord.Color.blue()
            )
            embed.set_image(url="attachment://problem.png")
            embed.set_footer(text="Type /answer YOUR_ANSWER to check.")
            await interaction.followup.send(embed=embed, file=image_file)
        else:
            # Fallback to raw text
            await interaction.followup.send(f"```latex\n{problem_text}\n```")
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