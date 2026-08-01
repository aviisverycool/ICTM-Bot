import subprocess
import sys

RUN_DURATION = 5 * 3600 + 50 * 60  # 5h50m; GitHub kills jobs at 6h
WORKFLOW = "keep-alive.yml"

bot = subprocess.Popen([sys.executable, "discord-bot.py"])
try:
    bot.wait(timeout=RUN_DURATION)
except subprocess.TimeoutExpired:
    bot.terminate()
try:
    bot.wait(timeout=30)
except subprocess.TimeoutExpired:
    bot.kill()
    bot.wait()
subprocess.run(["gh", "workflow", "run", WORKFLOW], check=False)
