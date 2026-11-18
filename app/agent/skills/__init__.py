"""加载随应用维护的训练规划技能，不从用户消息或工具结果选择文件。"""

from pathlib import Path

import yaml


TRAINING_PLAN_SKILL_PATH = Path(__file__).resolve().parent / "training-plan-design" / "SKILL.md"


def _read_training_plan_skill() -> tuple[dict, str]:
    """重新读取文件；规则缺失或损坏时明确失败，不静默丢弃。"""
    path = TRAINING_PLAN_SKILL_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(f"无法读取训练规划技能：{path}") from exc

    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError(f"训练规划技能缺少 YAML frontmatter：{path}")
    try:
        end = lines.index("---", 1)
        metadata = yaml.safe_load("\n".join(lines[1:end]))
    except (ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"训练规划技能 frontmatter 无效：{path}") from exc

    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != "training-plan-design"
        or not isinstance(metadata.get("description"), str)
        or not metadata["description"].strip()
    ):
        raise ValueError(f"训练规划技能 name 或 description 无效：{path}")

    body = "\n".join(lines[end + 1:]).strip()
    if not body:
        raise ValueError(f"训练规划技能正文为空：{path}")
    return metadata, body


def load_training_plan_skill_metadata() -> str:
    """仅供常驻上下文使用的名称与描述，不包含技能正文。"""
    metadata, _ = _read_training_plan_skill()
    return f"名称：{metadata['name']}\n描述：{metadata['description']}"


def load_training_plan_skill() -> str:
    """按需加载完整正文，不缓存，修改文件后下一次加载生效。"""
    _, body = _read_training_plan_skill()
    return body
