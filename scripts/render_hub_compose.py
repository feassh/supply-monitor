"""为每个发布镜像生成无需源码和环境变量的 Compose 部署文件。"""
from pathlib import Path
import re
import sys


def render(image):
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*/supply-monitor:[A-Za-z0-9_][A-Za-z0-9_.-]*", image):
        raise ValueError("Docker Hub 镜像名格式无效")
    source = Path(__file__).resolve().parents[1] / "compose.yaml"
    text = source.read_text(encoding="utf-8")
    if text.count("    image: supply-monitor:local\n") != 1 or text.count("    build: .\n") != 1:
        raise ValueError("Compose 模板结构已变化，请同步更新生成器")
    return text.replace("    image: supply-monitor:local\n", f"    image: {image}\n").replace("    build: .\n", "")


if __name__ == "__main__":
    target = Path("dist/compose.hub.yaml")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(sys.argv[1]), encoding="utf-8")
