"""
命令行对话（不需要启动 HTTP 服务），用于本地调试 Agent。

    python -m scripts.chat_cli

命令：/new 新会话  /profile 查看画像  /mem 查看记忆  /quit 退出
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets

from sqlalchemy import select

from app.agent.service import agent_service
from app.database.models import User, UserProfile
from app.database.session import SessionLocal, init_db
from app.memory.service import memory_service
from app.rag.retriever import init_retriever
from app.users.profile import get_profile
from app.utils.background import drain

CLI_USERNAME = "cli_user"


async def ensure_cli_user() -> str:
    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.username == CLI_USERNAME))).scalar_one_or_none()
        if user is None:
            user = User(username=CLI_USERNAME, password_hash="!" + secrets.token_hex(16))
            db.add(user)
            await db.flush()
            db.add(UserProfile(user_id=user.id))
            await db.commit()
        return user.id


async def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    await init_db()
    await init_retriever()
    user_id = await ensure_cli_user()
    session_id: str | None = None
    print("FitnessPro CLI，输入 /quit 退出，/new 新会话，/profile 查看画像，/mem 查看记忆\n")

    while True:
        try:
            text = (await asyncio.to_thread(input, "你：")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text == "/quit":
            break
        if text == "/new":
            session_id = None
            print("（已开始新会话）\n")
            continue
        if text == "/profile":
            print(json.dumps(await get_profile(user_id), ensure_ascii=False, indent=2), "\n")
            continue
        if text == "/mem":
            for m in await memory_service.list(user_id, ["active", "candidate"]):
                print(f"[{m.status}] {m.id} {m.kind}: {m.content}")
            print()
            continue

        session_id = await agent_service.prepare_session(user_id, session_id, text)
        queue = agent_service.start_turn(user_id=user_id, session_id=session_id, message=text)
        print("教练：", end="", flush=True)
        while (event := await queue.get()) is not None:
            if event.type == "content":
                print(event.data["delta"], end="", flush=True)
            elif event.type == "tool_call":
                args = json.dumps(event.data["arguments"], ensure_ascii=False)
                print(f"\n  ⚙ {event.data['name']}({args})", flush=True)
            elif event.type == "tool_result":
                mark = "✓" if event.data["success"] else f"✗ {event.data['error']}"
                print(f"  {mark} {event.data['ms']}ms", flush=True)
            elif event.type == "context":
                d = event.data
                print(
                    f"[上下文] 历史 {d['history_turns']} 轮 / {d['history_tokens']} tokens，"
                    f"摘要={'有' if d['has_summary'] else '无'}，记忆 {d['active_memories']}+{d['candidate_memories']}\n",
                    end="",
                )
                print("教练：", end="", flush=True)
            elif event.type == "error":
                print(f"\n[错误] {event.data.get('message')}", flush=True)
            elif event.type == "done":
                d = event.data
                print(f"\n[{d.get('status')}，{d.get('iterations')} 轮，工具 {d.get('tool_calls')} 次]\n")

    print("正在等待后台任务（摘要/记忆）完成…")
    await drain()


if __name__ == "__main__":
    asyncio.run(main())
