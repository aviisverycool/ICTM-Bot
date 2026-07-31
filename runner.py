import subprocess
import sys
import time

RUN_DURATION = 5 * 3600 + 50 * 60  # 5h50m; GitHub kills jobs at 6h
WORKFLOW = "keep-alive.yml"

bot = subprocess.Popen([sys.executable, "discord-bot.py"])
try:
    time.sleep(RUN_DURATION)
finally:
    bot.terminate()
    try:
        bot.wait(timeout=30)
    except subprocess.TimeoutExpired:
        bot.kill()
    subprocess.run(["gh", "workflow", "run", WORKFLOW], check=False)
